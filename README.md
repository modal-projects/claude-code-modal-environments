# Claude Code on Modal

Run Claude Code in a Modal Sandbox, resume from filesystem snapshots, and call
APIs through a sidecar that keeps credentials outside the sandbox.

Modal provides the infrastructure that hosts Claude. Working on a codebase that
itself builds or runs on Modal is an additional capability: enable the
[optional playground](#optional-work-on-a-modal-codebase) for that.

[Set up](#1-prepare-your-terminal) · [Use Claude](#4-use-claude) · [Customize](#customize)

## Before you start

You need:

- `uv` installed locally.
- A Modal workspace with permission to deploy apps and create Secrets.
- A Claude self-hosted environment, including its **environment ID and secret**.

## 1. Prepare your terminal

**On your computer**, open a terminal in this repository's root directory.
Replace `<...>` placeholders with your values throughout this guide.
Choose an existing Modal environment for the app and its Secrets:

```sh
uv sync --locked
uv run modal setup
export MODAL_ENVIRONMENT='<deployment-environment>'
```

Keep this terminal open for the remaining commands. `MODAL_ENVIRONMENT` selects
the deployment environment for this terminal session.

## 2. Add credentials

Store the **Claude environment ID and secret** in `claude-code-environment`.
Paste the values into this command:

```sh
uv run modal secret create claude-code-environment \
  CLAUDE_ENVIRONMENT_ID='<claude-environment-id>' \
  CLAUDE_ENVIRONMENT_SECRET='<claude-environment-secret>'
```

## 3. Deploy

```sh
uv run modal deploy src/app.py
```

This starts the service that receives Claude sessions and launches their sandboxes.

## 4. Use Claude

In Claude Code, select your self-hosted environment and **No repository**.
Start a conversation with this prompt:

> Create a Python script that prints the first ten Fibonacci numbers, then run it.

Continue working in the conversation. The example saves the sandbox's filesystem
when it exits and restores it automatically when you return.

## Customize

Edit the code, then run `uv run modal deploy src/app.py` again.

| Change | Edit here |
| --- | --- |
| CPU, memory, GPU | [Sandbox resources](src/app.py#L180): `cpu=2`, `memory=4096`, `gpu=None`. Try `gpu="T4"`. |
| Packages or Modal SDK installation | [Sandbox image](src/images.py#L25) |
| Claude version | [Version and checksum](src/images.py#L7) |
| Let Claude build and run Modal apps | [Optional playground](#optional-work-on-a-modal-codebase) |
| APIs and credentials | [Add another API](#add-another-api) |
| Allowed network destinations | [Network allowlist](src/app.py#L188) |

**Package changes need a new conversation.** Resumed conversations keep their
saved filesystem. Resource and sidecar changes apply on the next sandbox launch.

### Optional: work on a Modal codebase

Follow this section when Claude needs to build, run, or deploy Modal apps as part
of its work. The `claude-code-playground` environment is where those apps run.
The infrastructure hosting Claude stays in your chosen deployment environment.

**Create the playground** in the same Modal workspace:

```sh
uv run modal environment create claude-code-playground
```

**Give Claude access.** Create a dedicated API token in
[Modal token settings](https://modal.com/settings/tokens). To limit SDK access to
the playground, use a [service user](https://modal.com/docs/guide/service-users)
with **Contributor** access to `claude-code-playground`. Service users require
a shared Team or Enterprise workspace.

The playground also includes a protected example API to demonstrate HTTP
credential injection. Create its [proxy token](https://modal.com/docs/guide/webhook-proxy-auth):

```sh
uv run modal workspace proxy-tokens create
```

The output's `Modal-Key` is the proxy token ID; `Modal-Secret` is its secret.
Give the token access to the playground:

```sh
uv run modal workspace proxy-tokens allow '<proxy-token-id>' claude-code-playground
```

Store both token pairs in **one Secret in your deployment environment**.
Keep `MODAL_ENVIRONMENT` set to the value from step 1:

```sh
uv run modal secret create claude-code-modal \
  MODAL_TOKEN_ID='<modal-api-token-id>' \
  MODAL_TOKEN_SECRET='<modal-api-token-secret>' \
  CLAUDE_MODAL_PROXY_TOKEN_ID='<proxy-token-id>' \
  CLAUDE_MODAL_PROXY_TOKEN_SECRET='<proxy-token-secret>'
```

Only the sidecar receives this Secret. It adds credentials to requests from Claude.

**Enable the playground** in [app.py](src/app.py#L16):

```python
SIDECAR_SECRET_NAMES = ["claude-code-modal"]
PLAYGROUND_ENVIRONMENT = "claude-code-playground"
```

Deploy the example API into the playground, then redeploy the hosting app:

```sh
uv run modal deploy --env claude-code-playground src/example_api.py
uv run modal deploy src/app.py
```

In a new Claude conversation, try:

> Run `modal app list` to list the apps in `claude-code-playground`.
> Then run `curl "$CLAUDE_SIDECAR_URL/"` and show me the response.

The API returns `{"message": "Hello from Modal"}`. Both commands authenticate
through the sidecar. The Modal SDK is already installed in the sandbox image.

### Add another API

Use the sidecar for other APIs independently of the Modal playground.

1. Create a Secret in your deployment environment:

   ```sh
   uv run modal secret create claude-code-other-api OTHER_API_KEY='<api-key>'
   ```

2. Add its name to [`SIDECAR_SECRET_NAMES`](src/app.py#L16):

   ```python
   SIDECAR_SECRET_NAMES = ["claude-code-other-api"]
   ```

3. Add a route in [Caddyfile](src/Caddyfile) that forwards to your API and sets
   its authentication header from `{env.OTHER_API_KEY}`.
4. Redeploy with `uv run modal deploy src/app.py`.

Keep any existing Secret names when adding another. Each Secret can hold multiple
keys; use distinct key names for different APIs.

## Reference

- [Something failed](docs/modal-architecture.md#troubleshooting): logs and common symptoms.
- [How it works](docs/modal-architecture.md): components, credentials, and the two sidecar ports.
- [Update credentials](docs/modal-architecture.md#update-credentials): change keys in an existing Secret.
- [Session state](docs/modal-architecture.md#session-state): the records used to resume and prevent duplicate sandboxes.
