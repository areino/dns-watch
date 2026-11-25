#!/usr/bin/env python3
"""dns-watch: resolve domains via their authoritative NS, detect changes, alert to Taegis via HTTP Ingest."""

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

import dns.exception
import dns.flags
import dns.message
import dns.query
import dns.rcode
import dns.rdatatype
import dns.resolver

DEFAULT_TYPES = ["A", "AAAA", "CNAME", "MX", "NS", "TXT"]


def log(msg):
    print(msg, file=sys.stderr)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


class AuthResolver:
    """Resolves records by querying a zone's authoritative nameservers directly."""

    def __init__(self, timeout):
        self.timeout = timeout
        self.stub = dns.resolver.Resolver()
        self.stub.lifetime = timeout * 2
        self._zone_cache = {}

    def _addresses(self, host):
        addrs = []
        for rtype in ("A", "AAAA"):
            try:
                ans = self.stub.resolve(host, rtype)
                addrs.extend(r.address for r in ans)
            except dns.exception.DNSException:
                pass
        return addrs

    def authoritative_servers(self, domain):
        """Return (zone, {ns_hostname: [ips]}) for the zone containing `domain`."""
        zone = str(dns.resolver.zone_for_name(domain, resolver=self.stub)).rstrip(".")
        if zone not in self._zone_cache:
            ns_hosts = sorted(str(r.target).rstrip(".") for r in self.stub.resolve(zone, "NS"))
            self._zone_cache[zone] = {h: self._addresses(h) for h in ns_hosts}
        return zone, self._zone_cache[zone]

    def _query(self, ip, name, rdtype):
        q = dns.message.make_query(name, rdtype)
        try:
            resp = dns.query.udp(q, ip, timeout=self.timeout)
            if resp.flags & dns.flags.TC:
                resp = dns.query.tcp(q, ip, timeout=self.timeout)
        except (dns.exception.Timeout, OSError):
            resp = dns.query.tcp(q, ip, timeout=self.timeout)
        return resp

    def lookup(self, domain, rdtypes):
        """Return {"zone", "server", "status", "records": {type: [str,...]}}. Raises on total failure."""
        zone, servers = self.authoritative_servers(domain)
        last_err = None
        for host, ips in servers.items():
            for ip in ips:
                try:
                    return self._lookup_on(domain, rdtypes, zone, host, ip)
                except (dns.exception.DNSException, OSError, socket.error) as e:
                    last_err = f"{host} ({ip}): {e}"
        raise RuntimeError(f"no authoritative server answered for {domain}: {last_err}")

    def _lookup_on(self, domain, rdtypes, zone, host, ip):
        records = {}
        status = "NOERROR"
        for t in rdtypes:
            resp = self._query(ip, domain, t)
            rc = resp.rcode()
            if rc == dns.rcode.NXDOMAIN:
                status = "NXDOMAIN"
                break
            if rc != dns.rcode.NOERROR:
                raise dns.exception.DNSException(f"rcode {dns.rcode.to_text(rc)} for {t}")
            want = dns.rdatatype.from_text(t)
            for rrset in resp.answer:
                name = dns.rdatatype.to_text(rrset.rdtype)
                if rrset.rdtype == want or rrset.rdtype == dns.rdatatype.CNAME:
                    vals = records.setdefault(name, set())
                    vals.update(r.to_text() for r in rrset)
        return {
            "zone": zone,
            "server": f"{host} ({ip})",
            "status": status,
            "records": {k: sorted(v) for k, v in sorted(records.items())},
        }


def diff_entry(old, new):
    changes = []
    if old.get("status") != new["status"]:
        changes.append({"field": "status", "previous": old.get("status"), "current": new["status"]})
    for t in sorted(set(old.get("records", {})) | set(new["records"])):
        o = set(old.get("records", {}).get(t, []))
        n = set(new["records"].get(t, []))
        if o != n:
            changes.append({"field": t, "added": sorted(n - o), "removed": sorted(o - n)})
    return changes


def send_event(tg, event):
    token = os.environ.get(tg.get("token_env", "TAEGIS_TOKEN"), "")
    headers = {"Content-Type": "application/json", **tg.get("headers", {})}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        tg["url"], data=json.dumps(event).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=tg.get("timeout", 15)) as r:
        if not 200 <= r.status < 300:
            raise RuntimeError(f"HTTP {r.status}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-c", "--config", default="config.json", help="config JSON (default: config.json)")
    ap.add_argument("--first-run", action="store_true", help="rebuild baseline state, send no events")
    ap.add_argument("--dry-run", action="store_true", help="print events instead of sending; don't update state")
    args = ap.parse_args()

    cfg = load_json(args.config)
    domains = [d.strip().rstrip(".").lower() for d in cfg["domains"]]
    rdtypes = [t.upper() for t in cfg.get("record_types", DEFAULT_TYPES)]
    state_path = cfg.get("state_file", "dns_state.json")
    tg = cfg.get("taegis")

    first = args.first_run or not os.path.exists(state_path)
    state = {} if first else load_json(state_path)

    resolver = AuthResolver(cfg.get("timeout", 5))
    new_state = dict(state)
    events = []
    errors = 0

    for d in domains:
        try:
            res = resolver.lookup(d, rdtypes)
        except Exception as e:  # keep going; one bad domain shouldn't stop the sweep
            log(f"[error] {d}: {e}")
            errors += 1
            continue
        if first or d not in state:
            if not first:
                log(f"[new] {d}: added to baseline")
            new_state[d] = res
            continue
        changes = diff_entry(state[d], res)
        if changes:
            events.append((d, res, {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "dns-watch",
                "event_type": "dns_record_change",
                "domain": d,
                "zone": res["zone"],
                "answered_by": res["server"],
                "changes": changes,
                "previous": state[d],
                "current": res,
            }))

    if first:
        if not args.dry_run:
            save_json_atomic(state_path, new_state)
        log(f"[baseline] {len(new_state)} domain(s) stored in {state_path}")
        return 1 if errors else 0

    sent_fail = 0
    for d, res, ev in events:
        if args.dry_run or not tg:
            print(json.dumps(ev, indent=2))
            if not tg and not args.dry_run:
                log("[warn] no 'taegis' config block; change printed but state NOT updated")
            continue
        try:
            send_event(tg, ev)
            new_state[d] = res
            log(f"[sent] {d}: {len(ev['changes'])} change(s)")
        except (urllib.error.URLError, RuntimeError, OSError) as e:
            log(f"[error] sending event for {d}: {e} (state kept; will retry next run)")
            sent_fail += 1

    if not args.dry_run:
        save_json_atomic(state_path, new_state)
    if not events:
        log("[ok] no changes")
    return 1 if (errors or sent_fail) else 0


if __name__ == "__main__":
    sys.exit(main())
