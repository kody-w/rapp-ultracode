from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class UltraCodeError(Exception):
    message: str
    code: str = "ULTRACODE_ERROR"
    exit_code: int = 1
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


class UsageError(UltraCodeError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "INVALID_USAGE", 2)


class NotFound(UltraCodeError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "NOT_FOUND", 3)


class ApprovalRequired(UltraCodeError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "APPROVAL_REQUIRED", 4)


class StateConflict(UltraCodeError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "STATE_CONFLICT", 5)


class ExecutionFailed(UltraCodeError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, "EXECUTION_FAILED", 7, details or {})


class PolicyViolation(UltraCodeError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "POLICY_VIOLATION", 8)


class InternalFailure(UltraCodeError):
    def __init__(self) -> None:
        super().__init__("unexpected internal UltraCode failure", "INTERNAL_ERROR", 70)
