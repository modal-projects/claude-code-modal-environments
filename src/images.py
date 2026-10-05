"""Image recipes, built locally by: uv run modal deploy src/app.py"""

from pathlib import Path

import modal

CLAUDE_VERSION = "2.1.282"
CLAUDE_SHA256 = "3afe8535c0cc33f0e24f7b25dab7a1727b8b592196f8496a8bc302ba2161eed3"
MODAL_SDK = "modal==1.5.5"


def images():
    # Both processes need Claude; sandbox setup stays on its own branch.
    base = (
        modal.Image.debian_slim(python_version="3.12")
        .apt_install("ca-certificates", "curl")
        .run_commands(
            "curl -fsSL https://claude.ai/install.sh -o /tmp/install-claude.sh",
            f"bash /tmp/install-claude.sh {CLAUDE_VERSION}",
            "cp -L /root/.local/bin/claude /usr/local/bin/claude",
            f"echo '{CLAUDE_SHA256}  /usr/local/bin/claude' | sha256sum -c -",
            "chmod 755 /usr/local/bin/claude",
        )
    )
    sandbox = (
        base.apt_install("git", "ripgrep", "util-linux")
        .pip_install(MODAL_SDK)
        .run_commands(
            "useradd --create-home --uid 1000 sandbox",
            "mkdir -p /workspace /opt/cc-host-config",
            "echo '{}' > /opt/cc-host-config/settings.json",
            "chown -R 1000:1000 /workspace /opt/cc-host-config",
        )
        .env({"HOME": "/home/sandbox", "USER": "sandbox"})
    )
    orchestrator = base.pip_install(MODAL_SDK).env({"HOME": "/root", "USER": "root"})
    sidecar = modal.Image.from_registry("caddy:2.11").add_local_file(
        Path(__file__).with_name("Caddyfile"), "/etc/caddy/Caddyfile", copy=True
    )
    return {
        "claude-code-sandbox": sandbox,
        "claude-code-orchestrator": orchestrator,
        "claude-code-sidecar": sidecar,
    }


def build(app_name):
    # Build everything before publishing, so a build failure leaves existing names alone.
    app = modal.App.lookup(app_name, create_if_missing=True)
    with modal.enable_output():
        built = {name: image.build(app) for name, image in images().items()}
    for name, image in built.items():
        image.publish(name)
