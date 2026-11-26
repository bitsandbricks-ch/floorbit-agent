### Added

- One disk budget for everything the agent keeps (the buffer for while Floorbit is away, raw flows, plugins' files), set on the agent's page in Floorbit and applied without a restart (Floorbit issue #1485). Past it the oldest files go first, whichever kind; the agent reports what each kind uses.

### Changed

- Stopping the agent writes the raw flows of the minute still open instead of losing them.

### Removed

- `FLOORBIT_AGENT_BUFFER_MAX_MB`: the buffer counts toward the disk budget instead of a size of its own.
