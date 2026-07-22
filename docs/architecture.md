# Architecture

## Layering

```text
Brainstem UltraCodeFactory (optional, plan drafts only)
                         |
CLI / Python API -> Plan + approval + SQLite events
                         |
Fixed UltraCode interpreter
                         |
Restricted RDW runtime -> RDW budget/journal/schema/session engine
                         |
Isolated Git worktree + SHA-guarded file tools + operator checks
```

RDW remains the harness-neutral inference engine. UltraCode does not replace
`agent`, `parallel`, `pipeline`, `phase`, budgets, transcripts, or journal
replay.

UltraCode owns coding-specific facts that an inference journal cannot prove:

- immutable repository base;
- exact plan and approval digest;
- worktree and branch identity;
- task attempts and checkpoint commits;
- host check results;
- final review and run state.

## Run flow

1. Snapshot a clean Git repository and hash instruction files.
2. Produce a strict `PlanDraft` through an xhigh schema-only RDW session, or
   import one from the Brainstem factory/offline JSON.
3. Materialize `rapp-ultracode-plan/1.0`; compute its content digest and ID.
4. Require approval bound to that exact digest.
5. Create a managed worktree from the immutable base commit.
6. Execute mutation tasks sequentially. Each fresh RDW session receives only
   `uc_*` file tools and schema submission.
7. Run human-declared checks with direct argv and `shell=False`.
8. Commit each green task. The commit is the filesystem-effect checkpoint.
9. Run an xhigh read-only final reviewer. A blocking finding fails closed;
   repairing it requires a newly reviewed plan rather than unapproved scope.
10. Leave the branch and worktree for human inspection. Never merge or push.

## Resume

RDW safely replays planning and review results. Coding calls are effectful, so
their prompt fingerprint includes the durable task attempt number. An
uncheckpointed attempt reruns rather than receiving cached success.

Task commits are authoritative checkpoints. On recovery, a clean worktree
whose head is an UltraCode task commit repairs missing SQLite task state.
Dirty interrupted worktrees fail closed for manual inspection.

## Long-running operation

`run --detach` starts a dedicated worker process and records its PID and event.
The worker owns one RDW runtime and journal. `status`, `watch`, `logs`, and
`events` remain responsive from other terminals. A failed worker can be
resumed against the same worktree and RDW run ID.

v0.1 intentionally has no automatic PID-based cancellation or daemon adoption:
safe cross-platform process ownership requires stronger start-token and lease
verification. Those controls are planned for the supervisor release.

## Factory-agent comparison

The RAR factory family provides several lessons:

| Pattern | Keep | Do not copy |
|---|---|---|
| BookFactory 0.4 | Structured gates/errors, bounded retries, per-run artifacts, safe parallel reads | Provider dispatch and orchestration duplicated inside every agent |
| MomentFactory | Early deterministic gate | Fail-open parsing and error text flowing downstream |
| SwarmFactory | Single-file distribution and one public entrypoint | Model-authored Python, AST-only checks, immediate hot-load |
| BakeoffFactory | Persistent lineage and measurable selection | Unsupervised pump/PID lifecycle inside an agent |
| Estate/Leviathan | Intent to declarative design to deterministic materialization | Interpolated executable source and stub provisioning |
| PerpetualLoopFactory | Observable long-running coordination | Embedded daemons, copied tokens, port probing, PID-only killing |
| M365 thin factory | Small client surface | Downloading and importing mutable unpinned Python |

The resulting rule is:

> Factory agents produce inert plans; UltraCode interprets approved plans; RDW
> executes model sessions.

## Claude UltraCode comparison

Claude Code's public dynamic-workflow model moves orchestration into a script,
uses fresh parallel agents, tracks phases, and resumes completed agents.
RAPP UltraCode follows those principles while using:

- validated declarative JSON instead of generated JavaScript/Python;
- approval bound to plan and repository digests;
- persistent Git worktree checkpoints;
- RDW cross-process journals and AI-credit accounting;
- a harness-neutral runtime boundary;
- headless CLI/API and optional Brainstem planning cartridge.

Rolling parallel mutation waves, a fenced supervisor daemon, pause/cancel,
remote workers, and a TUI belong to later releases after their safety and
recovery contracts are proven.
