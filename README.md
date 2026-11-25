# dns-watch

Command-line tool that resolves a list of domains directly against their **authoritative nameservers**, keeps a local baseline, and sends a **Taegis event via HTTP Ingest** whenever a record changes.

## Install

```
pip install -r requirements.txt
```

Requires Python 3.8+ and `dnspython`.

## Configuration

Copy `config.example.json` to `config.json` and edit:

```json
{
  "domains": ["example.com", "www.example.com"],
  "record_types": ["A", "AAAA", "CNAME", "MX", "NS", "TXT"],
  "state_file": "dns_state.json",
  "timeout": 5,
  "taegis": {
    "url": "https://<your-taegis-http-ingest-endpoint>",
    "token_env": "TAEGIS_TOKEN",
    "headers": {},
    "timeout": 15
  }
}
```

| Key | Required | Description |
|-----|----------|-------------|
| `domains` | yes | Names to monitor (apex or subdomains). |
| `record_types` | no | Record types to query. Default: A, AAAA, CNAME, MX, NS, TXT. |
| `state_file` | no | Where the baseline is stored. Default: `dns_state.json`. |
| `timeout` | no | Per-query DNS timeout in seconds. Default: 5. |
| `taegis.url` | for alerting | HTTP Ingest endpoint. |
| `taegis.token_env` | no | Environment variable holding the token, sent as `Authorization: Bearer <token>`. Default: `TAEGIS_TOKEN`. |
| `taegis.headers` | no | Extra HTTP headers (merged over the defaults). |
| `taegis.timeout` | no | HTTP timeout in seconds. Default: 15. |

The token is read from the environment so it never lives in the config file:

```powershell
$env:TAEGIS_TOKEN = "..."
```

## Usage

```
python dns_watch.py -c config.json              # baseline on first run, compare afterwards
python dns_watch.py -c config.json --first-run  # rebuild the baseline, send no events
python dns_watch.py -c config.json --dry-run    # print events, send nothing, leave state untouched
```

## How it works

1. For each domain, find its zone and the zone's NS hosts (via the system resolver), then resolve those hosts to IPs.
2. Query each requested record type **directly on an authoritative server** (UDP, TCP fallback on truncation). The first server that answers is used.
3. **First run** (no state file, or `--first-run`): store the results and send nothing.
4. **Later runs**: compare against the stored state. Each changed domain produces one event. Domains newly added to the config are quietly added to the baseline.
5. After a successful send, the new state is saved.

CNAMEs in the answer are recorded under `CNAME`. An `NXDOMAIN` is stored as status `NXDOMAIN`, so a domain appearing or disappearing is itself a change.

## Event format

One JSON object per POST:

```json
{
  "timestamp": "2026-11-25T16:31:36+00:00",
  "source": "dns-watch",
  "event_type": "dns_record_change",
  "domain": "example.com",
  "zone": "example.com",
  "answered_by": "elliott.ns.cloudflare.com (162.159.44.228)",
  "changes": [
    { "field": "A", "added": ["172.66.147.243"], "removed": ["9.9.9.9"] }
  ],
  "previous": { "...": "full previous state for the domain" },
  "current":  { "...": "full current state for the domain" }
}
```

A status change appears as `{"field": "status", "previous": "NOERROR", "current": "NXDOMAIN"}`.

If your Taegis ingest expects a different schema or framing (for example NDJSON), adjust `send_event()` and the event dict in `main()` in `dns_watch.py`.

## Failure behavior

- **No authoritative server answers** for a domain: it is skipped, its previous state is kept, no event is sent, and the run exits with code 1.
- **Event send fails**: that domain's state is *not* updated, so the change is detected and retried on the next run. Exit code 1.
- **No `taegis` block configured**: changes are printed to stdout and state is not updated.
- Exit code is `0` when everything succeeded (with or without changes).

## Scheduling

Run it periodically, for example with Windows Task Scheduler:

```
schtasks /Create /SC MINUTE /MO 15 /TN "dns-watch" /TR "python C:\git\dns-watch\dns_watch.py -c C:\git\dns-watch\config.json"
```

Set `TAEGIS_TOKEN` in the task's environment, or as a system or user environment variable. On Linux, use cron or a systemd timer.

## Notes

- Zone discovery and NS address lookups use the system resolver; only the actual record queries go to the authoritative servers.
- Round-robin or geo-DNS records that legitimately vary between queries will look like changes. Avoid monitoring such names, or drop the affected record types from `record_types`.
- Keep `dns_state.json` and `config.json` out of version control if they contain anything sensitive.
