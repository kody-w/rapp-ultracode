from __future__ import annotations

import os
import tempfile
from importlib.resources import files
from pathlib import Path

from .errors import StateConflict


def export_factory_agent(destination: Path, *, replace: bool = False) -> dict[str, object]:
    resource = files("rapp_ultracode").joinpath("assets").joinpath("ultracode_factory_agent.py")
    try:
        source = resource.read_bytes()
    except FileNotFoundError:
        source = (
            Path(__file__).resolve().parents[2]
            / "integrations"
            / "rapp"
            / "ultracode_factory_agent.py"
        ).read_bytes()
    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if replace:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(source)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return {"path": str(destination), "bytes": len(source)}
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise StateConflict(f"refusing to overwrite {destination}") from exc
    with os.fdopen(fd, "wb") as handle:
        try:
            handle.write(source)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            destination.unlink(missing_ok=True)
            raise
    return {"path": str(destination), "bytes": len(source)}
