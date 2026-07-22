"""UltraCodeFactory: create inert, reviewable UltraCode PlanDraft JSON.

This single-file RAPP agent never imports RDW, runs commands, edits a
repository, or approves/executes a plan. Brainstem's outer model turns the
user's intent into a bounded structured draft. The trusted `rapp-ultracode`
CLI snapshots the repository, validates checks, creates the final
content-addressed plan, obtains approval, and executes it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

try:
    from agents.basic_agent import BasicAgent
except ModuleNotFoundError:
    from basic_agent import BasicAgent


__manifest__ = {
    "schema": "rapp-agent/1.0",
    "name": "@kody-w/ultracode_factory",
    "version": "0.1.0",
    "display_name": "UltraCode Factory",
    "description": (
        "Creates inert, reviewable RAPP UltraCode plan drafts from coding intent. "
        "It never executes, approves, or generates Python."
    ),
    "author": "Kody Wildfeuer",
    "tags": ["ultracode", "workflow", "factory", "rdw", "planning"],
    "category": "pipeline",
    "quality_tier": "community",
    "requires_env": [],
    "dependencies": ["@rapp/basic_agent"],
    "example_call": {
        "args": {
            "action": "design",
            "name": "cache-fix",
            "goal": "Fix cache invalidation and add regression coverage.",
            "summary": "Reproduce, fix, and verify the cache bug.",
            "tasks": [
                {
                    "id": "T1",
                    "title": "Fix invalidation",
                    "objective": "Correct cache invalidation.",
                    "file_hints": ["src/cache.py"],
                    "acceptance": ["Regression test passes."],
                    "check_ids": ["test"],
                }
            ],
        }
    },
}

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_MAX_DRAFT_BYTES = 128 * 1024


class UltraCodeFactoryAgent(BasicAgent):
    def __init__(self):
        self.name = "UltraCodeFactory"
        self.metadata = {
            "name": self.name,
            "description": (
                "Design, list, or read inert UltraCode PlanDraft JSON files. "
                "Use design for substantial coding work that benefits from an "
                "ordered multi-agent plan. This tool does not run or approve plans."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["capabilities", "design", "list", "read"],
                    },
                    "name": {"type": "string"},
                    "goal": {"type": "string"},
                    "summary": {"type": "string"},
                    "assumptions": {"type": "array", "items": {"type": "string"}},
                    "tasks": {
                        "type": "array",
                        "maxItems": 12,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "title": {"type": "string"},
                                "objective": {"type": "string"},
                                "file_hints": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "acceptance": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "check_ids": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["id", "title", "objective", "acceptance"],
                        },
                    },
                    "risks": {"type": "array", "items": {"type": "string"}},
                    "non_goals": {"type": "array", "items": {"type": "string"}},
                    "draft_id": {"type": "string"},
                },
                "required": ["action"],
            },
        }
        super().__init__(name=self.name, metadata=self.metadata)

    def perform(self, action="capabilities", **kwargs):
        try:
            if action == "capabilities":
                return _result(
                    "success",
                    {
                        "actions": ["capabilities", "design", "list", "read"],
                        "executes": False,
                        "approves": False,
                        "output": "PlanDraft JSON",
                    },
                )
            if action == "design":
                return self._design(**kwargs)
            if action == "list":
                return self._list()
            if action == "read":
                return self._read(kwargs.get("draft_id"))
            return _result("error", {"message": f"unknown action: {action}"})
        except (OSError, TypeError, ValueError) as exc:
            return _result("error", {"message": str(exc)})

    def _design(
        self,
        *,
        name="",
        goal="",
        summary="",
        tasks=None,
        assumptions=None,
        risks=None,
        non_goals=None,
        **_kwargs,
    ):
        if not goal or not summary:
            raise ValueError("goal and summary are required")
        goal = _text(goal, "goal", 16000)
        validated_tasks = _tasks(tasks)
        draft = {
            "summary": _text(summary, "summary", 2000),
            "assumptions": _strings(assumptions, "assumptions", 32),
            "tasks": validated_tasks,
            "risks": _strings(risks, "risks", 32),
            "non_goals": _strings(non_goals, "non_goals", 32),
        }
        encoded = (json.dumps(draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > _MAX_DRAFT_BYTES:
            raise ValueError("draft exceeds 128 KiB")
        slug = re.sub(r"[^a-z0-9-]+", "-", str(name or "ultracode").lower()).strip("-")
        slug = slug[:48] or "ultracode"
        digest = hashlib.sha256(encoded).hexdigest()
        draft_id = f"{slug}-{digest[:12]}"
        directory = _draft_root()
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{draft_id}.json"
        if destination.exists():
            existing = destination.read_bytes()
            if existing != encoded:
                raise ValueError("draft id collision")
        else:
            _atomic_write(destination, encoded)
        check_ids = sorted({check_id for task in validated_tasks for check_id in task["check_ids"]})
        run_argv = [
            "rapp-ultracode",
            "plan",
            goal,
            "--repo",
            ".",
            "--draft",
            str(destination),
        ]
        for check_id in check_ids:
            run_argv.extend(["--check", f"{check_id}=<REPLACE_WITH_APPROVED_COMMAND>"])
        return _result(
            "success",
            {
                "draft_id": draft_id,
                "path": str(destination),
                "sha256": digest,
                "goal": goal,
                "run_argv": run_argv,
                "executed": False,
                "approved": False,
            },
        )

    def _list(self):
        directory = _draft_root()
        drafts = []
        if directory.is_dir() and not directory.is_symlink():
            for path in sorted(directory.glob("*.json")):
                if path.is_symlink() or not path.is_file():
                    continue
                drafts.append(
                    {
                        "draft_id": path.stem,
                        "path": str(path),
                        "bytes": path.stat().st_size,
                    }
                )
                if len(drafts) >= 200:
                    break
        return _result("success", {"drafts": drafts})

    def _read(self, draft_id):
        if not isinstance(draft_id, str) or not re.fullmatch(r"[a-z0-9-]{1,80}", draft_id):
            raise ValueError("valid draft_id is required")
        path = _draft_root() / f"{draft_id}.json"
        if path.is_symlink() or not path.is_file():
            raise ValueError("draft not found")
        if path.stat().st_size > _MAX_DRAFT_BYTES:
            raise ValueError("draft exceeds 128 KiB")
        data = path.read_bytes()
        draft = _validate_draft(json.loads(data))
        file_digest = hashlib.sha256(data).hexdigest()
        if not draft_id.endswith(f"-{file_digest[:12]}"):
            raise ValueError("draft id does not match its content digest")
        return _result(
            "success",
            {
                "draft_id": draft_id,
                "sha256": file_digest,
                "draft": draft,
            },
        )


def _tasks(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        raise ValueError("tasks must contain 1 to 12 entries")
    seen = set()
    tasks = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("each task must be an object")
        unknown = set(item) - {
            "id",
            "title",
            "objective",
            "file_hints",
            "acceptance",
            "check_ids",
            "max_attempts",
        }
        if unknown:
            raise ValueError(f"unknown task fields: {sorted(unknown)}")
        task_id = item.get("id")
        if not isinstance(task_id, str) or not _ID_RE.fullmatch(task_id):
            raise ValueError("task id must be a safe identifier")
        if task_id in seen:
            raise ValueError(f"duplicate task id: {task_id}")
        seen.add(task_id)
        paths = item.get("file_hints") or []
        if not isinstance(paths, list) or len(paths) > 64:
            raise ValueError("file_hints must be an array of at most 64 paths")
        for path in paths:
            _safe_path(path)
        attempts = item.get("max_attempts", 2)
        if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 3:
            raise ValueError("max_attempts must be an integer from 1 to 3")
        tasks.append(
            {
                "id": task_id,
                "title": _text(item.get("title"), "title", 120),
                "objective": _text(item.get("objective"), "objective", 8000),
                "file_hints": paths,
                "acceptance": _strings(item.get("acceptance"), "acceptance", 32, required=True),
                "check_ids": _strings(item.get("check_ids"), "check_ids", 16),
                "max_attempts": attempts,
            }
        )
    return tasks


def _safe_path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("file hints must be POSIX-style relative paths")
    path = PurePosixPath(value)
    if (
        value == "."
        or path.is_absolute()
        or ".." in path.parts
        or any(part.casefold() == ".git" for part in path.parts)
    ):
        raise ValueError(f"unsafe file hint: {value}")


def _strings(value, name, limit, required=False):
    if value is None:
        value = []
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{name} must be an array of at most {limit} strings")
    if required and not value:
        raise ValueError(f"{name} must not be empty")
    return [_text(item, name, 2000) for item in value]


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    return value


def _draft_root():
    configured = os.environ.get("RAPP_ULTRACODE_DRAFTS")
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".rapp" / "ultracode" / "drafts"
    )
    if path.is_symlink():
        raise ValueError("draft root must not be a symlink")
    return path


def _atomic_write(path, data):
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _result(status, data):
    return json.dumps(
        {
            "schema": "rapp-ultracode-tool-result/1.0",
            "status": status,
            **data,
        },
        ensure_ascii=False,
    )


def _validate_draft(value):
    if not isinstance(value, dict):
        raise ValueError("draft must be a JSON object")
    unknown = set(value) - {
        "summary",
        "assumptions",
        "tasks",
        "risks",
        "non_goals",
    }
    if unknown:
        raise ValueError(f"unknown draft fields: {sorted(unknown)}")
    return {
        "summary": _text(value.get("summary"), "summary", 2000),
        "assumptions": _strings(value.get("assumptions"), "assumptions", 32),
        "tasks": _tasks(value.get("tasks")),
        "risks": _strings(value.get("risks"), "risks", 32),
        "non_goals": _strings(value.get("non_goals"), "non_goals", 32),
    }
