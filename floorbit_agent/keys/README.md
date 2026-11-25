# Agent signing keys

The public keys (`*.pub`, Ed25519, PEM) that sign agent updates (issue #98). An agent installs an
update only when it is signed with one of these; with no key here it never installs one.
`floorbit_agent_boot.py` reads them from the image, so an update can't change which keys count.

The private key lives only in the release workflow's `agent-signing` environment
(`FLOORBIT_AGENT_SIGNING_KEY`), never on a hosted Floorbit or in a repository.
