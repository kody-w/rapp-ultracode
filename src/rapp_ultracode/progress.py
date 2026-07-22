from __future__ import annotations

from typing import Any


class NullProgress:
    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def declare_phases(self, _phases: list[str]) -> None:
        pass

    def phase_started(self, _title: str) -> None:
        pass

    def agent_started(self, _label: str, _phase: str | None = None) -> None:
        pass

    def agent_finished(
        self,
        _label: str,
        _status: str,
        _detail: str = "",
    ) -> None:
        pass

    def agent_tokens(self, _label: str, _tokens: int) -> None:
        pass

    def agent_activity(self, _label: str, _activity: str) -> None:
        pass

    def log(self, _message: Any) -> None:
        pass
