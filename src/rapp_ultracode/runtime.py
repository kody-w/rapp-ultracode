from __future__ import annotations

import hashlib
from contextlib import AbstractAsyncContextManager
from typing import Any

from copilot.generated.rpc import PermissionDecisionReject
from rdw import Runtime, SessionHandle, Workflow

from .models import Plan, PlanDraft, ReviewResult, TaskSpec, WorkResult
from .repository import RepositorySnapshot


class RestrictedRuntime:
    """Restrict every RDW session to the exact custom tools supplied by the host."""

    def __init__(self, delegate: Runtime) -> None:
        self.delegate = delegate

    async def create_session(self, **kwargs: Any) -> SessionHandle:
        tools = list(kwargs.get("tools") or [])
        names = [name for tool in tools if isinstance((name := getattr(tool, "name", None)), str)]
        kwargs["available_tools"] = names
        kwargs["on_permission_request"] = _deny_permission
        return await self.delegate.create_session(**kwargs)

    def slot(self) -> AbstractAsyncContextManager[None]:
        return self.delegate.slot()

    async def close(self) -> None:
        await self.delegate.close()


def _deny_permission(_request: Any, _invocation: dict[str, str]) -> PermissionDecisionReject:
    return PermissionDecisionReject(
        feedback="UltraCode denies every permission outside its skip-permission uc_* tools."
    )


class RdwEngine:
    def __init__(self, workflow: Workflow) -> None:
        self.workflow = workflow

    async def plan(
        self,
        *,
        goal: str,
        snapshot: RepositorySnapshot,
        check_ids: list[str],
        model: str | None,
    ) -> PlanDraft:
        prompt = f"""
You are the xhigh planner for a supervised coding run.

Goal:
{goal}

Repository snapshot:
{snapshot.context}

Operator-approved check IDs:
{check_ids}

Return a small ordered plan of no more than 12 tasks. Every task needs
observable acceptance criteria. Reference only the supplied check IDs. File
hints are advisory repository-relative paths, never absolute paths. Do not
write code or request tools; submit the structured plan only.
""".strip()
        return await self.workflow.agent(
            prompt,
            schema=PlanDraft,
            label="ultracode-planner",
            model=model,
            effort="xhigh",
            timeout=900,
        )

    async def execute_task(
        self,
        *,
        task: TaskSpec,
        attempt: int,
        tools: list[Any],
        model: str | None,
        effort: str,
    ) -> WorkResult:
        prompt = f"""
You are implementing one approved task in an isolated Git worktree.

Task: {task.id} - {task.title}
Objective: {task.objective}
File hints: {task.file_hints}
Acceptance criteria:
{chr(10).join(f"- {item}" for item in task.acceptance)}

Attempt: {attempt}

Use only the provided uc_* tools. Read before replacing a file and pass its
SHA-256 back to uc_write_file. Do not access .git, run commands, use the
network, or modify paths outside the worktree. Finish by submitting a truthful
structured result. If the task cannot be completed safely, return blocked.
""".strip()
        return await self.workflow.agent(
            prompt,
            schema=WorkResult,
            label=f"task-{task.id}-attempt-{attempt}",
            model=model,
            effort=effort,
            timeout=1800,
            tools=tools,
        )

    async def review(
        self,
        *,
        plan: Plan,
        head_sha: str,
        diff: str,
        tools: list[Any],
        model: str | None,
    ) -> ReviewResult:
        diff_digest = hashlib.sha256(diff.encode("utf-8")).hexdigest()
        acceptance = [
            {
                "task": task.id,
                "objective": task.objective,
                "acceptance": task.acceptance,
            }
            for task in plan.tasks
        ]
        prompt = f"""
Review the completed isolated-worktree change for correctness.

Approved goal:
{plan.goal}

Approved assumptions:
{plan.assumptions}

Approved non-goals:
{plan.non_goals}

Approved task acceptance:
{acceptance}

Candidate HEAD: {head_sha}
Complete diff SHA-256: {diff_digest}

Git diff preview:
{diff[:120000]}

Use uc_diff when the preview is incomplete and use the other read-only uc_*
tools to verify important context. Report pass only when the implementation
satisfies every approved acceptance criterion, respects non-goals, and has no
blocking correctness issue. Missing evidence is blocked, never pass.
""".strip()
        return await self.workflow.agent(
            prompt,
            schema=ReviewResult,
            label="ultracode-final-review",
            model=model,
            effort="xhigh",
            timeout=1200,
            tools=tools,
        )
