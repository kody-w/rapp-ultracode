from __future__ import annotations

import subprocess
import sys

import pytest

from rapp_ultracode.errors import PolicyViolation
from rapp_ultracode.repository import GitRepository, Worktree, run_check


def test_worktree_keeps_caller_checkout_untouched(git_repo, tmp_path):
    repository = GitRepository.open(git_repo)
    snapshot = repository.snapshot()
    destination = tmp_path / "worktree"
    repository.create_worktree(destination, "ultracode/test", snapshot.spec.base_sha)
    (destination / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    worktree = Worktree(destination)

    commit = worktree.commit("T1", "Update value")

    assert len(commit) == 40
    assert (git_repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (destination / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_run_check_uses_argv_without_shell(git_repo):
    result = run_check(
        git_repo,
        [sys.executable, "-c", "from pathlib import Path; assert Path('app.py').exists()"],
        cwd=".",
        timeout_seconds=10,
    )

    assert result["returncode"] == 0


def test_worktree_reports_ignored_outputs(git_repo, tmp_path):
    (git_repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-m", "ignore"], cwd=git_repo, check=True)
    repository = GitRepository.open(git_repo)
    destination = tmp_path / "worktree"
    repository.create_worktree(
        destination,
        "ultracode/ignored",
        repository.base_sha(),
    )
    (destination / "app.py").unlink()
    (destination / "ignored.py").write_text("VALUE = 2\n", encoding="utf-8")
    worktree = Worktree(destination)

    assert worktree.changed_files() == ["app.py"]
    assert worktree.ignored_files() == ["ignored.py"]


def test_snapshot_rejects_executable_git_filters(git_repo):
    subprocess.run(
        ["git", "config", "filter.evil.clean", "/bin/true"],
        cwd=git_repo,
        check=True,
    )

    with pytest.raises(PolicyViolation, match="executable Git configuration"):
        GitRepository.open(git_repo).snapshot()
