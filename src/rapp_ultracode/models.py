from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical import digest

PLAN_SCHEMA = "rapp-ultracode-plan/1.0"
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class CheckSpec(StrictModel):
    id: str
    argv: list[str] = Field(min_length=1, max_length=32)
    cwd: str = "."
    timeout_seconds: int = Field(default=900, ge=1, le=3600)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("check id must be a safe identifier")
        return value

    @field_validator("argv")
    @classmethod
    def valid_argv(cls, value: list[str]) -> list[str]:
        if any(not item or "\x00" in item for item in value):
            raise ValueError("check argv entries must be non-empty and contain no NUL")
        return value

    @field_validator("cwd")
    @classmethod
    def valid_cwd(cls, value: str) -> str:
        _safe_relative_path(value, allow_dot=True)
        return value


class TaskSpec(StrictModel):
    id: str
    title: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=8000)
    file_hints: list[str] = Field(default_factory=list, max_length=64)
    acceptance: list[str] = Field(min_length=1, max_length=32)
    check_ids: list[str] = Field(default_factory=list, max_length=16)
    max_attempts: int = Field(default=2, ge=1, le=3)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not _ID_RE.fullmatch(value):
            raise ValueError("task id must be a safe identifier")
        return value

    @field_validator("file_hints")
    @classmethod
    def valid_paths(cls, value: list[str]) -> list[str]:
        for item in value:
            _safe_relative_path(item)
        return value


class PlanDraft(StrictModel):
    summary: str = Field(min_length=1, max_length=2000)
    assumptions: list[str] = Field(default_factory=list, max_length=32)
    tasks: list[TaskSpec] = Field(min_length=1, max_length=12)
    risks: list[str] = Field(default_factory=list, max_length=32)
    non_goals: list[str] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique_tasks(self) -> PlanDraft:
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        return self


class RepositorySpec(StrictModel):
    path: str
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    instruction_hashes: dict[str, str] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def absolute_repository_path(cls, value: str) -> str:
        if "\x00" in value or not Path(value).is_absolute():
            raise ValueError("repository path must be absolute")
        return value

    @field_validator("instruction_hashes")
    @classmethod
    def valid_instruction_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        for name, file_digest in value.items():
            _safe_relative_path(name)
            if not re.fullmatch(r"[0-9a-f]{64}", file_digest):
                raise ValueError("instruction hashes must be lowercase SHA-256")
        return value


class Plan(StrictModel):
    schema_: Literal["rapp-ultracode-plan/1.0"] = Field(
        default=PLAN_SCHEMA,
        alias="schema",
    )
    plan_id: str
    created_at: str
    goal: str = Field(min_length=1, max_length=16000)
    repository: RepositorySpec
    summary: str = Field(min_length=1, max_length=2000)
    assumptions: list[str] = Field(default_factory=list, max_length=32)
    tasks: list[TaskSpec] = Field(min_length=1, max_length=12)
    checks: list[CheckSpec] = Field(default_factory=list, max_length=32)
    risks: list[str] = Field(default_factory=list, max_length=32)
    non_goals: list[str] = Field(default_factory=list, max_length=32)
    digest: str

    @model_validator(mode="after")
    def valid_references(self) -> Plan:
        task_ids = [task.id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task ids must be unique")
        check_ids = {check.id for check in self.checks}
        if len(check_ids) != len(self.checks):
            raise ValueError("check ids must be unique")
        unknown = sorted(
            {
                check_id
                for task in self.tasks
                for check_id in task.check_ids
                if check_id not in check_ids
            }
        )
        if unknown:
            raise ValueError(f"tasks reference unknown checks: {', '.join(unknown)}")
        expected = digest(_plan_content(self), "rapp-ultracode/1:plan")
        if self.digest != expected:
            raise ValueError("plan digest does not match its content")
        if self.plan_id != f"uc-{expected[:16]}":
            raise ValueError("plan id does not match its digest")
        return self


class WorkResult(StrictModel):
    status: Literal["completed", "blocked"]
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class ReviewResult(StrictModel):
    verdict: Literal["pass", "fail", "blocked"]
    summary: str
    findings: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


def materialize_plan(
    *,
    goal: str,
    repository: RepositorySpec,
    draft: PlanDraft,
    checks: list[CheckSpec],
) -> Plan:
    created_at = datetime.now(UTC).isoformat()
    content = {
        "schema": PLAN_SCHEMA,
        "goal": goal,
        "repository": repository.model_dump(mode="json"),
        **draft.model_dump(mode="json"),
        "checks": [check.model_dump(mode="json") for check in checks],
    }
    plan_digest = digest(content, "rapp-ultracode/1:plan")
    return Plan(
        **content,
        created_at=created_at,
        plan_id=f"uc-{plan_digest[:16]}",
        digest=plan_digest,
    )


def _safe_relative_path(value: str, *, allow_dot: bool = False) -> None:
    path = PurePosixPath(value)
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("path must be a non-empty POSIX-style relative path")
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("path must remain inside the repository")
    if any(part.casefold() == ".git" for part in path.parts):
        raise ValueError(".git paths are forbidden")
    if not allow_dot and value == ".":
        raise ValueError("file path may not be the repository root")


def _plan_content(plan: Plan) -> dict[str, object]:
    return {
        "schema": PLAN_SCHEMA,
        "goal": plan.goal,
        "repository": plan.repository.model_dump(mode="json"),
        "summary": plan.summary,
        "assumptions": plan.assumptions,
        "tasks": [task.model_dump(mode="json") for task in plan.tasks],
        "checks": [check.model_dump(mode="json") for check in plan.checks],
        "risks": plan.risks,
        "non_goals": plan.non_goals,
    }
