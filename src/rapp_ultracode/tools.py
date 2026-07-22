from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from copilot.tools import Tool, define_tool
from pydantic import BaseModel, ConfigDict, Field

from .errors import PolicyViolation, StateConflict
from .repository import Worktree

_MAX_FILE_BYTES = 512 * 1024
_MAX_TOOL_OUTPUT = 1024 * 1024


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PathParams(Params):
    path: str


class ListParams(Params):
    prefix: str = "."
    limit: int = Field(default=500, ge=1, le=2000)


class SearchParams(Params):
    text: str = Field(min_length=1, max_length=1000)
    prefix: str = "."
    limit: int = Field(default=100, ge=1, le=500)


class WriteParams(Params):
    path: str
    content: str = Field(max_length=_MAX_FILE_BYTES)
    expected_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class DeleteParams(Params):
    path: str
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def list_files(self, prefix: str = ".", limit: int = 500) -> list[str]:
        start = self._path(prefix, allow_directory=True)
        if not start.exists():
            return []
        if start.is_file():
            return [start.relative_to(self.root).as_posix()]
        files: list[str] = []
        for directory, names, filenames in os.walk(start, followlinks=False):
            names[:] = sorted(
                name
                for name in names
                if name.casefold() != ".git" and not (Path(directory) / name).is_symlink()
            )
            for filename in sorted(filenames):
                path = Path(directory) / filename
                if path.is_symlink() or not path.is_file():
                    continue
                files.append(path.relative_to(self.root).as_posix())
                if len(files) >= limit:
                    return files
        return files

    def read(self, path: str) -> dict[str, Any]:
        candidate = self._path(path)
        data = _read_regular(candidate)
        if len(data) > _MAX_FILE_BYTES:
            raise PolicyViolation(f"file exceeds {_MAX_FILE_BYTES} bytes: {path}")
        return {
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "content": data.decode("utf-8", "replace"),
        }

    def search(self, text: str, prefix: str = ".", limit: int = 100) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for name in self.list_files(prefix, limit=2000):
            data = self.read(name)
            for line_number, line in enumerate(data["content"].splitlines(), start=1):
                if text in line:
                    results.append(
                        {
                            "path": name,
                            "line": line_number,
                            "text": line[:2000],
                        }
                    )
                    if len(results) >= limit:
                        return results
        return results

    def write(
        self,
        path: str,
        content: str,
        expected_sha256: str | None,
    ) -> dict[str, Any]:
        candidate = self._path(path, allow_missing=True)
        current = None
        mode = 0o600
        if candidate.exists():
            mode = stat.S_IMODE(os.lstat(candidate).st_mode)
            data = _read_regular(candidate)
            current = hashlib.sha256(data).hexdigest()
            if expected_sha256 is None:
                raise StateConflict(f"expected_sha256 is required to replace {path}")
            if current != expected_sha256:
                raise StateConflict(f"stale write for {path}")
        elif expected_sha256 is not None:
            raise StateConflict(f"expected existing file for {path}")
        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_FILE_BYTES:
            raise PolicyViolation(f"write exceeds {_MAX_FILE_BYTES} bytes")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_no_symlink(candidate.parent)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{candidate.name}.",
            suffix=".tmp",
            dir=candidate.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                if os.name == "posix":
                    os.fchmod(handle.fileno(), mode)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, candidate)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "path": path,
            "previous_sha256": current,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded),
        }

    def delete(self, path: str, expected_sha256: str) -> dict[str, Any]:
        candidate = self._path(path)
        data = _read_regular(candidate)
        current = hashlib.sha256(data).hexdigest()
        if current != expected_sha256:
            raise StateConflict(f"stale delete for {path}")
        candidate.unlink()
        return {"path": path, "deleted_sha256": current}

    def diff(self) -> str:
        value = Worktree(self.root).diff()
        encoded = value.encode("utf-8")
        if len(encoded) > _MAX_TOOL_OUTPUT:
            return encoded[:_MAX_TOOL_OUTPUT].decode("utf-8", "ignore")
        return value

    def _path(
        self,
        value: str,
        *,
        allow_missing: bool = False,
        allow_directory: bool = False,
    ) -> Path:
        path = PurePosixPath(value)
        if (
            not value
            or "\x00" in value
            or "\\" in value
            or path.is_absolute()
            or ".." in path.parts
            or any(part.casefold() == ".git" for part in path.parts)
        ):
            raise PolicyViolation(f"unsafe workspace path: {value!r}")
        candidate = self.root.joinpath(*path.parts)
        self._ensure_no_symlink(candidate.parent if candidate != self.root else self.root)
        if candidate.exists():
            if candidate.is_symlink():
                raise PolicyViolation(f"symlink access is forbidden: {value}")
            resolved = candidate.resolve()
            try:
                resolved.relative_to(self.root)
            except ValueError as exc:
                raise PolicyViolation(f"path escapes workspace: {value}") from exc
            if not allow_directory and not candidate.is_file():
                raise PolicyViolation(f"path is not a regular file: {value}")
        elif not allow_missing:
            raise PolicyViolation(f"path does not exist: {value}")
        return candidate

    def _ensure_no_symlink(self, directory: Path) -> None:
        current = directory
        while current != self.root:
            if current.exists() and _is_linklike(current):
                raise PolicyViolation(f"symlink directory is forbidden: {current}")
            try:
                current.relative_to(self.root)
            except ValueError as exc:
                raise PolicyViolation("path escapes workspace") from exc
            current = current.parent


def build_tools(workspace: Workspace, *, writable: bool = True) -> list[Tool]:
    tools = [
        define_tool(
            "uc_list_files",
            description="List regular files inside the isolated worktree.",
            params_type=ListParams,
            skip_permission=True,
            handler=lambda params, _inv: workspace.list_files(params.prefix, params.limit),
        ),
        define_tool(
            "uc_read_file",
            description="Read one regular file and return content plus SHA-256.",
            params_type=PathParams,
            skip_permission=True,
            handler=lambda params, _inv: workspace.read(params.path),
        ),
        define_tool(
            "uc_search_literal",
            description="Search for a literal string inside regular text files.",
            params_type=SearchParams,
            skip_permission=True,
            handler=lambda params, _inv: workspace.search(
                params.text,
                params.prefix,
                params.limit,
            ),
        ),
        define_tool(
            "uc_diff",
            description="Show the current worktree diff.",
            params_type=Params,
            skip_permission=True,
            handler=lambda _params, _inv: workspace.diff(),
        ),
    ]
    if not writable:
        return tools
    tools.extend(
        [
            define_tool(
                "uc_write_file",
                description=(
                    "Atomically create or replace one UTF-8 file. Replacements require "
                    "the SHA-256 returned by uc_read_file."
                ),
                params_type=WriteParams,
                skip_permission=True,
                handler=lambda params, _inv: workspace.write(
                    params.path,
                    params.content,
                    params.expected_sha256,
                ),
            ),
            define_tool(
                "uc_delete_file",
                description="Delete one regular file using an expected SHA-256 guard.",
                params_type=DeleteParams,
                skip_permission=True,
                handler=lambda params, _inv: workspace.delete(
                    params.path,
                    params.expected_sha256,
                ),
            ),
        ]
    )
    return tools


def _read_regular(path: Path) -> bytes:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise PolicyViolation(f"cannot inspect regular file: {path}") from exc
    if _is_linklike(path, before):
        raise PolicyViolation(f"symlink or reparse point is forbidden: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise PolicyViolation(f"cannot open regular file: {path}") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise PolicyViolation(f"not a regular file: {path}")
        if os.name == "posix" and (info.st_dev, info.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise PolicyViolation(f"file changed while opening: {path}")
        return handle.read(_MAX_FILE_BYTES + 1)


def _is_linklike(path: Path, info: os.stat_result | None = None) -> bool:
    record = info or os.lstat(path)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(record.st_mode) or bool(
        reparse and getattr(record, "st_file_attributes", 0) & reparse
    )
