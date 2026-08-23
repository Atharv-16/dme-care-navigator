from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


Handler = Callable[["Envelope"], Awaitable[Any]]


@dataclass
class Envelope:
    sender: str
    recipient: str
    kind: str
    body: dict[str, Any]
    conversation_id: str | None = None


@dataclass
class LocalBus:
    """In-process agent message bus — no phones, no network."""

    _handlers: dict[str, Handler] = field(default_factory=dict)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def register(self, agent_id: str, handler: Handler) -> None:
        self._handlers[agent_id] = handler

    def agents(self) -> list[str]:
        return sorted(self._handlers)

    async def ask(self, envelope: Envelope) -> Any:
        handler = self._handlers.get(envelope.recipient)
        if handler is None:
            raise KeyError(f"No agent registered: {envelope.recipient}")
        async with self._lock:
            self.transcript.append(
                {
                    "dir": "out",
                    "from": envelope.sender,
                    "to": envelope.recipient,
                    "kind": envelope.kind,
                    "body": envelope.body,
                }
            )
        result = await handler(envelope)
        async with self._lock:
            self.transcript.append(
                {
                    "dir": "in",
                    "from": envelope.recipient,
                    "to": envelope.sender,
                    "kind": f"{envelope.kind}.reply",
                    "body": result if isinstance(result, dict) else {"result": result},
                }
            )
        return result

    async def ask_many(self, envelopes: list[Envelope]) -> list[Any]:
        return await asyncio.gather(*[self.ask(e) for e in envelopes])
