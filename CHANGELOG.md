# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). `mason bump` turns `[Unreleased]` into the new version.

## [Unreleased]

### Added

- Every half minute the agent sends the hosted Floorbit a short report about itself (up time, link drops, syslog and NetFlow counts, busy calls), shown on its Local agents page. Over the link, or a POST on the long poll; an older Floorbit that has no place for it is asked once.
- Each kind of call runs in its own small pool, so a hung LibreNMS lookup holds up only more of the same kind. When more than 200 calls of one kind wait, the rest get a "busy" answer at once instead of piling up.
- `scripts/soak.py`: hours of steady load with link drops, checking that memory, threads and open files stay flat.
- `docs/threat-model.md`: what the agent protects, from whom, and how.
- `/metrics` (Prometheus) and `/healthz` on port 9520: the link, calls by method and outcome with their times, round trips to the hosted Floorbit, reconnects, syslog and NetFlow counts, backlog and drops. Optional bearer token. A Grafana dashboard in `grafana/floorbit-agent.json`.
- The WebSocket link (agent protocol 2): calls arrive over one open `wss://` connection the moment they are made. The agent falls back to the HTTPS long poll when the hosted Floorbit doesn't offer the link or it can't be opened, and stays on protocol 1 with an older Floorbit.
- The local agent, moved here from Floorbit (`floorbit/agent/`): call loop, `check` and `health`, syslog and NetFlow relays, gzip, protocol 1. Its own image, `ghcr.io/bitsandbricks-ch/floorbit-agent`, with the Python parts of Floorbit at `FLOORBIT_REF` (no web pages, no React app).

### Fixed

- A heartbeat file that can't be written (a full disk) no longer turns the heartbeat off for good: the agent keeps trying and says so once.
- `/healthz` no longer reads as unhealthy when the clock steps back.
