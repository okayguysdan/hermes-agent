"""Cross-process serialization for config.yaml writers."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import time


def _hold_lock(home: str, ready, release) -> None:
    import os
    os.environ["HERMES_HOME"] = home
    from hermes_cli.config import config_write_lock

    with config_write_lock():
        ready.set()
        release.wait(5)


def _measure_lock_wait(home: str, result) -> None:
    import os
    os.environ["HERMES_HOME"] = home
    from hermes_cli.config import config_write_lock

    started = time.monotonic()
    with config_write_lock():
        result.put(time.monotonic() - started)


def test_config_write_lock_serializes_independent_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    result = context.Queue()
    holder = context.Process(target=_hold_lock, args=(str(tmp_path), ready, release))
    waiter = context.Process(target=_measure_lock_wait, args=(str(tmp_path), result))

    holder.start()
    assert ready.wait(5)
    waiter.start()
    time.sleep(0.2)
    assert waiter.is_alive()
    release.set()
    holder.join(5)
    waiter.join(5)

    assert holder.exitcode == 0
    assert waiter.exitcode == 0
    assert result.get(timeout=1) >= 0.15


def test_config_write_lock_is_reentrant_in_one_process(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import config_write_lock

    with config_write_lock():
        with config_write_lock():
            pass


def test_config_snapshot_digest_matches_installer_contract(tmp_path):
    from hermes_cli.config import config_snapshot_digest

    config = tmp_path / "config.yaml"
    config.write_text("plugins: {}\n", encoding="utf-8")
    metadata = config.stat()
    record = {
        "mode": metadata.st_mode,
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "type": "file",
        "value": hashlib.sha256(config.read_bytes()).hexdigest(),
    }
    expected = hashlib.sha256(
        json.dumps(record, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert config_snapshot_digest(config) == expected
