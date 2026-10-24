"""Provider-neutral chat types and the streaming interface every adapter implements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ToolCall:
        return cls(id=data["id"], name=data["name"], arguments=data.get("arguments") or {})


@dataclass
class ImagePart:
    mime: str
    data_b64: str


@dataclass
class ChatMessage:
    role: str  # user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    images: list[ImagePart] = field(default_factory=list)  # user messages only


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCallEvent:
    call: ToolCall


@dataclass
class UsageEvent:
    input_tokens: int
    output_tokens: int
    cost: float | None = None  # vendor-reported cost, when available (OpenRouter)
    estimated: bool = False


@dataclass
class Done:
    stop_reason: str | None = None


StreamEvent = TextDelta | ToolCallEvent | UsageEvent | Done


class ProviderError(Exception):
    pass


class Provider(ABC):
    def __init__(self, *, api_key: str | None, base_url: str | None) -> None:
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    def stream(
        self,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
    ) -> AsyncIterator[StreamEvent]: ...

    async def list_models(self) -> list[dict[str, Any]]:
        """The vendor's catalog: [{"id", "name"?, "input_price"?, "output_price"?, "supports_images"?,
        "supports_tools"?, "chat"?}]. Empty if the vendor has no listing."""
        return []

    async def check_key(self) -> None:
        """Raise ProviderError if the API key is rejected (default: listing models is enough)."""
        return None


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0
