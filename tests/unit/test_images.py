from types import SimpleNamespace

import pytest

import images


@pytest.mark.parametrize("fail_last_build", (False, True))
def test_build_publishes_names_only_after_all_images_build(monkeypatch, fail_last_build):
    owner = object()

    def lookup(name, *, create_if_missing):
        assert name == "test-app"
        assert create_if_missing is True
        return owner

    monkeypatch.setattr(images.modal.App, "lookup", lookup)
    built = []
    published = []

    def recipes():
        def build(name):
            def run(app):
                assert app is owner
                built.append(name)
                if fail_last_build and name == "sidecar":
                    raise RuntimeError("Build failed")
                return SimpleNamespace(
                    object_id="im-" + name,
                    publish=lambda name: published.append(name),
                )

            return SimpleNamespace(build=run)

        return {
            f"claude-code-{name}": build(name) for name in ("sandbox", "orchestrator", "sidecar")
        }

    monkeypatch.setattr(images, "images", recipes)
    if fail_last_build:
        with pytest.raises(RuntimeError, match="Build failed"):
            images.build("test-app")
        assert published == []
    else:
        images.build("test-app")
        assert published == [f"claude-code-{name}" for name in built]
    assert built == ["sandbox", "orchestrator", "sidecar"]
