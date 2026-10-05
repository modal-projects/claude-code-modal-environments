"""Claude Code self-hosted environments on Modal.

Build images and deploy: uv run modal deploy src/app.py
"""

import json
import os
import secrets
import time
from pathlib import Path

import modal

APP_NAME = "claude-code"
STATE_NAME = APP_NAME + "-sessions"
SIDECAR_SECRET_NAMES = []  # Add API Secrets here; see README.
PLAYGROUND_ENVIRONMENT = None  # Set to "claude-code-playground" for Modal development.

SANDBOX_SCRIPT = Path(__file__).with_name("sandbox.py")
SANDBOX_PATH = "/opt/claude-sandbox.py"


def sandbox_command(token_path):
    return [
        "/usr/bin/setpriv",
        "--reuid=1000",
        "--regid=1000",
        "--clear-groups",
        "--",
        "/usr/local/bin/python",
        "-I",
        SANDBOX_PATH,
        "run",
        token_path,
    ]


def release_sandbox(sandbox, token_path, token):
    # The filesystem API initially writes as root. Keep that file inside a
    # root-private directory until its ownership and mode are ready.
    staging = token_path + ".staging"
    if sandbox.exec("mkdir", "-m", "700", staging, timeout=10).wait():
        raise RuntimeError("Could not prepare token directory")
    # Refresh the marker before Claude starts, including on snapshot restores.
    sandbox.filesystem.write_bytes((DEPLOYMENT_ID + "\n").encode(), "/opt/claude-code-version")
    sandbox.filesystem.write_bytes(token, staging + "/token")
    if sandbox.exec(
        "/usr/local/bin/python", "-I", SANDBOX_PATH, "publish", token_path, timeout=10
    ).wait():
        raise RuntimeError("Could not publish sandbox token")


if modal.is_local():
    from images import build

    # Build and publish under this app; remote containers only look up images.
    build(APP_NAME)
    DEPLOYMENT_ID = secrets.token_hex(6)
    print(f"Deployment ID: {DEPLOYMENT_ID}")
else:
    DEPLOYMENT_ID = os.environ["CLAUDE_DEPLOYMENT_ID"]
orchestrator_image = modal.Image.from_name("claude-code-orchestrator").add_local_python_source(
    "orchestrator", "spawn_runner", "sandbox"
)
sandbox_image = modal.Image.from_name("claude-code-sandbox")
sidecar_image = modal.Image.from_name("claude-code-sidecar")
app = modal.App(APP_NAME)
state = modal.Dict.from_name(STATE_NAME, create_if_missing=True)
claude_environment = modal.Secret.from_name(
    "claude-code-environment",
    required_keys=["CLAUDE_ENVIRONMENT_ID", "CLAUDE_ENVIRONMENT_SECRET"],
)
runtime_env = {
    "APP_NAME": APP_NAME,
    "CLAUDE_DEPLOYMENT_ID": DEPLOYMENT_ID,
}


@app.function(
    image=orchestrator_image,
    cpu=0.5,
    memory=512,
    timeout=240,
    max_containers=1,
    retries=0,
    env=runtime_env,
    secrets=[claude_environment],
)
@modal.concurrent(max_inputs=1)
def provision(order):
    """One assignment: deduplicate, restore, create, record, then release."""

    def report(status, **fields):
        result = {"status": status, **fields}
        print(
            json.dumps(
                {
                    "deployment_id": DEPLOYMENT_ID,
                    "order_id": order["order_id"],
                    "session_uuid": order["session_uuid"],
                    **result,
                }
            ),
            flush=True,
        )
        return result

    report("received")
    if order["environment_id"] != os.environ["CLAUDE_ENVIRONMENT_ID"]:
        return report("unapproved_environment")
    stage = "read"
    try:
        state.put("environment_id", order["environment_id"], skip_if_exists=True)
        if state.get("environment_id") != order["environment_id"]:
            return report("wrong_session_dict")
        order_key = "order:" + order["order_id"]
        session_key = "session:" + order["session_uuid"]
        if state.get(order_key) is not None:
            return report("duplicate_order")
        previous = state.get(session_key)
        image = sandbox_image
        record = {"order_id": order["order_id"], "generation": 1, "sandbox_id": None}
        if previous is not None:
            if not previous["sandbox_id"]:
                return report("needs_operator")
            stage = "snapshot"
            old = modal.Sandbox.from_id(previous["sandbox_id"])
            report("restoring", sandbox_id=old.object_id)
            # Native session release can precede Sandbox exit.
            deadline = time.monotonic() + 60
            while old.poll() is None:
                if time.monotonic() >= deadline:
                    return report("sandbox_running", sandbox_id=old.object_id)
                time.sleep(1)
            snapshot = old._experimental_get_exit_snapshot(timeout=45)
            # The exit-snapshot handle has no loader in Modal 1.5.5. Reopen it
            # before adding the current script as a startup mount.
            image = modal.Image.from_id(snapshot.object_id)
            record.update(
                generation=previous["generation"] + 1,
                previous_sandbox_id=old.object_id,
                snapshot_image_id=image.object_id,
            )
        # Resolve the published sidecar image and Secrets before claiming work.
        stage = "sidecar_image"
        sidecar_image.build(app)  # Named-image lookup only; no image recipe runs here.
        stage = "sidecar_secrets"
        report("preparing_sidecar")
        sidecar_secrets = [modal.Secret.from_name(name) for name in SIDECAR_SECRET_NAMES]
        for secret in sidecar_secrets:
            secret.hydrate()
        sidecar_env = {}
        if PLAYGROUND_ENVIRONMENT:
            stage = "playground"
            sidecar_env["CLAUDE_API_HOST"] = (
                modal.Function.from_name(
                    "claude-code-example-api", "hello", environment_name=PLAYGROUND_ENVIRONMENT
                )
                .get_web_url()
                .removeprefix("https://")
            )
        stage = "claim"
        if not state.put(order_key, {"session_uuid": order["session_uuid"]}, skip_if_exists=True):
            return report("duplicate_order")
        generation_key = f"generation:{order['session_uuid']}:{record['generation']}"
        if not state.put(generation_key, record, skip_if_exists=True):
            return report("needs_operator")
        state.put(session_key, record)
        token_path = "/run/claude-token-" + secrets.token_hex(16)
        stage = "create"
        report(
            "creating",
            generation=record["generation"],
            snapshot_image_id=record.get("snapshot_image_id"),
        )
        sandbox = modal.Sandbox.create(
            *sandbox_command(token_path),
            # Use this deployment's sandbox.py when restoring an older filesystem snapshot.
            image=image.add_local_file(SANDBOX_SCRIPT, SANDBOX_PATH),
            cpu=2,
            memory=4096,  # MiB
            gpu=None,  # e.g. "T4" or "L4"
            timeout=900,
            workdir="/workspace",
            include_oidc_identity_token=False,
            experimental_options={"enable_exit_snapshot": True},
            inbound_cidr_allowlist=[],
            outbound_domain_allowlist=[
                "api.anthropic.com",
                "claude.ai",
                "github.com",
                "api.github.com",
                "codeload.github.com",
                "raw.githubusercontent.com",
                "objects.githubusercontent.com",
                # The SDK calls regional servers directly after authenticating via Caddy.
                "*.modal.com",
                "*.modal2.com",
            ],
            env={
                "SANDBOX_RETIRE_AT": str(int(time.time()) + 720),
                "CLAUDE_SIDECAR_URL": "http://sidecar:8080",
                "CLAUDE_MODAL_ENVIRONMENT": PLAYGROUND_ENVIRONMENT or "",
            },
        )
        report("created", sandbox_id=sandbox.object_id)
        stage = "persist"
        state.put(session_key, {**record, "sandbox_id": sandbox.object_id})
        stage = "sidecar"
        report("starting_sidecar", sandbox_id=sandbox.object_id)
        sidecar = sandbox._experimental_sidecars.create(
            name="sidecar",
            image=sidecar_image,
            secrets=sidecar_secrets,
            env=sidecar_env,
        )
        ready = sandbox.exec(
            "bash",
            "-c",
            "for port in 8080 8081; do "
            "until (echo > /dev/tcp/sidecar/$port) 2>/dev/null; do sleep 0.1; done; done",
            timeout=30,
        )
        if ready.wait():
            raise RuntimeError("Sidecar did not become ready")
        report("sidecar_ready", sandbox_id=sandbox.object_id, sidecar_id=sidecar.object_id)
        stage = "release"
        release_sandbox(sandbox, token_path, order["token"])
        return report("released", sandbox_id=sandbox.object_id)
    except Exception as error:
        # Provider errors can echo inputs. Redact credentials before logging.
        message = str(error)
        sensitive = [
            order["token"].decode("utf-8", errors="replace"),
            repr(order["token"])[2:-1],
            os.environ.get("MODAL_TOKEN_ID", ""),
            os.environ.get("MODAL_TOKEN_SECRET", ""),
            os.environ.get("CLAUDE_ENVIRONMENT_SECRET", ""),
            os.environ.get("CLAUDE_ENVIRONMENT_ID", ""),
        ]
        for value in sensitive:
            if value:
                for encoded in (value, repr(value)[1:-1], json.dumps(value)[1:-1]):
                    message = message.replace(encoded, "[REDACTED]")
        report("failed", stage=stage, error_type=type(error).__name__, error=message)
        # Immutable claims still prevent another create after an unknown result.
        return {"status": "failed", "stage": stage}


@app.cls(
    image=orchestrator_image,
    cpu=0.5,
    memory=1024,
    min_containers=1,
    max_containers=1,
    env=runtime_env,
    secrets=[claude_environment],
)
class Orchestrator:
    @modal.enter()
    def start(self):
        from orchestrator import OrchestratorProcess

        self.process = OrchestratorProcess()
        self.process.start()

    @modal.exit()
    def stop(self):
        self.process.stop()
