"""Deployment proxy contract; no network, real accounts or public proxies."""
import logging
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
from python_socks import ProxyError
from telethon.crypto import AuthKey
from telethon.network.connection.connection import Connection
from telethon.sessions import StringSession

from app.adapters.base import ProviderError
from app.adapters.telegram import TelegramAdapter, _deployment_proxy


@pytest.fixture(autouse=True)
def clean_route(monkeypatch):
    monkeypatch.delenv("TELEGRAM_PROXY_URL", raising=False)


def config():
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(b"x" * 256)
    return {"api_id": 12345, "api_hash": "a" * 32, "session": session.save(),
            "telegram_bound_user_id": 42, "tg_self_id": 42,
            "tg_cursor": {"pts": 7}, "tg_identity": {"display_name": "Manager"}}


def fake_client(monkeypatch, error=None):
    client = Mock()
    client.is_connected.return_value = False
    client.connect = AsyncMock(side_effect=error)
    client.disconnect = AsyncMock()
    factory = Mock(return_value=client)
    monkeypatch.setattr("app.adapters.telegram.TelegramClient", factory)
    return client, factory


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, ""])
async def test_unset_or_empty_retains_direct_and_ignores_account_proxy(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("TELEGRAM_PROXY_URL", value)
    client, factory = fake_client(monkeypatch)
    saved = config() | {"proxy": {"addr": "untrusted.invalid"},
                        "TELEGRAM_PROXY_URL": "http://untrusted.invalid:80"}
    await TelegramAdapter(saved).start()
    assert factory.call_args.kwargs["proxy"] is None
    client.connect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("url,expected", [
    ("socks5://proxy.example:1080", {"proxy_type": "socks5", "addr": "proxy.example", "port": 1080,
                                    "rdns": True, "username": None, "password": None}),
    ("http://user%40name:p%3Ass%20word@proxy.example:3128", {"proxy_type": "http", "addr": "proxy.example", "port": 3128,
                                                          "rdns": True, "username": "user@name", "password": "p:ss word"}),
    ("socks5://[2001:db8::1]:1080", {"proxy_type": "socks5", "addr": "2001:db8::1", "port": 1080,
                                    "rdns": True, "username": None, "password": None}),
])
async def test_route_constructed_without_changing_saved_session_or_identity(monkeypatch, url, expected):
    monkeypatch.setenv("TELEGRAM_PROXY_URL", url)
    client, factory = fake_client(monkeypatch)
    saved = config()
    before = deepcopy(saved)
    await TelegramAdapter(saved).start()
    assert saved == before
    assert factory.call_args.kwargs["proxy"] == expected
    session = factory.call_args.args[0]
    assert session.dc_id == 2 and session.server_address == "149.154.167.51" and session.port == 443
    assert session.save() == before["session"]
    client.connect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    " ", "https://proxy.example:443", "socks4://proxy.example:1080", "proxy.example:1080",
    "socks5://proxy.example", "socks5://proxy.example:0", "http://proxy.example:65536",
    "http://proxy.example:abc", "http://proxy.example:80/", "http://proxy.example:80/path",
    "http://proxy.example:80?", "http://proxy.example:80?secret=1", "http://proxy.example:80#",
    "http://proxy.example:80#secret", "http://user@proxy.example:80", "http://:pass@proxy.example:80",
    "http://user:@proxy.example:80", "http://user:pa%ss@proxy.example:80",
    "http://user:p%0Ass@proxy.example:80", "http://user:p%FFss@proxy.example:80",
    "http://user:p%C2%85ss@proxy.example:80", "http://user:p\u0085ss@proxy.example:80",
    "http://proxy.exa\nmple:80", "http://user:pass@extra@proxy.example:80",
    "http://bad_host:80", "http://[invalid]:80", "http://[::1]extra:80",
    "http://proxy.example..:80", "http://" + "x" * 2050 + ":80",
])
async def test_bad_configuration_is_sanitized_and_never_constructs_client(monkeypatch, url):
    monkeypatch.setenv("TELEGRAM_PROXY_URL", url)
    client, factory = fake_client(monkeypatch)
    with pytest.raises(ProviderError) as raised:
        await TelegramAdapter(config()).start()
    assert str(raised.value) == "telegram_proxy_configuration_invalid"
    assert raised.value.__suppress_context__
    factory.assert_not_called()
    client.connect.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ProxyError("SECRET_PROXY_PASSWORD"), OSError("SECRET_PROXY_PASSWORD"), TimeoutError("SECRET_PROXY_PASSWORD")])
async def test_proxy_failure_has_no_direct_fallback_or_credential_mutation(monkeypatch, error):
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "socks5://user:SECRET_PROXY_PASSWORD@proxy.example:1080")
    client, factory = fake_client(monkeypatch, error)
    saved = config()
    before = deepcopy(saved)
    adapter = TelegramAdapter(saved)
    for _ in range(2):
        with pytest.raises(ProviderError) as raised:
            await adapter.start()
        assert str(raised.value) == "telegram_transport_error"
        assert raised.value.__suppress_context__
    assert saved == before
    factory.assert_called_once()
    assert factory.call_args.kwargs["proxy"]["addr"] == "proxy.example"
    assert client.connect.await_count == 2
    client.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_telethon_child_logs_cannot_expose_raw_proxy_or_auth_errors(monkeypatch, caplog):
    _, factory = fake_client(monkeypatch)
    await TelegramAdapter(config()).start()
    logger = factory.call_args.kwargs["base_logger"]
    with caplog.at_level(logging.DEBUG):
        logger.getChild("network.mtprotosender").warning("Connection failed: %s", "SECRET_PROXY_PASSWORD")
        logger.getChild("client.auth").error("SECRET_LOGIN_CODE", exc_info=True)
    assert "SECRET" not in caplog.text
    assert logger.propagate is False


@pytest.mark.parametrize("scheme", ["socks5", "http"])
def test_real_telethon_accepts_proxy_dictionary(monkeypatch, scheme):
    from python_socks import ProxyType
    monkeypatch.setenv("TELEGRAM_PROXY_URL", f"{scheme}://user:pass@proxy.example:1080")
    protocol, host, port, username, password, rdns = Connection._parse_proxy(**_deployment_proxy())
    assert protocol == (ProxyType.SOCKS5 if scheme == "socks5" else ProxyType.HTTP)
    assert (host, port, username, password, rdns) == ("proxy.example", 1080, "user", "pass", True)
