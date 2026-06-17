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
from clinvar_link.mcp.clinvar_date_cache import (
    has_cached_clinvar_release_date,
    set_cached_clinvar_release_date,
)
from clinvar_link.models import ClinVarVariant, GeneClinVarSummary
from clinvar_link.services.citation import (
    citation_template,
    gene_citation,
    recommended_citation,
)

# List response modes where per-row citations are hoisted to one _meta template
# and duplicate row fields (cdna_change == name) are dropped to save tokens.
_LEAN_LIST_MODES = frozenset({"minimal", "compact"})

_RSID_RE = re.compile(r"^rs(\d+)$", re.IGNORECASE)
_VCV_RE = re.compile(r"^VCV\d+$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"^\d+$")
_HGVS_HINTS = ("c.", "p.", "g.", "n.")

# Accepted ``id_type`` values; anything else is a structural input error.
_ID_TYPES = frozenset({"auto", "vcv", "variation_id", "rsid", "hgvs", "allele_id"})

_MATCH_MODES = frozenset({"auto", "and", "or"})
_COUNT_MODES = frozenset({"exact", "none"})
# Cap the search count scan; beyond this we report total_count_capped=True.
_SEARCH_COUNT_EXACT_MAX = 1000


def _ensure_id_type(id_type: str) -> None:
    """Raise :class:`ToolInputError` for an ``id_type`` outside the allowlist."""
    if id_type not in _ID_TYPES:
        raise ToolInputError(f"id_type must be one of {sorted(_ID_TYPES)} (got {id_type!r})")


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
        """Return the ClinVar weekly release date from build provenance.

        Also primes the process-level date cache the envelope/provenance layer
        reads, so EVERY response — including a cold get_variant issued before
        get_server_capabilities — can echo the live release (``clinvar_release``)
        instead of falling back to ``"unknown"``.
        """
        meta = await self._meta()
        raw = meta.get("clinvar_release_date")
        release = raw if raw else None
        if release is not None and not has_cached_clinvar_release_date():
            set_cached_clinvar_release_date(release)
        return release

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
        _ensure_id_type(id_type)
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
        raise ToolInputError(
            "unrecognized identifier shape; expected a VCV accession, dbSNP rsID, "
            "HGVS expression, ClinVar AlleleID, or VariationID — or call "
            "search_variants to locate the record"
        )

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

    async def get_variants(
        self,
        identifiers: list[str],
        *,
        id_type: str = "auto",
        response_mode: str = "compact",
    ) -> dict[str, Any]:
        """Resolve many identifiers in one call, collapsing N round-trips into 1.

        Each input maps to one result row that echoes its ``identifier`` and a
        ``found`` flag; misses are explicit rows (never silently dropped) so the
        caller can see exactly which identifiers failed. The batch is capped at
        ``MAX_PAGE_SIZE`` and sets ``truncated`` when the input exceeded it.
        """
        if not identifiers:
            raise ToolInputError("identifiers is required (a non-empty list)")
        # A bad id_type is a structural error: fail the whole batch up front
        # rather than silently turning every row into a miss in the loop below.
        _ensure_id_type(id_type)
        capped = list(identifiers)[: settings.MAX_PAGE_SIZE]
        release = await self._release_date()
        results: list[dict[str, Any]] = []
        found_count = 0
        for ident in capped:
            text = ident.strip() if isinstance(ident, str) else ""
            try:
                repo_dict = await self._resolve(text, id_type) if text else None
            except ToolInputError:
                repo_dict = None  # a malformed id in a batch is a miss, not a fatal error
            if repo_dict is None:
                results.append({"identifier": ident, "found": False})
                continue
            found_count += 1
            variant = ClinVarVariant(**repo_dict)
            variant.recommended_citation = recommended_citation(
                variant.variation_id, variant.vcv_accession, release
            )
            projected = self._project(variant.model_dump(), response_mode)
            projected["identifier"] = ident
            projected["found"] = True
            results.append(projected)
        out: dict[str, Any] = {
            "results": results,
            "count": len(results),
            "requested": len(capped),
            "found_count": found_count,
            "truncated": len(identifiers) > len(capped),
        }
        self._lean_list(out, results, release, response_mode)
        return out

    # -- search ----------------------------------------------------------------

    async def search_variants(
        self,
        query: str,
        *,
        gene_symbol: str | None = None,
        classification: str | None = None,
        min_stars: int | None = None,
        assembly: str | None = None,
        match_mode: str = "auto",
        count_mode: str = "exact",
        limit: int = 20,
        offset: int = 0,
        response_mode: str = "compact",
    ) -> dict[str, Any]:
        """Free-text search with AND default, OR fallback, and tiered count."""
        has_filter = bool(gene_symbol or classification or min_stars is not None)
        if not (query or "").strip() and not has_filter:
            raise ToolInputError(
                "query is required; to list a gene's variants use get_variants_by_gene"
            )
        if match_mode not in _MATCH_MODES:
            raise ToolInputError(
                f"match_mode must be one of {sorted(_MATCH_MODES)} (got {match_mode!r})"
            )
        if count_mode not in _COUNT_MODES:
            raise ToolInputError(
                f"count_mode must be one of {sorted(_COUNT_MODES)} (got {count_mode!r})"
            )
        limit = max(1, min(limit, settings.MAX_PAGE_SIZE))
        offset = max(0, offset)
        fetch = limit + 1  # over-fetch by one to compute has_more without a count

        async def _do(mode: str) -> list[dict[str, Any]]:
            return await asyncio.to_thread(
                self.repo.search,
                query,
                gene_symbol=gene_symbol,
                classification=classification,
                min_stars=min_stars,
                assembly=assembly,
                match_mode=mode,
                limit=fetch,
                offset=offset,
            )

        multi_token = len((query or "").split()) >= 2
        if match_mode == "auto":
            rows = await _do("and")
            used = "and"
            if not rows and multi_token:
                or_rows = await _do("or")
                if or_rows:
                    rows, used = or_rows, "or_fallback"
        else:
            rows = await _do(match_mode)
            used = match_mode

        has_more = len(rows) > limit
        rows = rows[:limit]
        count_match_mode = "or" if used in ("or", "or_fallback") else "and"
        total, capped = await self._count_for_search(
            query,
            gene_symbol=gene_symbol,
            classification=classification,
            min_stars=min_stars,
            assembly=assembly,
            match_mode=count_match_mode,
            count_mode=count_mode,
            has_more=has_more,
            returned=len(rows),
            offset=offset,
        )
        release = await self._release_date()
        results = [self._to_projected(row, release, response_mode) for row in rows]
        out: dict[str, Any] = {
            "results": results,
            "count": len(results),
            "query": query,
            "match_mode": used,
            **self._pagination(total, has_more, limit, offset, capped=capped),
        }
        self._lean_list(out, results, release, response_mode)
        return out

    async def _count_for_search(
        self,
        query: str,
        *,
        gene_symbol: str | None,
        classification: str | None,
        min_stars: int | None,
        assembly: str | None,
        match_mode: str,
        count_mode: str,
        has_more: bool,
        returned: int,
        offset: int,
    ) -> tuple[int | None, bool]:
        """Return (total, capped) for the search count; placeholder for Task 6."""
        if count_mode == "none":
            return None, False
        total = await asyncio.to_thread(
            self.repo.count_search,
            query,
            gene_symbol=gene_symbol,
            classification=classification,
            min_stars=min_stars,
            assembly=assembly,
        )
        return total, False

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
        limit = max(1, min(limit, settings.MAX_PAGE_SIZE))
        offset = max(0, offset)
        if sort not in ClinVarRepository.SORT_ORDERS:
            raise ToolInputError(
                f"sort must be one of {sorted(ClinVarRepository.SORT_ORDERS)} (got {sort!r})"
            )
        total = await asyncio.to_thread(
            self.repo.count_variants_by_gene,
            gene_symbol,
            classification=classification,
            min_stars=min_stars,
        )
        if total == 0:
            gene_total = await asyncio.to_thread(self.repo.count_variants_by_gene, gene_symbol)
            if gene_total == 0:
                raise DataNotFoundError(f"No ClinVar variants for gene {gene_symbol!r}")
            # Gene exists; the filter simply excluded everything -> empty success
            # (consistent with search_variants and out-of-range offset).
            return {
                "gene_symbol": gene_symbol,
                "results": [],
                "count": 0,
                **self._pagination(0, False, limit, offset),
            }
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
        has_more = (offset + len(results)) < total
        out: dict[str, Any] = {
            "gene_symbol": gene_symbol,
            "results": results,
            "count": len(results),
            **self._pagination(total, has_more, limit, offset),
        }
        self._lean_list(out, results, release, response_mode)
        return out

    # -- shaping helpers -------------------------------------------------------

    @staticmethod
    def _pagination(
        total: int | None,
        has_more: bool,
        limit: int,
        offset: int,
        *,
        capped: bool = False,
    ) -> dict[str, Any]:
        """Pagination block. ``total`` may be None when the caller skipped counting."""
        block: dict[str, Any] = {
            "total_count": total,
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
            "next_offset": (offset + limit) if has_more else None,
        }
        if capped:
            block["total_count_capped"] = True
        return block

    @staticmethod
    def _lean_list(
        out: dict[str, Any],
        results: list[dict[str, Any]],
        release: str | None,
        mode: str,
    ) -> None:
        """Hoist the citation to one ``_meta`` template and drop duplicate row fields.

        For the token-lean list modes (minimal/compact) the per-row
        ``recommended_citation`` — identical apart from the IDs — is replaced by a
        single ``_meta.citation_template`` the caller fills from each row's
        ``variation_id`` / ``vcv_accession``; ``cdna_change`` is dropped wherever
        it merely repeats ``name``. Richer modes keep self-contained rows.
        """
        if mode not in _LEAN_LIST_MODES:
            return
        for row in results:
            row.pop("recommended_citation", None)
            if row.get("cdna_change") is not None and row.get("cdna_change") == row.get("name"):
                row.pop("cdna_change", None)
        out.setdefault("_meta", {})["citation_template"] = citation_template(release)

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
