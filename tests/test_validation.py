"""Tests for the clean Home Assistant validation runner."""

from __future__ import annotations

import pytest

from scripts import validate


def test_docker_command_uses_versioned_image_and_read_only_source() -> None:
    command = validate.docker_command("2026.9.0")

    assert command[:4] == ["docker", "run", "--rm", "--pull=always"]
    assert f"type=bind,source={validate.ROOT},target=/workspace,readonly" in command
    assert "ghcr.io/home-assistant/home-assistant:2026.9.0" in command
    assert "uv pip install --system --index-strategy unsafe-best-match" in command[-1]
    assert "import aiousbwatcher, bluetooth_adapters, homeassistant" in command[-1]
    assert "-p no:cacheprovider" in command[-1]


@pytest.mark.parametrize(
    "version",
    ["latest", "2026.9", "2026.9.0; echo unsafe", "ghcr.io/example/image:tag"],
)
def test_docker_command_rejects_unversioned_or_unsafe_image_tags(version: str) -> None:
    with pytest.raises(ValueError, match="Invalid Home Assistant version"):
        validate.docker_command(version)


def test_workflows_use_container_validation_without_bare_home_assistant_install() -> None:
    for workflow_name in ("hassfest.yaml", "release.yaml"):
        workflow = (validate.ROOT / ".github" / "workflows" / workflow_name).read_text(
            encoding="utf-8"
        )
        assert "scripts/validate.py --home-assistant" in " ".join(workflow.split())
        assert "pip install homeassistant" not in workflow


def test_release_keeps_identity_and_atomic_push_contract() -> None:
    workflow = (validate.ROOT / ".github" / "workflows" / "release.yaml").read_text(
        encoding="utf-8"
    )

    assert "GIT_AUTHOR_NAME: Nick Betcher" in workflow
    assert "GIT_AUTHOR_EMAIL: nick@nickbetcher.com" in workflow
    assert "GIT_COMMITTER_NAME: Nick Betcher" in workflow
    assert "GIT_COMMITTER_EMAIL: nick@nickbetcher.com" in workflow
    assert "github-actions[bot]" not in workflow
    atomic_push = (
        'git push --atomic origin HEAD:${{ github.ref_name }} "${{ steps.bump.outputs.version }}"'
    )
    assert atomic_push in workflow
