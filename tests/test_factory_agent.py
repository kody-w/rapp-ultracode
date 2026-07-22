from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path


def load_agent():
    class BasicAgent:
        def __init__(self, name=None, metadata=None):
            self.name = name
            self.metadata = metadata

    agents = types.ModuleType("agents")
    agents.__path__ = []
    basic = types.ModuleType("agents.basic_agent")
    basic.BasicAgent = BasicAgent
    agents.basic_agent = basic
    sys.modules["agents"] = agents
    sys.modules["agents.basic_agent"] = basic
    path = (
        Path(__file__).resolve().parents[1] / "integrations" / "rapp" / "ultracode_factory_agent.py"
    )
    spec = importlib.util.spec_from_file_location("ultracode_factory_agent_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_factory_creates_inert_plan_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("RAPP_ULTRACODE_DRAFTS", str(tmp_path))
    module = load_agent()
    agent = module.UltraCodeFactoryAgent()

    result = json.loads(
        agent.perform(
            action="design",
            name="cache-fix",
            goal="Fix cache invalidation",
            summary="Fix and verify the cache.",
            tasks=[
                {
                    "id": "T1",
                    "title": "Fix cache",
                    "objective": "Correct invalidation.",
                    "file_hints": ["src/cache.py"],
                    "acceptance": ["Regression passes"],
                    "check_ids": ["test"],
                }
            ],
        )
    )

    assert result["status"] == "success"
    assert result["executed"] is False
    assert result["approved"] is False
    assert Path(result["path"]).is_file()
    assert result["run_argv"][0] == "rapp-ultracode"
    assert "test=<REPLACE_WITH_APPROVED_COMMAND>" in result["run_argv"]


def test_factory_rejects_path_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("RAPP_ULTRACODE_DRAFTS", str(tmp_path))
    agent = load_agent().UltraCodeFactoryAgent()

    result = json.loads(
        agent.perform(
            action="design",
            goal="unsafe",
            summary="unsafe",
            tasks=[
                {
                    "id": "T1",
                    "title": "Unsafe",
                    "objective": "Escape.",
                    "file_hints": ["../secret"],
                    "acceptance": ["never"],
                }
            ],
        )
    )

    assert result["status"] == "error"
    assert "unsafe file hint" in result["message"]


def test_factory_source_has_no_runtime_or_dynamic_execution():
    path = (
        Path(__file__).resolve().parents[1] / "integrations" / "rapp" / "ultracode_factory_agent.py"
    )
    source = path.read_text(encoding="utf-8")

    for forbidden in (
        "import rdw",
        "subprocess",
        "eval(",
        "exec(",
        "builtins.compile",
        "importlib",
    ):
        assert forbidden not in source
