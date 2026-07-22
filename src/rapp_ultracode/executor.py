from __future__ import annotations

from pathlib import Path

from .errors import ExecutionFailed, StateConflict
from .models import Plan
from .repository import GitRepository, Worktree, run_check
from .runtime import RdwEngine
from .store import Store
from .tools import Workspace, build_tools

_MAX_CHANGED_FILES = 256
_MAX_CHANGED_BYTES = 50 * 1024 * 1024


async def execute_run(
    *,
    engine: RdwEngine,
    store: Store,
    plan: Plan,
    run_id: str,
    repository: GitRepository,
    worktree_path: Path,
    model: str | None,
    effort: str,
    resume: bool = False,
) -> dict[str, object]:
    store.require_approval(plan)
    if not resume:
        repository.ensure_base(plan.repository.base_sha)
    branch = f"ultracode/{plan.plan_id}-{run_id[-6:]}"
    path = repository.create_worktree(
        worktree_path,
        branch,
        plan.repository.base_sha,
    )
    worktree = Worktree(path)
    if not worktree.is_clean():
        if resume:
            worktree.discard_uncommitted()
        else:
            raise StateConflict("new run worktree is unexpectedly dirty")
    workspace = Workspace(path)
    store.set_run_state(
        run_id,
        "running",
        worktree=str(path),
        branch=branch,
    )
    if worktree.is_clean() and worktree.head() != plan.repository.base_sha:
        subject = worktree.subject()
        for task_state in store.get_run(run_id)["tasks"]:
            if (
                task_state["state"] in {"running", "failed"}
                and task_state["attempt"] > 0
                and subject.startswith(f"ultracode({task_state['task_id']}):")
            ):
                store.finish_task(
                    run_id,
                    task_state["task_id"],
                    state="completed",
                    commit_sha=worktree.head(),
                    detail={"recovered_from_commit": True},
                )
    run_state = store.get_run(run_id)
    completed = {
        task["task_id"]
        for task in run_state["tasks"]
        if task["state"] == "completed" and task["commit_sha"]
    }
    for task_state in run_state["tasks"]:
        if (
            task_state["state"] == "completed"
            and task_state["commit_sha"]
            and not worktree.is_ancestor(task_state["commit_sha"])
        ):
            raise StateConflict(
                f"checkpoint {task_state['task_id']} is not present in worktree HEAD"
            )
    check_map = {check.id: check for check in plan.checks}

    try:
        for task in plan.tasks:
            if task.id in completed:
                continue
            if not worktree.is_clean():
                raise StateConflict(
                    f"worktree is dirty before task {task.id}; manual recovery required"
                )
            store.set_run_state(run_id, "running", current_task=task.id)
            attempt = store.begin_task(run_id, task.id)
            if attempt > task.max_attempts:
                raise ExecutionFailed(f"task {task.id} exhausted its attempt limit")
            result = await engine.execute_task(
                task=task,
                attempt=attempt,
                tools=build_tools(workspace),
                model=model,
                effort=effort,
            )
            if result.status != "completed":
                store.finish_task(
                    run_id,
                    task.id,
                    state="blocked",
                    commit_sha=None,
                    detail=result.model_dump(mode="json"),
                )
                raise ExecutionFailed(f"task {task.id} blocked: {'; '.join(result.blockers)}")
            repository.create_worktree(path, branch, plan.repository.base_sha)
            changed = worktree.changed_files()
            if not changed:
                raise ExecutionFailed(f"task {task.id} reported completion but changed no files")
            if worktree.ignored_files():
                raise ExecutionFailed("task created or renamed files into ignored paths")
            _enforce_change_limits(path, changed)
            change_fingerprint = worktree.change_fingerprint(changed)
            checks = []
            for check_id in task.check_ids:
                check = check_map[check_id]
                check_result = run_check(
                    path,
                    check.argv,
                    cwd=check.cwd,
                    timeout_seconds=check.timeout_seconds,
                )
                checks.append(check_result)
                repository.create_worktree(path, branch, plan.repository.base_sha)
                if (
                    worktree.changed_files() != changed
                    or worktree.change_fingerprint(changed) != change_fingerprint
                ):
                    raise ExecutionFailed(
                        f"check {check_id} modified tracked or untracked source files"
                    )
                if check_result["returncode"] != 0:
                    store.finish_task(
                        run_id,
                        task.id,
                        state="failed",
                        commit_sha=None,
                        detail={"result": result.model_dump(mode="json"), "checks": checks},
                    )
                    raise ExecutionFailed(f"check {check_id} failed for task {task.id}")
            commit_sha = worktree.commit(task.id, task.title)
            store.finish_task(
                run_id,
                task.id,
                state="completed",
                commit_sha=commit_sha,
                detail={
                    "result": result.model_dump(mode="json"),
                    "changed_files": changed,
                    "checks": checks,
                },
            )

        for check in plan.checks:
            result = run_check(
                path,
                check.argv,
                cwd=check.cwd,
                timeout_seconds=check.timeout_seconds,
            )
            repository.create_worktree(path, branch, plan.repository.base_sha)
            if result["returncode"] != 0:
                raise ExecutionFailed(f"final check {check.id} failed")
            if not worktree.is_clean():
                raise ExecutionFailed(f"final check {check.id} modified source files")

        final_diff = worktree.diff_from(plan.repository.base_sha)
        if len(final_diff.encode("utf-8")) > 1024 * 1024:
            raise ExecutionFailed("final diff exceeds the 1 MiB review limit")
        review = await engine.review(
            plan=plan,
            head_sha=worktree.head(),
            diff=final_diff,
            tools=build_tools(workspace, writable=False),
            model=model,
        )
        if review.verdict != "pass":
            raise ExecutionFailed(f"final review {review.verdict}: {'; '.join(review.findings)}")
        final_result = {
            "run_id": run_id,
            "state": "succeeded",
            "branch": branch,
            "worktree": str(path),
            "head": worktree.head(),
            "review": review.model_dump(mode="json"),
        }
        store.complete_run(run_id, final_result)
        return final_result
    except Exception as exc:
        store.set_run_state(
            run_id,
            "failed",
            failure=str(exc),
        )
        raise


def _enforce_change_limits(worktree: Path, changed: list[str]) -> None:
    if len(changed) > _MAX_CHANGED_FILES:
        raise ExecutionFailed(f"change touches {len(changed)} files; limit is {_MAX_CHANGED_FILES}")
    total = 0
    for name in changed:
        path = worktree / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ExecutionFailed(f"change contains unsupported file type: {name}")
        total += path.stat().st_size
        if total > _MAX_CHANGED_BYTES:
            raise ExecutionFailed("change exceeds the 50 MiB file-size limit")
