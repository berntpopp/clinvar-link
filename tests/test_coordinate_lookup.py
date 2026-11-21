"""A GRCh38 CHROM-POS-REF-ALT coordinate resolves as a get_variant identifier (#154).

VEP hands agents ``13-28018505-C-A``; it matches variant_summary's GRCh38 PositionVCF /
ReferenceAlleleVCF / AlternateAlleleVCF. A coordinate held by several records is
ambiguous_query, as for a multi-allelic rsID (#152).
"""

from __future__ import annotations

import pytest

from clinvar_link.mcp.facade import create_clinvar_mcp
from tests._fixture_db import build_service, call_tool
from tests.test_ambiguous_lookup import AMBIGUOUS


@pytest.fixture
def service(tmp_path):
    service = build_service(tmp_path)
    yield service
    service.repo.close()


@pytest.mark.parametrize(
    "identifier",
    ["17-40000137-T-G", "chr17:40000137:T:G", "CHR17-40000137-t-g", "17:40000137-T-G"],
)
async def test_coordinate_resolves_auto(service, identifier):
    out = await service.get_variant(identifier)
    assert out["vcv_accession"] == "VCV000100002"


async def test_indel_uses_the_vcf_anchor_base(service):
    out = await service.get_variant("2-40000685-AG-A", id_type="coordinate")
    assert out["vcv_accession"] == "VCV000100006"


async def test_grch37_position_is_not_matched(service):
    from clinvar_link.exceptions import DataNotFoundError

    with pytest.raises(DataNotFoundError):
        await service.get_variant("17-41000137-T-G")


async def test_forced_coordinate_rejects_other_shape(service):
    from clinvar_link.exceptions import ToolInputError

    with pytest.raises(ToolInputError, match="coordinate"):
        await service.get_variant("rs80357906", id_type="coordinate")


async def test_coordinate_on_several_records_is_ambiguous(tmp_path):
    service = build_service(tmp_path, AMBIGUOUS)
    mcp = create_clinvar_mcp(service_factory=lambda: service)
    out = await call_tool(mcp, "get_variant", {"identifier": "7-40002603-C-T"})
    service.repo.close()
    assert out["error_code"] == "ambiguous_query"
    assert out["candidates"][:2] == ["VCV000100020", "VCV000100031"]


async def test_bundle_without_the_new_index_still_answers(tmp_path):
    import sqlite3

    service = build_service(tmp_path)
    with sqlite3.connect(tmp_path / "t.sqlite") as conn:  # a bundle built before #154
        conn.execute("DROP INDEX idx_coord_vcf38")
    out = await service.get_variant("17-40000137-T-G")
    service.repo.close()
    assert out["vcv_accession"] == "VCV000100002"
