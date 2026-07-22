from __future__ import annotations

import os
from pathlib import Path

import pytest

from rapp_ultracode.errors import ApprovalRequired, StateConflict
from rapp_ultracode.models import (
    CheckSpec,
    PlanDraft,
    RepositorySpec,
    TaskSpec,
    materialize_plan,
)
from rapp_ultracode.store import Store


def draft() -> PlanDraft:
    return PlanDraft(
        summary="Change the value",
        tasks=[
            TaskSpec(
                id="T1",
                title="Update value",
                objective="Set VALUE to 2.",
                file_hints=["app.py"],
                acceptance=["app.py contains VALUE = 2"],
                check_ids=["test"],
            )
        ],
    )


def test_plan_approval_and_events(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    plan = materialize_plan(
        goal="change",
        repository=RepositorySpec(path=str(Path.cwd().resolve()), base_sha="a" * 40),
        draft=draft(),
        checks=[CheckSpec(id="test", argv=["pytest"])],
    )
    store.save_plan(plan)

    with pytest.raises(ApprovalRequired):
        store.require_approval(plan)

    store.approve(plan.plan_id, plan.digest)
    store.require_approval(plan)
    run_id = store.create_run(plan)
    attempt = store.begin_task(run_id, "T1")
    store.finish_task(
        run_id,
        "T1",
        state="completed",
        commit_sha="b" * 40,
        detail={"attempt": attempt},
    )

    assert store.get_run(run_id)["tasks"][0]["state"] == "completed"
    assert [event["type"] for event in store.events(run_id)] == [
        "run.created",
        "task.started",
        "task.completed",
    ]
    store.close()


def test_run_lease_allows_only_one_live_owner(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    plan = materialize_plan(
        goal="change",
        repository=RepositorySpec(path=str(Path.cwd().resolve()), base_sha="a" * 40),
        draft=draft(),
        checks=[CheckSpec(id="test", argv=["pytest"])],
    )
    store.save_plan(plan)
    run_id = store.create_run(plan)
    store.acquire_lease(run_id, "first", os.getpid())

    with pytest.raises(StateConflict, match="active worker"):
        store.acquire_lease(run_id, "second", os.getpid())

    store.release_lease(run_id, "first")
    store.acquire_lease(run_id, "second", os.getpid())
    store.release_lease(run_id, "second")
    store.close()
