"""Task-scoped OpenSeason course-data bridge.

The model can operate only on the run bound to the dispatcher-owned Kanban
task. HMAC credentials and lease tokens are read from macOS Keychain inside
the tool implementation and never enter prompts, task records, argv, files,
or environment variables.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Dict, Optional


WORKER_VERSION = "3.0.0"
BASE_URL = "https://openseason.golf"
SIGNING_SERVICE = "com.openseason.course-data.worker"
LEASE_SERVICE = "com.openseason.course-data.lease"
TOOLSET = "openseason-course-data"
class BridgeError(RuntimeError):
    """Fail-closed error suitable for returning through the tool boundary."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _stable_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _redact_text(value: Any, secrets: tuple[str, ...] = ()) -> str:
    output = str(value)
    for secret in secrets:
        if secret:
            output = output.replace(secret, "[REDACTED]")
    return output


def _public_result(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Remove credential-shaped fields before any value reaches the model."""
    if isinstance(value, list):
        return [_public_result(item, secrets) for item in value]
    if isinstance(value, str):
        return _redact_text(value, secrets)
    if not isinstance(value, dict):
        return value
    blocked = {
        "authorization",
        "hmacsecret",
        "leasetoken",
        "password",
        "privatekey",
        "signature",
        "xcoursedatasignature",
    }
    return {
        key: _public_result(item, secrets)
        for key, item in value.items()
        if "".join(character for character in str(key).lower() if character.isalnum())
        not in blocked
    }


def _signed_headers(
    *,
    method: str,
    request_path: str,
    body: str,
    idempotency_key: str,
    secret: str,
    key_id: str,
    timestamp: str,
    nonce: str,
) -> Dict[str, str]:
    body_hash = _sha256(body)
    canonical = "\n".join(
        [
            method.upper(),
            request_path,
            timestamp,
            nonce,
            idempotency_key,
            body_hash,
        ]
    )
    headers = {
        "x-course-data-timestamp": timestamp,
        "x-course-data-nonce": nonce,
        "x-course-data-body-sha256": body_hash,
        "x-course-data-signature": hmac.new(
            secret.encode(), canonical.encode(), hashlib.sha256
        ).hexdigest(),
        "x-course-data-key-id": key_id,
        "idempotency-key": idempotency_key,
    }
    if method.upper() == "POST":
        headers["content-type"] = "application/json"
    return headers


class MacKeychain:
    """Minimal Keychain reader; secrets are returned on stdout, never argv."""

    @staticmethod
    def _get(service: str, account: str) -> str:
        completed = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                "-w",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise BridgeError(f"Required Keychain item is unavailable: {service}/{account}")
        value = completed.stdout.rstrip("\r\n")
        if not value:
            raise BridgeError(f"Required Keychain item is empty: {service}/{account}")
        return value

    def signing_credentials(self) -> Dict[str, str]:
        return {
            "secret": self._get(SIGNING_SERVICE, "hmac-secret"),
            "key_id": self._get(SIGNING_SERVICE, "key-id"),
        }

    def lease(self, run_id: str) -> str:
        return self._get(LEASE_SERVICE, run_id)

    @staticmethod
    def delete_lease(run_id: str) -> None:
        completed = subprocess.run(
            [
                "security",
                "delete-generic-password",
                "-s",
                LEASE_SERVICE,
                "-a",
                run_id,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 and "could not be found" not in completed.stderr:
            raise BridgeError(
                "Transient Keychain lease cleanup failed; retry the same operation",
                retryable=True,
            )


def _default_task_resolver(task_id: str) -> Any:
    from hermes_cli import kanban_db

    connection = kanban_db.connect()
    try:
        current = kanban_db.get_task(connection, task_id)
    finally:
        connection.close()
    if current is None:
        raise BridgeError("The dispatcher-assigned Kanban task no longer exists")
    return current


def _default_request(
    *,
    method: str,
    base_url: str,
    request_path: str,
    body: str,
    headers: Dict[str, str],
    **_: Any,
) -> Any:
    request = urllib.request.Request(
        f"{base_url}{request_path}",
        data=body.encode() if method == "POST" else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as error:
        raw = error.read().decode(errors="replace")
        try:
            detail = json.loads(raw)
        except json.JSONDecodeError:
            detail = {}
        retryable = error.code in {408, 425, 429, 500, 502, 503, 504}
        raise BridgeError(
            f"OpenSeason returned HTTP {error.code}: "
            f"{str(detail.get('error') or detail.get('reason') or '')[:300]}",
            retryable=retryable,
        ) from None
    except (TimeoutError, urllib.error.URLError) as error:
        raise BridgeError("OpenSeason request failed transiently", retryable=True) from error
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as error:
        raise BridgeError("OpenSeason returned malformed JSON") from error


def _task_field(task: Any, *names: str) -> Any:
    for name in names:
        if isinstance(task, dict) and name in task:
            return task[name]
        if hasattr(task, name):
            return getattr(task, name)
    return None


def _decode_task_context(description: str) -> Dict[str, Any]:
    marker = "OPENSEASON_TASK_CONTEXT="
    encoded = next(
        (
            line[len(marker) :].strip()
            for line in str(description or "").splitlines()
            if line.startswith(marker)
        ),
        "",
    )
    if not encoded:
        raise BridgeError("The assigned task has no OpenSeason run context")
    try:
        padded = encoded + ("=" * ((4 - len(encoded) % 4) % 4))
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BridgeError("The assigned task has malformed OpenSeason run context") from error
    if not isinstance(value, dict):
        raise BridgeError("The assigned task has malformed OpenSeason run context")
    return value


class CourseDataBridge:
    def __init__(
        self,
        *,
        task_resolver: Callable[[str], Any] = _default_task_resolver,
        keychain: Any = None,
        request: Callable[..., Any] = _default_request,
        base_url: str = BASE_URL,
        now: Callable[[], str] = lambda: time.strftime(
            "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()
        ),
        nonce: Callable[[], str] = lambda: str(uuid.uuid4()),
        model_name: Callable[[], str] = lambda: os.environ.get(
            "HERMES_MODEL", "hermes-agent"
        ),
    ):
        self._task_resolver = task_resolver
        self._keychain = keychain or MacKeychain()
        self._request = request
        self._base_url = base_url.rstrip("/")
        self._now = now
        self._nonce = nonce
        self._model_name = model_name
        self._cached_context: Optional[Dict[str, Any]] = None

    def _assigned(self) -> Dict[str, Any]:
        task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
        if not task_id:
            raise BridgeError("This tool requires a dispatcher-assigned task")
        if os.environ.get("HERMES_PROFILE", "").strip() != "data-entry":
            raise BridgeError("This tool is restricted to the data-entry profile")
        task = self._task_resolver(task_id)
        if _task_field(task, "id") != task_id:
            raise BridgeError("Resolved task does not match the dispatcher assignment")
        if _task_field(task, "assignee", "assigned_to", "profile") != "data-entry":
            raise BridgeError("Assigned task does not belong to the data-entry profile")
        value = _decode_task_context(_task_field(task, "body", "description") or "")
        required = {
            "schema",
            "runId",
            "jobId",
            "attempt",
            "dispatchKey",
            "profile",
            "profileRelease",
            "skillDigest",
        }
        if set(value) != required:
            raise BridgeError("Assigned task context has unexpected fields")
        expected_key = f"course-data:{value.get('jobId')}:{value.get('attempt')}"
        if (
            value.get("schema") != "openseason-course-data-task/v1"
            or value.get("dispatchKey") != expected_key
            or _task_field(task, "idempotency_key") != expected_key
            or value.get("profile") != "data-entry"
            or value.get("profileRelease") != "openseason-course-data/v1"
            or not isinstance(value.get("skillDigest"), str)
            or len(value["skillDigest"]) != 64
        ):
            raise BridgeError("Assigned task context failed its identity fence")
        value["hermesTaskId"] = task_id
        return value

    def _call(
        self,
        *,
        method: str,
        request_path: str,
        payload: Optional[Dict[str, Any]],
        idempotency_key: str,
        secret_values: tuple[str, ...] = (),
    ) -> Any:
        credentials = self._keychain.signing_credentials()
        secrets = tuple(
            value
            for value in (credentials["secret"], *secret_values)
            if isinstance(value, str) and value
        )
        body = "" if method == "GET" else json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False
        )
        last_error: Optional[Exception] = None
        for attempt in range(3):
            timestamp = self._now()
            nonce = self._nonce()
            try:
                result = self._request(
                    method=method,
                    base_url=self._base_url,
                    request_path=request_path,
                    payload=payload,
                    body=body,
                    idempotency_key=idempotency_key,
                    headers=_signed_headers(
                        method=method,
                        request_path=request_path,
                        body=body,
                        idempotency_key=idempotency_key,
                        secret=credentials["secret"],
                        key_id=credentials["key_id"],
                        timestamp=timestamp,
                        nonce=nonce,
                    ),
                )
                return _public_result(result, secrets)
            except (TimeoutError, urllib.error.URLError) as error:
                last_error = error
            except BridgeError as error:
                last_error = error
                if not error.retryable:
                    raise BridgeError(
                        _redact_text(error, secrets),
                        retryable=False,
                    ) from None
            if attempt == 2:
                break
        raise BridgeError("OpenSeason request failed after retries", retryable=True) from last_error

    @staticmethod
    def _verify_upstream(assigned: Dict[str, Any], state: Dict[str, Any]) -> None:
        run = state.get("run") if isinstance(state, dict) else None
        job = state.get("job") if isinstance(state, dict) else None
        context = state.get("context") if isinstance(state, dict) else None
        if not all(isinstance(value, dict) for value in (run, job, context)):
            raise BridgeError("OpenSeason returned incomplete assigned-run context")
        comparisons = {
            "id": assigned["runId"],
            "jobId": assigned["jobId"],
            "attempt": assigned["attempt"],
            "hermesTaskId": assigned["hermesTaskId"],
            "profile": assigned["profile"],
            "profileRelease": assigned["profileRelease"],
            "skillDigest": assigned["skillDigest"],
        }
        if any(run.get(key) != value for key, value in comparisons.items()):
            raise BridgeError("OpenSeason run no longer matches the assigned task fence")
        if job.get("id") != assigned["jobId"] or job.get("courseId") != run.get("courseId"):
            raise BridgeError("OpenSeason job no longer matches the assigned task fence")
        if not isinstance(context.get("expectedIdentity"), dict) or not isinstance(
            context.get("courseSnapshot"), dict
        ):
            raise BridgeError("OpenSeason omitted immutable course context")

    def context(self) -> Dict[str, Any]:
        assigned = self._assigned()
        lease = self._keychain.lease(assigned["runId"])
        receipt_nonce = self._nonce()
        state = self._call(
            method="GET",
            request_path=f"/api/dataentry/course-data-agent-runs/{assigned['runId']}",
            payload=None,
            idempotency_key=(
                f"course-data:run:{assigned['runId']}:context:{receipt_nonce}"
            ),
            secret_values=(lease,),
        )
        self._verify_upstream(assigned, state)
        public_state = _public_result(state)
        self._cached_context = public_state
        return public_state

    def _state(self) -> tuple[Dict[str, Any], Dict[str, Any]]:
        assigned = self._assigned()
        state = self._cached_context or self.context()
        self._verify_upstream(assigned, state)
        return assigned, state

    def heartbeat(self, state: str) -> Dict[str, Any]:
        if state not in {"preflight", "researching", "submitting"}:
            raise BridgeError("Heartbeat state must be preflight, researching, or submitting")
        assigned, _ = self._state()
        lease = self._keychain.lease(assigned["runId"])
        result = self._call(
            method="POST",
            request_path=(
                f"/api/dataentry/course-data-agent-runs/{assigned['runId']}/heartbeat"
            ),
            payload={
                "leaseToken": lease,
                "hermesTaskId": assigned["hermesTaskId"],
                "state": state,
            },
            idempotency_key=(
                f"{assigned['dispatchKey']}:heartbeat:{state}:{self._now()}"
            ),
            secret_values=(lease,),
        )
        return _public_result(result)

    @staticmethod
    def _agent_fence(assigned: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "runId": assigned["runId"],
            "attempt": assigned["attempt"],
            "hermesTaskId": assigned["hermesTaskId"],
            "profile": assigned["profile"],
            "profileRelease": assigned["profileRelease"],
            "skillDigest": assigned["skillDigest"],
        }

    def submit(
        self,
        *,
        identity_evidence: Dict[str, Any],
        sources: list[Dict[str, Any]],
        proposals: Dict[str, Any],
    ) -> Dict[str, Any]:
        assigned, state = self._state()
        lease = self._keychain.lease(assigned["runId"])
        context = state["context"]
        course_id = state["run"]["courseId"]
        source_hashes = sorted(
            str(source.get("contentSha256", "")).lower() for source in sources
        )
        fingerprint = _sha256(
            _stable_json(
                {
                    "courseId": course_id,
                    "sourceHashes": source_hashes,
                    "proposals": proposals,
                }
            )
        )
        payload = {
            "jobId": assigned["jobId"],
            "courseId": course_id,
            "leaseToken": lease,
            "agentFence": self._agent_fence(assigned),
            "expectedIdentity": context["expectedIdentity"],
            "identityEvidence": identity_evidence,
            "sources": sources,
            "proposals": proposals,
            "snapshot": context["courseSnapshot"],
            "fingerprint": fingerprint,
            "worker": {
                "version": WORKER_VERSION,
                "codexModel": self._model_name(),
            },
        }
        result = self._call(
            method="POST",
            request_path="/api/dataentry/course-data-findings",
            payload=payload,
            idempotency_key=f"{assigned['dispatchKey']}:submit",
            secret_values=(lease,),
        )
        self._keychain.delete_lease(assigned["runId"])
        return _public_result(result)

    def fail(self, error: str, retryable: bool) -> Dict[str, Any]:
        assigned, _ = self._state()
        lease = self._keychain.lease(assigned["runId"])
        credentials = self._keychain.signing_credentials()
        message = str(error)
        for secret in (lease, credentials["secret"]):
            message = message.replace(secret, "[REDACTED]")
        result = self._call(
            method="POST",
            request_path=(
                f"/api/dataentry/course-data-jobs/{assigned['jobId']}/failure"
            ),
            payload={
                "error": message[:2000],
                "retryable": bool(retryable),
                "leaseToken": lease,
                "agentFence": self._agent_fence(assigned),
                "worker": {"version": WORKER_VERSION},
            },
            idempotency_key=f"{assigned['dispatchKey']}:failure",
            secret_values=(lease,),
        )
        self._keychain.delete_lease(assigned["runId"])
        return _public_result(result)


def _tool_result(callback: Callable[[], Any]) -> str:
    try:
        return json.dumps({"success": True, "data": callback()})
    except BridgeError as error:
        return json.dumps(
            {
                "success": False,
                "error": str(error),
                "retryable": error.retryable,
            }
        )


def register(ctx: Any) -> None:
    bridge = CourseDataBridge()
    tools = [
        (
            "openseason_course_context",
            "Fetch the immutable OpenSeason context for this assigned course-data run.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            lambda args, **kwargs: _tool_result(bridge.context),
        ),
        (
            "openseason_course_heartbeat",
            "Renew this assigned run's lease while researching or submitting.",
            {
                "type": "object",
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": ["preflight", "researching", "submitting"],
                    }
                },
                "required": ["state"],
                "additionalProperties": False,
            },
            lambda args, **kwargs: _tool_result(
                lambda: bridge.heartbeat(args.get("state", ""))
            ),
        ),
        (
            "openseason_course_submit",
            "Submit evidence and allowlisted proposals for manual OpenSeason review.",
            {
                "type": "object",
                "properties": {
                    "identityEvidence": {"type": "object"},
                    "sources": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 5,
                        "items": {"type": "object"},
                    },
                    "proposals": {"type": "object", "minProperties": 1},
                },
                "required": ["identityEvidence", "sources", "proposals"],
                "additionalProperties": False,
            },
            lambda args, **kwargs: _tool_result(
                lambda: bridge.submit(
                    identity_evidence=args.get("identityEvidence"),
                    sources=args.get("sources"),
                    proposals=args.get("proposals"),
                )
            ),
        ),
        (
            "openseason_course_fail",
            "Report a structured failure for this assigned run.",
            {
                "type": "object",
                "properties": {
                    "error": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "retryable": {"type": "boolean"},
                },
                "required": ["error", "retryable"],
                "additionalProperties": False,
            },
            lambda args, **kwargs: _tool_result(
                lambda: bridge.fail(
                    str(args.get("error", "")),
                    bool(args.get("retryable")),
                )
            ),
        ),
    ]
    for name, description, parameters, handler in tools:
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema={
                "name": name,
                "description": description,
                "parameters": parameters,
            },
            handler=handler,
            description=description,
        )


__all__ = [
    "BridgeError",
    "CourseDataBridge",
    "MacKeychain",
    "register",
]
