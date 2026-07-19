"""BlueBubbles webhook normalization through the real Office approval plugin."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.bluebubbles import BlueBubblesAdapter


OWNER = "+15551234567"
HOME_GUID = f"iMessage;-;{OWNER}"
MESSAGE_ID = "bluebubbles-message-guid"
DEFAULT_PLUGINS = (
    Path(
        "/Users/macboat/ios-codex-control/integrations/hermes/"
        "out-of-office-approval-plugin/__init__.py"
    ),
    Path(
        "/Users/macboat/ios-codex-control/.worktrees/out-of-office-audits/"
        "integrations/hermes/out-of-office-approval-plugin/__init__.py"
    ),
)


class _Request:
    query = {"password": "test-password"}
    headers = {}

    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    async def read(self):
        return self._raw


def _load_office_plugin():
    override = os.environ.get("HERMES_OOO_APPROVAL_PLUGIN_TEST_PATH")
    path = Path(override) if override else next(
        (candidate for candidate in DEFAULT_PLUGINS if candidate.is_file()),
        DEFAULT_PLUGINS[0],
    )
    if not path.is_file():
        pytest.fail(
            "Office Out of Office plugin fixture is required; set "
            "HERMES_OOO_APPROVAL_PLUGIN_TEST_PATH to its explicit checkout path"
        )
    spec = importlib.util.spec_from_file_location("ooo_plugin_integration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_missing_office_plugin_fixture_fails_instead_of_silently_skipping(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "HERMES_OOO_APPROVAL_PLUGIN_TEST_PATH", str(tmp_path / "missing.py")
    )
    with pytest.raises(pytest.fail.Exception, match="plugin fixture is required"):
        _load_office_plugin()


def _adapter():
    return BlueBubblesAdapter(PlatformConfig(enabled=True, extra={
        "server_url": "http://127.0.0.1:1234",
        "password": "test-password",
        "send_read_receipts": False,
    }))


def _payload(text: str, **overrides):
    record = {
        "guid": MESSAGE_ID,
        "text": text,
        "handle": {"address": OWNER},
        "chatGuid": HOME_GUID,
        "chatIdentifier": OWNER,
        "chats": [{"guid": HOME_GUID}],
        "isFromMe": False,
        "isGroup": False,
        "isForwarded": False,
        "isScheduled": False,
        "dateScheduled": None,
        "attachments": [],
        **overrides,
    }
    return {"type": "new-message", "chatGuid": HOME_GUID, "data": record}


async def _normalize(payload, monkeypatch):
    adapter = _adapter()
    events = []

    async def capture(event):
        events.append(event)

    monkeypatch.setattr(adapter, "handle_message", capture)
    response = await adapter._handle_webhook(_Request(payload))
    for _ in range(10):
        if events:
            break
        await asyncio.sleep(0)
    assert response.status == 200
    assert len(events) == 1
    return events[0]


def _gateway():
    home = SimpleNamespace(
        platform=Platform.BLUEBUBBLES,
        chat_id=HOME_GUID,
        thread_id=None,
    )
    return SimpleNamespace(
        config=SimpleNamespace(get_home_channel=lambda _platform: home),
    )


def test_owner_dm_webhook_normalizes_every_field_the_real_plugin_accepts(monkeypatch):
    plugin = _load_office_plugin()
    event = asyncio.run(_normalize(_payload("Fix android golfer abcdef12"), monkeypatch))
    store = plugin.CapabilityStore(token_factory=lambda: "A" * 43)
    captured = []

    result = plugin.authorize_approval(
        event, _gateway(), store,
        lambda token: captured.append(token) or "Approval queued.",
    )

    assert result == {"action": "respond", "text": "Approval queued."}
    assert event.source.platform is Platform.BLUEBUBBLES
    assert event.source.chat_id == HOME_GUID
    assert event.source.chat_id_alt == OWNER
    assert event.source.chat_type == "dm"
    assert event.source.user_id == OWNER
    assert event.message_id == MESSAGE_ID
    approval = store.redeem(captured[0])
    assert approval["status"] == "authorized"
    assert approval["repoHint"] == "android golfer"
    assert approval["findingHint"] == "abcdef12"
    assert approval["messageId"] == MESSAGE_ID


def test_refused_webhook_metadata_stays_bound_and_never_falls_through(monkeypatch):
    plugin = _load_office_plugin()
    event = asyncio.run(_normalize(
        _payload("Fix android golfer 2", isForwarded=True), monkeypatch,
    ))
    store = plugin.CapabilityStore(token_factory=lambda: "R" * 43)
    captured = []

    result = plugin.authorize_approval(
        event, _gateway(), store,
        lambda token: captured.append(token) or "Approval refused.",
    )

    assert result == {"action": "respond", "text": "Approval refused."}
    refusal = store.redeem(captured[0])
    assert refusal["status"] == "refused"
    assert refusal["messageId"] == MESSAGE_ID
    assert "configured BlueBubbles home DM" in refusal["message"]
