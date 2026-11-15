# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). `mason bump` turns `[Unreleased]` into the new version.

## [Unreleased]

### Added

- The WebSocket link (agent protocol 2): calls arrive over one open `wss://` connection the moment they are made. The agent falls back to the HTTPS long poll when the hosted Floorbit doesn't offer the link or it can't be opened, and stays on protocol 1 with an older Floorbit.
- The local agent, moved here from Floorbit (`floorbit/agent/`): call loop, `check` and `health`, syslog and NetFlow relays, gzip, protocol 1. Its own image, `ghcr.io/bitsandbricks-ch/floorbit-agent`, with the Python parts of Floorbit at `FLOORBIT_REF` (no web pages, no React app).
