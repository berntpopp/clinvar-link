"""Shared fixtures: a fixture-backed ClinVar index, repository, service, facade.

These session/function fixtures mirror the per-module helpers in
``tests/_fixture_db.py`` and the local fixtures in ``test_service.py`` /
``test_repository.py``. Those module-local fixtures take precedence over the
same-named fixtures here, so existing tests are unaffected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clinvar_link.config import Settings
from clinvar_link.data.repository import ClinVarRepository
from clinvar_link.ingest.builder import build_database
from clinvar_link.mcp.facade import create_clinvar_mcp
from clinvar_link.services.clinvar_service import ClinVarService

FIXTURE = Path(__file__).parent / "fixtures" / "variant_summary_sample.txt"


@pytest.fixture(scope="session")
def built_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a small ClinVar index from the checked-in fixture once per session."""
    d = tmp_path_factory.mktemp("clinvar_data")
    cfg = Settings(DATA_DIR=d, DB_FILENAME="clinvar.sqlite")
    build_database(cfg, source_path=FIXTURE, last_modified="Mon, 01 Jan 2026 00:00:00 GMT")
    return cfg.db_path


@pytest.fixture
def repo(built_db: Path) -> Any:
    """An open read-only repository over the fixture database."""
    r = ClinVarRepository(built_db)
    yield r
    r.close()


@pytest.fixture
def service(repo: ClinVarRepository) -> ClinVarService:
    """A service backed by the fixture repository."""
    return ClinVarService(repo)


@pytest.fixture
def facade(service: ClinVarService) -> Any:
    """A FastMCP facade with the fixture service injected."""
    return create_clinvar_mcp(service_factory=lambda: service)
