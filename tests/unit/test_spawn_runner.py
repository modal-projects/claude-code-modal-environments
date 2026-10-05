import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import spawn_runner as hook


@pytest.fixture
def environment(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_bytes(b" opaque\ntoken ")
    values = dict(
        CLAUDE_ENVIRONMENT_ID="ccpool_test",
        CLAUDE_RUNNER_POOL_ID="ccpool_test",
        CLAUDE_RUNNER_ORDER_ID="order_1",
        APP_NAME="example",
        CLAUDE_RUNNER_SESSION_UUID="00000000-0000-4000-8000-000000000001",
        CLAUDE_RUNNER_WORK_ORDER_FILE=str(token),
    )
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["spawn-runner"])
    return values


def test_token_is_copied_before_hook_returns(environment, monkeypatch):
    orders = []

    def spawn(order):
        Path(environment["CLAUDE_RUNNER_WORK_ORDER_FILE"]).unlink()
        orders.append(order)

    monkeypatch.setattr(
        hook.modal.Function, "from_name", lambda *a, **k: SimpleNamespace(spawn=spawn)
    )
    assert hook.main() == 0
    assert orders[0]["token"] == b" opaque\ntoken "
    assert set(orders[0]) == {"environment_id", "order_id", "session_uuid", "token"}


def test_ack_loss_is_retryable_with_no_local_retry_or_secret_log(environment, monkeypatch, capsys):
    calls = []

    def spawn(order):
        calls.append(order)
        raise RuntimeError("PRIVATE_TOKEN")

    monkeypatch.setattr(
        hook.modal.Function, "from_name", lambda *a, **k: SimpleNamespace(spawn=spawn)
    )
    assert hook.main() == 1
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert "PRIVATE_TOKEN" not in captured.out + captured.err


@pytest.mark.parametrize(
    "field,value",
    [
        ("CLAUDE_RUNNER_POOL_ID", "ccpool_other"),
        ("CLAUDE_RUNNER_SESSION_UUID", ""),
        ("CLAUDE_RUNNER_ORDER_ID", "../escape"),
    ],
)
def test_invalid_metadata_cannot_submit(environment, monkeypatch, field, value):
    monkeypatch.setenv(field, value)
    monkeypatch.setattr(hook.modal.Function, "from_name", lambda *a, **k: pytest.fail("No lookup"))
    assert hook.main() == 2


def test_unreadable_token_returns_retryable(environment):
    Path(environment["CLAUDE_RUNNER_WORK_ORDER_FILE"]).unlink()
    assert hook.main() == 1
