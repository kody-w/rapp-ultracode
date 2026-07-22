from __future__ import annotations

from pathlib import Path

from rapp_ultracode.api import UltraCodeAPI
from rapp_ultracode.models import PlanDraft, RepositorySpec, TaskSpec, materialize_plan


def test_headless_api_round_trip(tmp_path):
    plan = materialize_plan(
        goal="change",
        repository=RepositorySpec(path=str(Path.cwd().resolve()), base_sha="a" * 40),
        draft=PlanDraft(
            summary="change",
            tasks=[
                TaskSpec(
                    id="T1",
                    title="Change",
                    objective="Change value",
                    acceptance=["done"],
                )
            ],
        ),
        checks=[],
    )

    with UltraCodeAPI(tmp_path) as api:
        api.save_plan(plan)
        api.approve(plan.plan_id, plan.digest)
        run_id = api.create_run(plan.plan_id)

        assert api.plan(plan.plan_id).digest == plan.digest
        assert api.run(run_id)["state"] == "queued"
        assert api.events(run_id)[0]["type"] == "run.created"
