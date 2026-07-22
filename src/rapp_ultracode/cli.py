from __future__ import annotations

import argparse
import asyncio
import collections
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from rdw import CopilotRuntime, Workflow

from . import __version__
from .assets import export_factory_agent
from .errors import (
    ApprovalRequired,
    ExecutionFailed,
    InternalFailure,
    NotFound,
    StateConflict,
    UltraCodeError,
    UsageError,
)
from .executor import execute_run
from .models import CheckSpec, PlanDraft, materialize_plan
from .paths import state_root
from .planner import create_plan
from .progress import NullProgress
from .repository import GitRepository
from .runtime import RdwEngine, RestrictedRuntime
from .store import Store


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError(message)


def parser() -> Parser:
    root = Parser(
        prog="rapp-ultracode",
        description="Approval-gated long-running coding workflows built on RDW.",
    )
    root.add_argument("--version", action="store_true")
    root.add_argument("--json", action="store_true", help="emit one JSON document")
    root.add_argument("--state-root", help="override the per-user state directory")
    root.add_argument("--verbose", action="store_true")
    commands = root.add_subparsers(dest="command")

    doctor = commands.add_parser("doctor", help="check runtime prerequisites")
    doctor.set_defaults(handler=cmd_doctor)

    plan = commands.add_parser("plan", help="create an immutable coding plan")
    plan.add_argument("goal", nargs="+")
    plan.add_argument("--repo", default=".")
    plan.add_argument("--check", action="append", default=[], metavar="ID=COMMAND")
    plan.add_argument("--draft", help="offline PlanDraft JSON instead of model planning")
    plan.add_argument("--model")
    plan.add_argument("--budget", type=float, default=30)
    plan.set_defaults(handler=cmd_plan)

    approve = commands.add_parser("approve", help="approve one exact plan digest")
    approve.add_argument("plan_id")
    approve.add_argument("--expect-digest", required=True)
    approve.add_argument("--yes", action="store_true")
    approve.set_defaults(handler=cmd_approve)

    run = commands.add_parser("run", help="execute an approved plan")
    run.add_argument("plan_id")
    _run_options(run)
    run.set_defaults(handler=cmd_run)

    resume = commands.add_parser("resume", help="resume a failed or interrupted run")
    resume.add_argument("run_id")
    _run_options(resume)
    resume.set_defaults(handler=cmd_resume)

    status = commands.add_parser("status", help="show a run")
    status.add_argument("run_id")
    status.set_defaults(handler=cmd_status)

    watch = commands.add_parser("watch", help="watch a run until it finishes")
    watch.add_argument("run_id")
    watch.add_argument("--interval", type=float, default=1)
    watch.set_defaults(handler=cmd_watch)

    logs = commands.add_parser("logs", help="show a background worker log")
    logs.add_argument("run_id")
    logs.add_argument("--tail", type=int, default=100)
    logs.set_defaults(handler=cmd_logs)

    runs = commands.add_parser("runs", help="list runs")
    runs.set_defaults(handler=cmd_runs)

    plans = commands.add_parser("plans", help="list plans")
    plans.set_defaults(handler=cmd_plans)

    events = commands.add_parser("events", help="show ordered run events")
    events.add_argument("run_id")
    events.add_argument("--after", type=int, default=0)
    events.set_defaults(handler=cmd_events)

    factory = commands.add_parser(
        "factory-agent",
        help="export the single-file Brainstem PlanDraft factory",
    )
    factory.add_argument("--output", default="ultracode_factory_agent.py")
    factory.add_argument("--force", action="store_true")
    factory.set_defaults(handler=cmd_factory_agent)
    return root


def _run_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--model")
    command.add_argument("--effort", choices=["low", "medium", "high", "xhigh"], default="high")
    command.add_argument("--budget", type=float, required=True)
    command.add_argument("--max-agents", type=int, default=100)
    command.add_argument("--detach", action="store_true", help="run in a background worker")
    command.add_argument(
        "--allow-host-checks",
        action="store_true",
        help="explicitly allow approved checks to execute repository code on the host",
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    command_parser = parser()
    if not arguments:
        command_parser.print_help()
        return 0
    if "--version" in arguments:
        unexpected = [argument for argument in arguments if argument not in {"--version", "--json"}]
        if unexpected:
            error = UsageError("--version may only be combined with --json")
            _emit_error(error, "--json" in arguments)
            return error.exit_code
        if "--json" in arguments:
            _emit({"ok": True, "command": "version", "data": {"version": __version__}}, True)
        else:
            print(f"rapp-ultracode {__version__}")
        return 0
    try:
        args = command_parser.parse_args(arguments)
        if not hasattr(args, "handler"):
            raise UsageError("a command is required")
        return int(args.handler(args))
    except UltraCodeError as exc:
        _emit_error(exc, "--json" in arguments)
        return exc.exit_code
    except ValidationError as exc:
        error = UsageError(f"invalid plan data: {exc}")
        _emit_error(error, "--json" in arguments)
        return error.exit_code
    except KeyboardInterrupt:
        error = UltraCodeError("interrupted", "INTERRUPTED", 130)
        _emit_error(error, "--json" in arguments)
        return 130
    except BrokenPipeError:
        return 141
    except Exception:
        if "--verbose" in arguments:
            raise
        error = InternalFailure()
        _emit_error(error, "--json" in arguments)
        return error.exit_code


def cmd_doctor(args: argparse.Namespace) -> int:
    import importlib.util
    import shutil

    checks = {
        "git": shutil.which("git") is not None,
        "rdw": importlib.util.find_spec("rdw") is not None,
        "copilot_sdk": importlib.util.find_spec("copilot") is not None,
        "copilot_cli": shutil.which("copilot") is not None,
    }
    ok = all(checks.values())
    _emit({"ok": ok, "command": "doctor", "data": {"checks": checks}}, args.json)
    return 0 if ok else 6


def cmd_plan(args: argparse.Namespace) -> int:
    goal = " ".join(args.goal).strip()
    if not goal:
        raise UsageError("goal is required")
    checks = [_parse_check(value) for value in args.check]
    repository = GitRepository.open(args.repo)
    store = _store(args)
    try:
        if args.draft:
            draft = _read_draft(Path(args.draft))
            snapshot = repository.snapshot()
            plan = materialize_plan(
                goal=goal,
                repository=snapshot.spec,
                draft=draft,
                checks=checks,
            )
        else:
            _positive_budget(args.budget)
            plan_run_id = f"plan-{uuid.uuid4().hex[:16]}"
            plan = asyncio.run(
                _plan_live(
                    repository=repository,
                    checks=checks,
                    goal=goal,
                    model=args.model,
                    budget=args.budget,
                    run_id=plan_run_id,
                    root=_root(args),
                    json_mode=args.json,
                )
            )
        store.save_plan(plan)
        _emit(
            {
                "ok": True,
                "command": "plan",
                "data": {
                    **plan.model_dump(mode="json", by_alias=True),
                    "approval_command": (
                        f"rapp-ultracode approve {plan.plan_id} --expect-digest {plan.digest} --yes"
                    ),
                },
            },
            args.json,
        )
        return 0
    finally:
        store.close()


async def _plan_live(
    *,
    repository: GitRepository,
    checks: list[CheckSpec],
    goal: str,
    model: str | None,
    budget: float,
    run_id: str,
    root: Path,
    json_mode: bool,
):
    runtime = RestrictedRuntime(
        CopilotRuntime(
            working_directory=str(repository.root),
            concurrency=1,
        )
    )
    workflow = Workflow.open(
        run_id=run_id,
        root=root / "rdw",
        budget=budget,
        runtime=runtime,
        progress=NullProgress() if json_mode else None,
        model=model,
        effort="xhigh",
        cwd=str(repository.root),
        concurrency=1,
        max_agents=4,
        transcripts=True,
    )
    async with workflow:
        return await create_plan(
            engine=RdwEngine(workflow),
            goal=goal,
            repository=repository,
            checks=checks,
            model=model,
        )


def cmd_approve(args: argparse.Namespace) -> int:
    if not args.yes:
        raise ApprovalRequired("approval requires --yes")
    store = _store(args)
    try:
        store.approve(args.plan_id, args.expect_digest)
        _emit(
            {
                "ok": True,
                "command": "approve",
                "data": {"plan_id": args.plan_id, "digest": args.expect_digest},
            },
            args.json,
        )
        return 0
    finally:
        store.close()


def cmd_run(args: argparse.Namespace) -> int:
    _positive_budget(args.budget)
    _valid_max_agents(args.max_agents)
    store = _store(args)
    try:
        plan = store.get_plan(args.plan_id)
        store.require_approval(plan)
        run_id = store.create_run(plan)
    finally:
        store.close()
    if args.detach:
        return _spawn_worker(args, run_id, resume=False)
    return _execute(args, run_id, resume=False)


def cmd_resume(args: argparse.Namespace) -> int:
    _positive_budget(args.budget)
    _valid_max_agents(args.max_agents)
    if args.detach:
        return _spawn_worker(args, args.run_id, resume=True)
    return _execute(args, args.run_id, resume=True)


def _spawn_worker(args: argparse.Namespace, run_id: str, *, resume: bool) -> int:
    root = _root(args)
    store = _store(args)
    lease_token = uuid.uuid4().hex
    try:
        run = store.get_run(run_id)
        if run["state"] == "succeeded":
            raise StateConflict(f"run already succeeded: {run_id}")
        store.acquire_lease(run_id, lease_token, os.getpid())
    finally:
        store.close()
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "worker.log"
    command = [
        sys.executable,
        "-m",
        "rapp_ultracode.worker",
        "--state-root",
        str(root),
        "--run-id",
        run_id,
        "--effort",
        args.effort,
        "--budget",
        str(args.budget),
        "--max-agents",
        str(args.max_agents),
        "--lease-token",
        lease_token,
    ]
    if args.model:
        command.extend(["--model", args.model])
    if resume:
        command.append("--resume")
    if args.allow_host_checks:
        command.append("--allow-host-checks")
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        try:
            process = subprocess.Popen(
                command,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=fd,
                stderr=fd,
                start_new_session=os.name != "nt",
                close_fds=True,
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
                ),
            )
        except BaseException:
            store = _store(args)
            try:
                store.release_lease(run_id, lease_token)
            finally:
                store.close()
            raise
    finally:
        os.close(fd)
    store = _store(args)
    try:
        store.set_worker(run_id, process.pid)
    finally:
        store.close()
    _emit(
        {
            "ok": True,
            "command": "resume" if resume else "run",
            "data": {
                "run_id": run_id,
                "state": "queued",
                "worker_pid": process.pid,
                "log": str(log_path),
            },
        },
        args.json,
    )
    return 0


def _execute(args: argparse.Namespace, run_id: str, *, resume: bool) -> int:
    root = _root(args)
    store = _store(args)
    lease_token = getattr(args, "lease_token", None) or uuid.uuid4().hex
    preacquired = getattr(args, "lease_token", None) is not None
    lease_acquired = False
    try:
        if preacquired:
            store.transfer_lease(run_id, lease_token, os.getpid())
        else:
            store.acquire_lease(run_id, lease_token, os.getpid())
        lease_acquired = True
        run = store.get_run(run_id)
        if run["state"] == "succeeded":
            raise StateConflict(f"run already succeeded: {run_id}")
        plan = store.get_plan(run["plan_id"])
        if plan.checks and not args.allow_host_checks:
            raise ApprovalRequired("plan has host checks; execution requires --allow-host-checks")
        repository = GitRepository.open(plan.repository.path)
        worktree_path = root / "worktrees" / run_id
        result = asyncio.run(
            _execute_live(
                store=store,
                plan=plan,
                run_id=run_id,
                repository=repository,
                worktree_path=worktree_path,
                model=args.model,
                effort=args.effort,
                budget=args.budget,
                max_agents=args.max_agents,
                resume=resume,
                root=root,
                json_mode=args.json,
            )
        )
        _emit({"ok": True, "command": "resume" if resume else "run", "data": result}, args.json)
        return 0
    except BaseException as exc:
        if lease_acquired and store.get_run(run_id)["state"] != "failed":
            store.set_run_state(run_id, "failed", failure=str(exc))
        raise
    finally:
        if lease_acquired:
            store.release_lease(run_id, lease_token)
        store.close()


async def _execute_live(
    *,
    store: Store,
    plan,
    run_id: str,
    repository: GitRepository,
    worktree_path: Path,
    model: str | None,
    effort: str,
    budget: float,
    max_agents: int,
    resume: bool,
    root: Path,
    json_mode: bool,
):
    effective_budget = budget
    if resume:
        spent = _rdw_spent(root, run_id)
        effective_budget = budget - spent
        if effective_budget < 30:
            raise ExecutionFailed(
                f"lifetime budget has {effective_budget:.2f} AIU remaining; "
                "at least 30 AIU is required to resume safely"
            )
    runtime = RestrictedRuntime(
        CopilotRuntime(
            working_directory=str(worktree_path),
            concurrency=1,
        )
    )
    workflow = Workflow.open(
        run_id=run_id,
        root=root / "rdw",
        resume=resume,
        budget=effective_budget,
        runtime=runtime,
        progress=NullProgress() if json_mode else None,
        model=model,
        effort=effort,
        cwd=str(worktree_path),
        concurrency=1,
        max_agents=max_agents,
        transcripts=True,
    )
    async with workflow:
        return await execute_run(
            engine=RdwEngine(workflow),
            store=store,
            plan=plan,
            run_id=run_id,
            repository=repository,
            worktree_path=worktree_path,
            model=model,
            effort=effort,
            resume=resume,
        )


def cmd_status(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        _emit({"ok": True, "command": "status", "data": store.get_run(args.run_id)}, args.json)
        return 0
    finally:
        store.close()


def cmd_watch(args: argparse.Namespace) -> int:
    if not math.isfinite(args.interval) or args.interval <= 0 or args.interval > 60:
        raise UsageError("--interval must be greater than 0 and no more than 60")
    terminal = {"succeeded", "failed", "canceled"}
    store = _store(args)
    try:
        last = None
        while True:
            run = store.get_run(args.run_id)
            state = (run["state"], run.get("current_task"))
            if state != last and not args.json:
                print(
                    f"{args.run_id}: {run['state']}"
                    + (f" task={run['current_task']}" if run.get("current_task") else "")
                )
                last = state
            if run["state"] in terminal:
                if args.json:
                    _emit(
                        {
                            "ok": run["state"] == "succeeded",
                            "command": "watch",
                            "data": run,
                        },
                        True,
                    )
                return 0 if run["state"] == "succeeded" else 7
            time.sleep(args.interval)
    finally:
        store.close()


def cmd_logs(args: argparse.Namespace) -> int:
    if args.tail < 1 or args.tail > 10000:
        raise UsageError("--tail must be from 1 to 10000")
    run_id = _validated_run_id(args.run_id)
    path = _root(args) / "runs" / run_id / "worker.log"
    if not path.is_file():
        raise NotFound(f"worker log not found: {args.run_id}")
    with path.open(encoding="utf-8", errors="replace") as handle:
        lines = list(collections.deque(handle, maxlen=args.tail))
    lines = [line.rstrip("\r\n") for line in lines]
    data = {"run_id": run_id, "lines": lines[-args.tail :]}
    _emit({"ok": True, "command": "logs", "data": data}, args.json)
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        _emit({"ok": True, "command": "runs", "data": {"runs": store.list_runs()}}, args.json)
        return 0
    finally:
        store.close()


def cmd_plans(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        plans = [plan.model_dump(mode="json", by_alias=True) for plan in store.list_plans()]
        _emit({"ok": True, "command": "plans", "data": {"plans": plans}}, args.json)
        return 0
    finally:
        store.close()


def cmd_events(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        events = store.events(args.run_id, args.after)
        _emit({"ok": True, "command": "events", "data": {"events": events}}, args.json)
        return 0
    finally:
        store.close()


def cmd_factory_agent(args: argparse.Namespace) -> int:
    data = export_factory_agent(Path(args.output), replace=args.force)
    _emit({"ok": True, "command": "factory-agent", "data": data}, args.json)
    return 0


def _parse_check(value: str) -> CheckSpec:
    name, separator, command = value.partition("=")
    if not separator or not name or not command:
        raise UsageError("--check must use ID=COMMAND syntax")
    try:
        argv = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        raise UsageError(f"invalid --check command quoting: {exc}") from exc
    return CheckSpec(id=name, argv=argv)


def _read_draft(path: Path) -> PlanDraft:
    try:
        with path.expanduser().open("rb") as handle:
            data = handle.read(128 * 1024 + 1)
    except OSError as exc:
        raise UsageError(f"cannot read draft {path}: {exc}") from exc
    if len(data) > 128 * 1024:
        raise UsageError("draft exceeds 128 KiB")
    return PlanDraft.model_validate_json(data)


def _positive_budget(value: float) -> None:
    if not math.isfinite(value) or value < 30:
        raise UsageError("budget must be finite and at least 30 AIU")


def _valid_max_agents(value: int) -> None:
    if value < 1 or value > 1000:
        raise UsageError("--max-agents must be from 1 to 1000")


def _validated_run_id(value: str) -> str:
    if not re.fullmatch(r"run-[0-9a-f]{16}", value):
        raise UsageError("invalid run id")
    return value


def _rdw_spent(root: Path, run_id: str) -> float:
    journal = root / "rdw" / "runs" / run_id / "journal.jsonl"
    if not journal.is_file():
        return 0.0
    total = 0.0
    try:
        with journal.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "agent":
                    credits = record.get("credits")
                    if isinstance(credits, (int, float)) and math.isfinite(credits):
                        total += float(credits)
    except OSError as exc:
        raise ExecutionFailed(f"cannot read prior RDW spend: {exc}") from exc
    return total


def _root(args: argparse.Namespace) -> Path:
    return Path(args.state_root).expanduser().resolve() if args.state_root else state_root()


def _store(args: argparse.Namespace) -> Store:
    return Store(_root(args) / "state.sqlite3")


def _emit(payload: dict[str, Any], json_mode: bool) -> None:
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    data = payload.get("data")
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def _emit_error(error: UltraCodeError, json_mode: bool) -> None:
    payload = {
        "ok": False,
        "error": {
            "code": error.code,
            "message": error.message,
            "details": error.details,
        },
    }
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(f"error: {error.message}", file=sys.stderr)
