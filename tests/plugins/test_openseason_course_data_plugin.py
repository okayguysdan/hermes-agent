import base64
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


PLUGIN_PATH = (
    Path(__file__).parents[2]
    / "plugins"
    / "openseason_course_data"
    / "__init__.py"
)
DIGEST = "a" * 64


def load_plugin():
    spec = importlib.util.spec_from_file_location(
        "openseason_course_data_plugin_under_test",
        PLUGIN_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_production_requests_use_the_canonical_non_redirecting_origin():
    plugin = load_plugin()
    assert plugin.BASE_URL == "https://www.openseason.golf"


def encoded_context(**overrides):
    value = {
        "schema": "openseason-course-data-task/v1",
        "runId": "run-1",
        "jobId": "job-1",
        "attempt": 1,
        "dispatchKey": "course-data:job-1:1",
        "profile": "data-entry",
        "profileRelease": "openseason-course-data/v2",
        "skillDigest": DIGEST,
        **overrides,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"OPENSEASON_TASK_CONTEXT={encoded}"


def task(**overrides):
    return SimpleNamespace(
        id="task-1",
        assignee="data-entry",
        idempotency_key="course-data:job-1:1",
        body=encoded_context(),
        **overrides,
    )


def upstream_context():
    identity = {
        "name": "Fox Hollow Golf Club",
        "address": None,
        "city": "American Fork",
        "state": "Utah",
        "county": "Utah",
        "zipCode": "84003",
        "website": "https://foxhollow.example",
    }
    snapshot = {
        "name": "Fox Hollow Golf Club",
        "address": None,
        "city": "American Fork",
        "state": "Utah",
        "county": "Utah",
        "zipCode": "84003",
        "phone": None,
        "website": "https://foxhollow.example",
        "description": None,
        "amenities": None,
        "latitude": 40.3,
        "longitude": -111.7,
        "scorecardData": None,
        "image": None,
        "photos": [],
    }
    return {
        "run": {
            "id": "run-1",
            "jobId": "job-1",
            "courseId": "course-1",
            "attempt": 1,
            "hermesTaskId": "task-1",
            "profile": "data-entry",
            "profileRelease": "openseason-course-data/v2",
            "skillDigest": DIGEST,
            "state": "preflight",
        },
        "job": {"id": "job-1", "courseId": "course-1", "status": "leased"},
        "context": {
            "expectedIdentity": identity,
            "courseSnapshot": snapshot,
            "course": {"id": "course-1", **snapshot},
        },
    }


class FakeKeychain:
    def signing_credentials(self):
        return {"secret": "hmac-secret", "key_id": "worker-key"}

    def lease(self, run_id):
        assert run_id == "run-1"
        return "lease-secret"

    def delete_lease(self, run_id):
        assert run_id == "run-1"


def service(monkeypatch, request=None, task_value=None):
    plugin = load_plugin()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setenv("HERMES_PROFILE", "data-entry")
    calls = []

    def default_request(**kwargs):
        calls.append(kwargs)
        if kwargs["method"] == "GET":
            return upstream_context()
        if kwargs["request_path"].endswith("/heartbeat"):
            return {"ok": True, "run": {"state": kwargs["payload"]["state"]}}
        if kwargs["request_path"].endswith("/course-data-findings"):
            return {
                "ok": True,
                "duplicate": False,
                "finding": {"id": "finding-1", "status": "pending_review"},
            }
        if kwargs["request_path"].endswith("/failure"):
            return {"ok": True, "job": {"status": "queued"}}
        raise AssertionError(kwargs)

    instance = plugin.CourseDataBridge(
        task_resolver=lambda current_id: task_value or task(),
        keychain=FakeKeychain(),
        request=request or default_request,
        now=lambda: "2026-07-30T12:00:00.000Z",
        nonce=lambda: "nonce-1",
        model_name=lambda: "gpt-5.4",
    )
    return plugin, instance, calls


def identity_evidence():
    return {
        "accepted": True,
        "nameMatched": True,
        "stateMatched": True,
        "cityMatched": True,
        "countyMatched": False,
        "zipMatched": True,
        "explicitOtherStates": [],
    }


def sources():
    evidence = identity_evidence()
    return [
        {
            "url": "https://foxhollow.example/about",
            "title": "Fox Hollow Golf Club",
            "publisher": "Fox Hollow Golf Club",
            "publicationDate": None,
            "retrievedAt": "2026-07-30T12:00:00.000Z",
            "contentSha256": hashlib.sha256(b"official source").hexdigest(),
            "excerpt": "Fox Hollow Golf Club in American Fork, Utah.",
            "confidence": 0.95,
            "identity": evidence,
        }
    ]


def test_plugin_registers_only_assigned_run_tools_without_run_selectors():
    plugin = load_plugin()
    registered = []

    class Context:
        def register_tool(self, **kwargs):
            registered.append(kwargs)

    plugin.register(Context())
    assert {entry["name"] for entry in registered} == {
        "openseason_course_context",
        "openseason_course_digest",
        "openseason_course_heartbeat",
        "openseason_course_submit",
        "openseason_course_fail",
    }
    assert {entry["toolset"] for entry in registered} == {"openseason-course-data"}
    for entry in registered:
        properties = entry["schema"]["parameters"].get("properties", {})
        assert "runId" not in properties
        assert "hermesTaskId" not in properties
        assert "leaseToken" not in properties


def test_digest_hashes_the_exact_source_excerpt_without_credentials_or_http(monkeypatch):
    plugin = load_plugin()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setenv("HERMES_PROFILE", "data-entry")

    class ExplodingKeychain:
        def signing_credentials(self):
            raise AssertionError("credentials must not be read")

    bridge = plugin.CourseDataBridge(
        task_resolver=lambda _: task(),
        keychain=ExplodingKeychain(),
        request=lambda **_: pytest.fail("HTTP must not run"),
    )
    excerpt = "Fox Hollow Golf Club in American Fork, Utah."
    assert bridge.digest_excerpt(excerpt) == {
        "contentSha256": hashlib.sha256(excerpt.encode()).hexdigest(),
    }


def test_context_rejects_non_worker_or_wrong_profile_before_keychain_or_http(monkeypatch):
    plugin = load_plugin()
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "data-entry")

    class ExplodingKeychain:
        def signing_credentials(self):
            raise AssertionError("credentials must not be read")

    bridge = plugin.CourseDataBridge(
        task_resolver=lambda _: task(),
        keychain=ExplodingKeychain(),
        request=lambda **_: pytest.fail("HTTP must not run"),
    )
    with pytest.raises(plugin.BridgeError, match="dispatcher-assigned task"):
        bridge.context()

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setenv("HERMES_PROFILE", "default")
    with pytest.raises(plugin.BridgeError, match="data-entry profile"):
        bridge.context()


def test_context_signs_get_for_the_fenced_run_and_never_returns_lease(monkeypatch):
    plugin, bridge, calls = service(monkeypatch)
    result = bridge.context()
    assert result["run"]["id"] == "run-1"
    assert result["context"]["course"]["name"] == "Fox Hollow Golf Club"
    assert "lease-secret" not in json.dumps(result)
    call = calls[0]
    assert call["method"] == "GET"
    assert call["request_path"] == "/api/dataentry/course-data-agent-runs/run-1"
    assert call["idempotency_key"] == "course-data:run:run-1:context:nonce-1"
    body_hash = hashlib.sha256(b"").hexdigest()
    canonical = (
        "GET\n/api/dataentry/course-data-agent-runs/run-1\n"
        "2026-07-30T12:00:00.000Z\nnonce-1\n"
        f"course-data:run:run-1:context:nonce-1\n{body_hash}"
    )
    expected = hmac.new(b"hmac-secret", canonical.encode(), hashlib.sha256).hexdigest()
    assert call["headers"]["x-course-data-signature"] == expected


def test_successive_context_gets_use_fresh_receipts_and_observe_changed_state(monkeypatch):
    plugin = load_plugin()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setenv("HERMES_PROFILE", "data-entry")
    nonces = iter(["receipt-1", "request-1", "receipt-2", "request-2"])
    states = iter(["preflight", "awaiting_review"])
    calls = []
    receipts = {}

    def request(**kwargs):
        calls.append(kwargs)
        key = kwargs["idempotency_key"]
        if key in receipts:
            return receipts[key]
        value = upstream_context()
        value["run"]["state"] = next(states)
        receipts[key] = value
        return value

    bridge = plugin.CourseDataBridge(
        task_resolver=lambda _: task(),
        keychain=FakeKeychain(),
        request=request,
        now=lambda: "2026-07-30T12:00:00.000Z",
        nonce=lambda: next(nonces),
    )
    assert bridge.context()["run"]["state"] == "preflight"
    assert bridge.context()["run"]["state"] == "awaiting_review"
    assert [call["idempotency_key"] for call in calls] == [
        "course-data:run:run-1:context:receipt-1",
        "course-data:run:run-1:context:receipt-2",
    ]


def test_context_defensively_removes_any_upstream_credential_fields(monkeypatch):
    def credential_echo(**kwargs):
        state = upstream_context()
        state["run"]["leaseToken"] = "lease-secret"
        state["job"]["x-course-data-signature"] = "hmac-secret"
        return state

    _, bridge, _ = service(monkeypatch, request=credential_echo)
    result = bridge.context()
    serialized = json.dumps(result)
    assert "lease-secret" not in serialized
    assert "hmac-secret" not in serialized
    assert "leaseToken" not in serialized
    assert "x-course-data-signature" not in serialized


def test_context_scrubs_known_secret_values_even_under_innocuous_nested_keys(monkeypatch):
    def credential_echo(**kwargs):
        state = upstream_context()
        state["context"]["diagnostic"] = {
            "message": "echoed hmac-secret and lease-secret"
        }
        return state

    _, bridge, _ = service(monkeypatch, request=credential_echo)
    result = bridge.context()
    serialized = json.dumps(result)
    assert "hmac-secret" not in serialized
    assert "lease-secret" not in serialized
    assert "[REDACTED]" in serialized


def test_context_scrubs_known_secret_values_from_bridge_errors(monkeypatch):
    plugin = load_plugin()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-1")
    monkeypatch.setenv("HERMES_PROFILE", "data-entry")

    def credential_error(**kwargs):
        raise plugin.BridgeError("server echoed hmac-secret and lease-secret")

    bridge = plugin.CourseDataBridge(
        task_resolver=lambda _: task(),
        keychain=FakeKeychain(),
        request=credential_error,
    )
    with pytest.raises(plugin.BridgeError) as captured:
        bridge.context()
    assert "hmac-secret" not in str(captured.value)
    assert "lease-secret" not in str(captured.value)
    assert "[REDACTED]" in str(captured.value)


def test_heartbeat_uses_only_the_current_task_and_transient_lease(monkeypatch):
    _, bridge, calls = service(monkeypatch)
    result = bridge.heartbeat("researching")
    assert result["run"]["state"] == "researching"
    call = calls[-1]
    assert call["request_path"].endswith("/run-1/heartbeat")
    assert call["payload"] == {
        "leaseToken": "lease-secret",
        "hermesTaskId": "task-1",
        "state": "researching",
    }
    assert "lease-secret" not in json.dumps(result)


def test_submit_injects_immutable_context_and_full_agent_fence(monkeypatch):
    _, bridge, calls = service(monkeypatch)
    result = bridge.submit(
        identity_evidence=identity_evidence(),
        sources=sources(),
        proposals={"phone": "+1 801-555-0100"},
    )
    assert result["finding"]["status"] == "pending_review"
    submission = calls[-1]["payload"]
    assert submission["jobId"] == "job-1"
    assert submission["courseId"] == "course-1"
    assert submission["expectedIdentity"]["name"] == "Fox Hollow Golf Club"
    assert submission["snapshot"]["phone"] is None
    assert submission["agentFence"] == {
        "runId": "run-1",
        "attempt": 1,
        "hermesTaskId": "task-1",
        "profile": "data-entry",
            "profileRelease": "openseason-course-data/v2",
        "skillDigest": DIGEST,
    }
    canonical = json.dumps(
        {
            "courseId": "course-1",
            "sourceHashes": [sources()[0]["contentSha256"]],
            "proposals": {"phone": "+1 801-555-0100"},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert submission["fingerprint"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert submission["leaseToken"] == "lease-secret"
    assert submission["worker"] == {"version": "3.0.0", "codexModel": "gpt-5.4"}
    assert "lease-secret" not in json.dumps(result)


def test_submit_retries_a_lost_accepted_response_with_the_same_receipt_key(monkeypatch):
    calls = []

    def flaky_request(**kwargs):
        calls.append(kwargs)
        if kwargs["method"] == "GET":
            return upstream_context()
        if len([call for call in calls if call["method"] == "POST"]) == 1:
            raise TimeoutError("response lost after accept")
        return {
            "ok": True,
            "duplicate": True,
            "finding": {"id": "finding-1", "status": "pending_review"},
        }

    _, bridge, _ = service(monkeypatch, request=flaky_request)
    result = bridge.submit(
        identity_evidence=identity_evidence(),
        sources=sources(),
        proposals={"phone": "+1 801-555-0100"},
    )
    posts = [call for call in calls if call["method"] == "POST"]
    assert len(posts) == 2
    assert posts[0]["idempotency_key"] == posts[1]["idempotency_key"]
    assert posts[0]["body"] == posts[1]["body"]
    assert result["duplicate"] is True


def test_fail_redacts_secrets_and_injects_the_current_agent_fence(monkeypatch):
    _, bridge, calls = service(monkeypatch)
    result = bridge.fail(
        "upstream failed with lease-secret and hmac-secret",
        retryable=True,
    )
    payload = calls[-1]["payload"]
    assert payload["error"] == "upstream failed with [REDACTED] and [REDACTED]"
    assert payload["agentFence"]["runId"] == "run-1"
    assert payload["agentFence"]["hermesTaskId"] == "task-1"
    assert "lease-secret" not in json.dumps(result)


def test_keychain_cleanup_failure_is_surfaced_for_idempotent_retry(monkeypatch):
    plugin = load_plugin()
    monkeypatch.setattr(
        plugin.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="User interaction is not allowed",
        ),
    )

    with pytest.raises(plugin.BridgeError, match="cleanup failed"):
        plugin.MacKeychain.delete_lease("run-1")
