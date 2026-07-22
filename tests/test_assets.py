from __future__ import annotations

import pytest

from rapp_ultracode.assets import export_factory_agent
from rapp_ultracode.errors import StateConflict


def test_export_factory_agent(tmp_path):
    destination = tmp_path / "ultracode_factory_agent.py"

    result = export_factory_agent(destination)

    assert result["bytes"] > 1000
    assert "class UltraCodeFactoryAgent" in destination.read_text(encoding="utf-8")
    with pytest.raises(StateConflict):
        export_factory_agent(destination)
