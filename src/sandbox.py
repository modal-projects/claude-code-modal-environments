"""Wait for this assignment's token, then replace this process with Claude.

This standalone script is supplied to every Sandbox, including restored Images.
It deliberately has no imports from the restored project's Python packages.
"""

import os
import stat
import sys
import time
from pathlib import Path


def runner_environment(environ):
    allowed = (
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "TERM",
        "TZ",
        "CLAUDE_SIDECAR_URL",
        # Preserve GPU visibility and library paths when the sandbox requests a GPU.
        "CUDA_VISIBLE_DEVICES",
        "CUDA_HOME",
        "CUDA_PATH",
        "NVIDIA_VISIBLE_DEVICES",
        "NVIDIA_DRIVER_CAPABILITIES",
        "LD_LIBRARY_PATH",
    )
    result = {key: environ[key] for key in allowed if key in environ}
    result.update(
        SELF_HOSTED_RUNNER_HOST_CONFIG_DIR="/opt/cc-host-config",
        CLAUDE_CONFIG_DIR="/workspace/_runner-config",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_AUTOUPDATER="1",
        MODAL_CONFIG_PATH="/dev/null",
    )
    if environ.get("CLAUDE_MODAL_ENVIRONMENT"):
        result.update(
            MODAL_SERVER_URL="http://sidecar:8081",
            MODAL_ENVIRONMENT=environ["CLAUDE_MODAL_ENVIRONMENT"],
            # Satisfy the SDK's local credential check; Caddy replaces these headers.
            MODAL_TOKEN_ID="via-sidecar",
            MODAL_TOKEN_SECRET="via-sidecar",
        )
    return result


def publish(token_path):
    """Called as root after the provisioner has recorded the Sandbox ID."""
    staged = Path(str(token_path) + ".staging") / "token"
    staged.chmod(0o600)
    os.chown(staged, 1000, 1000)
    os.replace(staged, token_path)
    staged.parent.rmdir()


def run(token_path):
    if (os.geteuid(), os.getegid()) != (1000, 1000):
        raise ValueError("Sandbox must run as UID/GID 1000")
    retire_at = int(os.environ["SANDBOX_RETIRE_AT"])
    deadline = time.monotonic() + 90
    while not token_path.exists():
        if time.monotonic() >= deadline or time.time() >= retire_at:
            return 124
        time.sleep(0.25)
    info = token_path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not info.st_size:
        raise ValueError("Invalid token file")
    if time.time() >= retire_at:
        return 124
    command = [
        "/usr/local/bin/claude",
        "self-hosted-runner",
        "--environment-secret-file",
        str(token_path),
        "--base-dir",
        "/workspace",
        "--capacity",
        "1",
        "--health-port",
        "0",
        "--trust-workspace",
        "false",
        "--confine-repo-settings",
        "enforce",
        "--release-idle-session-min",
        "1",
        "--exit-if-unused-min",
        "1",
        "--retire-at",
        str(retire_at),
    ]
    os.execve(command[0], command, runner_environment(os.environ))


if __name__ == "__main__":
    try:
        action, path = sys.argv[1:]
        if action == "publish":
            publish(Path(path))
        elif action == "run":
            raise SystemExit(run(Path(path)))
        else:
            raise ValueError("Unknown sandbox action")
    except Exception:
        print("Sandbox startup failed", file=sys.stderr)
        raise SystemExit(1)
