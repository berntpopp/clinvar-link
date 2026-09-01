"""Tests for clinvar-link configuration (config.py)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from clinvar_link.config import ServerConfig, Settings, settings

ROOT = Path(__file__).resolve().parents[1]


_SHA = "a" * 64
_EXPANDED_SHA = "b" * 64

CLINVAR_RELEASE_TAG = "bundle-2026-08-31"
CLINVAR_BUNDLE_URL = (
    "https://github.com/berntpopp/clinvar-link/releases/download/"
    f"{CLINVAR_RELEASE_TAG}/clinvar.sqlite.zst"
)
CLINVAR_COMPRESSED_SHA256 = "463a73b2ae8aab3bc2758e703b3207d3b82eb23ca90b56b51f3fef3c73babac1"
CLINVAR_EXPANDED_SHA256 = "afb1e6cbc7e4487e2db586e1726b8ca81eaa29b88b35a901c08a2e668ba72b85"


def _production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "ENVIRONMENT": "production",
        "DATA_DIR": Path("/reference/current"),
        "BUNDLE_REFERENCE_ROOT": Path("/reference"),
        "BUNDLE_URL": (
            "https://github.com/berntpopp/clinvar-link/releases/download/"
            "bundle-2026-07-10/clinvar.sqlite.zst"
        ),
        "BUNDLE_RELEASE_TAG": "bundle-2026-07-10",
        "BUNDLE_EXPECTED_SHA256": _SHA,
        "BUNDLE_EXPECTED_EXPANDED_SHA256": _EXPANDED_SHA,
        "BUNDLE_EXPECTED_SCHEMA_VERSION": "1.0.0",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"BUNDLE_URL": "latest"}, "latest"),
        ({"BUNDLE_RELEASE_TAG": None}, "release tag"),
        ({"BUNDLE_EXPECTED_SHA256": None}, "compressed SHA-256"),
        ({"BUNDLE_EXPECTED_EXPANDED_SHA256": None}, "expanded SHA-256"),
    ],
)
def test_production_requires_exact_bundle_identity(
    override: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _production_settings(**override)


def test_production_rejects_development_latest() -> None:
    with pytest.raises(ValidationError, match="development_latest"):
        _production_settings(DEVELOPMENT_LATEST=True)


def test_development_latest_is_explicit_opt_in() -> None:
    with pytest.raises(ValidationError, match="DEVELOPMENT_LATEST"):
        Settings(BUNDLE_URL="latest")
    assert Settings(BUNDLE_URL="latest", DEVELOPMENT_LATEST=True).BUNDLE_URL == "latest"


def test_vendored_data_contract_matches_recorded_hash() -> None:
    schema = ROOT / "vendor/genefoundry/data-release-manifest.schema.json"
    recorded = (ROOT / "vendor/genefoundry/CONTRACT_SHA256").read_text().strip()
    assert hashlib.sha256(schema.read_bytes()).hexdigest() == recorded


def test_data_workflow_is_draft_first_and_non_overwriting() -> None:
    workflow = (ROOT / ".github/workflows/data-bundle.yml").read_text()
    assert "build:" in workflow and "publish:" in workflow
    assert "draft=true" in workflow
    # The invariant is "release assets carry a build-provenance attestation, and the
    # action supplying it is pinned by commit SHA" — not "pinned to one specific SHA".
    # Hard-coding the SHA made every legitimate version bump of this action a CI
    # failure, which is a maintenance trap rather than a supply-chain guarantee.
    assert re.search(r"actions/attest-build-provenance@[0-9a-f]{40} # v\d", workflow)
    assert "gh release verify-asset" in workflow
    assert "--clobber" not in workflow


def test_production_compose_splits_init_and_read_only_reference() -> None:
    base = (ROOT / "docker/docker-compose.yml").read_text()
    production = (ROOT / "docker/docker-compose.prod.yml").read_text()
    assert "clinvar-data-init:" in base
    # The init sidecar owns the writable reference volume; the server only reads it.
    assert "clinvar-reference:/data\n" in base
    assert "clinvar-reference:/data:ro" in base
    assert "clinvar-reference:/data:ro" in production
    assert "CLINVAR_LINK_ENVIRONMENT: production" in production
    assert "CLINVAR_LINK_BUNDLE_RELEASE_TAG" in production
    assert "CLINVAR_LINK_BUNDLE_EXPECTED_EXPANDED_SHA256" in production
    # Production installs exactly the pinned release rather than reusing the volume.
    assert '["clinvar-link-data", "pull"]' in production


def test_production_compose_uses_approved_restart_policies() -> None:
    production = (ROOT / "docker/docker-compose.prod.yml").read_text()
    init_service, application_service = production.split("  clinvar-link:\n", maxsplit=1)

    assert 'restart: "no"' in init_service
    assert "restart: unless-stopped" in application_service
    assert "restart: on-failure" not in production


def test_release_config_declares_the_init_sidecar_role() -> None:
    """The central compose gate authorizes the sidecar by role, never by name."""
    config = json.loads((ROOT / "container-release.json").read_text())
    (auxiliary,) = config["service"]["auxiliary"]
    assert auxiliary["name"] == "clinvar-data-init"
    assert auxiliary["role"] == "init"
    # The bundle is fetched from GitHub Releases, so the sidecar needs egress.
    assert auxiliary["egress"] == "approved-networks"
    assert sorted(auxiliary["writable_targets"]) == ["/data", "/tmp"]  # noqa: S108
    assert config["smoke"]["profile"] == "immutable-bundle"


def test_container_release_pins_verified_clinvar_bundle() -> None:
    """The central release record must select the accepted immutable bundle."""
    config = json.loads((ROOT / "container-release.json").read_text())
    assert config["data"]["release_tag"] == CLINVAR_RELEASE_TAG
    assert config["data"]["digest"] == f"sha256:{CLINVAR_COMPRESSED_SHA256}"


def _docker_example_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env.docker.example").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def test_docker_example_records_verified_clinvar_bundle_pin() -> None:
    """The documented Compose env source must carry the complete accepted identity."""
    env = _docker_example_env()
    assert env["CLINVAR_DATA_BUNDLE_URL"] == CLINVAR_BUNDLE_URL
    assert env["CLINVAR_DATA_RELEASE_TAG"] == CLINVAR_RELEASE_TAG
    assert env["CLINVAR_DATA_SHA256"] == CLINVAR_COMPRESSED_SHA256
    assert env["CLINVAR_DATA_EXPANDED_SHA256"] == CLINVAR_EXPANDED_SHA256
    assert env["CLINVAR_DATA_SCHEMA_VERSION"] == "1.0.0"


@pytest.mark.parametrize("filename", ["docker-compose.prod.yml", "docker-compose.npm.yml"])
def test_deployment_compose_consumes_exact_production_bundle_pin(filename: str) -> None:
    """Production and NPM Compose sources must reject floating/development data."""
    compose = (ROOT / "docker" / filename).read_text()
    assert "CLINVAR_LINK_ENVIRONMENT: production" in compose or (
        "CLINVAR_LINK_ENVIRONMENT=production" in compose
    )
    assert "CLINVAR_LINK_BUNDLE_URL" in compose and "${CLINVAR_DATA_BUNDLE_URL" in compose
    assert "CLINVAR_LINK_BUNDLE_RELEASE_TAG" in compose and "${CLINVAR_DATA_RELEASE_TAG" in compose
    assert "CLINVAR_LINK_BUNDLE_EXPECTED_SHA256" in compose and "${CLINVAR_DATA_SHA256" in compose
    assert "CLINVAR_LINK_BUNDLE_EXPECTED_EXPANDED_SHA256" in compose and (
        "${CLINVAR_DATA_EXPANDED_SHA256" in compose
    )
    assert "CLINVAR_LINK_DEVELOPMENT_LATEST=true" not in compose
    assert "CLINVAR_LINK_BUNDLE_URL=latest" not in compose


def test_npm_compose_preserves_container_hardening() -> None:
    """The NPM backend remains private and runs with the fleet hardening policy."""
    compose = (ROOT / "docker/docker-compose.npm.yml").read_text()
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:\n      - ALL" in compose
    assert "ports:" not in compose
    assert 'expose:\n      - "8000"' in compose


def test_npm_compose_forces_pinned_materialization_before_readonly_server() -> None:
    """NPM must pull the pin once, then run the app without bootstrap or writes."""
    compose = yaml.safe_load((ROOT / "docker/docker-compose.npm.yml").read_text())
    init = compose["services"]["clinvar-data-init"]
    app = compose["services"]["clinvar_link"]

    assert init["entrypoint"] == ["clinvar-link-data", "pull"]
    assert init["environment"]["CLINVAR_LINK_BUNDLE_URL"].startswith("${CLINVAR_DATA_BUNDLE_URL:")
    assert init["environment"]["CLINVAR_LINK_BUNDLE_EXPECTED_SHA256"].startswith(
        "${CLINVAR_DATA_SHA256:"
    )
    assert init["environment"]["CLINVAR_LINK_BUNDLE_EXPECTED_EXPANDED_SHA256"].startswith(
        "${CLINVAR_DATA_EXPANDED_SHA256:"
    )
    assert init["volumes"] == ["clinvar-reference:/data"]
    assert init["restart"] == "no"

    assert app["depends_on"]["clinvar-data-init"]["condition"] == ("service_completed_successfully")
    assert app["entrypoint"][:2] == ["clinvar-link", "serve"]
    assert "bootstrap" not in app["entrypoint"]
    assert app["volumes"] == ["clinvar-reference:/data:ro"]
    assert app["read_only"] is True


def test_compose_declares_no_top_level_extension_fields() -> None:
    """`docker compose config` emits `x-*` verbatim and the central policy rejects it."""
    for name in ("docker-compose.yml", "docker-compose.prod.yml"):
        text = (ROOT / "docker" / name).read_text()
        assert not any(line.startswith("x-") for line in text.splitlines())


def test_settings_loads_with_defaults() -> None:
    """A bare Settings() constructs with the documented defaults."""
    config = Settings()
    assert config.DB_FILENAME == "clinvar.sqlite"
    assert isinstance(config.DATA_DIR, Path)
    assert config.AUTO_BOOTSTRAP is False
    assert config.ENABLE_SUBMISSION_SUMMARY is False
    # hgvs4variation is opt-in (default off) so the shipped bundle stays lean.
    assert config.ENABLE_HGVS4VARIATION is False
    assert config.CACHE_SIZE == 1024
    assert config.CACHE_TTL_MINUTES == 60
    assert config.LOG_FORMAT == "json"
    assert config.CORS_ORIGINS == "*"
    assert config.SOURCE_URL.endswith("variant_summary.txt.gz")


def test_db_path_joins_data_dir_and_filename() -> None:
    """db_path is DATA_DIR / DB_FILENAME on the module singleton."""
    assert settings.db_path == settings.DATA_DIR / settings.DB_FILENAME


def test_db_path_on_fresh_instance() -> None:
    """db_path composes correctly on a freshly-constructed instance too."""
    config = Settings()
    assert config.db_path == config.DATA_DIR / config.DB_FILENAME


def test_refresh_ttl_days_default() -> None:
    """REFRESH_TTL_DAYS defaults to 7."""
    assert settings.REFRESH_TTL_DAYS == 7


def test_env_override_db_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLINVAR_LINK_ env prefix overrides DB_FILENAME for a fresh Settings()."""
    monkeypatch.setenv("CLINVAR_LINK_DB_FILENAME", "x.sqlite")
    config = Settings()
    assert config.DB_FILENAME == "x.sqlite"
    assert config.db_path == config.DATA_DIR / "x.sqlite"


def test_env_override_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """DATA_DIR is overridable via the env prefix and flows into db_path."""
    monkeypatch.setenv("CLINVAR_LINK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CLINVAR_LINK_DB_FILENAME", "clinvar.sqlite")
    config = Settings()
    assert tmp_path == config.DATA_DIR
    assert config.db_path == tmp_path / "clinvar.sqlite"


def test_cors_origins_list_wildcard() -> None:
    """cors_origins_list returns ['*'] for the wildcard default."""
    assert Settings().cors_origins_list == ["*"]


def test_cors_origins_list_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    """cors_origins_list splits comma-separated origins."""
    monkeypatch.setenv("CLINVAR_LINK_CORS_ORIGINS", "https://a.test, https://b.test")
    config = Settings()
    assert config.cors_origins_list == ["https://a.test", "https://b.test"]


def test_server_config_from_env_has_valid_port() -> None:
    """ServerConfig.from_env() returns a config with a usable port."""
    config = ServerConfig.from_env()
    assert isinstance(config, ServerConfig)
    assert isinstance(config.port, int)
    assert 1 <= config.port <= 65535
    assert config.mcp_path.startswith("/")


class _TolerantSafeLoader(yaml.SafeLoader):
    """A SafeLoader that ignores unknown custom tags (e.g. Compose ``!reset``)."""


_TolerantSafeLoader.add_multi_constructor(
    "!",
    lambda loader, suffix, node: (
        loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode) else None
    ),
)


def test_fleet_deploy_overlay_declares_numeric_user() -> None:
    """The fleet controller's Compose projection requires a numeric non-root ``user``
    on every service of the deployed NPM overlay; the shared release gate forbids
    the same field on the Compose files it validates."""
    # ruff's S506 only checks the loader name, not the class hierarchy;
    # _TolerantSafeLoader subclasses yaml.SafeLoader, so this is safe.
    compose = yaml.load(
        (ROOT / "docker/docker-compose.npm.yml").read_text(),
        Loader=_TolerantSafeLoader,  # noqa: S506
    )
    user_pattern = re.compile(r"^[1-9][0-9]*:[1-9][0-9]*$")
    for name, service in compose["services"].items():
        user = service.get("user")
        assert user is not None, f"{name}: missing numeric user"
        assert user_pattern.match(user), f"{name}: user {user!r} is not numeric non-root"

    release_config = json.loads((ROOT / "container-release.json").read_text())
    for filename in release_config["service"]["compose_files"]:
        release_compose = yaml.load(
            (ROOT / filename).read_text(),
            Loader=_TolerantSafeLoader,  # noqa: S506
        )
        for name, service in release_compose["services"].items():
            assert "user" not in service, f"{name}: user must not appear in {filename}"
