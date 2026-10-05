"""Deployment needs no local Claude credentials or credential files."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def offline_example(tmp_path):
    example = tmp_path / "example"
    source = example / "src"
    source.mkdir(parents=True)
    for name in (
        "app.py",
        "images.py",
        "orchestrator.py",
        "spawn_runner.py",
        "sandbox.py",
        "Caddyfile",
    ):
        shutil.copy2(ROOT / "src" / name, source / name)
    guard = tmp_path / "sitecustomize.py"
    guard.write_text(
        "import socket, sys\n"
        "def deny_network(*args, **kwargs):\n"
        "    print('OFFLINE_TEST_NETWORK_ATTEMPT', file=sys.stderr, flush=True)\n"
        "    raise RuntimeError('Network disabled in offline entrypoint test')\n"
        "socket.socket.connect = deny_network\n"
        "socket.socket.connect_ex = deny_network\n"
        "socket.create_connection = deny_network\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MODAL_", "ANTHROPIC_", "CLAUDE_", "SELF_HOSTED_", "SANDBOX_"))
    }
    env["MODAL_CONFIG_PATH"] = str(tmp_path / "absent-modal.toml")
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(source)))

    def run(*args):
        result = subprocess.run(
            [sys.executable, *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert "OFFLINE_TEST_NETWORK_ATTEMPT" not in result.stderr
        assert "test-secret" not in result.stdout + result.stderr
        return result

    return example, env, run


@pytest.mark.parametrize("legacy_credentials", [False, True])
def test_deploy_import_needs_no_local_credentials(offline_example, legacy_credentials):
    example, env, run = offline_example
    if legacy_credentials:
        (example / ".env").write_text(
            "CLAUDE_ENVIRONMENT_ID=ccpool_obsolete\nCLAUDE_ENVIRONMENT_SECRET=test-secret\n"
        )
        env.update(CLAUDE_ENVIRONMENT_ID="ccpool_obsolete", CLAUDE_ENVIRONMENT_SECRET="test-secret")
    result = run(
        "-c",
        """
from unittest.mock import patch
with patch('images.build') as build:
    import app
build.assert_called_once_with(app.APP_NAME)
assert "'claude-code-sandbox'" in repr(app.sandbox_image)
assert "'claude-code-sidecar'" in repr(app.sidecar_image)
assert not any(key.endswith('_IMAGE_ID') for key in app.runtime_env)
assert app.runtime_env['CLAUDE_DEPLOYMENT_ID'] == app.DEPLOYMENT_ID
assert not hasattr(app, 'CLAUDE_ENVIRONMENT_ID')
assert 'CLAUDE_ENVIRONMENT_ID' not in app.runtime_env
assert 'CLAUDE_ENVIRONMENT_SECRET' not in app.runtime_env
""",
    )
    assert result.returncode == 0, result.stderr


def test_image_recipes_need_no_credentials_or_built_images(offline_example):
    _, _, run = offline_example
    result = run("-c", "import images; images.images()")
    assert result.returncode == 0, result.stderr
