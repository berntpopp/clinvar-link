"""Read-only SQLite repository for the built ClinVar index.

All indexes (resolution lookups, gene membership, FTS5, per-gene summaries) are
pre-computed by :mod:`clinvar_link.ingest.builder`, so this layer only reads
rows, decodes the JSON list/object columns, and stitches the per-assembly
coordinates back onto each canonical ``variant`` row.

FTS5 queries are sanitized so raw user text never reaches ``MATCH`` (which can
raise on bare operator characters), with a ``LIKE`` fallback for empty or
pathological input.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from clinvar_link.exceptions import ClinVarDataError

_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _like_escape(value: str) -> str:
    """Escape LIKE wildcards so caller text is matched literally (ESCAPE '\\')."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# JSON-encoded columns on the ``variant`` table that decode to Python lists.
_JSON_LIST_FIELDS = ("molecular_consequence", "traits", "rcv_accessions")
# GRCh38 sorts first, GRCh37 next, then anything else.
_ASSEMBLY_ORDER = {"GRCh38": 0, "GRCh37": 1}


class ClinVarRepository:
    """Read-only access to the built ClinVar SQLite index."""

    def __init__(self, db_path: Path | str) -> None:
        """Open a read-only connection to the ClinVar database."""
        self._path = Path(db_path)
        if not self._path.exists():
            raise ClinVarDataError(
                f"ClinVar database not found at {self._path}. "
                "Build it with the clinvar-link ingest CLI."
            )
        try:
            self._conn = sqlite3.connect(
                f"file:{self._path}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:  # pragma: no cover - rare OS-level failure
            raise ClinVarDataError(f"Cannot open ClinVar database at {self._path}: {exc}.") from exc
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA query_only=ON")

    # -- row decoding ----------------------------------------------------------

    def _coordinates(self, vid: int) -> list[dict[str, Any]]:
        """Return per-assembly coordinate dicts for a variant (GRCh38 first)."""
        rows = self._conn.execute(
            "SELECT * FROM variant_coordinate WHERE variation_id = ?",
            (vid,),
        ).fetchall()
        coords = [dict(r) for r in rows]
        coords.sort(key=lambda c: _ASSEMBLY_ORDER.get(c.get("assembly", ""), 99))
        return coords

    def _row_to_variant(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a ``variant`` Row to a dict, decoding JSON and attaching coords."""
        record: dict[str, Any] = {}
        for key in row.keys():  # noqa: SIM118 - sqlite3.Row.keys() needed for names
            value = row[key]
            if key in _JSON_LIST_FIELDS:
                record[key] = json.loads(value) if value else []
            else:
                record[key] = value
        record["coordinates"] = self._coordinates(int(row["variation_id"]))
        return record

    # -- direct lookups --------------------------------------------------------

    def get_by_variation_id(self, vid: int) -> dict[str, Any] | None:
        """Return the canonical variant for a VariationID, or ``None``."""
        row = self._conn.execute("SELECT * FROM variant WHERE variation_id = ?", (vid,)).fetchone()
        return self._row_to_variant(row) if row is not None else None

    def get_by_vcv(self, vcv: str) -> dict[str, Any] | None:
        """Return a variant by VCV accession; handles ``VCV000012345`` or ``12345``."""
        text = (vcv or "").strip()
        if text.upper().startswith("VCV"):
            text = text[3:]
        text = text.strip()
        try:
            vid = int(text)
        except (ValueError, TypeError):
            return None
        return self.get_by_variation_id(vid)

    def get_by_rsid(self, rsid: int) -> dict[str, Any] | None:
        """Return the first variant linked to a dbSNP rsid, or ``None``."""
        row = self._conn.execute(
            "SELECT v.* FROM rsid_lookup r JOIN variant v "
            "ON v.variation_id = r.variation_id WHERE r.rsid = ? LIMIT 1",
            (rsid,),
        ).fetchone()
        return self._row_to_variant(row) if row is not None else None

    def get_by_allele_id(self, allele_id: int) -> dict[str, Any] | None:
        """Return the first variant linked to a ClinVar AlleleID, or ``None``."""
        row = self._conn.execute(
            "SELECT v.* FROM allele_id_lookup a JOIN variant v "
            "ON v.variation_id = a.variation_id WHERE a.allele_id = ? LIMIT 1",
            (allele_id,),
        ).fetchone()
        return self._row_to_variant(row) if row is not None else None

    def get_by_hgvs(self, hgvs: str) -> dict[str, Any] | None:
        """Return the first variant matching a normalized HGVS string, or ``None``.

        Tries the exact normalized key first, then a gene-qualifier-insensitive
        match so a clean transcript-qualified expression that omits the ``(GENE)``
        qualifier (``NM_007294.4:c.5266dupC``) still resolves against the stored
        canonical key (``NM_007294.4(BRCA1):c.5266dupC``) on the first call.
        """
        norm = (hgvs or "").strip().lower()
        if not norm:
            return None
        row = self._conn.execute(
            "SELECT v.* FROM hgvs_lookup h JOIN variant v "
            "ON v.variation_id = h.variation_id WHERE h.hgvs_norm = ? LIMIT 1",
            (norm,),
        ).fetchone()
        if row is not None:
            return self._row_to_variant(row)
        return self._get_by_hgvs_gene_insensitive(norm)

    def _get_by_hgvs_gene_insensitive(self, norm: str) -> dict[str, Any] | None:
        """Match a stored gene-qualified key when the query omits ``(GENE)``.

        Only fires for an ``accession:change`` shape that has no parenthesised
        gene before the colon; inserts a single LIKE wildcard for the gene and
        anchors on the accession prefix + change suffix (both escaped so caller
        text is matched literally). Widens the gene, never the change.
        """
        head, sep, tail = norm.partition(":")
        if not sep or "(" in head:
            return None
        pattern = f"{_like_escape(head)}(%):{_like_escape(tail)}"
        row = self._conn.execute(
            "SELECT v.* FROM hgvs_lookup h JOIN variant v "
            "ON v.variation_id = h.variation_id "
            "WHERE h.hgvs_norm LIKE ? ESCAPE '\\' "
            "ORDER BY h.variation_id LIMIT 1",
            (pattern,),
        ).fetchone()
        return self._row_to_variant(row) if row is not None else None

    # -- search ----------------------------------------------------------------

    @staticmethod
    def _fts_query(text: str) -> str:
        """Build a safe FTS5 MATCH string (token OR, last token prefix-matched)."""
        tokens = _FTS_TOKEN_RE.findall(text or "")
        if not tokens:
            return '""'
        quoted = [f'"{tok}"' for tok in tokens[:-1]]
        quoted.append(f'"{tokens[-1]}"*')
        return " OR ".join(quoted)

    def search(
        self,
        query: str,
        *,
        gene_symbol: str | None = None,
        classification: str | None = None,
        min_stars: int | None = None,
        assembly: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Search variants by free text (FTS5) with optional filters.

        Joins ``variant_fts.rowid`` to ``variant.variation_id``. When the query is
        empty or FTS5 raises on the sanitized input, falls back to a ``LIKE`` scan
        over ``variant.name`` / ``variant.gene_symbol``.
        """
        filter_sql, filter_params = self._search_filters(
            gene_symbol=gene_symbol,
            classification=classification,
            min_stars=min_stars,
            assembly=assembly,
        )

        tokens = _FTS_TOKEN_RE.findall(query or "")
        if tokens:
            match = self._fts_query(query)
            # filter_sql is built only from hardcoded clause strings; all
            # user-supplied values are bound via ``?`` parameters.
            sql = (
                "SELECT v.* FROM variant_fts f "  # noqa: S608
                "JOIN variant v ON v.variation_id = f.rowid "
                "WHERE variant_fts MATCH ?"
                f"{filter_sql} "
                "ORDER BY rank, v.variation_id LIMIT ? OFFSET ?"
            )
            try:
                rows = self._conn.execute(sql, (match, *filter_params, limit, offset)).fetchall()
                return [self._row_to_variant(r) for r in rows]
            except sqlite3.Error:
                pass  # fall through to LIKE

        return self._search_like(
            query,
            filter_sql=filter_sql,
            filter_params=filter_params,
            limit=limit,
            offset=offset,
        )

    def _search_like(
        self,
        query: str,
        *,
        filter_sql: str,
        filter_params: list[Any],
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        """LIKE fallback over ``variant.name`` / ``variant.gene_symbol``."""
        cleaned = (query or "").replace("%", "").replace("_", "").strip().upper()
        pattern = f"%{cleaned}%"
        # filter_sql is hardcoded; user values are bound via ``?`` parameters.
        sql = (
            "SELECT v.* FROM variant v "  # noqa: S608
            "WHERE (UPPER(v.name) LIKE ? OR UPPER(v.gene_symbol) LIKE ?)"
            f"{filter_sql} "
            "ORDER BY v.star_rating DESC, v.variation_id LIMIT ? OFFSET ?"
        )
        rows = self._conn.execute(sql, (pattern, pattern, *filter_params, limit, offset)).fetchall()
        return [self._row_to_variant(r) for r in rows]

    def count_search(
        self,
        query: str,
        *,
        gene_symbol: str | None = None,
        classification: str | None = None,
        min_stars: int | None = None,
        assembly: str | None = None,
    ) -> int:
        """Return the total match count for :meth:`search` (for pagination totals).

        Mirrors :meth:`search`'s FTS5 / LIKE-fallback dispatch and filters, but
        counts rows instead of materializing them, so the service can report
        ``total_count`` / ``has_more`` without fetching every page.
        """
        filter_sql, filter_params = self._search_filters(
            gene_symbol=gene_symbol,
            classification=classification,
            min_stars=min_stars,
            assembly=assembly,
        )
        tokens = _FTS_TOKEN_RE.findall(query or "")
        if tokens:
            match = self._fts_query(query)
            # filter_sql is hardcoded; user values are bound via ``?`` params.
            sql = (
                "SELECT COUNT(*) AS n FROM variant_fts f "  # noqa: S608
                "JOIN variant v ON v.variation_id = f.rowid "
                "WHERE variant_fts MATCH ?"
                f"{filter_sql}"
            )
            try:
                row = self._conn.execute(sql, (match, *filter_params)).fetchone()
                return int(row["n"]) if row is not None else 0
            except sqlite3.Error:
                pass  # fall through to LIKE
        cleaned = (query or "").replace("%", "").replace("_", "").strip().upper()
        pattern = f"%{cleaned}%"
        # filter_sql is hardcoded; user values are bound via ``?`` params.
        sql = (
            "SELECT COUNT(*) AS n FROM variant v "  # noqa: S608
            "WHERE (UPPER(v.name) LIKE ? OR UPPER(v.gene_symbol) LIKE ?)"
            f"{filter_sql}"
        )
        row = self._conn.execute(sql, (pattern, pattern, *filter_params)).fetchone()
        return int(row["n"]) if row is not None else 0

    @staticmethod
    def _search_filters(
        *,
        gene_symbol: str | None,
        classification: str | None,
        min_stars: int | None,
        assembly: str | None,
    ) -> tuple[str, list[Any]]:
        """Build the shared optional-filter SQL fragment and its bound params."""
        clauses: list[str] = []
        params: list[Any] = []
        if gene_symbol:
            clauses.append("UPPER(v.gene_symbol) = ?")
            params.append(gene_symbol.upper())
        if classification:
            clauses.append("v.classification = ?")
            params.append(classification)
        if min_stars is not None:
            clauses.append("v.star_rating >= ?")
            params.append(min_stars)
        if assembly:
            clauses.append(
                "EXISTS (SELECT 1 FROM variant_coordinate c "
                "WHERE c.variation_id = v.variation_id AND c.assembly = ?)"
            )
            params.append(assembly)
        fragment = "".join(f" AND {clause}" for clause in clauses)
        return fragment, params

    # -- gene-scoped listing ---------------------------------------------------

    def variants_by_gene(
        self,
        gene_symbol: str,
        *,
        classification: str | None = None,
        min_stars: int | None = None,
        sort: str = "stars_desc",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return variants for a gene (via ``gene_index``), with optional filters."""
        where, params = self._gene_filters(
            gene_symbol, classification=classification, min_stars=min_stars
        )
        order = "v.star_rating DESC, v.variation_id" if sort == "stars_desc" else "v.variation_id"
        # ``where``/``order`` are built from hardcoded fragments; user values
        # are bound via ``?`` parameters.
        sql = (
            "SELECT v.* FROM gene_index g JOIN variant v "  # noqa: S608
            "ON v.variation_id = g.variation_id "
            f"WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?"
        )
        rows = self._conn.execute(sql, (*params, limit, offset)).fetchall()
        return [self._row_to_variant(r) for r in rows]

    def count_variants_by_gene(
        self,
        gene_symbol: str,
        *,
        classification: str | None = None,
        min_stars: int | None = None,
    ) -> int:
        """Return the total variant count for a gene (for pagination totals)."""
        where, params = self._gene_filters(
            gene_symbol, classification=classification, min_stars=min_stars
        )
        # ``where`` is hardcoded; user values are bound via ``?`` parameters.
        sql = (
            "SELECT COUNT(*) AS n FROM gene_index g JOIN variant v "  # noqa: S608
            "ON v.variation_id = g.variation_id "
            f"WHERE {where}"
        )
        row = self._conn.execute(sql, tuple(params)).fetchone()
        return int(row["n"]) if row is not None else 0

    @staticmethod
    def _gene_filters(
        gene_symbol: str,
        *,
        classification: str | None,
        min_stars: int | None,
    ) -> tuple[str, list[Any]]:
        """Build the WHERE fragment shared by gene listing + count queries."""
        clauses = ["g.gene_symbol_upper = ?"]
        params: list[Any] = [gene_symbol.upper()]
        if classification:
            clauses.append("v.classification = ?")
            params.append(classification)
        if min_stars is not None:
            clauses.append("v.star_rating >= ?")
            params.append(min_stars)
        return " AND ".join(clauses), params

    # -- aggregates + provenance -----------------------------------------------

    def gene_summary(self, gene_symbol: str) -> dict[str, Any] | None:
        """Return the precomputed per-gene summary dict, or ``None`` if absent."""
        row = self._conn.execute(
            "SELECT summary_json FROM gene_summary WHERE gene_symbol_upper = ?",
            (gene_symbol.upper(),),
        ).fetchone()
        if row is None or row["summary_json"] is None:
            return None
        result: dict[str, Any] = json.loads(row["summary_json"])
        return result

    def meta(self) -> dict[str, Any] | None:
        """Return build provenance from the ``meta`` table, or ``None``."""
        row = self._conn.execute("SELECT * FROM meta WHERE id = 1").fetchone()
        return dict(row) if row is not None else None

    def close(self) -> None:
        """Release the underlying database connection."""
        self._conn.close()
