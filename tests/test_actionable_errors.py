"""Every rejected input must be actionable, and no rejection may masquerade as a miss.

Review findings on PR #27 (Codex, gpt-5.6-sol):

1. The int64 guard fixed `get_variant` and left `get_variants` silently wrong: the batch path
   caught EVERY ToolInputError and turned it into `found: false`, so a malformed identifier came
   back as `success: true, found_count: 0` — once again indistinguishable from a valid-but-absent
   record. That is the very defect this PR exists to kill, reintroduced one tool over.
2. Only SOME call sites raised an actionable ToolInputError. A real call —
   `get_variant(identifier="rs334", id_type="vcv")` — still answered "The request was rejected as
   invalid." with no field_errors, because the error builder only produces detail when BOTH
   `field` and `public_reason` are set.
3. Two error sites built a CallToolResult with `isError: true` but NO structuredContent, so the
   machine-readable envelope was lost exactly where the PR claimed it never is.

The guard for (2) is a SOURCE scan, not a list of examples: a hand-kept list of bad inputs is the
same bug one level up — whoever forgets to add the next raise site ships an unactionable error
while the suite still reports PASS.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastmcp import Client

from clinvar_link.mcp.facade import create_clinvar_mcp
from clinvar_link.mcp.output_validation import _fixed_tool_not_found_result
from clinvar_link.services.clinvar_service import ClinVarService
from tests._fixture_db import build_service, call_tool

PACKAGE = Path(__file__).resolve().parent.parent / "clinvar_link"


@pytest.fixture
def service(tmp_path):
    svc = build_service(tmp_path)
    yield svc
    svc.repo.close()


@pytest.fixture
def mcp(tmp_path):
    svc = build_service(tmp_path)
    yield create_clinvar_mcp(service_factory=lambda: svc)
    svc.repo.close()


# --------------------------------------------------------------- 1. the batch silent-empty


async def test_batch_malformed_identifier_errors_instead_of_reporting_a_miss(mcp):
    """A 20-digit rsID cannot exist. `found: false` says "ClinVar does not have it" — a lie."""
    out = await call_tool(mcp, "get_variants", {"identifiers": ["rs99999999999999999999"]})
    assert out["success"] is False
    assert out["error_code"] == "invalid_input"
    assert "identifiers" in out["message"]
    assert [fe["field"] for fe in out["field_errors"]] == ["identifiers.0"]


async def test_batch_names_the_position_of_the_offending_identifier(mcp):
    """The model must know WHICH element to fix — the position is server-derived, so it is safe."""
    out = await call_tool(
        mcp, "get_variants", {"identifiers": ["VCV000100001", "not an identifier at all"]}
    )
    assert out["success"] is False
    assert out["error_code"] == "invalid_input"
    assert [fe["field"] for fe in out["field_errors"]] == ["identifiers.1"]


async def test_batch_blank_identifier_errors(mcp):
    out = await call_tool(mcp, "get_variants", {"identifiers": ["   "]})
    assert out["success"] is False
    assert out["error_code"] == "invalid_input"


async def test_batch_absent_but_well_formed_identifier_is_still_a_miss(mcp):
    """The distinction the whole fix rests on: MALFORMED errors, ABSENT is a truthful miss."""
    out = await call_tool(mcp, "get_variants", {"identifiers": ["VCV000100001", "VCV999999999"]})
    assert out["success"] is True
    assert out["found_count"] == 1
    misses = [row for row in out["results"] if not row["found"]]
    assert [row["identifier"] for row in misses] == ["VCV999999999"]


# --------------------------------------------------------------- 2. actionable everywhere


@pytest.mark.parametrize(
    "args",
    [
        {"identifier": "rs334", "id_type": "vcv"},
        {"identifier": "rs334", "id_type": "variation_id"},
        {"identifier": "VCV000100001", "id_type": "hgvs"},
        {"identifier": "not an identifier at all"},
        {"identifier": "   "},
        {"identifier": "VCV000100001‮\x00"},
    ],
)
async def test_every_identifier_rejection_names_the_parameter(mcp, args):
    out = await call_tool(mcp, "get_variant", args)
    assert out["success"] is False
    assert out["error_code"] == "invalid_input"
    assert "identifier" in out["message"], out["message"]
    assert [fe["field"] for fe in out["field_errors"]] == ["identifier"]
    # and the caller's value is still never echoed back
    assert "rs334" not in out["message"]


async def test_search_forbidden_codepoints_name_their_parameter(mcp):
    out = await call_tool(mcp, "search_variants", {"query": "BRCA1‮\x00"})
    assert out["success"] is False
    assert out["error_code"] == "invalid_input"
    assert [fe["field"] for fe in out["field_errors"]] == ["query"]


def test_every_tool_input_error_in_the_package_is_actionable():
    """SOURCE-level partition: every `raise ToolInputError(...)` supplies field + public_reason.

    This is the guard the review asked for. It cannot be satisfied by remembering to add the next
    example to a test list — it walks the AST of every module in the package, so a new raise site
    is gated the moment it is written.
    """
    unactionable: list[str] = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            func = node.exc.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name != "ToolInputError":
                continue
            kwargs = {kw.arg for kw in node.exc.keywords}
            if not {"field", "public_reason"} <= kwargs:
                unactionable.append(f"{path.name}:{node.lineno}")
    assert not unactionable, (
        "these ToolInputError raises name no parameter, so the caller is told only "
        f"'The request was rejected as invalid.': {unactionable}"
    )


# --------------------------------------------------------------- 3. structuredContent on errors


async def test_an_unknown_tool_error_still_carries_the_structured_envelope(mcp):
    """isError:true with structuredContent:null throws away the machine-readable envelope."""
    async with Client(mcp) as client:
        result = await client.call_tool("no_such_tool_at_all", {}, raise_on_error=False)
    assert result.is_error is True
    assert result.structured_content is not None, "structuredContent was discarded"
    assert result.structured_content["success"] is False
    assert result.structured_content["error_code"] == "not_found"


def test_the_protocol_backstop_builds_both_mirrors():
    """The fixed not-found result must populate structuredContent, not only the text mirror."""
    call_result = _fixed_tool_not_found_result().root
    assert call_result.isError is True
    assert call_result.structuredContent is not None
    assert call_result.structuredContent["error_code"] == "not_found"
    # the requested name is still never echoed
    assert "_meta" in call_result.structuredContent
    assert "tool" not in call_result.structuredContent["_meta"]


# --------------------------------------------------------------- 4. gene scoping


async def test_a_gene_symbol_late_in_the_prose_is_still_promoted(mcp):
    """The scan stopped at the first 8 tokens, so a gene at the end of a sentence was missed."""
    out = await call_tool(
        mcp,
        "search_variants",
        {"query": "pathogenic truncating frameshift variants reported in exon 11 of BRCA1"},
    )
    assert out["success"] is True
    assert out["results"]
    assert {row["gene_symbol"] for row in out["results"]} == {"BRCA1"}
    assert out["_meta"]["search"]["gene_symbol_inferred"] == "BRCA1"


async def test_a_lowercase_letter_only_gene_symbol_is_promoted(mcp):
    """`ttn` carries no digit and is not upper-case, so it was ignored entirely."""
    out = await call_tool(mcp, "search_variants", {"query": "ttn truncating variant"})
    assert out["success"] is True
    assert out["results"]
    assert {row["gene_symbol"] for row in out["results"]} == {"TTN"}
    assert out["_meta"]["search"]["gene_symbol_inferred"] == "TTN"


async def test_an_english_word_that_is_also_a_gene_symbol_does_not_hijack_the_query(service):
    """SET, REST and CAT are real genes. In lowercase prose they are almost never the gene.

    Broadening the scan must not buy coverage by silently filtering a query down to a gene the
    caller never asked for — that would be the same class of wrong answer, from the other side.
    """
    genes = {"SET", "REST", "CAT", "EGFR"}
    service.repo.gene_exists = lambda symbol: symbol.upper() in genes

    # lowercase English words that happen to be gene symbols are NOT promoted
    assert await service._infer_gene("set of pathogenic variants") is None
    assert await service._infer_gene("the rest of the exon") is None
    # written as an explicit symbol, it IS the gene the caller means
    assert await service._infer_gene("SET pathogenic variants") == "SET"
    # a lowercase letter-only symbol that is NOT an English word is still promoted (was ignored)
    assert await service._infer_gene("egfr missense") == "EGFR"


async def test_two_different_gene_symbols_infer_nothing(service):
    """Ambiguity must not be resolved by picking the first one."""
    assert await service._infer_gene("BRCA1 and TTN") is None


def test_the_service_and_the_repository_agree_on_the_infer_helper(service):
    """_infer_gene is exercised above through a stub; make sure the real repo has the method."""
    assert callable(ClinVarService(service.repo).repo.gene_exists)
