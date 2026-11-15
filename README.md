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

## How it relates to Floorbit

The hosted side (the call tunnel, the `/api/agent/v1/` routes, the Local agents page) lives in
Floorbit. This repository is the program that runs at the event. They speak a versioned protocol
(`Floorbit-Agent-Protocol`, now 1), so they can be released apart: a Floorbit that doesn't speak the
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
