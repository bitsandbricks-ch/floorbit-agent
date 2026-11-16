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
plugins. If you set it up to, it also forwards syslog and a NetFlow summary. Nothing on the event
network has to be reachable from outside.

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
# fill in FLOORBIT_URL, FLOORBIT_AGENT_KEY and LibreNMS (and NetBox) in agent.env
docker compose run --rm agent python -m floorbit_agent check
docker compose up -d
```

## Use

```sh
python -m floorbit_agent           # run: poll the hosted Floorbit and answer its calls
python -m floorbit_agent check     # try each part once and say what works
python -m floorbit_agent health    # 0 when the hosted Floorbit answered in the last two minutes
```

Every setting, what works through the agent and the protocol are in Floorbit's
[docs/agent.md](https://github.com/bitsandbricks-ch/floorbit/blob/master/docs/agent.md).

## When the uplink is down

Syslog the agent can't send goes to a disk buffer in its state volume (`/app/state/buffer`), so
nothing is lost while the hosted Floorbit is away or the agent restarts. Once it is back, the agent
sends the newest lines first and catches up on the buffer behind them, oldest first, at up to
5,000 lines a second. The buffer keeps at most 2 GB and 24 hours (`FLOORBIT_AGENT_BUFFER_MAX_MB`,
`FLOORBIT_AGENT_BUFFER_MAX_HOURS`; `0` MB turns it off); past that the oldest lines go first, counted
in `floorbit_agent_syslog_dropped_total`. Every line carries an ID made of its sender and text, the
same on every agent, so the hosted side can drop copies.

A busy event's switches send syslog and NetFlow in bursts. The agent asks the kernel for an 8 MB
receive buffer on each port, but Linux caps it at `net.core.rmem_max` (often 208 KB): on the agent
machine, `sysctl -w net.core.rmem_max=8388608` (and in `/etc/sysctl.d/` to keep it) gives it the room.

## Monitor it

The agent answers on port 9520 (`FLOORBIT_AGENT_METRICS`; `compose.yml` publishes it on the
machine's 127.0.0.1 only):

- `GET /healthz` (never needs the token, for local checks): 200 while the hosted Floorbit answered in the last two minutes, 503 otherwise,
  with the reason and whether the WebSocket link is up.
- `GET /metrics`: Prometheus text. Covers the link and connection, calls by method and outcome and how long
  they took, round trips to the hosted Floorbit, reconnects and link drops, syslog received, sent,
  dropped and waiting, and NetFlow packets and summaries sent.

It's all counts and timings: no IPs, names, log lines or keys. To scrape it from another machine,
publish the port on the LAN and set `FLOORBIT_AGENT_METRICS_TOKEN` (it guards `/metrics` only); Prometheus then sends it as a
bearer token (`authorization: {credentials: ...}` in the scrape config).

Without Prometheus, the hosted Floorbit's Local agents page shows a summary of the same numbers:
the agent sends it every half minute.

[`grafana/floorbit-agent.json`](grafana/floorbit-agent.json) is a Grafana dashboard for these
metrics (Dashboards > New > Import), with one row per agent (`instance`).

## Several agents, shared IPs

Agents can share virtual IPs with VRRP, so the event network keeps reaching one when another goes
down: keepalived in the image (`compose.vrrp.yml`), on the machine, or kube-vip/MetalLB on k3s.
See [docs/vrrp.md](docs/vrrp.md).

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

Pull requests are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Coding agents follow
[AGENTS.md](AGENTS.md). Security problems go through a private report, see [SECURITY.md](SECURITY.md).

## Releases

`mason bump floorbit-agent minor` opens the version PR, `mason release tag floorbit-agent` pushes the tag, and
the Release workflow publishes it. See [CHANGELOG.md](CHANGELOG.md).

## Support

If this saves you time, you can [buy me a coffee](https://buymeacoffee.com/bitsandbricks).

---

Powered by [BitsAndBricks](https://bitsandbricks.ch).
