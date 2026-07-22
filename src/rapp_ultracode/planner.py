from __future__ import annotations

from .models import CheckSpec, Plan, materialize_plan
from .repository import GitRepository
from .runtime import RdwEngine


async def create_plan(
    *,
    engine: RdwEngine,
    goal: str,
    repository: GitRepository,
    checks: list[CheckSpec],
    model: str | None,
) -> Plan:
    snapshot = repository.snapshot()
    draft = await engine.plan(
        goal=goal,
        snapshot=snapshot,
        check_ids=[check.id for check in checks],
        model=model,
    )
    return materialize_plan(
        goal=goal,
        repository=snapshot.spec,
        draft=draft,
        checks=checks,
    )
