"""Atomic SQLite builder for the ClinVar ``variant_summary.txt`` dump.

A VariationID appears once per assembly (GRCh38 + GRCh37). We keep one canonical
``variant`` row (GRCh38 preferred over GRCh37 over anything else) and BOTH
assemblies' coordinates in ``variant_coordinate``. The build streams the TSV
twice:

* **Pass 1** records, per VariationID, the highest assembly priority seen so the
  canonical row can be chosen deterministically regardless of row order.
* **Pass 2** inserts every coordinate row, then — for the single winning
  assembly row of each VariationID — the canonical ``variant`` row, resolution
  indexes (rsid / allele_id / hgvs), gene membership, and the FTS5 row (keyed to
  ``rowid = variation_id`` per the schema contract), while feeding a
  :class:`GeneAccumulator` per gene.

Everything is written to a temp file in ``DATA_DIR`` and atomically swapped into
place with :func:`os.replace`, so readers never see a half-built database.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from clinvar_link.ingest.parsing import GeneAccumulator, load_star_map, parse_variant_row

if TYPE_CHECKING:
    from clinvar_link.config import Settings

SCHEMA_VERSION = 1
_INSERT_BATCH = 2000
_HASH_CHUNK = 1 << 20

# Assembly preference for choosing the canonical ``variant`` row.
_PRIORITY: dict[str, int] = {"GRCh38": 3, "GRCh37": 2, "na": 1}

_VARIANT_COLUMNS: tuple[str, ...] = (
    "variation_id",
    "vcv_accession",
    "allele_id",
    "rsid",
    "name",
    "gene_symbol",
    "gene_id",
    "hgnc_id",
    "variant_type",
    "clinical_significance",
    "classification",
    "review_status",
    "star_rating",
    "protein_change",
    "cdna_change",
    "molecular_consequence",
    "traits",
    "rcv_accessions",
    "number_submitters",
    "last_evaluated",
    "origin",
    "canonical_assembly",
    "chromosome",
    "cytogenetic",
)

_COORD_COLUMNS: tuple[str, ...] = (
    "variation_id",
    "assembly",
    "chromosome_accession",
    "chromosome",
    "start",
    "stop",
    "reference_allele",
    "alternate_allele",
    "position_vcf",
    "reference_allele_vcf",
    "alternate_allele_vcf",
)


def _load_schema_sql() -> str:
    """Read the bundled schema DDL from package data."""
    return (files("clinvar_link.data") / "schema.sql").read_text(encoding="utf-8")


def _load_indexes_sql() -> str:
    """Read the bundled secondary-index DDL (applied after bulk insert)."""
    return (files("clinvar_link.data") / "indexes.sql").read_text(encoding="utf-8")


def _priority(assembly: str) -> int:
    """Return the canonical-row preference for an assembly (default 1)."""
    return _PRIORITY.get(assembly, 1)


@contextmanager
def _open_source(path: Path) -> Iterator[TextIO]:
    """Open the source TSV, transparently decompressing ``.gz`` inputs."""
    if path.suffix == ".gz":
        handle: TextIO = gzip.open(path, "rt", encoding="utf-8", errors="replace")  # noqa: SIM115
    else:
        handle = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115
    try:
        yield handle
    finally:
        handle.close()


def _sha256(path: Path) -> str:
    """Compute the SHA-256 hex digest of the source file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _int_or_none(value: str) -> int | None:
    """Parse a base-10 int, returning None for empty/sentinel/non-numeric text."""
    text = (value or "").strip()
    if not text or text in ("-", "na", "NA"):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _scan_winning(path: Path) -> dict[int, int]:
    """Pass 1: map each VariationID to the max assembly priority seen for it."""
    winning: dict[int, int] = {}
    with _open_source(path) as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            vid = _int_or_none(row.get("VariationID", "") or "")
            if vid is None:
                continue
            prio = _priority((row.get("Assembly", "") or "").strip())
            if prio > winning.get(vid, 0):
                winning[vid] = prio
    return winning


def _normalize_hgvs(value: str) -> str:
    """Normalize an HGVS / accession string for the resolution index."""
    return (value or "").strip().lower()


def _strip_gene_qualifier(expr: str) -> str | None:
    """Return ``<accession>:<change>`` with the ``(GENE)`` qualifier removed, or None.

    ``NM_007294.4(BRCA1):c.5266dupC`` -> ``NM_007294.4:c.5266dupC``. Returns None
    when there is no gene parenthesis before the first colon (nothing to strip).
    """
    head, sep, tail = expr.partition(":")
    if not sep or "(" not in head:
        return None
    accession = head.split("(", 1)[0].strip()
    if not accession:
        return None
    return f"{accession}:{tail}"


def _coord_values(vid: int, row: dict[str, str]) -> tuple[Any, ...]:
    """Build a ``variant_coordinate`` row tuple from a raw TSV row."""
    return (
        vid,
        (row.get("Assembly", "") or "").strip(),
        row.get("ChromosomeAccession", "") or "",
        row.get("Chromosome", "") or "",
        _int_or_none(row.get("Start", "") or ""),
        _int_or_none(row.get("Stop", "") or ""),
        row.get("ReferenceAllele", "") or "",
        row.get("AlternateAllele", "") or "",
        _int_or_none(row.get("PositionVCF", "") or ""),
        row.get("ReferenceAlleleVCF", "") or "",
        row.get("AlternateAlleleVCF", "") or "",
    )


def _variant_values(vid: int, parsed: dict[str, Any]) -> tuple[Any, ...]:
    """Build a canonical ``variant`` row tuple from a parsed variant dict."""
    allele_id = _int_or_none(str(parsed.get("allele_id", "")))
    return (
        vid,
        parsed.get("accession"),
        allele_id,
        parsed.get("rsid"),
        parsed.get("name"),
        parsed.get("gene_symbol"),
        parsed.get("gene_id"),
        parsed.get("hgnc_id"),
        parsed.get("variant_type"),
        parsed.get("clinical_significance"),
        parsed.get("classification"),
        parsed.get("review_status"),
        parsed.get("star_rating"),
        parsed.get("protein_change"),
        parsed.get("cdna_change"),
        json.dumps(parsed.get("molecular_consequences", [])),
        json.dumps(parsed.get("traits", [])),
        json.dumps(parsed.get("rcv_accessions", [])),
        parsed.get("number_submitters"),
        parsed.get("last_evaluated"),
        parsed.get("origin"),
        parsed.get("assembly"),
        parsed.get("chromosome"),
        parsed.get("cytogenetic"),
    )


class _Batches:
    """Accumulates pending insert tuples and flushes them in bulk."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.variant: list[tuple[Any, ...]] = []
        self.coord: list[tuple[Any, ...]] = []
        self.rsid: list[tuple[int, int]] = []
        self.allele: list[tuple[int, int]] = []
        self.hgvs: list[tuple[str, int]] = []
        self.gene: list[tuple[str, int]] = []
        self.fts: list[tuple[int, str, str, str]] = []

        variant_cols = ", ".join(_VARIANT_COLUMNS)
        variant_ph = ", ".join("?" for _ in _VARIANT_COLUMNS)
        self._variant_sql = f"INSERT INTO variant ({variant_cols}) VALUES ({variant_ph})"  # noqa: S608
        coord_cols = ", ".join(_COORD_COLUMNS)
        coord_ph = ", ".join("?" for _ in _COORD_COLUMNS)
        self._coord_sql = f"INSERT INTO variant_coordinate ({coord_cols}) VALUES ({coord_ph})"  # noqa: S608

    def maybe_flush(self) -> None:
        if len(self.coord) >= _INSERT_BATCH or len(self.variant) >= _INSERT_BATCH:
            self.flush()

    def flush(self) -> None:
        conn = self._conn
        if self.variant:
            conn.executemany(self._variant_sql, self.variant)
            self.variant.clear()
        if self.coord:
            conn.executemany(self._coord_sql, self.coord)
            self.coord.clear()
        if self.rsid:
            conn.executemany(
                "INSERT INTO rsid_lookup (rsid, variation_id) VALUES (?, ?)", self.rsid
            )
            self.rsid.clear()
        if self.allele:
            conn.executemany(
                "INSERT INTO allele_id_lookup (allele_id, variation_id) VALUES (?, ?)",
                self.allele,
            )
            self.allele.clear()
        if self.hgvs:
            conn.executemany(
                "INSERT OR IGNORE INTO hgvs_lookup (hgvs_norm, variation_id) VALUES (?, ?)",
                self.hgvs,
            )
            self.hgvs.clear()
        if self.gene:
            conn.executemany(
                "INSERT INTO gene_index (gene_symbol_upper, variation_id) VALUES (?, ?)",
                self.gene,
            )
            self.gene.clear()
        if self.fts:
            conn.executemany(
                "INSERT INTO variant_fts (rowid, name, gene_symbol, traits) VALUES (?, ?, ?, ?)",
                self.fts,
            )
            self.fts.clear()


# hgvs4variation.txt positional columns (the file has no usable header row;
# every comment/explanation line is '#'-prefixed and skipped).
_HGVS4VAR_TYPE_COL = 4
_HGVS4VAR_VID_COL = 2
# Expression columns to index: only the PRECISE, transcript-/protein-qualified
# full expressions -- NucleotideExpression (col 6, ``NM_...:c.`` / ``NR_...:n.``)
# and ProteinExpression (col 8, ``NP_...:p.``). The bare short forms
# NucleotideChange (col 7, e.g. ``c.5266dupC``) and ProteinChange (col 9, e.g.
# ``p.Gln1756fs``) are AMBIGUOUS -- the same change maps to many variants across
# genes/transcripts -- so they are intentionally NOT indexed.
_HGVS4VAR_EXPR_COLS = (6, 8)
_HGVS4VAR_MIN_FIELDS = 13


def _load_hgvs4variation(conn: sqlite3.Connection, path: Path, emitted: set[int]) -> int:
    """Index the precise coding/protein HGVS expressions from ``hgvs4variation.txt[.gz]``.

    For each data row whose VariationID was kept (in ``emitted``) and is not a
    ``genomic``-Type row (huge ``g.`` coordinate strings, low value for lookups),
    inserts the normalized full NucleotideExpression (col 6) and ProteinExpression
    (col 8) into ``hgvs_lookup``. The bare short forms (NucleotideChange col 7,
    ProteinChange col 9) are ambiguous and deliberately skipped. Inserts use
    ``INSERT OR IGNORE`` against the UNIQUE ``idx_hgvs_norm`` so the GRCh37/GRCh38
    rows that repeat the same expression collapse to one ``(hgvs_norm,
    variation_id)`` row. Streams the file and flushes in bounded batches so memory
    stays flat on the full dump. Returns the number of insert attempts made (some
    may be ignored as duplicates).
    """
    pending: list[tuple[str, int]] = []
    inserted = 0
    sql = "INSERT OR IGNORE INTO hgvs_lookup (hgvs_norm, variation_id) VALUES (?, ?)"

    def flush() -> None:
        nonlocal inserted
        if pending:
            conn.executemany(sql, pending)
            inserted += len(pending)
            pending.clear()

    with _open_source(path) as handle:
        reader = csv.reader(handle, delimiter="\t")
        for fields in reader:
            if not fields or fields[0].startswith("#"):
                continue
            if len(fields) < _HGVS4VAR_MIN_FIELDS:
                continue
            if fields[_HGVS4VAR_TYPE_COL] == "genomic":
                continue
            vid = _int_or_none(fields[_HGVS4VAR_VID_COL])
            if vid is None or vid not in emitted:
                continue
            for col in _HGVS4VAR_EXPR_COLS:
                value = fields[col]
                if not value or value == "-":
                    continue
                hgvs_norm = _normalize_hgvs(value)
                if hgvs_norm:
                    pending.append((hgvs_norm, vid))
            if len(pending) >= _INSERT_BATCH:
                flush()
    flush()
    return inserted


def _emit_canonical(
    batches: _Batches,
    accumulators: dict[str, GeneAccumulator],
    star_map: dict[str, int],
    vid: int,
    row: dict[str, str],
) -> None:
    """Insert the canonical variant + resolution/index/FTS rows for one VariationID."""
    parsed = parse_variant_row(row, star_map)
    batches.variant.append(_variant_values(vid, parsed))

    rsid = parsed.get("rsid")
    if rsid is not None:
        batches.rsid.append((int(rsid), vid))

    allele_id = _int_or_none(str(parsed.get("allele_id", "")))
    if allele_id is not None:
        batches.allele.append((allele_id, vid))

    name = parsed.get("name") or ""
    hgvs_norm = _normalize_hgvs(name)
    if hgvs_norm:
        batches.hgvs.append((hgvs_norm, vid))
        # Also index the canonical NUCLEOTIDE expression: the part of Name
        # before the protein suffix. ClinVar Name is
        # "<nucleotide-expression> (<protein-change>)", e.g.
        # "NM_007294.4(BRCA1):c.5266dupC (p.Gln1756fs)". Indexing the bare
        # nucleotide form lets get_by_hgvs("NM_007294.4(BRCA1):c.5266dupC")
        # resolve even though the stored Name carries the trailing "(p....)".
        # The parenthetical short protein is AMBIGUOUS and deliberately NOT
        # indexed. INSERT OR IGNORE against UNIQUE(hgvs_norm, variation_id)
        # makes the duplicate (when no protein suffix exists) a no-op.
        if " (" in name:
            nucleotide = name.split(" (")[0].strip()
            if nucleotide and nucleotide != name:
                batches.hgvs.append((_normalize_hgvs(nucleotide), vid))
    vcv = _normalize_hgvs(parsed.get("accession") or "")
    if vcv:
        batches.hgvs.append((vcv, vid))

    # Also index the GENE-STRIPPED canonical form so a gene-less but
    # transcript-qualified query (NM_007294.4:c.5266dupC) resolves via the
    # equality index instead of the slower LIKE fallback. INSERT OR IGNORE
    # keeps it a no-op when the name already had no gene qualifier.
    canonical_nuc = name.split(" (")[0].strip() if " (" in name else name
    stripped = _strip_gene_qualifier(canonical_nuc)
    if stripped:
        stripped_norm = _normalize_hgvs(stripped)
        if stripped_norm and stripped_norm != _normalize_hgvs(canonical_nuc):
            batches.hgvs.append((stripped_norm, vid))

    gene_symbol = parsed.get("gene_symbol") or ""
    if gene_symbol:
        gene_upper = gene_symbol.upper()
        batches.gene.append((gene_upper, vid))
        acc = accumulators.get(gene_upper)
        if acc is None:
            acc = GeneAccumulator(star_map)
            accumulators[gene_upper] = acc
        acc.add_variant(parsed)

    traits_text = " ".join(t.get("name", "") for t in parsed.get("traits", []))
    batches.fts.append((vid, name, gene_symbol, traits_text))


def _write_gene_summaries(
    conn: sqlite3.Connection,
    accumulators: dict[str, GeneAccumulator],
    gene_display: dict[str, str],
) -> int:
    """Write one ``gene_summary`` row per gene; return the gene count."""
    rows = [
        (gene_upper, gene_display.get(gene_upper, gene_upper), json.dumps(acc.finalize()))
        for gene_upper, acc in accumulators.items()
    ]
    if rows:
        conn.executemany(
            "INSERT INTO gene_summary (gene_symbol_upper, gene_symbol, summary_json) "
            "VALUES (?, ?, ?)",
            rows,
        )
    return len(rows)


def _write_meta(
    conn: sqlite3.Connection,
    *,
    config: Settings,
    etag: str | None,
    last_modified: str | None,
    release_date: str | None,
    source_sha256: str,
    variant_count: int,
    gene_count: int,
    build_duration_s: float,
) -> None:
    conn.execute(
        """
        INSERT INTO meta (
            id, schema_version, clinvar_release_date, source_url, source_etag,
            source_last_modified, source_sha256, variant_count, gene_count,
            build_utc, build_duration_s
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            SCHEMA_VERSION,
            release_date or last_modified,
            config.SOURCE_URL,
            etag,
            last_modified,
            source_sha256,
            variant_count,
            gene_count,
            datetime.now(tz=UTC).isoformat(),
            round(build_duration_s, 3),
        ),
    )


def build_database(
    config: Settings,
    *,
    source_path: Path,
    hgvs_source_path: Path | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    release_date: str | None = None,
    source_sha256: str | None = None,
) -> dict[str, Any]:
    """Build the ClinVar SQLite index from ``source_path``, atomically.

    Streams the ``variant_summary`` TSV twice (dedup pass + emit pass), writes
    all tables + the FTS5 index into a temp database in ``config.DATA_DIR``, then
    atomically swaps it into ``config.db_path``.

    When ``hgvs_source_path`` is given, the secondary ``hgvs4variation`` source is
    streamed afterwards to index every coding/protein HGVS expression of each
    kept variant into ``hgvs_lookup`` (before the deferred indexes are built).

    Returns a summary dict: ``variant_count``, ``gene_count``,
    ``clinvar_release_date``, ``db_path``.
    """
    start = time.perf_counter()
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    star_map = load_star_map()

    winning = _scan_winning(source_path)

    fd, tmp_name = tempfile.mkstemp(dir=config.DATA_DIR, suffix=".sqlite.tmp")
    os.close(fd)
    tmp_path = Path(tmp_name)

    try:
        conn = sqlite3.connect(tmp_path)
        try:
            # Build-time PRAGMAs on the throwaway temp DB: durability mid-build
            # is irrelevant since the file is atomically os.replace'd into place.
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=-262144")  # ~256 MB page cache
            conn.execute("PRAGMA mmap_size=268435456")  # 256 MB
            conn.executescript(_load_schema_sql())

            # Create the UNIQUE hgvs index UP FRONT (the other 8 secondary indexes
            # stay deferred to indexes.sql). It must exist during the inserts so
            # INSERT OR IGNORE dedups exact (hgvs_norm, variation_id) pairs as the
            # build streams (GRCh37/GRCh38 repeat the same coding/protein form).
            # A UNIQUE (hgvs_norm, variation_id) index still serves
            # ``WHERE hgvs_norm = ?`` lookups as a leftmost-prefix scan.
            conn.execute(
                "CREATE UNIQUE INDEX idx_hgvs_norm ON hgvs_lookup (hgvs_norm, variation_id)"
            )

            batches = _Batches(conn)
            accumulators: dict[str, GeneAccumulator] = {}
            gene_display: dict[str, str] = {}
            emitted: set[int] = set()

            with _open_source(source_path) as handle:
                reader = csv.DictReader(handle, delimiter="\t")
                for row in reader:
                    vid = _int_or_none(row.get("VariationID", "") or "")
                    if vid is None:
                        continue

                    # Always record the per-assembly coordinates.
                    batches.coord.append(_coord_values(vid, row))

                    assembly = (row.get("Assembly", "") or "").strip()
                    if _priority(assembly) == winning.get(vid) and vid not in emitted:
                        gene_symbol = (row.get("GeneSymbol", "") or "").strip()
                        if gene_symbol:
                            gene_display.setdefault(gene_symbol.upper(), gene_symbol)
                        _emit_canonical(batches, accumulators, star_map, vid, row)
                        emitted.add(vid)

                    batches.maybe_flush()

            batches.flush()

            # Secondary source: index ALL coding/protein HGVS expressions of the
            # kept variants. Runs before the deferred indexes so idx_hgvs_norm is
            # built once over the full table at the end.
            if hgvs_source_path is not None:
                _load_hgvs4variation(conn, hgvs_source_path, emitted)

            gene_count = _write_gene_summaries(conn, accumulators, gene_display)
            sha256 = source_sha256 if source_sha256 is not None else _sha256(source_path)
            _write_meta(
                conn,
                config=config,
                etag=etag,
                last_modified=last_modified,
                release_date=release_date,
                source_sha256=sha256,
                variant_count=len(emitted),
                gene_count=gene_count,
                build_duration_s=time.perf_counter() - start,
            )

            # Build the secondary B-tree indexes now that the bulk insert is
            # done (no per-row index maintenance), then compact the FTS index.
            conn.executescript(_load_indexes_sql())
            conn.execute("INSERT INTO variant_fts(variant_fts) VALUES('optimize')")

            # journal_mode=OFF leaves no rollback journal; reset to DELETE so the
            # file is in a clean state that opens cleanly read-only.
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.commit()
            variant_count = len(emitted)
            release = release_date or last_modified
        finally:
            conn.close()
        os.replace(tmp_path, config.db_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    return {
        "variant_count": variant_count,
        "gene_count": gene_count,
        "clinvar_release_date": release,
        "db_path": str(config.db_path),
    }
