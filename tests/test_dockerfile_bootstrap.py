"""Build-hardening regression: the uv builder bootstrap must be digest-pinned.

F-19 — replace the floating `pip install --upgrade pip uv` bootstrap in the
builder stage with a digest-pinned `COPY --from=ghcr.io/astral-sh/uv:...` so the
image build cannot silently pull an unpinned pip/uv from the network.
"""

from pathlib import Path


def test_dockerfile_pins_uv_and_has_no_floating_pip_upgrade():
    text = Path("docker/Dockerfile").read_text()
    assert "pip install --upgrade" not in text, "floating pip/uv upgrade must be removed"
    assert (
        "ghcr.io/astral-sh/uv:0.8.7@sha256:"
        "1e26f9a868360eeb32500a35e05787ffff3402f01a8dc8168ef6aee44aef0aab"
    ) in text
