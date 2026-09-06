"""Run validation inside a clean, versioned Home Assistant container."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE_REPOSITORY = "ghcr.io/home-assistant/home-assistant"
VERSION_PATTERN = re.compile(r"[0-9]{4}\.[0-9]{1,2}\.[0-9]+")

CONTAINER_SCRIPT = """
set -eu
uv pip install --system --index-strategy unsafe-best-match --requirement requirements_test.txt
python -c 'import aiousbwatcher, bluetooth_adapters, homeassistant'
ruff check --no-cache custom_components/elkbledom tests scripts
ruff format --check --no-cache custom_components/elkbledom tests scripts
python -m pytest -q -p no:cacheprovider
""".strip()


def docker_command(home_assistant_version: str, pull: str = "always") -> list[str]:
    """Build the Docker command without involving a host shell."""
    if VERSION_PATTERN.fullmatch(home_assistant_version) is None:
        raise ValueError(f"Invalid Home Assistant version: {home_assistant_version!r}")
    if pull not in {"always", "missing", "never"}:
        raise ValueError(f"Invalid Docker pull policy: {pull!r}")

    return [
        "docker",
        "run",
        "--rm",
        f"--pull={pull}",
        "--mount",
        f"type=bind,source={ROOT},target=/workspace,readonly",
        "--workdir",
        "/workspace",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        f"{IMAGE_REPOSITORY}:{home_assistant_version}",
        "/bin/sh",
        "-c",
        CONTAINER_SCRIPT,
    ]


def main() -> None:
    """Parse command-line arguments and run validation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home-assistant", required=True, help="Home Assistant image version")
    parser.add_argument(
        "--pull",
        choices=("always", "missing", "never"),
        default="always",
        help="Docker image pull policy (default: always)",
    )
    args = parser.parse_args()
    subprocess.run(docker_command(args.home_assistant, args.pull), check=True)  # noqa: S603


if __name__ == "__main__":
    main()
