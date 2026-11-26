"""Gene identifier forms a `gene_symbol` argument may carry besides the symbol.

The index is keyed by gene symbol. An HGNC id converts through the HGNC table the index
already holds; an Ensembl gene id or an NCBI Gene id does not, and is rejected with the
hgnc-link tool that converts it instead of being looked up as a symbol nobody has.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from clinvar_link.exceptions import UnsupportedGeneIdError

if TYPE_CHECKING:
    from clinvar_link.data.repository import ClinVarRepository

_HGNC_RE = re.compile(r"HGNC:\d+", re.IGNORECASE)
_ENSEMBL_RE = re.compile(r"ENSG\d{11}(\.\d+)?", re.IGNORECASE)
_NCBI_RE = re.compile(r"(?:NCBI_?Gene|Entrez(?:Gene)?|GeneID):(\d+)", re.IGNORECASE)


def _step(tool: str, **arguments: str) -> dict[str, Any]:
    return {"tool": tool, "arguments": arguments}


def resolve_gene_argument(
    repo: ClinVarRepository, raw: str | None
) -> tuple[str | None, dict[str, str] | None]:
    """Return ``(symbol, resolved_from)`` for a `gene_symbol` argument.

    A symbol passes through with ``resolved_from`` None. An HGNC id the index knows becomes its
    approved symbol. Any other gene id form raises :class:`UnsupportedGeneIdError`.
    """
    text = (raw or "").strip()
    if _HGNC_RE.fullmatch(text):
        hgnc_id = text.upper()
        symbol = repo.symbol_for_hgnc_id(hgnc_id)
        if symbol:
            return symbol, {"input": text, "form": "hgnc_id", "resolved_to": symbol}
        raise UnsupportedGeneIdError(text, _step("hgnc_resolve_symbol", query=hgnc_id))
    if _ENSEMBL_RE.fullmatch(text):
        raise UnsupportedGeneIdError(text, _step("hgnc_resolve_symbol", query=text))
    if match := _NCBI_RE.fullmatch(text):
        step = _step("hgnc_resolve_gene_by_xref", source="entrez_id", value=match.group(1))
        raise UnsupportedGeneIdError(text, step)
    return raw, None
