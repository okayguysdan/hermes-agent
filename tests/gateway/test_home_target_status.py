"""Redacted BlueBubbles home-target readiness status tests."""

from unittest.mock import patch

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.run import GatewayRunner


def _runner_with_home(chat_id: str | None) -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    home = None
    if chat_id is not None:
        home = HomeChannel(
            platform=Platform.BLUEBUBBLES,
            chat_id=chat_id,
            name="SENTINEL_PRIVATE_HOME_NAME",
            thread_id="SENTINEL_PRIVATE_THREAD",
        )
    runner.config = GatewayConfig(platforms={
        Platform.BLUEBUBBLES: PlatformConfig(enabled=True, home_channel=home),
    })
    return runner


def test_connected_bluebubbles_with_nonblank_home_is_ready_without_serializing_target():
    runner = _runner_with_home("SENTINEL_PRIVATE_HOME_TARGET")

    with patch("gateway.status.write_runtime_status") as writer:
        runner._update_platform_runtime_status(
            "bluebubbles",
            platform_state="connected",
            error_code=None,
            error_message=None,
        )

    kwargs = writer.call_args.kwargs
    assert kwargs["home_target_ready"] is True
    serialized_call = repr(writer.call_args)
    assert "SENTINEL_PRIVATE_HOME_TARGET" not in serialized_call
    assert "SENTINEL_PRIVATE_HOME_NAME" not in serialized_call
    assert "SENTINEL_PRIVATE_THREAD" not in serialized_call


def test_connected_bluebubbles_without_home_is_not_ready():
    runner = _runner_with_home(None)

    with patch("gateway.status.write_runtime_status") as writer:
        runner._update_platform_runtime_status(
            "bluebubbles",
            platform_state="connected",
        )

    assert writer.call_args.kwargs["home_target_ready"] is False


def test_connected_bluebubbles_with_blank_home_is_not_ready():
    runner = _runner_with_home("   ")

    with patch("gateway.status.write_runtime_status") as writer:
        runner._update_platform_runtime_status(
            "bluebubbles",
            platform_state="connected",
        )

    assert writer.call_args.kwargs["home_target_ready"] is False


def test_non_bluebubbles_status_does_not_add_home_readiness_fields():
    runner = _runner_with_home("SENTINEL_PRIVATE_HOME_TARGET")

    with patch("gateway.status.write_runtime_status") as writer:
        runner._update_platform_runtime_status("telegram", platform_state="connected")

    assert "home_target_ready" not in writer.call_args.kwargs
