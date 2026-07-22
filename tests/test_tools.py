from __future__ import annotations

import hashlib
import os

import pytest

from rapp_ultracode.errors import PolicyViolation, StateConflict
from rapp_ultracode.tools import Workspace, build_tools


def test_guarded_read_write_delete(tmp_path):
    root = tmp_path / "worktree"
    root.mkdir()
    path = root / "app.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    workspace = Workspace(root)

    original = workspace.read("app.py")
    changed = workspace.write("app.py", "VALUE = 2\n", original["sha256"])
    workspace.delete("app.py", changed["sha256"])

    assert not path.exists()


def test_stale_write_is_rejected(tmp_path):
    root = tmp_path / "worktree"
    root.mkdir()
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    workspace = Workspace(root)

    with pytest.raises(StateConflict):
        workspace.write("app.py", "VALUE = 2\n", hashlib.sha256(b"stale").hexdigest())


@pytest.mark.parametrize(
    "path",
    ["../secret", "/etc/passwd", ".git/config", ".GIT/config", r"..\secret"],
)
def test_workspace_rejects_escape_paths(tmp_path, path):
    workspace = Workspace(tmp_path)
    with pytest.raises(PolicyViolation):
        workspace.read(path)


def test_read_only_tools_exclude_mutations(tmp_path):
    names = {tool.name for tool in build_tools(Workspace(tmp_path), writable=False)}
    assert names == {"uc_list_files", "uc_read_file", "uc_search_literal", "uc_diff"}


@pytest.mark.skipif(os.name == "nt", reason="symlink creation is not reliably available")
def test_workspace_rejects_symlink_file(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("secret", encoding="utf-8")
    root = tmp_path / "worktree"
    root.mkdir()
    (root / "linked").symlink_to(outside)

    with pytest.raises(PolicyViolation):
        Workspace(root).read("linked")


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode assertion")
def test_replacing_file_preserves_executable_mode(tmp_path):
    root = tmp_path / "worktree"
    root.mkdir()
    path = root / "script.sh"
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    workspace = Workspace(root)
    current = workspace.read("script.sh")

    workspace.write("script.sh", "#!/bin/sh\necho ok\n", current["sha256"])

    assert path.stat().st_mode & 0o777 == 0o755
