# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). `mason bump` turns `[Unreleased]` into the new version.

## [Unreleased]

### Added

- The local agent, moved here from Floorbit (`floorbit/agent/`): call loop, `check` and `health`, syslog and NetFlow relays, gzip, protocol 1. Its own image, `ghcr.io/bitsandbricks-ch/floorbit-agent`, with the Python parts of Floorbit at `FLOORBIT_REF` (no web pages, no React app).
