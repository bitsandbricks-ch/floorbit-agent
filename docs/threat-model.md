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
- **Catch the key with a redirect:** the agent follows no redirect. The key goes in its own header,
  which requests (unlike `Authorization`) keeps on a redirect to another host, even to `http://`. A
  redirect is reported with where it points, and the agent tries again later.

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
  The crew's own checks (`check_probe`) go to the same addresses, but to any port: a ping (three
  echo requests), a TCP connect that sends nothing, one fixed UDP question (DNS, NTP, Source query
  or an empty packet) or one HTTP(S) `GET` with no body, no cookies and no login, reading at most
  64 KB of the answer. They let a taken-over hosted Floorbit map which ports answer on networks the
  agent may reach, as anyone in the hall could; `FLOORBIT_AGENT_PROBE_NETS` narrows that.
  Switch SSH (`switch_call`, `shell_open`, `shell_io`, `shell_close`, floorbit_agent/switches.py)
  runs only the port plugin's own methods (`bounce_port`, `set_port`, `send_commands`, a shell):
  which commands a template may send is decided by the hosted side's role checks, so a taken-over
  hosted Floorbit can do on the switches what an admin there could, with the logins it holds, and
  every such call is in its audit log. It can't send the agent anywhere else: switches are reached
  only at private addresses, or `FLOORBIT_AGENT_SWITCH_NETS` when set, never this machine. The login
  comes with the call and is cleared when the connection is done; it is never kept, logged or
  written to the agent's disk. Host keys are pinned on the hosted side and checked here; a changed
  key stops the call before anything is sent. At most `MAX_SHELLS` (10) shells are open, and one
  nobody reads for 90 seconds is closed.
- **Offer a plugin update that writes elsewhere:** a bundle runs only when its signature verifies
  against a key built into the image. Before that, its id and version name folders under the
  agent's state, so an offer whose id or version could be a path (`/some/dir`, `9/../../x`) is
  ignored, and an offer the agent can't read waits instead of stopping the plugins' settings.
- **Flood the agent:** at most `WORKERS` calls of one kind run at a time (more for switch calls and
  terminals, `POOL_SIZES`), and at most `MAX_WAITING`
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
- **Ask the Captive Portal API** (only with the plugin "Check-in note on phones" on): it has to be reachable by
  attendees, by design. A client learns only whether its own IP counts as checked in, and the URL of
  the check-in page. The list of checked-in IPs never leaves the agent. `X-Forwarded-For` is
  believed only from the plugin's trusted proxies, so a client can't ask about
  another IP. A wrong answer can only show or hide the note: nothing is blocked either way.
- **Use the front door** (only with the plugin "Front door for attendees" on): it reaches the hosted Floorbit
  and nothing else, and never passes on the agent's own API (`/api/agent/`). It adds the agent key
  and the client's address to each request; whatever a client sends under those names, and
  `X-Forwarded-For`, is dropped, so an attendee can't name another seat's address. A client behind
  a proxy is named only by the plugin's trusted proxies. The key is in the
  request to the hosted side only (over HTTPS), never in an answer. At most 200 requests are passed
  on at once (more get 503), bodies over 32 MB are refused, and the speed test answered here shares
  Floorbit's per-client byte budget and its cap of streams at once.
- **Send a seat walk-through hello** (`/floorbit/hello/<code>` on the same port): without a code the
  crew got from a signed-in Floorbit page in the last ten minutes, the hosted side refuses it. A code
  is used once. With a stolen code an attendee could only make that walk-through step find their own
  seat's port, which the crew sees on the page. The redirect back goes only to the `FLOORBIT_URL`
  origin, so the endpoint is no open redirect, and its page escapes everything it shows. Each hello
  is a request to the hosted Floorbit, so one address gets 10 a minute; the rest are answered 429
  without asking Floorbit.
- **Flood the UDP ports:** the syslog backlog has a fixed size, the newest lines are dropped and
  counted (`floorbit_agent_plugin_dropped{plugin="syslog-relay"}`), and memory stays bounded.
- **Send broken or crafted flow packets:** a packet the collector can't read is skipped and counted;
  the listener keeps running. Templates are capped, so made-up ones can't fill memory.
- **Hold connections open** on the captive or metrics port: a connection that sends nothing for 10
  seconds is closed, one address gets at most 16 at a time and the port 256, and the rest are closed
  at once. Loopback is never refused, so keepalived's `/healthz` keeps answering during a flood. A
  flood can make the captive note late for others; it can't take the agent down.
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
