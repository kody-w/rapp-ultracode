from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

from copilot.generated.rpc import PermissionDecisionReject

from rapp_ultracode.runtime import RestrictedRuntime


class FakeRuntime:
    def __init__(self):
        self.kwargs = None

    async def create_session(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(session_id="fake")

    @asynccontextmanager
    async def slot(self):
        yield

    async def close(self):
        pass


async def test_restricted_runtime_sets_exact_tool_allowlist():
    delegate = FakeRuntime()
    runtime = RestrictedRuntime(delegate)
    tools = [SimpleNamespace(name="uc_read_file"), SimpleNamespace(name="submit_result")]

    await runtime.create_session(tools=tools, available_tools=["bash"])

    assert delegate.kwargs["available_tools"] == ["uc_read_file", "submit_result"]
    assert isinstance(delegate.kwargs["on_permission_request"](None, {}), PermissionDecisionReject)
