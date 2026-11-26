"""Pairing must not overwrite another configured environment's credential."""
import httpx
import pytest

from talaria.auth_direct import login, logout
from talaria.errors import TalariaError


class Context:
    def __init__(self):
        self.settings = {"environments": {"work-laptop": {"base_url": "https://first.example.test"}}}
    def get_config(self, key, default=None):
        return self.settings.get(key, default)
    def set_config(self, key, value):
        self.settings[key] = value


def test_pairing_collision_is_rejected_before_token_exchange():
    # Arrange.
    ctx = Context()
    store = {"T3CODE_TOKEN_WORK_LAPTOP": "first-fake-token"}
    exchanged = []
    def peer(request):
        if request.url.path == "/.well-known/t3/environment":
            return httpx.Response(200, json={"label": "second"})
        exchanged.append(request.url.path)
        return httpx.Response(200, json={"access_token": "second-fake-token"})
    with httpx.Client(transport=httpx.MockTransport(peer)) as http:
        # Act / Assert: existing credential and host mapping survive intact.
        with pytest.raises(TalariaError, match="collid"):
            login("https://second.example.test/#token=fake-pairing", name="work_laptop",
                  ctx=ctx, store=store, client=http)
    assert exchanged == []
    assert store == {"T3CODE_TOKEN_WORK_LAPTOP": "first-fake-token"}
    assert list(ctx.settings["environments"]) == ["work-laptop"]


def test_logout_ambiguous_legacy_config_preserves_shared_secret():
    # Arrange: already configured by an older plugin version.
    ctx = Context()
    ctx.settings["environments"]["work_laptop"] = {"base_url": "https://second.example.test"}
    store = {"T3CODE_TOKEN_WORK_LAPTOP": "fake-token"}
    # Act / Assert.
    with pytest.raises(TalariaError, match="collid"):
        logout("work-laptop", ctx=ctx, store=store)
    assert store == {"T3CODE_TOKEN_WORK_LAPTOP": "fake-token"}
