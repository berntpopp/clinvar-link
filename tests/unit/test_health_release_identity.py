"""`/health` publishes the runtime-v1 data identity, and refuses to lie about it.

The fleet controller reads readiness to decide whether a deployment is serving the data
release it was configured for, so ``/health`` has to carry both halves of the comparison
and has to go unhealthy when they differ. A deployment that pins nothing (development)
publishes no ``release_identity`` at all rather than an empty or invented one.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import structlog

from clinvar_link import config
from clinvar_link.config import ServerConfig
from clinvar_link.logging_config import configure_logging
from clinvar_link.runtime_data_identity import write_identity_manifest
from clinvar_link.server_manager import UnifiedServerManager

RELEASE_TAG = "bundle-2026-08-31"


async def _health(monkeypatch: pytest.MonkeyPatch, data_dir: Path, **overrides: object) -> tuple:
    monkeypatch.setattr(config.settings, "DATA_DIR", data_dir, raising=False)
    monkeypatch.setattr(config.settings, "DB_FILENAME", "clinvar.sqlite", raising=False)
    monkeypatch.setattr(config.settings, "BUNDLE_RELEASE_TAG", None, raising=False)
    monkeypatch.setattr(config.settings, "DATA_IDENTITY_DIGEST", None, raising=False)
    for key, value in overrides.items():
        monkeypatch.setattr(config.settings, key, value, raising=False)

    configure_logging("INFO", "console")
    manager = UnifiedServerManager()
    manager.logger = structlog.get_logger("clinvar_link_test")
    app = await manager._create_fastapi_app(ServerConfig())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")
    return response.status_code, response.json()


@pytest.fixture
def sealed_data_dir(built_db: Path, tmp_path: Path) -> tuple[Path, str]:
    """A materialized root holding the fixture index, sealed with its identity manifest."""
    root = tmp_path / "materialized"
    root.mkdir()
    (root / "clinvar.sqlite").write_bytes(built_db.read_bytes())
    identity = write_identity_manifest(root, RELEASE_TAG, [root / "clinvar.sqlite"])
    return root, identity["digest"]


async def test_health_publishes_the_verified_runtime_identity(
    sealed_data_dir: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, digest = sealed_data_dir
    status, body = await _health(
        monkeypatch,
        root,
        BUNDLE_RELEASE_TAG=RELEASE_TAG,
        DATA_IDENTITY_DIGEST=digest,
    )
    assert status == 200
    assert body["status"] == "healthy"
    assert body["data_available"] is True
    identity = body["release_identity"]
    assert identity["schema_version"] == 1
    expected = {"release_tag": RELEASE_TAG, "digest": digest}
    assert identity["data_identity"] == {"expected": expected, "actual": expected}
    assert set(identity["data_identity"]["expected"]) == {"release_tag", "digest"}
    assert set(identity["data_identity"]["actual"]) == {"release_tag", "digest"}


async def test_health_is_unhealthy_when_the_pinned_digest_differs(
    sealed_data_dir: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A volume carrying some other data release must never be reported as ready."""
    root, _ = sealed_data_dir
    status, body = await _health(
        monkeypatch,
        root,
        BUNDLE_RELEASE_TAG=RELEASE_TAG,
        DATA_IDENTITY_DIGEST=f"sha256:{'0' * 64}",
    )
    assert status == 503
    assert body["status"] == "degraded"
    assert body["data_available"] is False
    assert "release_identity" not in body
    assert body["reason"] == "reference data unavailable"


async def test_health_is_unhealthy_when_the_pinned_release_tag_differs(
    sealed_data_dir: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, digest = sealed_data_dir
    status, body = await _health(
        monkeypatch,
        root,
        BUNDLE_RELEASE_TAG="bundle-2026-09-07",
        DATA_IDENTITY_DIGEST=digest,
    )
    assert status == 503
    assert body["data_available"] is False


async def test_health_is_unhealthy_when_the_volume_carries_no_identity(
    built_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unsealed volume cannot prove anything, so it must fail closed, not pass."""
    status, body = await _health(
        monkeypatch,
        built_db.parent,
        BUNDLE_RELEASE_TAG=RELEASE_TAG,
        DATA_IDENTITY_DIGEST=f"sha256:{'0' * 64}",
    )
    assert status == 503
    assert body["data_available"] is False


async def test_health_omits_release_identity_when_nothing_is_pinned(
    built_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Development pins no identity, so there is nothing to prove and nothing to publish."""
    status, body = await _health(monkeypatch, built_db.parent)
    assert status == 200
    assert body["status"] == "healthy"
    assert body["data_available"] is True
    assert "release_identity" not in body
