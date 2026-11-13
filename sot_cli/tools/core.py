from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolExecutionResult:
    name: str
    content: str
    record_content: str
    supplemental_messages: list[dict[str, Any]]
    is_error: bool = False


@dataclass
class ToolPayload:
    payload: dict[str, Any]
    model_content: str | None = None
    supplemental_messages: list[dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.supplemental_messages is None:
            self.supplemental_messages = []
