from .base import Adapter, AuthResult, ProviderError


class DemoAdapter(Adapter):
    async def auth(self, action, payload):
        if action == "disconnect":
            return AuthResult("disconnected")
        if action in {"login", "sync"}:
            return AuthResult("connected", {"notice": "Локальный демонстрационный канал"})
        raise ProviderError("unsupported_action")

    async def send(self, peer, text, client_id):
        return "demo:" + client_id
