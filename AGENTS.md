# Rules for coding agents

These rules are for AI coding agents (Claude Code, Codex, Copilot, ...) working on Floorbit Agent.
Humans: see [CONTRIBUTING.md](CONTRIBUTING.md); the same rules apply to you in spirit.
`CLAUDE.md` imports this file.

This repo follows the [BitsAndBricks handbook](https://github.com/bitsandbricks-ch/handbook),
profile **agent** (see `.mason.toml`). Read its core chapters and the blocks that profile lists,
above all [local agents](https://github.com/bitsandbricks-ch/handbook/blob/main/blocks/local-agents.md).
Where this file differs from the handbook, this file wins for this repo.

**Where this agent differs from the handbook's agent contract, on purpose** (Mike, 2026-11-24):

- **SSH to switches.** The agent is the only way Floorbit reaches the event network, so port
  actions, switch commands, config backups and the switch terminal open their SSH connection here,
  not from fixed signed jobs only.
- **Features on by default.** A crew starts a fresh agent with nothing to set up; the front door,
  seat walk-through and check-in note work without switching each one on.
- **Its own protocol.** `Floorbit-Agent-Protocol` 2: WebSocket with an HTTPS long-poll fallback,
  not the handbook's agent protocol 1. The handbook's conformance tests don't apply.

Outbound only, enrolment and revoke, signed releases and audit still hold. Don't propose to change
these; a change is Mike's call.

## What this is

The local agent for a hosted Floorbit: it runs on the event network and answers for it over one outgoing HTTPS connection. Overview for users: [README.md](README.md).

| Path | What lives there |
| --- | --- |
| `floorbit_agent/` | The program: the call loop and `check`/`health` (`__init__.py`), the syslog relay, the flow relay. |
| `tests/` | pytest against a fake hosted Floorbit; no network. |
| `FLOORBIT_REF` | The Floorbit commit the agent is built on and tested with. |
| `compose.yml`, `agent.env.example` | How it's run at an event. |

The hosted side lives in bitsandbricks-ch/floorbit (`floorbit/agent_link.py`,
`floorbit/blueprints/agent.py`). A protocol change needs a PR there too, and a new protocol number
when the other side can't follow.

## Workflow

1. **Branch:** work on your own branch, never push to `main`. One topic per branch and pull request.
2. **Before you push**, run what CI runs and make it pass locally:
   `pip install -r ../floorbit/requirements.txt -e ".[dev]"`, `ruff check .`, `pytest -q`, `docker build .`
3. **Pull request:** open it as a **draft** and fill in the template
   (`.github/pull_request_template.md`). Link the issue.
4. **CI:** wait for the checks; `CI passed` is the one the branch rules require. Fix failures
   yourself. Never skip, disable or weaken a check or a test to get green.
5. **Done** means the task is complete, the checklist is ticked and the checks are green: then mark
   the pull request ready for review. Leave it a draft only while work is open or you are blocked,
   and say why in the description.
6. **Never merge** your own pull request, never force-push to a branch someone else works on, and
   never push a tag. Merging and releasing are the maintainer's call.
7. Keep the pull request description current when you push more commits.

## Rules

- **Ask, don't guess.** Never make up facts about the project, its users or its author (features,
  history, numbers, names). Leave a `TODO` and ask instead.
- **No secrets** in code, commits, screenshots, logs or docs. Use example values (`example.com`).
- **Names:** the brand is written "BitsAndBricks". Public text names the author as "Mike" only.
- **Releases:** don't edit `VERSION` or the changelog headings by hand; `mason bump` does that.
  Describe your change in a new `changelog.d/<topic>.md` fragment, not in `CHANGELOG.md`
  (see `changelog.d/README.md`).
- **Workflows:** pin every action to a full commit sha with the version as a comment
  (`uses: actions/checkout@<sha> # v7.0.1`), keep `permissions` minimal, and never put
  `${{ github.event.* }}` text straight into a `run:` script (pass it through `env:`).
- **Dependencies:** add one only when it clearly pays for itself, and say why in the pull request.
- **Tests never talk to a real Floorbit, LibreNMS or NetBox:** use the fakes in `tests/`.
- **Privacy:** only aggregates leave the event network for NetFlow, DNS and Wi-Fi; keep it that way.
- Write short comments that explain why, not what.
- Stay inside the repository. Ask before deleting files you didn't create.
