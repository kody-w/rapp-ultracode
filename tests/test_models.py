from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from rapp_ultracode.models import (
    CheckSpec,
    PlanDraft,
    RepositorySpec,
    TaskSpec,
    materialize_plan,
)


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


def test_plan_digest_is_content_addressed():
    repo = RepositorySpec(path=str(Path.cwd().resolve()), base_sha="a" * 40)
    check = CheckSpec(id="test", argv=["pytest", "-q"])

    first = materialize_plan(goal="change", repository=repo, draft=draft(), checks=[check])
    second = materialize_plan(goal="change", repository=repo, draft=draft(), checks=[check])

    assert first.digest == second.digest
    assert first.plan_id == second.plan_id
    assert first.created_at != ""


@pytest.mark.parametrize(
    "path",
    ["/tmp/file", "../file", ".git/config", ".GIT/config", r"..\file"],
)
def test_task_paths_are_confined(path):
    with pytest.raises(ValidationError):
        TaskSpec(
            id="T1",
            title="Unsafe",
            objective="Unsafe path",
            file_hints=[path],
            acceptance=["safe"],
        )


def test_unknown_check_reference_is_rejected():
    repo = RepositorySpec(path=str(Path.cwd().resolve()), base_sha="a" * 40)
    with pytest.raises(ValidationError, match="unknown checks"):
        materialize_plan(goal="change", repository=repo, draft=draft(), checks=[])


def test_tampered_plan_is_rejected():
    repo = RepositorySpec(path=str(Path.cwd().resolve()), base_sha="a" * 40)
    plan = materialize_plan(
        goal="change",
        repository=repo,
        draft=draft(),
        checks=[CheckSpec(id="test", argv=["pytest", "-q"])],
    )
    payload = plan.model_dump(mode="json", by_alias=True)
    payload["goal"] = "tampered"

    with pytest.raises(ValidationError, match="digest"):
        type(plan).model_validate(payload)


def test_plan_enforces_task_count_and_uniqueness():
    repo = RepositorySpec(path=str(Path.cwd().resolve()), base_sha="a" * 40)
    plan = materialize_plan(
        goal="change",
        repository=repo,
        draft=draft(),
        checks=[CheckSpec(id="test", argv=["pytest", "-q"])],
    )
    payload = plan.model_dump(mode="json", by_alias=True)
    payload["tasks"] = payload["tasks"] * 13
    with pytest.raises(ValidationError):
        type(plan).model_validate(payload)

    payload = plan.model_dump(mode="json", by_alias=True)
    payload["tasks"] = payload["tasks"] * 2
    with pytest.raises(ValidationError, match="unique"):
        type(plan).model_validate(payload)
