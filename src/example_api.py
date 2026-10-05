"""A protected test API: uv run modal deploy --env claude-code-playground src/example_api.py"""

import modal

app = modal.App("claude-code-example-api")


@app.function(image=modal.Image.debian_slim().pip_install("fastapi[standard]"))
@modal.fastapi_endpoint(requires_proxy_auth=True)
def hello():
    return {"message": "Hello from Modal"}
