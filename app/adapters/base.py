"""Provider boundary. No database or HTTP API dependencies in adapters."""
from dataclasses import dataclass, field
from typing import Any


class ProviderError(Exception):
    def __init__(self, code: str, *, retry_after: int | None = None, uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.uncertain = uncertain


@dataclass
class Event:
    provider_id: str
    peer: str
    text: str
    direction: str = "inbound"
    timestamp: float | None = None
    peer_name: str | None = None
    status: str = "received"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuthResult:
    state: str
    public: dict[str, Any] = field(default_factory=dict)


class Adapter:
    """config is decrypted private persistent state; mutations are saved by worker.

    poll returns replayable events; worker persists config only after event commit.
    send receives a stable UUID client_id; providers that support idempotency must
    derive their protocol nonce from it. No ambiguous send is automatically retried.
    """
    def __init__(self, config: dict[str, Any]):
        self.config = config

    async def start(self) -> None:
        pass

    async def auth(self, action: str, payload: dict[str, Any]) -> AuthResult:
        raise ProviderError("unsupported_action")

    async def send(self, peer: str, text: str, client_id: str) -> str:
        raise ProviderError("unsupported_send")

    async def poll(self) -> list[Event]:
        return []

    async def close(self) -> None:
        pass
