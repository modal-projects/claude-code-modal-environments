"""Exercise the real provisioning function at its failure boundaries."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

import app


def order(number=1, session="00000000-0000-4000-8000-000000000001"):
    return dict(
        environment_id="ccpool_test",
        order_id=f"order_{number}",
        session_uuid=session,
        token=b"OPAQUE_TOKEN\n",
    )


class State:
    def __init__(self, platform):
        self.data = {}
        self.platform = platform

    def get(self, key):
        return deepcopy(self.data.get(key))

    def put(self, key, value, *, skip_if_exists=False):
        if skip_if_exists and key in self.data:
            return False
        if key.startswith("session:"):
            stage = "persist" if value.get("sandbox_id") else "reserve"
            if self.platform.fail == stage:
                raise RuntimeError("PRIVATE_ERROR")
        self.data[key] = deepcopy(value)
        self.platform.events.append(("put", key))
        return True


class Image:
    def __init__(self, object_id):
        self.object_id = object_id
        self.script = None

    def add_local_file(self, source, target):
        self.script = (source, target)
        return self


class Sandbox:
    def __init__(self, platform, object_id):
        self.platform, self.object_id = platform, object_id
        self.exit_code = None

    def poll(self):
        if self.platform.fail == "poll":
            raise RuntimeError("PRIVATE_ERROR")
        return self.exit_code

    def _experimental_get_exit_snapshot(self, timeout):
        assert self.exit_code is not None
        if self.platform.fail == "snapshot":
            raise RuntimeError("PRIVATE_ERROR")
        return Image("im-exit")

    @property
    def _experimental_sidecars(self):
        def create(**kwargs):
            self.platform.events.append(("sidecar", self.object_id))
            self.platform.sidecars.append(kwargs)
            if self.platform.fail == "sidecar":
                raise RuntimeError("Sidecar startup failed")
            return SimpleNamespace(object_id="sc-" + self.object_id)

        return SimpleNamespace(create=create)

    def exec(self, *args, **kwargs):
        self.platform.events.append(("ready", self.object_id))
        return SimpleNamespace(wait=lambda: 1 if self.platform.fail == "ready" else 0)


@pytest.fixture
def platform(monkeypatch):
    p = SimpleNamespace(events=[], sandboxes={}, fail=None, now=0, options=[], sidecars=[])
    p.state = State(p)

    def create(*command, image, **kwargs):
        sandbox = Sandbox(p, f"sb-{len(p.sandboxes) + 1}")
        p.sandboxes[sandbox.object_id] = sandbox
        p.events.append(("create", sandbox.object_id))
        p.options.append((command, image, kwargs))
        if p.fail == "create":
            raise RuntimeError("PRIVATE_ERROR")
        return sandbox

    def release(sandbox, path, token):
        assert any(
            v.get("sandbox_id") == sandbox.object_id
            for k, v in p.state.data.items()
            if k.startswith("session:")
        )
        assert token == b"OPAQUE_TOKEN\n"
        p.events.append(("release", sandbox.object_id))
        if p.fail == "release":
            raise RuntimeError("PRIVATE_ERROR")

    def sleep(seconds):
        p.now += seconds

    monkeypatch.setenv("CLAUDE_ENVIRONMENT_ID", "ccpool_test")
    monkeypatch.setattr(app, "SIDECAR_SECRET_NAMES", ["claude-code-modal"])
    monkeypatch.setattr(app, "PLAYGROUND_ENVIRONMENT", "claude-code-playground")

    def example_api(app_name, function_name, *, environment_name):
        assert (app_name, function_name, environment_name) == (
            "claude-code-example-api",
            "hello",
            "claude-code-playground",
        )
        return SimpleNamespace(get_web_url=lambda: "https://example.modal.run")

    monkeypatch.setattr(app.modal.Function, "from_name", example_api)
    monkeypatch.setattr(app, "state", p.state)
    monkeypatch.setattr(app, "sandbox_image", Image("im-base"))
    p.reopened_images = []

    def from_image_id(image_id):
        p.reopened_images.append(image_id)
        return Image(image_id)

    monkeypatch.setattr(app.modal.Image, "from_id", from_image_id)
    monkeypatch.setattr(app, "release_sandbox", release)

    def resolve_sidecar(owner):
        assert owner is app.app
        p.events.append(("resolve-sidecar",))
        if p.fail == "sidecar_image":
            raise RuntimeError("Named sidecar image not found")

    monkeypatch.setattr(
        app, "sidecar_image", SimpleNamespace(object_id="im-sidecar", build=resolve_sidecar)
    )
    monkeypatch.setattr(
        app.modal.Secret,
        "from_name",
        lambda name: SimpleNamespace(
            name=name, hydrate=lambda: p.events.append(("resolve-secret", name))
        ),
    )
    monkeypatch.setattr(
        app.modal,
        "Sandbox",
        SimpleNamespace(create=create, from_id=lambda sandbox_id: p.sandboxes[sandbox_id]),
    )
    monkeypatch.setattr(app.time, "monotonic", lambda: p.now)
    monkeypatch.setattr(app.time, "sleep", sleep)
    p.run = app.provision.local
    return p


def test_creates_once_records_before_release_and_never_stores_token(platform):
    p = platform
    assert p.run(order())["status"] == "released"
    assert p.run(order())["status"] == "duplicate_order"
    assert len(p.sandboxes) == 1
    assert "OPAQUE_TOKEN" not in repr(p.state.data)
    command, image, options = p.options[0]
    assert command[-1].startswith("/run/claude-token-")
    assert "-c" not in command
    assert image.script[1] == "/opt/claude-sandbox.py"
    assert options["experimental_options"]["enable_exit_snapshot"] is True
    assert not options.get("secrets")
    assert len(p.sidecars) == 1


@pytest.mark.parametrize("names", [[], ["claude-code-other-api"]])
def test_start_and_resume_without_modal_playground(platform, monkeypatch, names):
    p = platform
    monkeypatch.setattr(app, "PLAYGROUND_ENVIRONMENT", None)
    monkeypatch.setattr(app, "SIDECAR_SECRET_NAMES", names)

    def unexpected_api_lookup(*args, **kwargs):
        pytest.fail("Hosting Claude must not require a playground API")

    monkeypatch.setattr(app.modal.Function, "from_name", unexpected_api_lookup)
    assert p.run(order())["status"] == "released"
    p.sandboxes["sb-1"].exit_code = 0
    assert p.run(order(2))["status"] == "released"
    for _, _, options in p.options:
        assert options["env"]["CLAUDE_MODAL_ENVIRONMENT"] == ""
        assert not options.get("secrets")
    for sidecar in p.sidecars:
        assert sidecar["env"] == {}
        assert [secret.name for secret in sidecar["secrets"]] == names
    assert ("resolve-secret", "claude-code-modal") not in p.events


def test_running_predecessor_prevents_replacement_without_consuming_order(platform):
    p = platform
    p.run(order())
    assert p.run(order(2))["status"] == "sandbox_running"
    assert p.now == 60
    assert "order:order_2" not in p.state.data
    assert len(p.sandboxes) == 1


@pytest.mark.parametrize("missing_name", ["claude-code-modal", "claude-code-other-api"])
def test_missing_named_secret_does_not_claim_work(platform, monkeypatch, missing_name):
    p = platform
    monkeypatch.setattr(app, "SIDECAR_SECRET_NAMES", ["claude-code-modal", "claude-code-other-api"])

    def from_name(name):
        def hydrate():
            if name == missing_name:
                raise RuntimeError("Secret not found")

        return SimpleNamespace(name=name, hydrate=hydrate)

    monkeypatch.setattr(app.modal.Secret, "from_name", from_name)
    assert p.run(order()) == {"status": "failed", "stage": "sidecar_secrets"}
    assert "order:order_1" not in p.state.data
    assert "session:" + order()["session_uuid"] not in p.state.data
    assert not p.sandboxes


def test_running_predecessor_skips_sidecar_preparation(platform):
    p = platform
    p.run(order())
    p.events.clear()
    assert p.run(order(2))["status"] == "sandbox_running"
    assert not any(event[0] == "resolve-secret" for event in p.events)
    assert not any(event[0] == "resolve-sidecar" for event in p.events)


def test_missing_named_sidecar_image_does_not_claim_work(platform):
    platform.fail = "sidecar_image"
    assert platform.run(order()) == {"status": "failed", "stage": "sidecar_image"}
    assert "order:order_1" not in platform.state.data
    assert "session:" + order()["session_uuid"] not in platform.state.data
    assert not platform.sandboxes


def test_resume_uses_exact_exit_image_with_current_script_and_fresh_token_path(platform):
    p = platform
    p.run(order())
    p.sandboxes["sb-1"].exit_code = 0
    assert p.run(order(2))["status"] == "released"
    first, second = p.options
    assert first[0][-1] != second[0][-1]
    assert second[1].object_id == "im-exit"
    assert p.reopened_images == ["im-exit"]
    assert second[1].script == first[1].script
    record = p.state.data["session:" + order()["session_uuid"]]
    assert record["generation"] == 2
    assert record["snapshot_image_id"] == "im-exit"


@pytest.mark.parametrize("failure", ["reserve", "create", "persist", "release"])
def test_unknown_outcomes_never_create_another_sandbox(platform, failure, capsys):
    p = platform
    p.fail = failure
    assert p.run(order())["status"] == "failed"
    p.fail = None
    assert p.run(order(2))["status"] in {"needs_operator", "sandbox_running"}
    assert len(p.sandboxes) <= 1
    output = capsys.readouterr().out
    assert '"error_type": "RuntimeError"' in output
    assert '"error": "PRIVATE_ERROR"' in output
    assert "OPAQUE_TOKEN" not in output


def test_failure_logs_context_without_credentials(platform, monkeypatch, capsys):
    p = platform
    monkeypatch.setenv("MODAL_TOKEN_ID", "operator-token-id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "operator-token-secret")

    def fail(*args, **kwargs):
        raise RuntimeError(
            "create rejected: "
            + repr(order()["token"])
            + " operator-token-id operator-token-secret "
            + order()["environment_id"]
        )

    monkeypatch.setattr(app.modal.Sandbox, "create", fail)
    assert p.run(order()) == {"status": "failed", "stage": "create"}
    output = capsys.readouterr().out
    assert "create rejected" in output
    assert '"stage": "create"' in output
    assert order()["order_id"] in output
    assert order()["session_uuid"] in output
    assert app.DEPLOYMENT_ID in output
    assert "OPAQUE_TOKEN" not in output
    assert "operator-token-id" not in output
    assert "operator-token-secret" not in output
    assert order()["environment_id"] not in output


@pytest.mark.parametrize(
    "names", [["claude-code-modal"], ["claude-code-modal", "claude-code-other-api"]]
)
def test_only_sidecar_receives_named_secrets_on_start_and_resume(platform, monkeypatch, names):
    p = platform
    monkeypatch.setattr(app, "SIDECAR_SECRET_NAMES", names)
    assert p.run(order())["status"] == "released"
    p.sandboxes["sb-1"].exit_code = 0
    assert p.run(order(2))["status"] == "released"
    assert len(p.sidecars) == 2
    for number, (command, image, options) in enumerate(p.options, start=1):
        assert not options.get("secrets")
        assert options["env"]["CLAUDE_SIDECAR_URL"] == "http://sidecar:8080"
        assert options["env"]["CLAUDE_MODAL_ENVIRONMENT"] == "claude-code-playground"
        sidecar = p.sidecars[number - 1]
        assert [secret.name for secret in sidecar["secrets"]] == names
        assert sidecar["env"] == {"CLAUDE_API_HOST": "example.modal.run"}
        sandbox_id = f"sb-{number}"
        assert p.events.index(("sidecar", sandbox_id)) < p.events.index(("ready", sandbox_id))
        assert p.events.index(("ready", sandbox_id)) < p.events.index(("release", sandbox_id))
    for name in names:
        assert p.events.count(("resolve-secret", name)) == 2
        assert p.events.index(("resolve-secret", name)) < p.events.index(("create", "sb-1"))


@pytest.mark.parametrize("failure", ["sidecar", "ready"])
def test_sidecar_failure_keeps_sandbox_record_without_releasing_token(platform, failure):
    p = platform
    p.fail = failure
    assert p.run(order()) == {"status": "failed", "stage": "sidecar"}
    assert not any(event[0] == "release" for event in p.events)
    assert p.state.data["session:" + order()["session_uuid"]]["sandbox_id"] == "sb-1"


@pytest.mark.parametrize("failure", ["poll", "snapshot"])
def test_failed_snapshot_preserves_previous_session_and_order(platform, failure):
    p = platform
    p.run(order())
    p.sandboxes["sb-1"].exit_code = 0
    previous = deepcopy(p.state.data)
    p.fail = failure
    assert p.run(order(2))["status"] == "failed"
    assert previous == p.state.data
    assert len(p.sandboxes) == 1


def test_existing_generation_claim_blocks_create(platform):
    p = platform
    p.state.data["generation:" + order()["session_uuid"] + ":1"] = {}
    assert p.run(order())["status"] == "needs_operator"
    assert not p.sandboxes


def test_wrong_environment_cannot_create(platform):
    p = platform
    assert (
        p.run({**order(), "environment_id": "ccpool_other"})["status"] == "unapproved_environment"
    )
    p.state.data["environment_id"] = "ccpool_other"
    assert p.run(order())["status"] == "wrong_session_dict"
    assert not p.sandboxes


def test_independent_sessions_can_create(platform):
    p = platform
    p.run(order())
    assert p.run(order(2, "00000000-0000-4000-8000-000000000002"))["status"] == "released"
    assert len(p.sandboxes) == 2
