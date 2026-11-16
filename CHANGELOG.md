# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). `mason bump` turns `[Unreleased]` into the new version.

## [Unreleased]

### Added

- The agent answers `game_query` and `game_rcon`, so a hosted Floorbit can watch the hall's game servers and send RCON commands from the event network (bitsandbricks-ch/floorbit#472). Only private addresses are asked, or the networks in `FLOORBIT_AGENT_GAME_NETS`; the RCON password comes with each call and is never kept or logged.
- The agent answers `port_clients`, a switch's MAC table by port, so a hosted Floorbit can list the machines on seat ports that its patch plan doesn't explain (bitsandbricks-ch/floorbit#473).
- A disk buffer for syslog: lines the hosted Floorbit can't take go to the state volume instead of being dropped, survive a restart, and are sent oldest first behind the live lines once it is back (at most 2 GB and 24 hours). Every line carries a stable ID so copies can be dropped.
- VRRP mode: agents share virtual IPs through keepalived in the image (`compose.vrrp.yml`, `docs/vrrp.md`), each holding its preferred VIP while it is up and the others taking over in seconds. State in `/metrics` and the log. `scripts/vrrp_check.py` checks failover with the real keepalived in two network namespaces, also in CI.
- Every half minute the agent sends the hosted Floorbit a short report about itself (up time, link drops, syslog and NetFlow counts, busy calls), shown on its Local agents page. Over the link, or a POST on the long poll; an older Floorbit that has no place for it is asked once.
- Each kind of call runs in its own small pool, so a hung LibreNMS lookup holds up only more of the same kind. When more than 200 calls of one kind wait, the rest get a "busy" answer at once instead of piling up.
- `scripts/soak.py`: hours of steady load with link drops, checking that memory, threads and open files stay flat.
- `docs/threat-model.md`: what the agent protects, from whom, and how.
- `/metrics` (Prometheus) and `/healthz` on port 9520: the link, calls by method and outcome with their times, round trips to the hosted Floorbit, reconnects, syslog and NetFlow counts, backlog and drops. Optional bearer token. A Grafana dashboard in `grafana/floorbit-agent.json`.
- The WebSocket link (agent protocol 2): calls arrive over one open `wss://` connection the moment they are made. The agent falls back to the HTTPS long poll when the hosted Floorbit doesn't offer the link or it can't be opened, and stays on protocol 1 with an older Floorbit.
- The local agent, moved here from Floorbit (`floorbit/agent/`): call loop, `check` and `health`, syslog and NetFlow relays, gzip, protocol 1. Its own image, `ghcr.io/bitsandbricks-ch/floorbit-agent`, with the Python parts of Floorbit at `FLOORBIT_REF` (no web pages, no React app).

### Fixed

- A burst of syslog no longer loses most of its lines: one thread reads the port instead of a thread per datagram, which kept only about a fifth of a 25,000-line-a-second burst. Syslog and NetFlow ask for an 8 MB receive buffer.
- A heartbeat file that can't be written (a full disk) no longer turns the heartbeat off for good: the agent keeps trying and says so once.
- `/healthz` no longer reads as unhealthy when the clock steps back.
