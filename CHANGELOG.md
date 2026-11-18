# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). `mason bump` turns `[Unreleased]` into the new version.

## [Unreleased]

### Added

- `plugin_test`: the hosted Settings > Plugins > Test button for a plugin that reaches the event network runs here, where the plugin runs.
- `FLOORBIT_AGENT_KEY_FILE`: the key from a file. Floorbit, which now reaches the event network only through an agent also when self-hosted, writes it for the agent next to it (its `docker-compose.yml` and Kubernetes manifest) and enrols it itself; the agent waits up to two minutes for the file.
- The attendee front door (`FLOORBIT_AGENT_FRONT_DOOR`): the hosted Floorbit's pages served on the event network, each request with the PC's own address and the agent's key, so check-in, the seat help page and the sign-in limits work behind the hall's NAT. The seat speed test is answered here and measures the hall. A short English and German page shows while the hosted side is away. Counts in `/metrics` and the report.
- Switch SSH for a hosted Floorbit (`switch_call`, `shell_open`, `shell_io`, `shell_close`): port actions, switch commands, config backups and the switch terminal are opened here, in the hall, with the SSH port actions plugin the hosted Floorbit turned on. Only private addresses, or `FLOORBIT_AGENT_SWITCH_NETS`; the login comes with each call and is cleared after it; host keys stay pinned on the hosted side. Terminals live here (at most 10) and close after 90 seconds nobody reads them.
- The crew's seat walk-through works with a hosted Floorbit or one behind NAT: the captive port answers `/floorbit/hello/<code>`, reports the crew laptop's own IP with the page's one-time code and sends the browser back to Floorbit. `FLOORBIT_AGENT_LAN_URL` tells Floorbit where laptops in the hall reach it. The test lab walks a seat block this way.
- A test lab (`lab/`, `docs/lab/`): a hosted Floorbit, two agents, a lab LibreNMS and a simulated event network (a Nexus core, a Catalyst 4500, four 2960-X, two OPNsense firewalls, a LAN cache) with syslog, traps and NetFlow on a loop, and end-to-end tests from seat lookup and check-in to agent failover. CI runs it on every pull request. `docs/lab/capture.md` lists what to record from real gear, and `lab/tools/anonymise.py` strips it before it is shared.
- Device telemetry without LibreNMS: with Floorbit's Telemetry plugin turned on, the agent is the collector. A Telegraf sidecar (`docker compose --profile telemetry up -d`) polls SNMP, takes traps, gNMI and Cisco model-driven telemetry, and sends to the agent on port 8186; the agent also asks NX-API, RESTCONF and the OPNsense API. The hosted Floorbit gets the per-port and per-device numbers, and which MAC with which IP is on which port for its IP and MAC history. How the collector is doing shows on the Local agents page.
- Plugin updates without a new image: the agent installs the signed plugin bundles the hosted Floorbit offers, only when they verify against the key built into the image and are newer than what runs, needing nothing the image lacks. `FLOORBIT_AGENT_PLUGIN_UPDATES` picks `auto` (default), `approve` (`python -m floorbit_agent plugins approve <id>`) or `off`. A new version that fails to start or reports an error in its first five minutes is rolled back and never tried again here. The report says which version each plugin runs, which waits and why, and the last rollouts.
- Plugins installed from Floorbit's Marketplace reach the agent too, even when its image doesn't have them: they come as the same signed bundles, with versions of their own.
- `ctx.primary()` for agent plugins: False on a standby agent while another holds the first VRRP address, so only one agent per hall polls devices and logs in. The report says whether this agent is primary and which addresses it holds.
- Agent plugins: the hosted Floorbit says which of its plugins run on the agent and with which settings (pulled every minute and when they change). Each runs in the agent with its own disk spool for what it sends, threads and listeners that stop with it, and health, numbers and errors in `/metrics` and the report. Actions that change something in the hall run only when `FLOORBIT_AGENT_ALLOW_ACTIONS` on the agent allows them. `check` asks every plugin too. Plugins without an agent part (LibreNMS, NetBox, Pi-hole, ...) follow the hosted Floorbit too: what is on there is turned on here, with its settings kept in memory only; the agent's own environment still wins, and `FLOORBIT_AGENT_PLUGINS` keeps a plugin on regardless. A plugin can ask the hosted part for what it needs (`ctx.fetch`), and a kind marked `latest` keeps only the newest record while the hosted side is away. A plugin's HTTP listener can serve HTTPS with `FLOORBIT_AGENT_TLS_CERT` and `FLOORBIT_AGENT_TLS_KEY`. See Floorbit's docs/agent-plugins.md.
- The agent can answer the Captive Portal API (RFC 8908) on the event network (`FLOORBIT_AGENT_CAPTIVE`), for DHCP option 114: phones and laptops show a quiet "sign in to network" note until their seat has checked in on the hosted Floorbit, and nothing is ever blocked. It keeps answering from the last word when the hosted side is away (bitsandbricks-ch/floorbit#473).
- Captive portal numbers in `/metrics` and the report to the hosted Floorbit: answers by `captive` or `done`, clients checked in, the age of the list and whether asking for it fails. Counts only, never an IP. The log says when the agent starts answering and how many clients the list has.
- The agent answers `firewall_feeds`: a hosted Floorbit's **Import from NetBox** on Firewalls reads the seat ranges from the event's NetBox through it.
- The agent answers `dns_probe` and `tcp_probe`, so a hosted Floorbit's services view measures the event's internet lines and resolvers from the event network (bitsandbricks-ch/floorbit#471). Never towards the agent machine or link-local addresses (or only `FLOORBIT_AGENT_PROBE_NETS`); TCP probes only to ports 53, 80, 443 and 853, sending nothing.
- The agent answers `neighbours`, what each switch sees by LLDP or CDP, for the hosted topology page (bitsandbricks-ch/floorbit#468).
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

### Changed

- Built for a 1,000-switch hall: 16 seat lookups run side by side (was 3), with more room for SSH and port calls, and the NetFlow socket is read in its own thread, away from writing the raw files and the minute's summary, so a busy summary no longer loses datagrams.
- Built and tested on Floorbit with the night's work in it (`FLOORBIT_REF`).
- The work from #8 to #15 reaches `main`: those pull requests were merged into stack branches that had already gone in.

### Fixed

- Stopping the agent while it was sending syslog could fail with "pop from an empty deque" and keep the waiting lines on disk twice.
- A syslog batch the hosted Floorbit refuses as too big is sent again in halves instead of blocking the lines behind it; a single line still too big is skipped and logged.
- A burst of syslog no longer loses most of its lines: one thread reads the port instead of a thread per datagram, which kept only about a fifth of a 25,000-line-a-second burst. Syslog and NetFlow ask for an 8 MB receive buffer.
- A heartbeat file that can't be written (a full disk) no longer turns the heartbeat off for good: the agent keeps trying and says so once.
- `/healthz` no longer reads as unhealthy when the clock steps back.

### Security

- Game server queries no longer go to link-local addresses (`169.254.169.254`, where clouds keep their metadata service): Python counts them as private, so they passed the private-only check.
- Game queries and service probes check an IPv4-mapped IPv6 address (`::ffff:127.0.0.1`) as the IPv4 address it reaches, also on Python releases that don't do so themselves.
- Game queries and service probes also check the IPv4 address inside NAT64 (`64:ff9b::/96`, `64:ff9b:1::/48`) and 6to4 (`2002::/16`) addresses: Python calls `64:ff9b:1::a9fe:a9fe` private, but on a NAT64 network it is the metadata service at `169.254.169.254`. Probes now refuse this machine and link-local addresses even when `FLOORBIT_AGENT_PROBE_NETS` would allow them.
