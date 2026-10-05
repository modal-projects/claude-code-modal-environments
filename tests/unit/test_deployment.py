import runpy

import app as deployment


def test_remote_import_uses_deployed_images_and_deployment_id_without_building(monkeypatch):
    import images

    def unexpected_build(app_name):
        raise AssertionError("Remote containers must not build images")

    monkeypatch.setattr(images, "build", unexpected_build)
    for key, value in deployment.runtime_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("CLAUDE_ENVIRONMENT_SECRET", raising=False)
    monkeypatch.delenv("CLAUDE_ENVIRONMENT_ID", raising=False)
    monkeypatch.setattr(deployment.modal, "is_local", lambda: False)
    remote = runpy.run_path(deployment.__file__)
    for component in ("sandbox", "sidecar"):
        assert repr(remote[f"{component}_image"]) == repr(getattr(deployment, f"{component}_image"))
        assert f"'claude-code-{component}'" in repr(remote[f"{component}_image"])
    assert remote["DEPLOYMENT_ID"] == deployment.DEPLOYMENT_ID
    assert "CLAUDE_ENVIRONMENT_SECRET" not in remote["runtime_env"]
    assert "CLAUDE_ENVIRONMENT_ID" not in remote["runtime_env"]
    assert not any(key.endswith("_IMAGE_ID") for key in remote["runtime_env"])

    def dependencies(function):
        return [type(dep).__name__ for dep in function._deps_(only_explicit_mounts=True)]

    # Modal pairs local and remote dependencies by position.
    assert (
        dependencies(remote["provision"])
        == dependencies(deployment.provision)
        == ["Secret", "Secret", "Image"]
    )
    assert dependencies(remote["Orchestrator"]._get_class_service_function()) == dependencies(
        deployment.Orchestrator._get_class_service_function()
    )


def test_named_claude_secret_is_attached_to_control_functions():
    def named_secrets(function):
        return [
            dep.name
            for dep in function._deps_(only_explicit_mounts=True)
            if isinstance(dep, deployment.modal.Secret) and dep.name is not None
        ]

    assert named_secrets(deployment.provision) == ["claude-code-environment"]
    assert named_secrets(deployment.Orchestrator._get_class_service_function()) == [
        "claude-code-environment"
    ]
    assert "CLAUDE_ENVIRONMENT_SECRET" not in deployment.runtime_env
    assert "CLAUDE_ENVIRONMENT_ID" not in deployment.runtime_env
    assert "claude-code-environment" not in deployment.SIDECAR_SECRET_NAMES
