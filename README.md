# Floorbit Agent

[![CI](https://github.com/bitsandbricks-ch/floorbit-agent/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/bitsandbricks-ch/floorbit-agent/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/bitsandbricks-ch/floorbit-agent?color=f27a4f&labelColor=1a1a1a)](https://github.com/bitsandbricks-ch/floorbit-agent/releases/latest)
[![License](https://img.shields.io/github/license/bitsandbricks-ch/floorbit-agent?color=43a868&labelColor=1a1a1a)](LICENSE)
[![Buy Me a Coffee](https://img.shields.io/badge/buy_me_a_coffee-bitsandbricks-f27a4f?logo=buymeacoffee&logoColor=white&labelColor=1a1a1a)](https://buymeacoffee.com/bitsandbricks)

The local agent for a hosted Floorbit: it runs on the event network and answers for it over one outgoing HTTPS connection.

## Why

A hosted [Floorbit](https://github.com/bitsandbricks-ch/floorbit) can't reach the event network:
LibreNMS, NetBox and the switches sit behind the event's firewall. The agent runs inside, next to
them, and connects out to the hosted Floorbit. It takes the hosted side's calls (switch status,
ports, ARP, the inventory, health checks, DNS and Wi-Fi counts) and answers them with the local
plugins. If you set it up to, it also forwards syslog and a NetFlow summary. Which plugins it runs,
and with which settings, comes from the hosted Floorbit (**Settings > Plugins**): the agent's own
environment holds only its address and key. Nothing on the event network has to be reachable from
outside.

Everything goes over TLS, checked against the usual certificate authorities. Calls come over a
WebSocket link (`wss://`) that stays open, so the agent gets them the moment the hosted side asks.
If something on the way doesn't pass WebSockets, the agent falls back to an HTTPS long poll by
itself (after at most 15 seconds of trying) and tries the link again every five minutes. Syslog and flow summaries go as HTTPS posts,
gzipped. The agent key is sent with each request and kept on the hosted side only as a hash.

## Install

In the hosted Floorbit, **Settings > Local agents**: enrol an agent and copy its key. Then, on a
machine on the event network:

```sh
curl -fsSLO https://raw.githubusercontent.com/bitsandbricks-ch/floorbit-agent/main/compose.yml
curl -fsSLo agent.env https://raw.githubusercontent.com/bitsandbricks-ch/floorbit-agent/main/agent.env.example
# fill in FLOORBIT_URL and FLOORBIT_AGENT_KEY in agent.env; the rest is set up in Floorbit
docker compose run --rm agent python -m floorbit_agent check
docker compose up -d
curl -s http://127.0.0.1:9520/healthz     # {"ok": true, ...} once it reached the hosted Floorbit
```

That's one container. Telegraf, which collects device telemetry (SNMP, traps, gNMI, Cisco MDT),
is in the image and runs inside the agent when the **Telemetry** plugin is on: there's no
second container to start. `compose.yml` publishes what is needed by default (9520 on this machine
only, 162/udp for traps and 57000 for MDT); the ports of the plugins that listen in the hall
(syslog 514/udp, NetFlow 2055/udp, a firewall's own Telegraf on 8186, the check-in note on 8908, the
front door on 8443) are commented out until you use them. Every port, and how to check that each
one works: Floorbit's [docs/agent.md, "Ports"](https://github.com/bitsandbricks-ch/floorbit/blob/master/docs/agent.md#ports).

**Updating from a compose file with a `telegraf` service:** delete that service, take the new
`compose.yml` (or add the two telemetry ports to yours), and run `docker compose pull && docker
compose up -d --remove-orphans`. A telegraf container that keeps running is refused by the agent
and named on its page in Floorbit.

## Use

```sh
python -m floorbit_agent           # run: poll the hosted Floorbit and answer its calls
python -m floorbit_agent check     # try each part once and say what works
python -m floorbit_agent health    # 0 when the hosted Floorbit answered in the last two minutes
```

Every setting, what works through the agent and the protocol are in Floorbit's
[docs/agent.md](https://github.com/bitsandbricks-ch/floorbit/blob/master/docs/agent.md).

## Settings from Floorbit

Each agent's page in Floorbit (**Local agents**, one agent, **Settings**) sets where laptops reach
it, its metrics port and token, its disk buffer, the Telegraf port, the log level and summary line,
VRRP and the plugin settings that
differ on this agent (the flow port, a listen address). The agent takes them with its plugin config,
keeps them in its state folder (`agent-settings.json`) and starts with them next time. Its own
environment variable still wins when it is set; the page shows where each value comes from and
which take effect at the next start (the address and the log level apply at once).

What the hall allows stays in the agent's environment only and is shown read-only:
`FLOORBIT_AGENT_ALLOW_ACTIONS`, `FLOORBIT_AGENT_PLUGIN_UPDATES` and the networks it may reach
(`FLOORBIT_AGENT_SWITCH_NETS`, `_GAME_NETS`, `_PROBE_NETS`). A stolen Floorbit login can't widen
them.

**New key.** *Roll key* on the agent's page has the agent fetch a new key itself and keep it in its
state folder (`agent-key`, readable only by the agent). From then on it wins over
`FLOORBIT_AGENT_KEY`, which only enrols. The old key works until the new one is used. When the
stored key is refused (the agent was enrolled again), the agent sets it aside and uses the env's.

**Certificate.** With the names the agent is reached by listed on its page, the agent makes its own
key (`tls/agent-key.pem`, it never leaves the machine), Floorbit's agents CA signs it, and the
plugins' HTTPS listeners (the front door, the telemetry listener) serve with it. It asks again 30
days before the end. Install the CA (*Download CA* on the Local agents page) on crew devices once.
`FLOORBIT_AGENT_TLS_CERT` and `_KEY` set by the hall win.

**Let's Encrypt.** When the hosted Floorbit has a domain for its agents, it sends the tenant's
Let's Encrypt certificate for `<tenant>.agent.<domain>` as well, so laptops trust the front door
without a CA. The listeners keep both and pick per connection by the name the browser asks for: the
Let's Encrypt one for its own name while it is valid, the CA's for the agent's address, its other
names and browsers that send no name. All the tenant's agents share the Let's Encrypt key: Floorbit
sends it encrypted to the agent's own key (ECDH P-256, HKDF-SHA256, AES-256-GCM), and the agent keeps
it in `tls/public-key.pem` (0600). Point the name at the agent in the hall's DNS.

## Settings from an older agent

An agent before this one read LibreNMS, NetBox, `FLOORBIT_PLUGIN_*`, `FLOORBIT_AGENT_PLUGINS` and
its relays (`FLOORBIT_AGENT_SYSLOG_PORT`, `_FLOWS_PORT`, `_CAPTIVE`, `_FRONT_DOOR`, ...) from its
own environment. It no longer does. The first time it gets through after the update, it sends what
those set to the hosted Floorbit once, which takes it over for each plugin nobody has set up there
yet and says so in its audit log. The agent logs what was taken over and which variables to remove.
Update the hosted Floorbit first: an older one doesn't know this handover.

## When the uplink is down

What a plugin sends to the hosted Floorbit (syslog lines, flow summaries, ...) goes to a disk
buffer in the agent's state volume first, so nothing is lost while the hosted side is away or the
agent restarts. Once it is back, each plugin catches up oldest first. Each plugin's buffer keeps at
most 2 GB and 24 hours (`FLOORBIT_AGENT_BUFFER_MAX_MB`, `FLOORBIT_AGENT_BUFFER_MAX_HOURS`); past that
the oldest go first, counted in `floorbit_agent_plugin_dropped`. Every syslog line carries an ID
made of its sender and text, the same on every agent, so the hosted side can drop copies.

A busy event's switches send syslog and NetFlow in bursts. The agent asks the kernel for an 8 MB
receive buffer on each port, but Linux caps it at `net.core.rmem_max` (often 208 KB): on the agent
machine, `sysctl -w net.core.rmem_max=8388608` (and in `/etc/sysctl.d/` to keep it) gives it the room.

## Monitor it

The agent answers on port 9520 (`FLOORBIT_AGENT_METRICS`; `compose.yml` publishes it on the
machine's 127.0.0.1 only):

- `GET /healthz` (never needs the token, for local checks): 200 while the hosted Floorbit answered in the last two minutes, 503 otherwise,
  with the reason and whether the WebSocket link is up.
- `GET /metrics`: Prometheus text. Covers the link and connection, calls by method and outcome and how long
  they took, round trips to the hosted Floorbit, reconnects and link drops, and each plugin's own
  numbers as `floorbit_agent_plugin_<name>{plugin="<id>"}` (syslog received, dropped and waiting,
  NetFlow packets and summaries, what waits in the buffer).

- `GET /` (never needs the token): a status page for a quick look in a browser, reloading every
  10 s: connected or not, the agent's numbers, and each plugin's state, message and numbers (for
  telemetry: points received, devices heard and failing, sends to Floorbit, which device fails and
  why). `GET /status.json` gives the same as JSON. Read only, no scripts, no forms; plugin settings,
  logins and keys are never on it, and anything shaped like a token, password or community is masked.

### The log

`docker compose logs -f agent` shows one line per event, each with the time, the level and the part
that speaks:

```
2026-11-19T10:51:14+0100 INFO  [agent] connected to https://floor.example.com as "Hall A" (Floorbit 1.0.0)
2026-11-19T10:51:15+0100 INFO  [telemetry] Telegraf started (pid 41)
2026-11-19T10:52:15+0100 INFO  [telemetry] polls, last minute: 6,720 points from 1 of 2 devices: sw-a-01 6720
2026-11-19T10:52:15+0100 WARNING [telemetry] nothing in the last minute from 1: sw-a-02 (no answer)
2026-11-19T10:56:14+0100 INFO  [agent] connected over WebSocket; up 5 min | telemetry warning (2 devices, 1 devices failing, 6,720 points, 69 sent)
```

It says when the agent connects, loses the link or falls back to the long poll; when a plugin starts,
stops, fails or changes state (`[telemetry] now warning: ...`); when Telegraf starts, stops or reloads;
each Test now and its result; and every minute what each device gave. Every five minutes
(`FLOORBIT_AGENT_LOG_SUMMARY`, seconds, 0 for never) a summary line says how the agent and each plugin
are doing. `FLOORBIT_AGENT_LOG_LEVEL=debug` adds every call from the hosted Floorbit with how long it
took, every send with its count, every post from Telegraf and Telegraf's own lines; `warning` keeps
only what went wrong. Keys and logins are never logged.

`/metrics` is all counts and timings: no IPs, names, log lines or keys. The status page shows
plugin messages, which can name a device and its address. To scrape it from another machine,
publish the port on the LAN and set `FLOORBIT_AGENT_METRICS_TOKEN` (it guards `/metrics` only); Prometheus then sends it as a
bearer token (`authorization: {credentials: ...}` in the scrape config).

To open the status page from a laptop on the event network, publish the port there instead of on
127.0.0.1 (`"9520:9520"` in `compose.yml`, or the agent's LAN address), and keep it off networks
players use.

Without Prometheus, the hosted Floorbit's Local agents page shows a summary of the same numbers:
the agent sends it every half minute.

[`grafana/floorbit-agent.json`](grafana/floorbit-agent.json) is a Grafana dashboard for these
metrics (Dashboards > New > Import), with one row per agent (`instance`).

## Several agents, shared IPs

Agents can share virtual IPs with VRRP, so the event network keeps reaching one when another goes
down: keepalived in the image (`compose.vrrp.yml`), on the machine, or kube-vip/MetalLB on k3s.
See [docs/vrrp.md](docs/vrrp.md).

## The check-in note (Captive Portal API)

With the plugin **Check-in note on phones** turned on for it in Floorbit (address
`0.0.0.0:8908`), the agent answers `/.well-known/captive-portal`
(RFC 8908). Point DHCP option 114 at it, through the event's HTTPS reverse proxy, and phones and
laptops show a quiet "sign in to network" note until their seat has checked in. Nothing is ever
blocked. See Floorbit's
[docs/integrations.md](https://github.com/bitsandbricks-ch/floorbit/blob/master/docs/integrations.md#the-sign-in-to-network-note-captive-portal-api).
With VRRP, point option 114 at a virtual IP, so the note keeps working when one agent is down.

The same port answers the crew's seat walk-through (Floorbit's
[docs/seat-walk.md](https://github.com/bitsandbricks-ch/floorbit/blob/master/docs/seat-walk.md)): a
hosted Floorbit, or one behind NAT, only sees the hall's public address, so the page sends the crew
laptop once to `/floorbit/hello/<code>` here. The agent reports the laptop's own IP with that one-time
code and sends the browser back, only ever to the Floorbit in `FLOORBIT_URL`. Set
`FLOORBIT_AGENT_LAN_URL` to the address laptops in the hall use for this port (for example
`http://192.0.2.10:8908`), and the page finds it.

## Security

[docs/threat-model.md](docs/threat-model.md) says what the agent protects, from whom, and how;
[SECURITY.md](SECURITY.md) how to report a problem.

## How it relates to Floorbit

The hosted side (the call tunnel, the `/api/agent/v1/` routes, the Local agents page) lives in
Floorbit. This repository is the program that runs at the event. They speak a versioned protocol
(`Floorbit-Agent-Protocol`, now 2), so they can be released apart: a Floorbit that doesn't speak the
agent's protocol answers 426, and the agent stops and says so.

The agent reuses Floorbit's plugins and flow collector instead of copying them. The image takes the
Python parts of Floorbit at one commit, `FLOORBIT_REF` (no web pages, no React app), and CI tests
against that same commit.

## Develop

```sh
git clone https://github.com/bitsandbricks-ch/floorbit.git
git clone https://github.com/bitsandbricks-ch/floorbit-agent.git
cd floorbit-agent
pip install -r ../floorbit/requirements.txt -e ".[dev]"
ruff check . && pytest -q          # tests find ../floorbit, or set FLOORBIT_SRC
```

The test lab runs Floorbit, two agents and a simulated event network (Nexus core, Catalyst
switches, firewalls, LAN cache) without any event hardware: see [docs/lab](docs/lab/README.md).

Pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Coding agents follow
[AGENTS.md](AGENTS.md). Security problems go through a private report, see [SECURITY.md](SECURITY.md).

## Releases

`mason bump floorbit-agent minor` opens the version PR, `mason release tag floorbit-agent` pushes the tag, and
the Release workflow publishes it. See [CHANGELOG.md](CHANGELOG.md).

## Support

If this saves you time, you can [buy me a coffee](https://buymeacoffee.com/bitsandbricks).

---

Powered by [BitsAndBricks](https://bitsandbricks.ch).
