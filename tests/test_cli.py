from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import rapp_ultracode.cli as cli_module
from rapp_ultracode.cli import main
from rapp_ultracode.errors import UsageError
from rapp_ultracode.models import Plan
from rapp_ultracode.store import Store


def test_offline_plan_and_approval(git_repo, tmp_path, capsys):
    draft = tmp_path / "draft.json"
    draft.write_text(
        json.dumps(
            {
                "summary": "Update value",
                "assumptions": [],
                "tasks": [
                    {
                        "id": "T1",
                        "title": "Update value",
                        "objective": "Set VALUE to 2.",
                        "file_hints": ["app.py"],
                        "acceptance": ["VALUE equals 2"],
                        "check_ids": [],
                        "max_attempts": 2,
                    }
                ],
                "risks": [],
                "non_goals": [],
            }
        ),
        encoding="utf-8",
    )
    state = tmp_path / "state"

    assert (
        main(
            [
                "--json",
                "--state-root",
                str(state),
                "plan",
                "Update",
                "VALUE",
                "--repo",
                str(git_repo),
                "--draft",
                str(draft),
            ]
        )
        == 0
    )
    planned = json.loads(capsys.readouterr().out)
    plan = planned["data"]

    assert (
        main(
            [
                "--json",
                "--state-root",
                str(state),
                "approve",
                plan["plan_id"],
                "--expect-digest",
                plan["digest"],
                "--yes",
            ]
        )
        == 0
    )
    approved = json.loads(capsys.readouterr().out)
    assert approved["data"]["plan_id"] == plan["plan_id"]


def test_json_version(capsys):
    assert main(["--json", "--version"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["version"] == "0.1.0"


def test_detached_run_records_worker(git_repo, tmp_path, monkeypatch, capsys):
    draft = tmp_path / "draft.json"
    draft.write_text(
        json.dumps(
            {
                "summary": "Update value",
                "tasks": [
                    {
                        "id": "T1",
                        "title": "Update",
                        "objective": "Set VALUE to 2",
                        "acceptance": ["VALUE is 2"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    state = tmp_path / "state"
    assert (
        main(
            [
                "--json",
                "--state-root",
                str(state),
                "plan",
                "update",
                "--repo",
                str(git_repo),
                "--draft",
                str(draft),
            ]
        )
        == 0
    )
    plan_data = json.loads(capsys.readouterr().out)["data"]
    plan_data.pop("approval_command")
    plan = Plan.model_validate(plan_data)
    store = Store(state / "state.sqlite3")
    store.approve(plan.plan_id, plan.digest)
    store.close()

    monkeypatch.setattr(
        cli_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: SimpleNamespace(pid=4321),
    )
    assert (
        main(
            [
                "--json",
                "--state-root",
                str(state),
                "run",
                plan.plan_id,
                "--budget",
                "30",
                "--detach",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    run_id = payload["data"]["run_id"]
    store = Store(state / "state.sqlite3")
    assert store.get_run(run_id)["worker_pid"] == 4321
    store.close()


@pytest.mark.parametrize("value", [0, 29.9, float("nan"), float("inf")])
def test_budget_requires_finite_provider_floor(value):
    with pytest.raises(UsageError):
        cli_module._positive_budget(value)
