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
from clinvar_link.mcp.untrusted_content import (
    FORBIDDEN_CODEPOINTS,
    UntrustedText,
    enforce_untrusted_text_limits,
    fence_untrusted_text,
)
from clinvar_link.models import ClinVarVariant, GeneClinVarSummary
from clinvar_link.models.enums import (
    ASSEMBLY_VALUES,
    CLASSIFICATION_VALUES,
    COUNT_MODES,
    ID_TYPES,
    MATCH_MODES,
    normalize_assembly,
    normalize_classification,
)
from clinvar_link.services.citation import (
    citation_template,
    gene_citation,
    recommended_citation,
)

# List response modes where per-row citations are hoisted to one _meta template
# and duplicate row fields (cdna_change == name) are dropped to save tokens.
_LEAN_LIST_MODES = frozenset({"minimal", "compact"})

# Response-Envelope v1.1: the batch/list tools (get_variants, search_variants,
# get_variants_by_gene) aggregate every row's fenced trait objects into ONE
# enforce_untrusted_text_limits call over the WHOLE response. Trait count per
# variant is not capped by the model, so up to MAX_PAGE_SIZE rows each
# carrying several conditions is a legitimate, non-hostile shape; a generous
# ceiling avoids raising on a real multi-condition batch while the 2 MiB/object
# and 8 MiB/total byte ceilings (always enforced, never overridden) remain the
# real DoS backstop. get_variant (single row) and get_gene_clinvar_summary
# (top_traits capped at 5 by ingest) keep the library default of 128.
_LIST_TOOL_MAX_OBJECTS = 10_000

_RSID_RE = re.compile(r"^rs(\d+)$", re.IGNORECASE)
_VCV_RE = re.compile(r"^VCV\d+$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"^\d+$")
_HGVS_HINTS = ("c.", "p.", "g.", "n.")
# Mirrors the repository's FTS tokenizer, so the tokens this layer reasons about are exactly the
# tokens the index will match.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
# A gene symbol as a human writes one: BRCA1, TP53, COL4A5. Used to promote a symbol written in
# free-text search into the gene filter (never a bare number).
_GENE_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,14}$")
# Common English words that are ALSO HGNC gene symbols. A bare LOWERCASE occurrence of one of
# these in prose ("the rest of the exon", "set of variants") is far more likely the English word
# than the gene, so it is not promoted unless written as an explicit uppercase symbol. This buys
# lowercase-symbol coverage (egfr, kras, ttn) without letting a stopword hijack a query; it is a
# heuristic, deliberately conservative, and never the ONLY thing between the caller and a wrong
# answer (an inferred gene is always declared in _meta.search, and an explicit gene_symbol wins).
_GENE_WORD_STOPWORDS: frozenset[str] = frozenset(
    {
        "set",
        "rest",
        "cat",
        "met",
        "camp",
        "ache",
        "max",
        "fat",
        "gap",
        "arc",
        "cap",
        "mice",
        "clock",
        "star",
        "impact",
        "damage",
        "was",
        "not",
        "the",
        "and",
        "for",
        "with",
        "his",
        "type",
        "spam",
        "sos",
        "mars",
        "wars",
    }
)

# Accepted ``id_type`` values; anything else is a structural input error.
_ID_TYPES = frozenset(ID_TYPES)

_MATCH_MODES = frozenset(MATCH_MODES)
_COUNT_MODES = frozenset(COUNT_MODES)
# SQLite binds integers as int64; a larger value raises OverflowError deep in the driver and
# used to escape as an unactionable internal error.
_INT64_MAX = 2**63 - 1
# Cap the search count scan; beyond this we report total_count_capped=True.
_SEARCH_COUNT_EXACT_MAX = 1000

_IDENTIFIER_REASON = (
    "must be a VCV accession (VCV000007105), a dbSNP rsID (rs334), an HGVS expression, a "
    "ClinVar AlleleID or a VariationID; a numeric id must fit in 64 bits"
)


def _ensure_id_type(id_type: str) -> None:
    """Raise :class:`ToolInputError` for an ``id_type`` outside the allowlist."""
    if id_type not in _ID_TYPES:
        raise ToolInputError(
            f"id_type must be one of {sorted(_ID_TYPES)} (got {id_type!r})",
            field="id_type",
            public_reason=f"must be one of: {', '.join(sorted(_ID_TYPES))}",
        )


def _ensure_classification(value: str | None) -> str | None:
    """Normalize a classification filter, or REJECT it — never silently match nothing.

    THE defect this server shipped: ``classification='Likely pathogenic'`` — ClinVar's own
    published wording — returned ``total_count: 0, success: true`` while 559 BRCA1 variants sat
    behind ``likely_pathogenic``. An unrecognised value is now an ``invalid_input`` error naming
    the parameter and listing the vocabulary (Response-Envelope v1.1: "silent omission is not
    compliant"), and ClinVar's own wording is normalized onto the canonical token.
    """
    if value is None:
        return None
    canonical = normalize_classification(value)
    if canonical is None:
        raise ToolInputError(
            f"classification must be one of {list(CLASSIFICATION_VALUES)} (got {value!r})",
            field="classification",
            public_reason=(
                "must be one of: "
                + ", ".join(CLASSIFICATION_VALUES)
                + " (ClinVar's own wording, e.g. 'Likely pathogenic' or "
                "'Uncertain significance', is accepted and normalized)"
            ),
        )
    return canonical


def _ensure_assembly(value: str | None) -> str | None:
    """Normalize an assembly filter ('hg19' -> 'GRCh37'), or reject it."""
    if value is None:
        return None
    canonical = normalize_assembly(value)
    if canonical is None:
        raise ToolInputError(
            f"assembly must be one of {list(ASSEMBLY_VALUES)} (got {value!r})",
            field="assembly",
            public_reason=(
                "must be one of: "
                + ", ".join(ASSEMBLY_VALUES)
                + " ('hg38' / 'hg19' are accepted and normalized)"
            ),
        )
    return canonical


def _as_sqlite_int(text: str, *, field: str) -> int | None:
    """Parse a numeric identifier, rejecting one SQLite could never store (int64 overflow)."""
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    if abs(value) > _INT64_MAX:
        raise ToolInputError(
            f"{field} numeric id exceeds the 64-bit range",
            field=field,
            public_reason=_IDENTIFIER_REASON,
        )
    return value


def _reject_forbidden_codepoints(value: str, *, field: str) -> None:
    """Reject a free-text input carrying fenced forbidden code points.

    Control/zero-width/bidi/NUL code points have no place in a ClinVar identifier
    or query; they are a smuggling vector into caller-visible strings and into the
    per-item rows of an otherwise-successful batch response. Reject them at the
    tool boundary with a FIXED message that never echoes the offending value, so a
    hostile input cannot ride into any surfaced string. ``field`` is a fixed,
    server-authored parameter name (never caller data).
    """
    if any(ord(char) in FORBIDDEN_CODEPOINTS for char in value):
        raise ToolInputError(
            f"{field} contains forbidden control or bidirectional characters",
            field=field,
            public_reason=("must not contain control, zero-width or bidirectional characters"),
        )


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
        get_server_capabilities — can echo the live ``clinvar_release_date``
        (which is omitted only while the release date is still unknown).
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
            raise ToolInputError(
                "identifier is required",
                field="identifier",
                public_reason="is required (a VCV accession, rsID, HGVS, AlleleID or VariationID)",
            )
        _reject_forbidden_codepoints(text, field="identifier")

        repo_dict = await self._resolve(text, id_type)
        if repo_dict is None:
            raise DataNotFoundError(f"No ClinVar variant for {identifier!r}")

        variant = ClinVarVariant(**repo_dict)
        release = await self._release_date()
        variant.recommended_citation = recommended_citation(
            variant.variation_id, variant.vcv_accession, release
        )
        projected, fenced = self._project(
            variant.model_dump(), response_mode, keep_traits_in_minimal=True
        )
        enforce_untrusted_text_limits(fenced)
        return projected

    @staticmethod
    def _validate_shape(text: str, id_type: str) -> None:
        """Reject a value whose shape cannot match an explicitly forced id_type.

        Every raise here names the parameter and states the shape it wants: an error the model
        cannot act on is a defect, and "The request was rejected as invalid." names nothing. The
        reasons are server-authored constants — the rejected VALUE is never interpolated into
        them (it stays in the message, which is server-side only).
        """
        if id_type == "vcv" and not _VCV_RE.match(text):
            raise ToolInputError(
                f"id_type='vcv' requires a VCV accession (got {text!r})",
                field="identifier",
                public_reason="must be a VCV accession (e.g. VCV000007105) when id_type='vcv'",
            )
        if id_type == "rsid" and not (_RSID_RE.match(text) or _DIGITS_RE.match(text)):
            raise ToolInputError(
                f"id_type='rsid' requires an rsID (got {text!r})",
                field="identifier",
                public_reason="must be a dbSNP rsID (e.g. rs334) when id_type='rsid'",
            )
        if id_type in {"variation_id", "allele_id"} and not _DIGITS_RE.match(text):
            raise ToolInputError(
                f"id_type={id_type!r} requires a numeric id (got {text!r})",
                field="identifier",
                public_reason=(
                    "must be a plain integer id when id_type='variation_id' or 'allele_id'"
                ),
            )
        if id_type == "hgvs" and ":" not in text and not any(h in text for h in _HGVS_HINTS):
            raise ToolInputError(
                f"id_type='hgvs' requires an HGVS expression (got {text!r})",
                field="identifier",
                public_reason=(
                    "must be an HGVS expression (e.g. NM_007294.4:c.5266dupC) when id_type='hgvs'"
                ),
            )

    async def _resolve(self, text: str, id_type: str) -> dict[str, Any] | None:
        """Dispatch identifier resolution by explicit or auto-detected type."""
        _ensure_id_type(id_type)
        if id_type != "auto":
            self._validate_shape(text, id_type)
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
            "search_variants to locate the record",
            field="identifier",
            public_reason=(
                "does not look like any accepted shape (VCV accession, rsID, HGVS, AlleleID or "
                "VariationID); use search_variants to locate the record from loose text"
            ),
        )

    async def _maybe_variation_id(self, text: str) -> dict[str, Any] | None:
        """Resolve as a VariationID; non-integer input resolves to ``None``."""
        vid = _as_sqlite_int(text, field="identifier")
        if vid is None:
            return None
        return await asyncio.to_thread(self.repo.get_by_variation_id, vid)

    async def _maybe_rsid(self, text: str) -> dict[str, Any] | None:
        """Resolve as a dbSNP rsid (``rs123`` or bare digits)."""
        match = _RSID_RE.match(text)
        raw = match.group(1) if match else text
        rsid = _as_sqlite_int(raw, field="identifier")
        if rsid is None:
            return None
        return await asyncio.to_thread(self.repo.get_by_rsid, rsid)

    async def _maybe_allele_id(self, text: str) -> dict[str, Any] | None:
        """Resolve as a ClinVar AlleleID; non-integer input resolves to ``None``."""
        aid = _as_sqlite_int(text, field="identifier")
        if aid is None:
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
            raise ToolInputError(
                "identifiers is required (a non-empty list)",
                field="identifiers",
                public_reason="is required (a non-empty list of variant identifiers)",
            )
        # Reject the batch if any identifier carries forbidden code points, so a
        # hostile value cannot ride into a per-item (otherwise-successful) miss row.
        for ident in identifiers:
            if isinstance(ident, str):
                _reject_forbidden_codepoints(ident, field="identifiers")
        # A bad id_type is a structural error: fail the whole batch up front
        # rather than silently turning every row into a miss in the loop below.
        _ensure_id_type(id_type)
        capped = list(identifiers)[: settings.MAX_PAGE_SIZE]
        release = await self._release_date()
        results: list[dict[str, Any]] = []
        all_fenced: list[UntrustedText] = []
        found_count = 0
        for index, ident in enumerate(capped):
            text = ident.strip() if isinstance(ident, str) else ""
            # A MALFORMED element fails the batch with invalid_input naming its position — it must
            # NOT become `found: false`. `found: false` means "well-formed but absent from the
            # index", and a caller reading it concludes ClinVar has no such record; a blank or
            # unparseable identifier (e.g. a 20-digit rsID that overflows int64) is a structural
            # caller mistake, exactly as it is for the single-variant get_variant. The old code
            # swallowed every ToolInputError here, reintroducing the silent-empty this PR exists
            # to kill — one tool over. A well-formed-but-ABSENT id still resolves to None (the repo
            # returns None, no exception) and stays a truthful miss.
            if not text:
                raise ToolInputError(
                    f"identifiers[{index}] is blank",
                    field=f"identifiers.{index}",
                    public_reason="is blank; every identifier in the list must be non-empty",
                )
            try:
                repo_dict = await self._resolve(text, id_type)
            except ToolInputError as exc:
                raise ToolInputError(
                    f"identifiers[{index}] is malformed",
                    field=f"identifiers.{index}",
                    public_reason=exc.public_reason or "is not a well-formed ClinVar identifier",
                ) from exc
            if repo_dict is None:
                results.append({"identifier": ident, "found": False})
                continue
            found_count += 1
            variant = ClinVarVariant(**repo_dict)
            variant.recommended_citation = recommended_citation(
                variant.variation_id, variant.vcv_accession, release
            )
            projected, fenced = self._project(variant.model_dump(), response_mode)
            all_fenced.extend(fenced)
            projected["identifier"] = ident
            projected["found"] = True
            results.append(projected)
        # v1.1: enforce over every fenced object the WHOLE response emits, not
        # per row — a batch tool's real cap is settings.MAX_PAGE_SIZE rows, each
        # with an uncapped condition list, so the ceiling is generous.
        enforce_untrusted_text_limits(all_fenced, max_objects=_LIST_TOOL_MAX_OBJECTS)
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
        """Free-text search, scoped to the gene the query names, with an HONEST fallback.

        The tool's own documented usage — "a gene symbol plus a change or other loose text" —
        used to return variants of four unrelated genes: the AND pass matched nothing (clinical
        words like "pathogenic" are not in the indexed variant name), so it fell back to OR,
        which matched noise tokens with no preference for the gene symbol, and presented the
        result as a confident ranking.

        Two fixes, in order:
          1. a gene symbol written in the query is PROMOTED to the gene filter (reported back as
             ``_meta.search.gene_symbol_inferred``), so loose text can only narrow WITHIN the
             gene — never wander into another one;
          2. any degradation (OR fallback, or dropping the text entirely) is DECLARED in
             ``match_mode`` and ``_meta.search`` instead of being passed off as a ranked answer.
        """
        _reject_forbidden_codepoints(query or "", field="query")
        if gene_symbol:
            _reject_forbidden_codepoints(gene_symbol, field="gene_symbol")
        classification = _ensure_classification(classification)
        assembly = _ensure_assembly(assembly)
        has_filter = bool(gene_symbol or classification or min_stars is not None)
        if not (query or "").strip() and not has_filter:
            raise ToolInputError(
                "query is required; to list a gene's variants use get_variants_by_gene",
                field="query",
                public_reason=(
                    "is required (free text, e.g. 'BRCA1 c.5266dup'); to list a gene's variants "
                    "use get_variants_by_gene"
                ),
            )
        if match_mode not in _MATCH_MODES:
            raise ToolInputError(
                f"match_mode must be one of {sorted(_MATCH_MODES)} (got {match_mode!r})",
                field="match_mode",
                public_reason=f"must be one of: {', '.join(sorted(_MATCH_MODES))}",
            )
        if count_mode not in _COUNT_MODES:
            raise ToolInputError(
                f"count_mode must be one of {sorted(_COUNT_MODES)} (got {count_mode!r})",
                field="count_mode",
                public_reason=f"must be one of: {', '.join(sorted(_COUNT_MODES))}",
            )
        limit = max(1, min(limit, settings.MAX_PAGE_SIZE))
        offset = max(0, offset)

        # An explicit gene filter that matches no gene is a not_found, never an empty success.
        if gene_symbol and not await asyncio.to_thread(self.repo.gene_exists, gene_symbol):
            raise DataNotFoundError(f"No ClinVar variants for gene {gene_symbol!r}")
        inferred = None if gene_symbol else await self._infer_gene(query)
        effective_gene = gene_symbol or inferred
        text = self._residual_text(query, inferred)

        rows, used, count_text, has_more = await self._search_rows(
            text,
            gene_symbol=effective_gene,
            classification=classification,
            min_stars=min_stars,
            assembly=assembly,
            match_mode=match_mode,
            limit=limit,
            offset=offset,
        )
        count_match_mode = "or" if used in ("or", "or_fallback") else "and"
        total, capped = await self._count_for_search(
            count_text,
            gene_symbol=effective_gene,
            classification=classification,
            min_stars=min_stars,
            assembly=assembly,
            match_mode=count_match_mode,
            count_mode=count_mode,
        )
        release = await self._release_date()
        projected_rows = [self._to_projected(row, release, response_mode) for row in rows]
        results = [row for row, _ in projected_rows]
        all_fenced = [obj for _, fenced in projected_rows for obj in fenced]
        # v1.1: one response-wide enforcement call, not per row (§ get_variants).
        enforce_untrusted_text_limits(all_fenced, max_objects=_LIST_TOOL_MAX_OBJECTS)
        out: dict[str, Any] = {
            "results": results,
            "count": len(results),
            "query": query,
            "match_mode": used,
            **self._pagination(total, has_more, limit, offset, capped=capped),
        }
        self._lean_list(out, results, release, response_mode)
        out.setdefault("_meta", {})["search"] = self._search_meta(used, inferred, effective_gene)
        return out

    async def _infer_gene(self, query: str) -> str | None:
        """Promote a gene symbol written in the free text to the gene filter.

        Scans the WHOLE query (a gene at the end of a sentence must promote just as one at the
        start does — the old 8-token window silently missed it) and considers both cases:

        * a STRONG signal — written ALL-CAPS (BRCA1, TTN) or carrying a digit (brca1, MLH1) — is
          almost never accidental, so any real gene of that shape is a candidate;
        * a WEAK signal — a lowercase, letter-only token (ttn, egfr, kras) — is a candidate too
          (the old code ignored these entirely), EXCEPT for the handful of common English words
          that are also HGNC symbols (``set``, ``rest``, ``cat`` …): in lowercase prose those are
          almost always the word, and promoting one would filter the query down to a gene the
          caller never named — the same class of wrong answer, from the other side.

        Every candidate is confirmed against the index (``gene_exists``), so only a REAL gene is
        ever promoted; ambiguity (two distinct real symbols) infers nothing; and the caller's own
        ``gene_symbol`` always wins upstream of this. The result is always reported in
        ``_meta.search.gene_symbol_inferred`` — inference is never silent.
        """
        shaped: list[str] = []
        for token in _TOKEN_RE.findall(query or ""):
            if not _GENE_TOKEN_RE.match(token):
                continue
            strong = token.isupper() or any(char.isdigit() for char in token)
            if not strong and token.casefold() in _GENE_WORD_STOPWORDS:
                continue
            upper = token.upper()
            if upper not in shaped:
                shaped.append(upper)
        confirmed: list[str] = [
            symbol for symbol in shaped if await asyncio.to_thread(self.repo.gene_exists, symbol)
        ]
        return confirmed[0] if len(confirmed) == 1 else None

    @staticmethod
    def _residual_text(query: str, inferred: str | None) -> str:
        """The query text minus the symbol that became the gene filter."""
        if not inferred:
            return query or ""
        tokens = _TOKEN_RE.findall(query or "")
        return " ".join(token for token in tokens if token.upper() != inferred)

    async def _search_rows(
        self,
        text: str,
        *,
        gene_symbol: str | None,
        classification: str | None,
        min_stars: int | None,
        assembly: str | None,
        match_mode: str,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], str, str, bool]:
        """Run the match ladder. Returns (rows, mode_used, text_the_count_must_use, has_more)."""
        fetch = limit + 1  # over-fetch by one to compute has_more without a count

        async def _do(mode: str, query_text: str) -> list[dict[str, Any]]:
            return await asyncio.to_thread(
                self.repo.search,
                query_text,
                gene_symbol=gene_symbol,
                classification=classification,
                min_stars=min_stars,
                assembly=assembly,
                match_mode=mode,
                limit=fetch,
                offset=offset,
            )

        if match_mode != "auto":
            rows = await _do(match_mode, text)
            return rows[:limit], match_mode, text, len(rows) > limit

        rows = await _do("and", text)
        used, count_text = "and", text
        if not rows and len(text.split()) >= 2:
            or_rows = await _do("or", text)
            if or_rows:
                rows, used = or_rows, "or_fallback"
        if not rows and gene_symbol and text.strip():
            # Nothing in the gene matches the free text at all. Returning zero rows here would
            # be a silent dead end; returning OTHER genes' variants is what produced the OCRL
            # answer to a BRCA1 question. Drop the text, keep the gene, and SAY SO.
            gene_rows = await _do("and", "")
            if gene_rows:
                rows, used, count_text = gene_rows, "gene_fallback", ""
        return rows[:limit], used, count_text, len(rows) > limit

    @staticmethod
    def _search_meta(used: str, inferred: str | None, applied: str | None) -> dict[str, Any]:
        """Declare what the search actually did: what it inferred, and how it degraded."""
        notices = {
            "or_fallback": (
                "DEGRADED: no record matched ALL query terms, so these rows match ANY term. "
                "They are a broad, low-confidence match — re-query with fewer terms, or "
                "match_mode='and', before treating them as answers."
            ),
            "gene_fallback": (
                "DEGRADED: no variant matched the query text, so the free text was DROPPED and "
                "these are the gene's variants by review confidence. The text did NOT filter "
                "them — narrow with classification / min_stars instead."
            ),
        }
        fallback = used if used in notices else None
        return {
            "gene_symbol_inferred": inferred,
            "gene_symbol_applied": applied,
            "fallback": fallback,
            "notice": notices.get(used),
        }

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
    ) -> tuple[int | None, bool]:
        """Return (total, capped) for the search count."""
        if count_mode == "none":
            return None, False
        return await asyncio.to_thread(
            self.repo.count_search,
            query,
            gene_symbol=gene_symbol,
            classification=classification,
            min_stars=min_stars,
            assembly=assembly,
            match_mode=match_mode,
            count_exact_max=_SEARCH_COUNT_EXACT_MAX,
        )

    # -- gene-scoped views -----------------------------------------------------

    async def get_gene_clinvar_summary(
        self,
        gene_symbol: str,
        *,
        response_mode: str = "compact",
    ) -> dict[str, Any]:
        """Return the precomputed per-gene ClinVar summary with a citation."""
        _reject_forbidden_codepoints(gene_symbol or "", field="gene_symbol")
        summary = await asyncio.to_thread(self.repo.gene_summary, gene_symbol)
        if summary is None:
            raise DataNotFoundError(f"No ClinVar gene summary for {gene_symbol!r}")
        release = await self._release_date()
        model = GeneClinVarSummary(**{**summary, "gene_symbol": gene_symbol})
        model.recommended_citation = gene_citation(gene_symbol, release)
        payload = model.model_dump()
        known = (
            payload["pathogenic_count"]
            + payload["likely_pathogenic_count"]
            + payload["vus_count"]
            + payload["likely_benign_count"]
            + payload["benign_count"]
            + payload["conflicting_count"]
            + payload["not_provided_count"]
        )
        payload["other_count"] = max(0, payload["variant_count"] - known)
        fenced = self._fence_top_traits(payload, gene_symbol)
        enforce_untrusted_text_limits(fenced)
        if response_mode == "minimal":
            # `top_traits` (capped at 5 by ingest) is the record's payload, not optional detail:
            # a mode that returns the counts but throws away WHAT the gene is associated with is
            # the response-mode form of a silent empty. Only the wide breakdowns are dropped.
            for key in ("consequence_categories", "star_distribution"):
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
        """List a gene's variants (projected) with a total for pagination.

        ``classification`` is normalized-or-rejected BEFORE it reaches SQL (see
        :func:`_ensure_classification`). It used to be interpolated raw into
        ``WHERE v.classification = ?``, so any value the vocabulary did not contain — including
        ClinVar's own "Likely pathogenic" — matched no row and returned an empty SUCCESS.
        """
        _reject_forbidden_codepoints(gene_symbol or "", field="gene_symbol")
        classification = _ensure_classification(classification)
        limit = max(1, min(limit, settings.MAX_PAGE_SIZE))
        offset = max(0, offset)
        if sort not in ClinVarRepository.SORT_ORDERS:
            raise ToolInputError(
                f"sort must be one of {sorted(ClinVarRepository.SORT_ORDERS)} (got {sort!r})",
                field="sort",
                public_reason=f"must be one of: {', '.join(sorted(ClinVarRepository.SORT_ORDERS))}",
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
            # The gene exists and the filters are all valid (an unrecognised value would have
            # been rejected above) — so an empty page here is a TRUE zero: this gene really has
            # no variant with that classification / star floor. It is safe to report as success.
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
        projected_rows = [self._to_projected(row, release, response_mode) for row in rows]
        results = [row for row, _ in projected_rows]
        all_fenced = [obj for _, fenced in projected_rows for obj in fenced]
        # v1.1: one response-wide enforcement call, not per row (§ get_variants).
        enforce_untrusted_text_limits(all_fenced, max_objects=_LIST_TOOL_MAX_OBJECTS)
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
    ) -> tuple[dict[str, Any], list[UntrustedText]]:
        """Validate a repo row, attach its citation, and project it.

        Returns the projected row alongside the v1.1 fenced objects it emitted,
        so a list-tool caller can aggregate every row's fenced objects into one
        response-wide ``enforce_untrusted_text_limits`` call instead of
        enforcing per row.
        """
        variant = ClinVarVariant(**repo_dict)
        variant.recommended_citation = recommended_citation(
            variant.variation_id, variant.vcv_accession, release
        )
        return self._project(variant.model_dump(), mode)

    @staticmethod
    def _fence_traits(payload: dict[str, Any], record_id_base: str) -> list[UntrustedText]:
        """Fence each ``traits[i].name`` in place as a v1.1 ``untrusted_text`` object.

        Mutates ``payload["traits"]`` so every downstream projection (full,
        standard, compact) reads the already-fenced value; the compact
        projection then merely extracts the now-typed object instead of the
        bare string it used to. Returns the fenced objects for the caller to
        aggregate into one response-wide limits check.
        """
        fenced: list[UntrustedText] = []
        for i, trait in enumerate(payload.get("traits") or []):
            if not isinstance(trait, dict):
                continue
            raw = trait.get("name")
            if not isinstance(raw, str):
                continue
            obj = fence_untrusted_text(
                raw, source="clinvar", record_id=f"{record_id_base}#trait:{i}"
            )
            trait["name"] = obj.model_dump(mode="json")
            fenced.append(obj)
        return fenced

    @staticmethod
    def _fence_top_traits(payload: dict[str, Any], gene_symbol: str) -> list[UntrustedText]:
        """Fence each ``top_traits[i].trait`` label in place as v1.1 ``untrusted_text``.

        NOTE: the stored key is ``trait`` (see
        :mod:`clinvar_link.ingest.parsing` ``compute_stats``), not ``name`` as
        the :class:`~clinvar_link.models.gene_models.GeneClinVarSummary`
        docstring example suggests — that example is documentation drift, not
        a second field. This fences the field that is actually emitted.
        """
        fenced: list[UntrustedText] = []
        for i, entry in enumerate(payload.get("top_traits") or []):
            if not isinstance(entry, dict):
                continue
            raw = entry.get("trait")
            if not isinstance(raw, str):
                continue
            obj = fence_untrusted_text(raw, source="clinvar", record_id=f"{gene_symbol}#trait:{i}")
            entry["trait"] = obj.model_dump(mode="json")
            fenced.append(obj)
        return fenced

    @staticmethod
    def _trim_null_keys(payload: dict[str, Any]) -> dict[str, Any]:
        """Drop information-free keys (null trait ids, 'na' alleles) from a payload.

        Applied to ``standard`` as well as ``full``. It was full-only, which made the response
        -mode ladder NON-MONOTONIC: standard (444kB) came back LARGER than full (405kB) for the
        same rows, so an agent economising by stepping down from full to standard got a bigger
        payload — 38kB of always-null trait ids and literal 'na' alleles.
        """
        for trait in payload.get("traits", []) or []:
            if isinstance(trait, dict):
                for key in ("omim_id", "medgen_id", "mondo_id"):
                    if trait.get(key) is None:
                        trait.pop(key, None)
        for coord in payload.get("coordinates", []) or []:
            if isinstance(coord, dict):
                for key in ("reference_allele", "alternate_allele"):
                    if coord.get(key) in (None, "na", "NA"):
                        coord.pop(key, None)
        return payload

    @staticmethod
    def _project(
        payload: dict[str, Any],
        mode: str,
        *,
        keep_traits_in_minimal: bool = False,
    ) -> tuple[dict[str, Any], list[UntrustedText]]:
        """Project a full variant payload down to the requested verbosity.

        Pure dict transform; tolerant of missing keys. ``full`` and ``standard`` return the
        payload with null trait ids and 'na' alleles stripped (a strictly monotonic ladder).
        Every mode that emits ``traits`` fences each trait name as v1.1 ``untrusted_text``, since
        compact mode's "trait names only" projection is still the same upstream free text and
        must be fenced too, not just the full/standard object form. Only traits that survive into
        the actual response are fenced — fencing (and thus limit-checking) a trait compact mode
        discards would inflate the response-wide object count against text that never leaves the
        server.

        ``keep_traits_in_minimal`` is set by the SINGLE-record tool (get_variant), where the
        trait list IS the record's payload: a minimal projection that returned an identifier and
        nothing else would be a response mode that destroys what it was asked for. The batch/list
        tools leave it off — there the rows themselves are the payload, and per-row traits are
        exactly the optional detail ``minimal`` exists to drop.
        """
        record_id_base = payload.get("vcv_accession") or str(payload.get("variation_id") or "")

        if mode == "minimal":
            out = {key: payload[key] for key in _MINIMAL_FIELDS if key in payload}
            if not keep_traits_in_minimal:
                return out, []
            return ClinVarService._with_capped_traits(out, payload, record_id_base)

        if mode == "full":
            fenced = ClinVarService._fence_traits(payload, record_id_base)
            return ClinVarService._trim_null_keys(payload), fenced

        out = {key: payload[key] for key in _MINIMAL_FIELDS if key in payload}
        for key in _COMPACT_EXTRA_FIELDS:
            if key in payload:
                out[key] = payload[key]

        if mode == "standard":
            fenced = ClinVarService._fence_traits(payload, record_id_base)
            trimmed = ClinVarService._trim_null_keys(payload)
            out["traits"] = trimmed.get("traits", [])
            for key in _STANDARD_EXTRA_FIELDS:
                if key in trimmed:
                    out[key] = trimmed[key]
            return out, fenced

        # compact (default): fence only the first 5 traits — the truncation
        # this projection actually emits.
        return ClinVarService._with_capped_traits(out, payload, record_id_base)

    @staticmethod
    def _with_capped_traits(
        out: dict[str, Any], payload: dict[str, Any], record_id_base: str
    ) -> tuple[dict[str, Any], list[UntrustedText]]:
        """Attach the first 5 fenced trait names to a lean projection."""
        capped_traits = (payload.get("traits") or [])[:5]
        fenced = ClinVarService._fence_traits({"traits": capped_traits}, record_id_base)
        out["traits"] = [t.get("name") for t in capped_traits if isinstance(t, dict)]
        return out, fenced
