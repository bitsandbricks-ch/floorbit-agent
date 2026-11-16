# Threat model

What the agent protects, who might attack it, what they could do, and what stops them. Keep this
page current when the agent learns something new (issue #3). Found something that isn't covered?
See [SECURITY.md](../SECURITY.md).

## What the agent is

A program on the event network that connects **out** to a hosted Floorbit over HTTPS (a WebSocket
link, or an HTTPS long poll), and answers a fixed list of questions about the event network by
asking the event's LibreNMS and NetBox. Optionally it takes the switches' syslog and NetFlow on
UDP and sends them on, the flows only as a summary.

## What is worth protecting

| Asset | Where it lives |
| --- | --- |
| The agent key (`fba_<id>_<secret>`) | `agent.env` on the agent machine; only its SHA-256 on the hosted side |
| LibreNMS and NetBox tokens | `agent.env`; they never leave the agent machine |
| The event network's layout: switches, ports, ARP (IP ↔ MAC) | LibreNMS/NetBox; passes through the agent as answers |
| Syslog lines | from the switches, through the agent, to the hosted Logs page; while the uplink is down, in the agent's disk buffer (`/app/state/buffer`, readable by the agent's user only, at most 24 h) |
| Traffic data | NetFlow stays on the agent; only aggregates leave it (seat totals, protocol mix, 5+ seats rule) |
| The agent machine itself | the event network |

## Who might attack, and what stops them

### Someone on the internet

- **Reach the agent:** the agent listens on nothing on the internet. It only connects out. The
  metrics port is published on the machine's 127.0.0.1 by default.
- **Pretend to be the hosted Floorbit:** the agent refuses `http://` unless
  `FLOORBIT_AGENT_INSECURE=1`, and checks the hosted side's TLS certificate (requests' default,
  never turned off). Without the hosted side's certificate key, no call reaches the agent.
- **Read or change data on the way:** TLS for the long poll and the WebSocket link (`wss://`).

### Someone who controls the hosted Floorbit (or steals its database)

- **Make the agent do something else:** the hosted side can only ask the methods in
  `floorbit_agent.METHODS` (switches, ports, ARP, inventory, health and DNS feeds). Anything else is
  refused on the agent, and unknown methods don't even become a metrics label. There is no "run
  this command" call. Game servers (`game_query`, `game_rcon`) are asked only at private
  addresses, or at `FLOORBIT_AGENT_GAME_NETS` when set, never at the agent machine itself, so the
  hosted side can't use the agent to reach the internet or local ports. The host is resolved once
  and that address is used. An RCON password comes with the call and is neither kept nor logged.
  The services' probes (`dns_probe`, `tcp_probe`) may go to public addresses, since internet lines
  and resolvers usually are, but never to the agent machine or link-local addresses (or only to
  `FLOORBIT_AGENT_PROBE_NETS` when set). A TCP probe only connects to port 53, 80, 443 or 853, at
  most ten times, and sends nothing; a DNS probe sends one A query. Neither can talk to a service.
  Port actions and SSH are not part of the agent today. When they come
  (bitsandbricks-ch/floorbit#474), each will be its own method with its own checks, and its
  credentials will be sent per call and never stored on the agent.
- **Flood the agent:** at most `WORKERS` calls of one kind run at a time, and at most `MAX_WAITING`
  wait. Anything more gets a "busy" answer at once, so memory can't grow without end and one slow
  kind can't hold up the others.
- **Get the LibreNMS/NetBox tokens:** they are never sent. The hosted side gets answers, not
  access.
- **Use a stolen agent-key hash:** the database only has SHA-256 hashes of the keys. A hash can't
  be used as a key.

### Someone on the event network (an attendee)

- **Talk to the agent:** it takes UDP on the syslog and NetFlow ports only when they are turned on.
  Fake syslog lines are dropped by the hosted side's allow-list (`SYSLOG_ALLOW`, the switches'
  management addresses). Fake flows can skew traffic statistics but carry nothing back. Put the
  ports on the management network or limit them with a firewall rule.
- **Ask the Captive Portal API** (only with `FLOORBIT_AGENT_CAPTIVE`): it has to be reachable by
  attendees, by design. A client learns only whether its own IP counts as checked in, and the URL of
  the check-in page. The list of checked-in IPs never leaves the agent. `X-Forwarded-For` is
  believed only from the addresses in `FLOORBIT_AGENT_CAPTIVE_PROXIES`, so a client can't ask about
  another IP. A wrong answer can only show or hide the note: nothing is blocked either way.
- **Flood the UDP ports:** the syslog backlog has a fixed size, the newest lines are dropped and
  counted (`floorbit_agent_syslog_dropped_total`), and memory stays bounded.
- **Read the report:** the half-minute report to the hosted Floorbit carries the same kind of counts
  as `/metrics`, nothing more.
- **Read `/metrics`:** it is only counts and timings, with no IPs, names, log lines or keys. When
  the port is reachable from the LAN, set `FLOORBIT_AGENT_METRICS_TOKEN`. `/healthz` stays open on
  purpose: it says only up or down, the link and how long ago.
- **Take over the agent's address:** a captive portal or VRRP address taken by an attendee is a
  layer-2 problem. VRRP has no real authentication: anyone on the VLAN can claim a VIP with a
  higher priority. Keep the agents and their virtual IPs on a management VLAN.
- **VRRP mode's rights:** the container gets `NET_ADMIN` and the host network. The agent still runs
  as user 10001; only the keepalived program carries the network capabilities, and it runs no
  scripts. Its config is written by the agent from validated settings (addresses parsed, the
  interface name checked), so a setting can't add config of its own.

### Someone with the agent key

They could connect as that agent and answer calls with made-up data, or read nothing more than
the calls themselves (which hold no secrets). **Revoke** the key under Settings > Local agents and
enrol again. The old key stops working at the next request, and the link is closed. Keep
`agent.env` readable only by the user running the agent.

### Someone with a shell on the agent machine

They have the agent key and the LibreNMS/NetBox tokens. That is out of scope for the agent itself:
patch the machine, keep it on the management network, and give the LibreNMS and NetBox tokens
read-only rights.

## Running safely

- Let the agent connect to `https://` only, and leave certificate checks on.
- Give LibreNMS and NetBox read-only tokens.
- Keep the syslog, NetFlow and metrics ports on the management network.
- Run the image as it comes: it runs as user 10001, not root.
- Revoke the keys of agents you no longer use. The hosted side allows at most 10.

## Not covered yet

- The disk buffer and agent-to-agent sync (bitsandbricks-ch/floorbit#465): the agents will
  authenticate each other with their own keys, over TLS. That design gets its own section here
  when it is built.
- Pinning the hosted side's certificate.
