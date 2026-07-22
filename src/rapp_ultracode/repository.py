from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import ExecutionFailed, PolicyViolation, StateConflict, UsageError
from .models import RepositorySpec

_MAX_CONTEXT_BYTES = 128 * 1024


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    spec: RepositorySpec
    context: str


class GitRepository:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    @classmethod
    def open(cls, path: str | Path) -> GitRepository:
        candidate = Path(path).expanduser().resolve()
        result = _git(candidate, ["rev-parse", "--show-toplevel"])
        root = Path(result.stdout.strip()).resolve()
        repository = cls(root)
        repository._require_safe_git_configuration()
        return repository

    def require_clean(self) -> None:
        status = _git(self.root, ["status", "--porcelain=v1", "-z"]).stdout
        if status:
            raise StateConflict("repository must be clean before planning")

    def base_sha(self) -> str:
        value = _git(self.root, ["rev-parse", "HEAD"]).stdout.strip()
        if len(value) != 40:
            raise ExecutionFailed("git returned an invalid HEAD")
        return value

    def snapshot(self) -> RepositorySnapshot:
        self.require_clean()
        self._require_safe_git_configuration()
        base_sha = self.base_sha()
        tracked = [
            item for item in _git(self.root, ["ls-files", "-z"]).stdout.split("\x00") if item
        ]
        instructions: dict[str, str] = {}
        context_parts = [f"Base commit: {base_sha}", "Tracked files:", *tracked]
        for name in ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md"):
            path = self.root / name
            if not path.is_file() or path.is_symlink():
                continue
            data = path.read_bytes()
            if len(data) > _MAX_CONTEXT_BYTES:
                raise PolicyViolation(f"{name} exceeds the 128 KiB context limit")
            instructions[name] = hashlib.sha256(data).hexdigest()
            context_parts.extend([f"\n{name}:", data.decode("utf-8", "replace")])
        context = "\n".join(context_parts)
        encoded = context.encode("utf-8")
        if len(encoded) > _MAX_CONTEXT_BYTES:
            context = encoded[:_MAX_CONTEXT_BYTES].decode("utf-8", "ignore")
        return RepositorySnapshot(
            spec=RepositorySpec(
                path=str(self.root),
                base_sha=base_sha,
                instruction_hashes=instructions,
            ),
            context=context,
        )

    def _require_safe_git_configuration(self) -> None:
        risky = _git(
            self.root,
            [
                "config",
                "--local",
                "--name-only",
                "--get-regexp",
                r"^(filter\.|core\.hooksPath$|core\.fsmonitor$|diff\.external$)",
            ],
            check=False,
        )
        if risky.returncode == 0 and risky.stdout.strip():
            raise PolicyViolation(
                f"repository has executable Git configuration: {risky.stdout.strip()}"
            )
        common = _common_git_dir(self.root)
        attributes = [self.root / ".gitattributes", common / "info" / "attributes"]
        for path in attributes:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if re.search(r"(?:^|\s)filter\s*=", text, re.IGNORECASE):
                raise PolicyViolation(f"Git filters are not allowed: {path}")

    def ensure_base(self, expected_sha: str) -> None:
        current = self.base_sha()
        if current != expected_sha:
            raise StateConflict(f"repository moved from {expected_sha[:12]} to {current[:12]}")

    def create_worktree(self, destination: Path, branch: str, base_sha: str) -> Path:
        destination = destination.resolve()
        try:
            destination.relative_to(self.root)
        except ValueError:
            pass
        else:
            raise PolicyViolation("managed worktree must not live inside the source checkout")
        if destination.exists():
            expected_common = _common_git_dir(self.root)
            actual_common = _common_git_dir(destination)
            if expected_common != actual_common:
                raise StateConflict("existing worktree belongs to another repository")
            current_branch = _git(
                destination,
                ["symbolic-ref", "--quiet", "--short", "HEAD"],
            ).stdout.strip()
            if current_branch != branch:
                raise StateConflict(
                    f"existing worktree uses branch {current_branch!r}, expected {branch!r}"
                )
            ancestor = _git(
                destination,
                ["merge-base", "--is-ancestor", base_sha, "HEAD"],
                check=False,
            )
            if ancestor.returncode != 0:
                raise StateConflict("existing worktree is not descended from the plan base")
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        branch_exists = (
            _git(
                self.root,
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                check=False,
            ).returncode
            == 0
        )
        if branch_exists:
            ancestor = _git(
                self.root,
                ["merge-base", "--is-ancestor", base_sha, branch],
                check=False,
            )
            if ancestor.returncode != 0:
                raise StateConflict(
                    f"existing branch {branch!r} is not descended from the plan base"
                )
        args = (
            ["worktree", "add", str(destination), branch]
            if branch_exists
            else ["worktree", "add", "-b", branch, str(destination), base_sha]
        )
        _git(self.root, args)
        return destination


class Worktree:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def head(self) -> str:
        return _git(self.root, ["rev-parse", "HEAD"]).stdout.strip()

    def subject(self) -> str:
        return _git(self.root, ["log", "-1", "--format=%s"]).stdout.strip()

    def is_clean(self) -> bool:
        return not bool(_git(self.root, ["status", "--porcelain=v1", "-z"]).stdout)

    def discard_uncommitted(self) -> None:
        _git(self.root, ["reset", "--hard", "HEAD"])
        _git(self.root, ["clean", "-fdx"])

    def diff(self) -> str:
        return _git(
            self.root,
            ["diff", "--no-ext-diff", "--no-textconv", "--"],
        ).stdout

    def diff_from(self, base_sha: str) -> str:
        return _git(
            self.root,
            ["diff", "--no-ext-diff", "--no-textconv", f"{base_sha}..HEAD", "--"],
        ).stdout

    def is_ancestor(self, ancestor: str, descendant: str = "HEAD") -> bool:
        return (
            _git(
                self.root,
                ["merge-base", "--is-ancestor", ancestor, descendant],
                check=False,
            ).returncode
            == 0
        )

    def changed_files(self) -> list[str]:
        tracked = _git(self.root, ["diff", "--name-only", "-z", "--"]).stdout.split("\x00")
        untracked = _git(
            self.root,
            ["ls-files", "--others", "--exclude-standard", "-z"],
        ).stdout.split("\x00")
        return sorted({path for path in [*tracked, *untracked] if path})

    def ignored_files(self) -> list[str]:
        return sorted(
            {
                path
                for path in _git(
                    self.root,
                    ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
                ).stdout.split("\x00")
                if path
            }
        )

    def change_fingerprint(self, changed: list[str]) -> str:
        hasher = hashlib.sha256()
        for name in sorted(changed):
            hasher.update(name.encode("utf-8"))
            hasher.update(b"\0")
            path = self.root / name
            if not path.exists():
                hasher.update(b"deleted")
            elif path.is_file() and not path.is_symlink():
                hasher.update(path.read_bytes())
            else:
                hasher.update(b"unsupported")
            hasher.update(b"\0")
        return hasher.hexdigest()

    def commit(self, task_id: str, title: str) -> str:
        _git(self.root, ["add", "-A"])
        staged = _git(self.root, ["diff", "--cached", "--quiet"], check=False)
        if staged.returncode == 0:
            raise ExecutionFailed(f"task {task_id} produced no changes")
        if staged.returncode != 1:
            raise ExecutionFailed(f"could not inspect staged changes for {task_id}")
        _git(
            self.root,
            [
                "-c",
                "user.name=RAPP UltraCode",
                "-c",
                "user.email=ultracode@local",
                "commit",
                "-m",
                f"ultracode({task_id}): {title}",
            ],
        )
        return self.head()


def run_check(
    worktree: Path,
    argv: Sequence[str],
    *,
    cwd: str,
    timeout_seconds: int,
) -> dict[str, object]:
    check_cwd = (worktree / cwd).resolve()
    try:
        check_cwd.relative_to(worktree.resolve())
    except ValueError as exc:
        raise PolicyViolation("check cwd escapes the worktree") from exc
    isolated_home = worktree.parent / f".{worktree.name}-home"
    isolated_home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(isolated_home),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", ""),
        "CI": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if os.name == "nt":
        for name in ("SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "TEMP", "TMP"):
            if os.environ.get(name):
                env[name] = os.environ[name]
    try:
        completed = subprocess.run(
            list(argv),
            cwd=check_cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExecutionFailed(f"check failed to start or timed out: {argv[0]}") from exc
    return {
        "argv": list(argv),
        "returncode": completed.returncode,
        "stdout": completed.stdout[-100_000:],
        "stderr": completed.stderr[-100_000:],
    }


def _git(
    cwd: Path,
    args: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "diff.external=",
                *args,
            ],
            cwd=cwd,
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=120,
            shell=False,
            check=False,
        )
    except OSError as exc:
        raise UsageError(f"cannot run git: {exc}") from exc
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ExecutionFailed(f"git {' '.join(args)} failed: {detail[:500]}")
    return completed


def _common_git_dir(cwd: Path) -> Path:
    value = Path(_git(cwd, ["rev-parse", "--git-common-dir"]).stdout.strip())
    return (cwd / value).resolve() if not value.is_absolute() else value.resolve()


def _git_env() -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    if os.name == "nt":
        for name in ("SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "TEMP", "TMP"):
            if os.environ.get(name):
                environment[name] = os.environ[name]
    return environment
