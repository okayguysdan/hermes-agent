"""Cross-process serialization for config.yaml writers."""

from __future__ import annotations

import hashlib
import json
import multiprocessing


def _hold_lock(home: str, ready, release) -> None:
    import os
    os.environ["HERMES_HOME"] = home
    from hermes_cli.config import config_write_lock

    with config_write_lock():
        ready.set()
        release.wait(5)


def _wait_for_lock(home: str, attempted, acquired) -> None:
    import os
    os.environ["HERMES_HOME"] = home
    from hermes_cli.config import config_write_lock

    attempted.set()
    with config_write_lock():
        acquired.set()


def _stale_load_then_save(home: str, loaded, proceed, result) -> None:
    import os
    os.environ["HERMES_HOME"] = home
    from hermes_cli.config import ConfigConflictError, load_config, save_config

    config = load_config()
    loaded.set()
    proceed.wait(5)
    config["stale_writer"] = True
    try:
        save_config(config)
    except ConfigConflictError:
        result.put("conflict")
    else:
        result.put("overwritten")


def _fresh_load_and_save(home: str, loaded, saved) -> None:
    import os
    os.environ["HERMES_HOME"] = home
    from hermes_cli.config import load_config, save_config

    loaded.wait(5)
    config = load_config()
    config["fresh_writer"] = True
    save_config(config)
    saved.set()


def test_config_write_lock_serializes_independent_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    attempted = context.Event()
    acquired = context.Event()
    holder = context.Process(target=_hold_lock, args=(str(tmp_path), ready, release))
    waiter = context.Process(target=_wait_for_lock, args=(str(tmp_path), attempted, acquired))

    holder.start()
    assert ready.wait(5)
    waiter.start()
    assert attempted.wait(5)
    assert not acquired.wait(0.2)
    release.set()
    assert acquired.wait(5)
    holder.join(5)
    waiter.join(5)

    assert holder.exitcode == 0
    assert waiter.exitcode == 0


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


def test_stale_load_cannot_overwrite_a_later_process_save(tmp_path):
    import yaml

    (tmp_path / "config.yaml").write_text("original: true\n", encoding="utf-8")
    context = multiprocessing.get_context("spawn")
    loaded = context.Event()
    proceed = context.Event()
    saved = context.Event()
    result = context.Queue()
    stale = context.Process(
        target=_stale_load_then_save,
        args=(str(tmp_path), loaded, proceed, result),
    )
    fresh = context.Process(
        target=_fresh_load_and_save,
        args=(str(tmp_path), loaded, saved),
    )

    stale.start()
    fresh.start()
    assert saved.wait(10)
    proceed.set()
    stale.join(10)
    fresh.join(10)

    assert stale.exitcode == 0
    assert fresh.exitcode == 0
    assert result.get(timeout=1) == "conflict"
    persisted = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert persisted["fresh_writer"] is True
    assert "stale_writer" not in persisted


def test_loaded_config_remains_save_compatible_and_replacement_is_explicit(
    tmp_path, monkeypatch,
):
    import pytest
    import yaml
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import (
        ConfigVersionRequiredError,
        load_config,
        save_config,
        save_config_replacement,
    )

    (tmp_path / "config.yaml").write_text("original: true\n", encoding="utf-8")
    config = load_config()
    config["compatible"] = True
    save_config(config)
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["compatible"] is True

    with pytest.raises(ConfigVersionRequiredError):
        save_config({"replacement": True})
    save_config_replacement({"replacement": True})
    assert yaml.safe_load((tmp_path / "config.yaml").read_text())["replacement"] is True


def test_compare_restore_is_atomic_and_preserves_conflicting_writer(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import (
        ConfigConflictError,
        compare_restore_config,
        config_snapshot_digest,
    )

    config = tmp_path / "config.yaml"
    backup = tmp_path / "baseline.yaml"
    backup.write_text("plugins:\n  enabled: [baseline]\n", encoding="utf-8")
    config.write_text("plugins:\n  enabled: [installer]\n", encoding="utf-8")
    expected = config_snapshot_digest(config)

    assert compare_restore_config(expected, backup) == "restored"
    assert config.read_bytes() == backup.read_bytes()

    config.write_text("plugins:\n  enabled: [concurrent-writer]\n", encoding="utf-8")
    with pytest.raises(ConfigConflictError):
        compare_restore_config(expected, backup)
    assert config.read_text(encoding="utf-8") == "plugins:\n  enabled: [concurrent-writer]\n"

    absent_expected = config_snapshot_digest(config)
    assert compare_restore_config(
        absent_expected, backup_absent=True,
    ) == "restored"
    assert not config.exists()


def test_compare_restore_cli_reports_success_and_conflict(
    tmp_path, monkeypatch, capsys,
):
    from types import SimpleNamespace
    import pytest
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.config import config_command, config_snapshot_digest

    config = tmp_path / "config.yaml"
    backup = tmp_path / "baseline.yaml"
    backup.write_text("baseline: true\n", encoding="utf-8")
    config.write_text("installer: true\n", encoding="utf-8")
    expected = config_snapshot_digest(config)
    args = SimpleNamespace(
        config_command="compare-restore",
        expected_digest=expected,
        backup_path=str(backup),
        backup_absent=False,
    )

    config_command(args)
    assert capsys.readouterr().out == "config-restore:restored\n"

    config.write_text("concurrent: true\n", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        config_command(args)
    assert error.value.code == 3
    assert "manual inspection" in capsys.readouterr().err
    assert config.read_text(encoding="utf-8") == "concurrent: true\n"
