import os
import shlex
import sys
import time
from pathlib import Path

import pytest

from orchestrator import OrchestratorProcess


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not observed before the deadline")


@pytest.fixture
def process(monkeypatch):
    monkeypatch.setenv("CLAUDE_ENVIRONMENT_SECRET", "HOST_SECRET")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "DEPLOYMENT_TOKEN")
    process = OrchestratorProcess()
    yield process
    process.stop()
    assert not process._thread.is_alive()
    assert not Path(process.scratch.name).exists()


def test_private_secret_and_native_hook(process):
    command = process._command
    secret = Path(command[command.index("--environment-secret-file") + 1])
    assert secret.read_text() == "HOST_SECRET"
    assert secret.stat().st_mode & 0o777 == 0o600
    assert secret.parent.stat().st_mode & 0o777 == 0o700
    hook = secret.parent / "hooks" / "spawn-runner"
    assert hook.stat().st_mode & 0o777 == 0o700
    hook_command = shlex.split(hook.read_text().splitlines()[1])
    assert hook_command == [
        "exec",
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "src/spawn_runner.py"),
    ]
    assert "HOST_SECRET" not in " ".join(command)
    assert "CLAUDE_ENVIRONMENT_SECRET" not in process._env
    assert process._env["MODAL_TOKEN_SECRET"] == "DEPLOYMENT_TOKEN"


def test_restarts_an_exited_child_with_inherited_output(process, tmp_path, capfd):
    starts = tmp_path / "starts"
    code = (
        "import os, sys; print('child stdout'); print('child stderr', file=sys.stderr); "
        f"open({str(starts)!r}, 'a').write(str(os.getpid()) + '\\n')"
    )
    process._command = [sys.executable, "-u", "-c", code]
    process.start()
    eventually(lambda: starts.exists() and len(starts.read_text().splitlines()) >= 2)
    process.stop()
    pids = starts.read_text().splitlines()
    assert pids[0] != pids[1]
    out, err = capfd.readouterr()
    assert "child stdout" in out
    assert "child stderr" in err


def test_stop_during_restart_delay_prevents_another_launch(process, tmp_path):
    marker = tmp_path / "starts"
    process._command = [sys.executable, "-c", f"open({str(marker)!r}, 'a').write('started\\n')"]
    process.start()
    eventually(lambda: marker.exists() and process._child is None)
    started = time.monotonic()
    process.stop()
    assert time.monotonic() - started < 1
    assert marker.read_text() == "started\n"


def test_shutdown_forces_a_stubborn_child(process, tmp_path):
    ready = tmp_path / "ready"
    code = (
        "import os, signal, time, pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    process._command = [sys.executable, "-c", code]
    process.start()
    eventually(ready.exists)
    pid = int(ready.read_text())
    assert os.getpgid(pid) == pid
    started = time.monotonic()
    process.stop()
    assert time.monotonic() - started < 7
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_failed_start_retries_and_stops_promptly(process, caplog):
    process._command = ["/does-not-exist/test-claude"]
    process.start()
    eventually(lambda: sum("start failed" in r.message for r in caplog.records) >= 2)
    started = time.monotonic()
    process.stop()
    assert time.monotonic() - started < 1


def test_stop_before_start_never_launches_child(process):
    process.stop()
    with pytest.raises(RuntimeError, match="stopped"):
        process.start()


def test_shutdown_gives_child_time_to_exit(process, tmp_path):
    marker, ready = tmp_path / "terminated", tmp_path / "ready"
    code = (
        "import signal, time, pathlib; "
        f"signal.signal(signal.SIGTERM, lambda *_: (pathlib.Path({str(marker)!r}).touch(), exit(0))); "
        f"pathlib.Path({str(ready)!r}).touch(); time.sleep(30)"
    )
    process._command = [sys.executable, "-c", code]
    process.start()
    eventually(ready.exists)
    process.stop()
    assert marker.exists()


@pytest.mark.parametrize("parent_exits_first", [False, True])
def test_retired_process_group_cannot_leave_stubborn_grandchild(
    process, tmp_path, parent_exits_first
):
    ready = tmp_path / "grandchild_pid"
    grandchild = (
        "import signal, os, pathlib, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    parent = (
        "import subprocess, sys, pathlib, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        f"p=pathlib.Path({str(ready)!r});\n"
        "while not p.exists(): time.sleep(0.01)\n"
        + ("raise SystemExit(0)" if parent_exits_first else "time.sleep(30)")
    )
    process._command = [sys.executable, "-c", parent]
    process.start()
    eventually(ready.exists)
    grandchild_pid = int(ready.read_text())

    def gone_or_zombie():
        # PID 1 may reap orphaned descendants asynchronously; a zombie is dead.
        try:
            return Path(f"/proc/{grandchild_pid}/stat").read_text().split()[2] == "Z"
        except FileNotFoundError:
            return True

    if parent_exits_first:
        eventually(gone_or_zombie)
    process.stop()
    eventually(gone_or_zombie)


def test_failed_shutdown_keeps_private_file_until_retry(process, monkeypatch):
    process._command = [sys.executable, "-c", "import time; time.sleep(30)"]
    process.start()
    eventually(lambda: process._child is not None)

    def cannot_signal(*args):
        raise OSError("Cannot signal child")

    with monkeypatch.context() as patch:
        patch.setattr(process, "_signal_group", cannot_signal)
        with pytest.raises(OSError, match="Cannot signal"):
            process.stop()
    assert (Path(process.scratch.name) / "environment-secret").exists()
