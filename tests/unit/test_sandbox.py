from pathlib import Path

import pytest

import sandbox as sandbox_runtime
from app import release_sandbox


def test_atomic_publication_keeps_bytes_private_until_ready(tmp_path, monkeypatch):
    token = tmp_path / "fresh-token"
    staging = Path(str(token) + ".staging")
    staging.mkdir(mode=0o700)
    staged = staging / "token"
    staged.write_bytes(b" opaque\ntoken ")
    ownership = []

    def chown(path, uid, gid):
        assert not token.exists()
        assert path.stat().st_mode & 0o777 == 0o600
        ownership.append((uid, gid))

    monkeypatch.setattr(sandbox_runtime.os, "chown", chown)
    sandbox_runtime.publish(token)
    assert token.read_bytes() == b" opaque\ntoken "
    assert token.stat().st_mode & 0o777 == 0o600
    assert ownership == [(1000, 1000)]
    assert not staging.exists()


def test_runner_waits_for_fresh_token_then_execs_native_claude(tmp_path, monkeypatch):
    token = tmp_path / "fresh"
    (tmp_path / "old-token").write_bytes(b"OLD")
    calls = []
    monkeypatch.setattr(sandbox_runtime.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(sandbox_runtime.os, "getegid", lambda: 1000)
    monkeypatch.setenv("SANDBOX_RETIRE_AT", "9999999999")

    def publish(_):
        calls.append("wait")
        token.write_bytes(b"FRESH")
        token.chmod(0o600)

    monkeypatch.setattr(sandbox_runtime.time, "sleep", publish)
    monkeypatch.setattr(sandbox_runtime.os, "execve", lambda *args: calls.append(args))
    sandbox_runtime.run(token)
    assert calls[0] == "wait"
    binary, command, environment = calls[1]
    assert binary == "/usr/local/bin/claude"
    assert command[1] == "self-hosted-runner"
    assert command[command.index("--environment-secret-file") + 1] == str(token)
    assert "CLAUDE_ENVIRONMENT_SECRET" not in environment


def test_missing_token_times_out_without_launch(tmp_path, monkeypatch):
    clock = iter([0, 91])
    monkeypatch.setattr(sandbox_runtime.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(sandbox_runtime.os, "getegid", lambda: 1000)
    monkeypatch.setenv("SANDBOX_RETIRE_AT", "9999999999")
    monkeypatch.setattr(sandbox_runtime.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(sandbox_runtime.os, "execve", lambda *a: pytest.fail("Must not launch"))
    assert sandbox_runtime.run(tmp_path / "missing") == 124


def test_sandbox_rejects_root_and_public_token(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_runtime.os, "geteuid", lambda: 0)
    with pytest.raises(ValueError):
        sandbox_runtime.run(tmp_path / "token")
    monkeypatch.setattr(sandbox_runtime.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(sandbox_runtime.os, "getegid", lambda: 1000)
    monkeypatch.setenv("SANDBOX_RETIRE_AT", "9999999999")
    token = tmp_path / "token"
    token.write_bytes(b"token")
    token.chmod(0o644)
    with pytest.raises(ValueError):
        sandbox_runtime.run(token)


def test_runner_receives_sidecar_url_without_credentials_or_hook_overrides():
    environment = sandbox_runtime.runner_environment(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/sandbox",
            "PYTHONPATH": "/evil",
            "MODAL_TOKEN_SECRET": "operator",
            "CLAUDE_ENVIRONMENT_SECRET": "host",
            "SELF_HOSTED_RUNNER_EXEC_PATH": "/old-copy-wrapper",
            "CLAUDE_MODAL_TOKEN_ID": "sandbox-id",
            "CLAUDE_MODAL_TOKEN_SECRET": "fresh-sandbox",
            "CLAUDE_MODAL_ENVIRONMENT": "sandbox-env",
            "CLAUDE_MODAL_PROXY_TOKEN_ID": "proxy-id",
            "CLAUDE_MODAL_PROXY_TOKEN_SECRET": "proxy-secret",
            "CLAUDE_SIDECAR_URL": "http://sidecar:8080",
        }
    )
    assert environment["CLAUDE_SIDECAR_URL"] == "http://sidecar:8080"
    assert environment["MODAL_CONFIG_PATH"] == "/dev/null"
    assert environment["MODAL_SERVER_URL"] == "http://sidecar:8081"
    assert environment["MODAL_ENVIRONMENT"] == "sandbox-env"
    assert not any(
        value
        in {
            "operator",
            "host",
            "/evil",
            "/old-copy-wrapper",
            "fresh-sandbox",
            "proxy-id",
            "proxy-secret",
        }
        for value in environment.values()
    )


def test_missing_sandbox_credentials_never_fall_back_to_operator_identity():
    environment = sandbox_runtime.runner_environment(
        {
            "MODAL_TOKEN_ID": "operator-id",
            "MODAL_TOKEN_SECRET": "operator-secret",
            "MODAL_ENVIRONMENT": "deployment-environment",
            "MODAL_CONFIG_PATH": "/operator/.modal.toml",
            "MODAL_SERVER_URL": "https://operator.invalid",
            "MODAL_PROFILE": "operator-profile",
        }
    )
    assert environment["MODAL_CONFIG_PATH"] == "/dev/null"
    assert {key for key in environment if key.startswith("MODAL_")} == {"MODAL_CONFIG_PATH"}


def test_gpu_environment_survives_native_runner_launch(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_bytes(b"FRESH")
    token.chmod(0o600)
    gpu_env = {
        "CUDA_VISIBLE_DEVICES": "0",
        "CUDA_HOME": "/usr/local/cuda",
        "CUDA_PATH": "/usr/local/cuda",
        "NVIDIA_VISIBLE_DEVICES": "GPU-test",
        "NVIDIA_DRIVER_CAPABILITIES": "compute,utility",
        "LD_LIBRARY_PATH": "/usr/local/nvidia/lib64",
    }
    for key, value in gpu_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SANDBOX_RETIRE_AT", "9999999999")
    monkeypatch.setenv("CLAUDE_MODAL_PROXY_TOKEN_SECRET", "must-not-reach-runner")
    monkeypatch.setattr(sandbox_runtime.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(sandbox_runtime.os, "getegid", lambda: 1000)
    environments = []
    monkeypatch.setattr(sandbox_runtime.os, "execve", lambda _, args, env: environments.append(env))
    sandbox_runtime.run(token)
    assert all(environments[0][key] == value for key, value in gpu_env.items())
    assert "CLAUDE_MODAL_PROXY_TOKEN_SECRET" not in environments[0]


def test_publication_failure_does_not_continue():
    from types import SimpleNamespace

    writes = []
    sandbox = SimpleNamespace(
        exec=lambda *a, **k: SimpleNamespace(wait=lambda: 1),
        filesystem=SimpleNamespace(write_bytes=lambda *a: writes.append(a)),
    )
    with pytest.raises(RuntimeError):
        release_sandbox(sandbox, "/run/fresh", b"TOKEN")
    assert not writes
