from __future__ import annotations

import os
from pathlib import Path


def state_root() -> Path:
    configured = os.environ.get("RAPP_ULTRACODE_STATE")
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "rapp-ultracode"
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return (base / "rapp-ultracode").resolve()
