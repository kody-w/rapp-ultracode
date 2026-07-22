# RAPP UltraCode

Approval-gated, resumable coding workflows built on
[RAPP Dynamic Workflows](https://github.com/kody-w/rapp-dynamic-workflows).

RAPP UltraCode moves coding orchestration into a validated plan and runs each
approved task in an isolated Git worktree. RDW remains the model-session,
budget, schema, journal, and replay engine.

## Why it exists

Claude Code's publicly documented UltraCode mode combines xhigh reasoning with
automatic dynamic workflows for substantial tasks. RAPP UltraCode applies that
structure to the RAPP ecosystem while adding:

- content-addressed plans and digest-bound approval;
- a fixed interpreter rather than generated Python;
- exact custom-tool allowlists instead of unrestricted shell access;
- isolated worktrees that leave the caller checkout untouched;
- operator-declared checks executed with `shell=False`;
- SQLite run/task/event state plus RDW journals;
- resume that reruns an uncheckpointed mutation instead of trusting a cached
  text result.

This project is independent and is not affiliated with Anthropic.

## Install

The first release pins the reviewed RDW commit while RDW is not yet published:

```console
pipx install git+https://github.com/kody-w/rapp-ultracode.git
```

Python 3.11 or newer, Git, the GitHub Copilot CLI, and Copilot authentication
are required for live planning and execution.

## Workflow

```console
# 1. Create a schema-validated xhigh plan. Checks are operator-owned argv.
rapp-ultracode plan \
  "Fix the cache invalidation bug and add regression coverage" \
  --repo . \
  --check 'test=python3 -m pytest -q' \
  --budget 30

# 2. Review plan JSON, then bind approval to its exact digest.
rapp-ultracode approve uc-PLAN_ID \
  --expect-digest FULL_SHA256 \
  --yes

# 3. Execute in a managed worktree. A finite credit budget is mandatory.
rapp-ultracode run uc-PLAN_ID --budget 100 --model gpt-5.6-sol \
  --allow-host-checks

# Or return immediately and let a detached worker continue.
rapp-ultracode run uc-PLAN_ID --budget 100 --model gpt-5.6-sol \
  --allow-host-checks --detach
rapp-ultracode watch run-ID
rapp-ultracode logs run-ID

# 4. Inspect durable state and resume an interrupted run.
rapp-ultracode status run-ID
rapp-ultracode events run-ID
rapp-ultracode resume run-ID --budget 100 --model gpt-5.6-sol \
  --allow-host-checks
```

Export the single-file Brainstem planning cartridge:

```console
rapp-ultracode factory-agent --output ~/.brainstem/src/rapp_brainstem/agents/ultracode_factory_agent.py
```

`UltraCodeFactory` creates inert `PlanDraft` JSON only. It cannot approve or
execute a run.

For offline plan validation, supply a strict `PlanDraft` JSON file:

```console
rapp-ultracode plan "Update VALUE" \
  --repo . \
  --draft draft.json \
  --check 'test=python3 -m pytest -q'
```

## Safety boundary

Live coding agents receive only:

- `uc_list_files`
- `uc_read_file`
- `uc_search_literal`
- `uc_write_file`
- `uc_delete_file`
- `uc_diff`
- RDW's schema submission tool

They cannot call built-in shell, edit, web, MCP, or Git tools. File writes and
deletes are SHA-256 guarded, symlink/traversal paths are rejected, and `.git`
is inaccessible.

Checks are explicitly supplied by the operator and execute repository code on
the host with a reduced environment. Use only trusted repositories. RAPP
UltraCode v0.1 does not claim to be an OS sandbox.

The original checkout is never edited, merged, pushed, reset, or cleaned. A
successful run leaves a local `ultracode/...` branch and retained worktree for
human inspection.

## Plan contract

`rapp-ultracode-plan/1.0` is strict JSON:

- at most 12 ordered tasks;
- at most three attempts per task;
- repository-relative paths only;
- no `.git`, absolute paths, traversal, raw tool definitions, or model-written
  commands;
- checks referenced by stable IDs and supplied by the operator;
- content-addressed plan digest independent of creation time.

## Factory agents versus RDW

RAR's hardened BookFactory pattern established useful disciplines: real gates,
structured failures, per-run workspaces, bounded loops, safe parallel reads,
and traceable partial results. RDW generalizes those disciplines with reusable
runtime, budget, schema, journal, resume, and orchestration primitives.

RAPP UltraCode therefore treats factory agents as planning/client adapters,
not execution engines. It intentionally does not copy SwarmFactory's
model-generated Python, PerpetualLoopFactory's embedded daemons/PID handling,
or thin clients that download and import unpinned source.

## Current scope

v0.1 is deliberately supervised:

- foreground or detached local workers;
- sequential mutation tasks;
- one final xhigh review;
- durable SQLite run/task/events and task-commit checkpoints;
- no supervisor daemon, remote workers, TUI, auto-merge, push, PR creation, package
  installation, arbitrary shell, or unattended mode.

Future versions can add a durable daemon, rolling worktree wavefronts,
pause/cancel controls, event streaming, and a thin Brainstem factory client
without changing the plan contract.

See [docs/architecture.md](docs/architecture.md) for the factory/RDW comparison,
checkpoint model, and Claude UltraCode parity boundary.

## Development

```console
python3.13 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m build
```

Tests use fake engines and local Git repositories. Live inference is never
required by the default suite.

## License

MIT. See [LICENSE](LICENSE) and [DISCLAIMER.md](DISCLAIMER.md).
