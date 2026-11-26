### Added

- The agent updates itself without a new image (#98). When Floorbit offers a newer agent release, the agent installs its signed update, stops cleanly and starts again on it in the same container, with no Docker access. It takes an update only when it is signed with the agent key in the image, is newer than what runs and was built for this image; a release that needs a new image still shows the pull command. A new version that doesn't reach Floorbit within five minutes, or crashes at start, is rolled back and not tried again. `FLOORBIT_AGENT_UPDATES` picks `auto` (default), `approve` (`python -m floorbit_agent_boot update approve`) or `off`, and only the hall can set it.
- The agent reports the capability `self_update` and an `agent_update` block (policy, running version, image, what waits and why, the last step).

### Changed

- The image starts `python -m floorbit_agent_boot`, which picks the newest good update or the image's own code. Each release also carries the signed update (`floorbit-agent-<version>.tar.gz`, `floorbit-agent-bundle.json`).
