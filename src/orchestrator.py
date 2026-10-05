"""Launch and restart the native Claude orchestrator."""

import logging
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


def _write_private(path, contents, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(contents)


class OrchestratorProcess:
    def __init__(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="claude-host-")
        root = Path(self.scratch.name)
        try:
            config, hooks = root / "config", root / "hooks"
            config.mkdir()
            hooks.mkdir()
            secret = root / "environment-secret"
            _write_private(secret, os.environ["CLAUDE_ENVIRONMENT_SECRET"].encode())
            hook = shlex.join([sys.executable, str(Path(__file__).with_name("spawn_runner.py"))])
            _write_private(
                hooks / "spawn-runner", ("#!/bin/sh\nexec " + hook + "\n").encode(), 0o700
            )
            environment = dict(os.environ)
            for key in (
                "CLAUDE_ENVIRONMENT_SECRET",
                "SELF_HOSTED_RUNNER_ENVIRONMENT_SECRET",
                "SELF_HOSTED_RUNNER_POOL_SECRET",
                "ENVIRONMENT_KEY",
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN",
                "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
            ):
                environment.pop(key, None)
            environment.update(
                CLAUDE_CONFIG_DIR=str(config),
                SELF_HOSTED_RUNNER_HOST_CONFIG_DIR=str(config),
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                DISABLE_AUTOUPDATER="1",
            )
            self._command = [
                "/usr/local/bin/claude",
                "self-hosted-runner",
                "orchestrator",
                "--environment-secret-file",
                str(secret),
                "--hooks-dir",
                str(hooks),
                "--min-idle",
                "0",
                "--health-port",
                "0",
                "--hook-concurrency",
                "1",
                "--hook-timeout",
                "30",
                "--expected-spawn-seconds",
                "180",
            ]
            self._env = environment
        except BaseException:
            self.scratch.cleanup()
            raise

        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._child = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        if self._stopping.is_set():
            raise RuntimeError("orchestrator has been stopped")
        self._thread.start()

    def _run(self):
        while not self._stopping.is_set():
            try:
                # Serialize launch with stop so shutdown cannot miss a new child.
                with self._lock:
                    if self._stopping.is_set():
                        return
                    self._child = child = subprocess.Popen(
                        self._command,
                        env=self._env,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                returncode = child.wait()
                # Retired hooks must not survive into the next orchestrator process.
                self._signal_group(child.pid, signal.SIGKILL)
                with self._lock:
                    self._child = None
                if not self._stopping.is_set():
                    logging.warning("Claude orchestrator exited with status %s", returncode)
            except OSError as error:
                logging.warning("Claude orchestrator start failed: %s", type(error).__name__)
            if self._stopping.wait(1):
                return

    def stop(self):
        with self._lock:
            self._stopping.set()
            child = self._child
        if child is not None:
            self._signal_group(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            finally:
                self._signal_group(child.pid, signal.SIGKILL)
            child.wait(timeout=1)
        if self._thread.ident is not None:
            self._thread.join(timeout=1)
            if self._thread.is_alive():
                raise TimeoutError("orchestrator did not stop within its shutdown budget")
        self.scratch.cleanup()

    @staticmethod
    def _signal_group(pid, signum):
        try:
            os.killpg(pid, signum)
        except ProcessLookupError:
            pass
