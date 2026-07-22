from __future__ import annotations

import sys
from pathlib import Path

import pytest

from rapp_ultracode.errors import ExecutionFailed
from rapp_ultracode.executor import execute_run
from rapp_ultracode.models import (
    CheckSpec,
    PlanDraft,
    RepositorySpec,
    ReviewResult,
    TaskSpec,
    WorkResult,
    materialize_plan,
)
from rapp_ultracode.repository import GitRepository
from rapp_ultracode.store import Store


class FakeEngine:
    def __init__(self, worktree: Path):
        self.worktree = worktree

    async def execute_task(self, **_kwargs):
        (self.worktree / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        return WorkResult(
            status="completed",
            summary="updated",
            files_changed=["app.py"],
            evidence=["VALUE is 2"],
        )

    async def review(self, **_kwargs):
        return ReviewResult(verdict="pass", summary="looks good")


class FailingEngine(FakeEngine):
    async def execute_task(self, **_kwargs):
        (self.worktree / "app.py").write_text("VALUE = 99\n", encoding="utf-8")
        raise ExecutionFailed("simulated interruption")


async def test_executor_commits_in_isolated_worktree(git_repo, tmp_path):
    repository = GitRepository.open(git_repo)
    snapshot = repository.snapshot()
    plan = materialize_plan(
        goal="change value",
        repository=RepositorySpec(
            path=str(git_repo),
            base_sha=snapshot.spec.base_sha,
        ),
        draft=PlanDraft(
            summary="update",
            tasks=[
                TaskSpec(
                    id="T1",
                    title="Update",
                    objective="Set VALUE to 2",
                    file_hints=["app.py"],
                    acceptance=["VALUE is 2"],
                    check_ids=["check"],
                )
            ],
        ),
        checks=[
            CheckSpec(
                id="check",
                argv=[sys.executable, "-c", "from app import VALUE; assert VALUE == 2"],
            )
        ],
    )
    store = Store(tmp_path / "state.sqlite3")
    store.save_plan(plan)
    store.approve(plan.plan_id, plan.digest)
    run_id = store.create_run(plan)
    worktree = tmp_path / "worktree"

    result = await execute_run(
        engine=FakeEngine(worktree),
        store=store,
        plan=plan,
        run_id=run_id,
        repository=repository,
        worktree_path=worktree,
        model=None,
        effort="high",
    )

    assert result["state"] == "succeeded"
    assert (git_repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    stored = store.get_run(run_id)
    assert stored["tasks"][0]["state"] == "completed"
    assert stored["result"]["head"] == result["head"]
    store.close()


async def test_resume_discards_uncheckpointed_managed_changes(git_repo, tmp_path):
    repository = GitRepository.open(git_repo)
    snapshot = repository.snapshot()
    plan = materialize_plan(
        goal="change value",
        repository=snapshot.spec,
        draft=PlanDraft(
            summary="update",
            tasks=[
                TaskSpec(
                    id="T1",
                    title="Update",
                    objective="Set VALUE to 2",
                    acceptance=["VALUE is 2"],
                )
            ],
        ),
        checks=[],
    )
    store = Store(tmp_path / "state.sqlite3")
    store.save_plan(plan)
    store.approve(plan.plan_id, plan.digest)
    run_id = store.create_run(plan)
    worktree = tmp_path / "worktree"

    with pytest.raises(ExecutionFailed):
        await execute_run(
            engine=FailingEngine(worktree),
            store=store,
            plan=plan,
            run_id=run_id,
            repository=repository,
            worktree_path=worktree,
            model=None,
            effort="high",
        )

    result = await execute_run(
        engine=FakeEngine(worktree),
        store=store,
        plan=plan,
        run_id=run_id,
        repository=repository,
        worktree_path=worktree,
        model=None,
        effort="high",
        resume=True,
    )

    assert result["state"] == "succeeded"
    assert (worktree / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    store.close()
