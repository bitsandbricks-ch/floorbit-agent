# Changelog fragments

Each pull request adds **one new file** here instead of editing `CHANGELOG.md`, so open pull
requests don't conflict on the changelog. Name it after the topic, e.g. `front-door-port.md`:

```markdown
### Changed

- The front door answers on port 443 only.

### Fixed

- The agent reconnects after the hosted Floorbit restarts.
```

Headings are the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) ones: Added, Changed,
Deprecated, Removed, Fixed, Security. Write for the crews who run the agent.

At release, `python scripts/changelog.py collect` merges every fragment into `CHANGELOG.md`'s
`[Unreleased]` section and deletes the files; then `mason bump` turns `[Unreleased]` into the
version. `python scripts/changelog.py preview` shows the result without changing anything, and
CI runs `python scripts/changelog.py check`.
