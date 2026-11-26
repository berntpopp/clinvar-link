"""Response-Envelope Standard v1.2: every data tool's success validates against the schema.

The schema is vendored from berntpopp/genefoundry-envelope, tag v1.2.0
(https://raw.githubusercontent.com/berntpopp/genefoundry-envelope/v1.2.0/envelope-v1.2.schema.json).
"""

from __future__ import annotations

import io
import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from clinvar_link.config import Settings
from clinvar_link.data.repository import ClinVarRepository
from clinvar_link.ingest.builder import build_database
from clinvar_link.ingest.submissions import load_submissions
from clinvar_link.mcp.facade import create_clinvar_mcp
from clinvar_link.services import ClinVarService
from tests._fixture_db import FIXTURE, call_tool

SCHEMA_PATH = Path(__file__).with_name("envelope-v1.2.schema.json")
SCHEMA_SHA256 = "c3c47e20d7dd189aac2568fc963b5514d7a5ac2c3e5f21d126f42c96d1ad1ae3"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
FIXTURES = Path(__file__).parent.parent / "fixtures"
RETRIEVED_AT = "2026-11-17T06:00:00Z"

# One successful call per data tool: (index, arguments). get_server_capabilities is an
# ops/meta tool and outside the v1.2 rule.
CALLS: dict[str, tuple[str, dict[str, Any]]] = {
    "get_variant": ("germline", {"identifier": "VCV000100001"}),
    "get_variants": ("germline", {"identifiers": ["VCV000100001", "rs80357906"]}),
    "search_variants": ("germline", {"query": "BRCA1"}),
    "get_variants_by_pmid": ("germline", {"pmid": "29058690"}),
    "get_gene_clinvar_summary": ("germline", {"gene_symbol": "BRCA1"}),
    "get_variants_by_gene": ("germline", {"gene_symbol": "BRCA1"}),
    "search_somatic_classifications": ("somatic", {}),
}


@pytest.fixture(scope="module")
def facades(tmp_path_factory: pytest.TempPathFactory) -> Any:
    germline = Settings(DATA_DIR=tmp_path_factory.mktemp("germline"), DB_FILENAME="g.sqlite")
    build_database(
        germline,
        source_path=FIXTURE,
        citations_source_path=FIXTURES / "var_citations_sample.txt",
        last_modified="Mon, 16 Nov 2026 21:41:14 GMT",
        source_retrieved_at=RETRIEVED_AT,
    )
    somatic = Settings(DATA_DIR=tmp_path_factory.mktemp("somatic"), DB_FILENAME="s.sqlite")
    xml = (FIXTURES / "vcv_somatic_sample.xml").read_bytes()
    build_database(
        somatic,
        source_path=FIXTURES / "variant_summary_somatic.txt",
        secondary_loader=lambda conn: load_submissions(conn, io.BytesIO(xml)),
        last_modified="Mon, 16 Nov 2026 21:41:14 GMT",
        source_retrieved_at=RETRIEVED_AT,
    )
    services = {
        "germline": ClinVarService(ClinVarRepository(germline.db_path)),
        "somatic": ClinVarService(ClinVarRepository(somatic.db_path)),
    }
    yield {
        name: create_clinvar_mcp(service_factory=lambda s=service: s)
        for name, service in services.items()
    }
    for service in services.values():
        service.repo.close()


def test_vendored_schema_is_the_v1_2_0_release() -> None:
    assert sha256(SCHEMA_PATH.read_bytes()).hexdigest() == SCHEMA_SHA256


async def test_every_data_tool_is_covered(facades: Any) -> None:
    registered = {tool.name for tool in await facades["germline"].list_tools()}
    assert registered - {"get_server_capabilities"} == set(CALLS)


@pytest.mark.parametrize("tool", sorted(CALLS))
async def test_success_validates_against_the_envelope_schema(facades: Any, tool: str) -> None:
    index, arguments = CALLS[tool]
    envelope = await call_tool(facades[index], tool, arguments)
    assert envelope["success"] is True
    jsonschema.validate(envelope, SCHEMA)


@pytest.mark.parametrize("tool", sorted(CALLS))
async def test_success_carries_provenance(facades: Any, tool: str) -> None:
    index, arguments = CALLS[tool]
    meta = (await call_tool(facades[index], tool, arguments))["_meta"]
    assert meta["source"] == "clinvar"
    assert meta["data_version"] == meta["clinvar_release_date"] != ""
    # The download time recorded with the index, not the time of this call.
    assert meta["retrieved_at"] == RETRIEVED_AT


async def test_error_envelope_validates_too(facades: Any) -> None:
    envelope = await call_tool(facades["germline"], "get_variant", {"identifier": "VCV999999999"})
    assert envelope["success"] is False
    jsonschema.validate(envelope, SCHEMA)
