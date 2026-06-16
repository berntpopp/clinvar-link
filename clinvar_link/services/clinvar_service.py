"""Async orchestration layer over the read-only :class:`ClinVarRepository`.

The repository is synchronous SQLite; every read is wrapped in
``asyncio.to_thread`` so the MCP event loop is never blocked. This layer also
owns the request-shaping concerns the repository does not: identifier
resolution (``id_type`` heuristics), pydantic validation into the public
models, attaching the recommended citation, and projecting payloads down to the
requested ``response_mode`` to control token cost.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from clinvar_link.config import settings
from clinvar_link.data.repository import ClinVarRepository
from clinvar_link.exceptions import DataNotFoundError, ToolInputError
from clinvar_link.models import ClinVarVariant, GeneClinVarSummary
from clinvar_link.services.citation import gene_citation, recommended_citation

_RSID_RE = re.compile(r"^rs(\d+)$", re.IGNORECASE)
_VCV_RE = re.compile(r"^VCV\d+$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"^\d+$")
_HGVS_HINTS = ("c.", "p.", "g.", "n.")

# Fields kept in the ``minimal`` variant projection.
_MINIMAL_FIELDS = (
    "variation_id",
    "vcv_accession",
    "classification",
    "star_rating",
    "gene_symbol",
    "recommended_citation",
)
# Extra scalar fields added on top of ``minimal`` for the ``compact`` projection.
_COMPACT_EXTRA_FIELDS = (
    "name",
    "rsid",
    "review_status",
    "protein_change",
    "cdna_change",
    "canonical_assembly",
)
# Extra fields added on top of ``compact`` for the ``standard`` projection.
_STANDARD_EXTRA_FIELDS = (
    "coordinates",
    "rcv_accessions",
    "molecular_consequence",
    "allele_id",
    "number_submitters",
    "last_evaluated",
)


class ClinVarService:
    """Async, projection-aware facade over :class:`ClinVarRepository`."""

    def __init__(self, repo: ClinVarRepository) -> None:
        """Store the repository and prime the (lazily filled) meta cache."""
        self.repo = repo
        self._meta_cache: dict[str, Any] | None = None

    # -- meta / provenance -----------------------------------------------------

    async def _meta(self) -> dict[str, Any]:
        """Return cached build provenance, fetching it once off-thread."""
        if self._meta_cache is None:
            self._meta_cache = await asyncio.to_thread(self.repo.meta) or {}
        return self._meta_cache

    async def _release_date(self) -> str | None:
        """Return the ClinVar weekly release date from build provenance."""
        meta = await self._meta()
        release = meta.get("clinvar_release_date")
        return release if release else None

    async def get_clinvar_meta(self) -> dict[str, Any]:
        """Return build provenance: release date and variant/gene counts."""
        meta = await self._meta()
        return {
            "release_date": meta.get("clinvar_release_date"),
            "variant_count": meta.get("variant_count"),
            "gene_count": meta.get("gene_count"),
        }

    # -- single-variant resolution ---------------------------------------------

    async def get_variant(
        self,
        identifier: str,
        *,
        id_type: str = "auto",
        response_mode: str = "compact",
    ) -> dict[str, Any]:
        """Resolve a single variant by identifier and return a projected dict."""
        text = (identifier or "").strip()
        if not text:
            raise ToolInputError("identifier is required")

        repo_dict = await self._resolve(text, id_type)
        if repo_dict is None:
            raise DataNotFoundError(f"No ClinVar variant for {identifier!r}")

        variant = ClinVarVariant(**repo_dict)
        release = await self._release_date()
        variant.recommended_citation = recommended_citation(
            variant.variation_id, variant.vcv_accession, release
        )
        return self._project(variant.model_dump(), response_mode)

    async def _resolve(self, text: str, id_type: str) -> dict[str, Any] | None:
        """Dispatch identifier resolution by explicit or auto-detected type."""
        if id_type == "vcv":
            return await asyncio.to_thread(self.repo.get_by_vcv, text)
        if id_type == "variation_id":
            return await self._maybe_variation_id(text)
        if id_type == "rsid":
            return await self._maybe_rsid(text)
        if id_type == "hgvs":
            return await asyncio.to_thread(self.repo.get_by_hgvs, text)
        if id_type == "allele_id":
            return await self._maybe_allele_id(text)
        return await self._resolve_auto(text)

    async def _resolve_auto(self, text: str) -> dict[str, Any] | None:
        """Heuristically resolve an identifier when ``id_type`` is ``auto``."""
        if _RSID_RE.match(text):
            return await self._maybe_rsid(text)
        if _VCV_RE.match(text):
            return await asyncio.to_thread(self.repo.get_by_vcv, text)
        if _DIGITS_RE.match(text):
            result = await self._maybe_variation_id(text)
            if result is not None:
                return result
            return await self._maybe_allele_id(text)
        if ":" in text or any(hint in text for hint in _HGVS_HINTS):
            return await asyncio.to_thread(self.repo.get_by_hgvs, text)
        result = await asyncio.to_thread(self.repo.get_by_hgvs, text)
        if result is not None:
            return result
        return await self._maybe_allele_id(text)

    async def _maybe_variation_id(self, text: str) -> dict[str, Any] | None:
        """Resolve as a VariationID; non-integer input resolves to ``None``."""
        try:
            vid = int(text)
        except (TypeError, ValueError):
            return None
        return await asyncio.to_thread(self.repo.get_by_variation_id, vid)

    async def _maybe_rsid(self, text: str) -> dict[str, Any] | None:
        """Resolve as a dbSNP rsid (``rs123`` or bare digits)."""
        match = _RSID_RE.match(text)
        raw = match.group(1) if match else text
        try:
            rsid = int(raw)
        except (TypeError, ValueError):
            return None
        return await asyncio.to_thread(self.repo.get_by_rsid, rsid)

    async def _maybe_allele_id(self, text: str) -> dict[str, Any] | None:
        """Resolve as a ClinVar AlleleID; non-integer input resolves to ``None``."""
        try:
            aid = int(text)
        except (TypeError, ValueError):
            return None
        return await asyncio.to_thread(self.repo.get_by_allele_id, aid)

    # -- search ----------------------------------------------------------------

    async def search_variants(
        self,
        query: str,
        *,
        gene_symbol: str | None = None,
        classification: str | None = None,
        min_stars: int | None = None,
        assembly: str | None = None,
        limit: int = 20,
        offset: int = 0,
        response_mode: str = "compact",
    ) -> dict[str, Any]:
        """Free-text search returning projected variant dicts plus pagination."""
        limit = min(limit, settings.MAX_PAGE_SIZE)
        rows = await asyncio.to_thread(
            self.repo.search,
            query,
            gene_symbol=gene_symbol,
            classification=classification,
            min_stars=min_stars,
            assembly=assembly,
            limit=limit,
            offset=offset,
        )
        release = await self._release_date()
        results = [self._to_projected(row, release, response_mode) for row in rows]
        return {
            "results": results,
            "count": len(results),
            "limit": limit,
            "offset": offset,
            "query": query,
        }

    # -- gene-scoped views -----------------------------------------------------

    async def get_gene_clinvar_summary(
        self,
        gene_symbol: str,
        *,
        response_mode: str = "compact",
    ) -> dict[str, Any]:
        """Return the precomputed per-gene ClinVar summary with a citation."""
        summary = await asyncio.to_thread(self.repo.gene_summary, gene_symbol)
        if summary is None:
            raise DataNotFoundError(f"No ClinVar gene summary for {gene_symbol!r}")
        release = await self._release_date()
        model = GeneClinVarSummary(**{**summary, "gene_symbol": gene_symbol})
        model.recommended_citation = gene_citation(gene_symbol, release)
        payload = model.model_dump()
        if response_mode == "minimal":
            for key in ("consequence_categories", "top_traits", "star_distribution"):
                payload.pop(key, None)
        return payload

    async def get_variants_by_gene(
        self,
        gene_symbol: str,
        *,
        classification: str | None = None,
        min_stars: int | None = None,
        sort: str = "stars_desc",
        limit: int = 50,
        offset: int = 0,
        response_mode: str = "compact",
    ) -> dict[str, Any]:
        """List a gene's variants (projected) with a total for pagination."""
        limit = min(limit, settings.MAX_PAGE_SIZE)
        total = await asyncio.to_thread(
            self.repo.count_variants_by_gene,
            gene_symbol,
            classification=classification,
            min_stars=min_stars,
        )
        if total == 0:
            raise DataNotFoundError(f"No ClinVar variants for gene {gene_symbol!r}")
        rows = await asyncio.to_thread(
            self.repo.variants_by_gene,
            gene_symbol,
            classification=classification,
            min_stars=min_stars,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        release = await self._release_date()
        results = [self._to_projected(row, release, response_mode) for row in rows]
        return {
            "gene_symbol": gene_symbol,
            "results": results,
            "count": len(results),
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    # -- shaping helpers -------------------------------------------------------

    def _to_projected(
        self, repo_dict: dict[str, Any], release: str | None, mode: str
    ) -> dict[str, Any]:
        """Validate a repo row, attach its citation, and project it."""
        variant = ClinVarVariant(**repo_dict)
        variant.recommended_citation = recommended_citation(
            variant.variation_id, variant.vcv_accession, release
        )
        return self._project(variant.model_dump(), mode)

    def _project(self, payload: dict[str, Any], mode: str) -> dict[str, Any]:
        """Project a full variant payload down to the requested verbosity.

        Pure dict transform; tolerant of missing keys. ``full`` returns the
        payload unchanged.
        """
        if mode == "full":
            return payload

        out = {key: payload[key] for key in _MINIMAL_FIELDS if key in payload}
        if mode == "minimal":
            return out

        for key in _COMPACT_EXTRA_FIELDS:
            if key in payload:
                out[key] = payload[key]

        if mode == "standard":
            out["traits"] = payload.get("traits", [])
            for key in _STANDARD_EXTRA_FIELDS:
                if key in payload:
                    out[key] = payload[key]
            return out

        # compact (default): trait names only, capped at 5.
        traits = payload.get("traits", []) or []
        out["traits"] = [t.get("name") for t in traits if isinstance(t, dict)][:5]
        return out
