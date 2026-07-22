from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import Plan
from .store import Store


class UltraCodeAPI:
    """Headless programmatic access to durable plans, approvals, runs, and events."""

    def __init__(self, state_directory: str | Path) -> None:
        self.state_directory = Path(state_directory).expanduser().resolve()
        self.store = Store(self.state_directory / "state.sqlite3")

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> UltraCodeAPI:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def save_plan(self, plan: Plan) -> None:
        self.store.save_plan(plan)

    def plan(self, plan_id: str) -> Plan:
        return self.store.get_plan(plan_id)

    def plans(self) -> list[Plan]:
        return self.store.list_plans()

    def approve(self, plan_id: str, digest: str) -> None:
        self.store.approve(plan_id, digest)

    def create_run(self, plan_id: str) -> str:
        plan = self.store.get_plan(plan_id)
        self.store.require_approval(plan)
        return self.store.create_run(plan)

    def run(self, run_id: str) -> dict[str, Any]:
        return self.store.get_run(run_id)

    def runs(self) -> list[dict[str, Any]]:
        return self.store.list_runs()

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self.store.events(run_id, after)
