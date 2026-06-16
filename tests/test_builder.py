"""Tests for the ClinVar ingest pipeline (builder + downloader).

These run entirely offline: the builder consumes the committed TSV fixture and
the downloader test mocks NCBI with ``respx`` (a 200 then a conditional 304).
"""

import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import respx

from clinvar_link.config import Settings
from clinvar_link.ingest.builder import build_database
from clinvar_link.ingest.downloader import download_source

FIXTURE = Path(__file__).parent / "fixtures" / "variant_summary_sample.txt"

# The fixture authors 20 distinct VariationIDs (100001..100020), each present on
# both GRCh38 and GRCh37 (40 data rows total), across 4 genes.
EXPECTED_VARIANTS = 20
EXPECTED_GENES = 4

# The 9 secondary B-tree indexes deferred to after the bulk insert (indexes.sql).
EXPECTED_INDEXES = {
    "idx_variant_gene",
    "idx_variant_class",
    "idx_variant_stars",
    "idx_coord_vid",
    "idx_coord_assembly",
    "idx_rsid",
    "idx_allele_id",
    "idx_hgvs_norm",
    "idx_gene_index",
}


def test_build_dedup_and_meta(tmp_path):
    cfg = Settings(DATA_DIR=tmp_path, DB_FILENAME="t.sqlite")
    summary = build_database(
        cfg,
        source_path=FIXTURE,
        etag='"e"',
        last_modified="Mon, 01 Jan 2026 00:00:00 GMT",
    )
    assert summary["variant_count"] == EXPECTED_VARIANTS
    assert summary["gene_count"] == EXPECTED_GENES
    assert summary["db_path"] == str(cfg.db_path)

    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    nvar = conn.execute("SELECT COUNT(*) c FROM variant").fetchone()["c"]
    ncoord = conn.execute("SELECT COUNT(*) c FROM variant_coordinate").fetchone()["c"]
    assert nvar == EXPECTED_VARIANTS
    assert nvar >= 4
    assert ncoord == 2 * nvar  # both assemblies per variant

    # Canonical row always prefers GRCh38.
    asm = {r["canonical_assembly"] for r in conn.execute("SELECT canonical_assembly FROM variant")}
    assert asm == {"GRCh38"}

    meta = conn.execute("SELECT * FROM meta").fetchone()
    assert meta["variant_count"] == nvar and meta["gene_count"] >= 4
    assert meta["clinvar_release_date"]
    assert meta["schema_version"] == 1
    assert meta["source_etag"] == '"e"'
    assert meta["source_sha256"]
    assert meta["build_utc"]

    # A known rsid resolves to its variant.
    assert (
        conn.execute("SELECT variation_id FROM rsid_lookup WHERE rsid=80357906").fetchone()
        is not None
    )

    # gene_summary JSON parses and carries counts.
    gs = conn.execute(
        "SELECT summary_json FROM gene_summary WHERE gene_symbol_upper='BRCA1'"
    ).fetchone()
    assert gs and json.loads(gs["summary_json"])["total_count"] >= 1

    # FTS over the gene symbol works (rowid == variation_id).
    hit = conn.execute(
        "SELECT rowid FROM variant_fts WHERE variant_fts MATCH 'BRCA1' LIMIT 1"
    ).fetchone()
    assert hit is not None

    conn.close()


def test_build_resolution_indexes(tmp_path):
    cfg = Settings(DATA_DIR=tmp_path, DB_FILENAME="t.sqlite")
    build_database(cfg, source_path=FIXTURE)
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # allele_id_lookup is populated (one per emitted variant).
    nallele = conn.execute("SELECT COUNT(*) c FROM allele_id_lookup").fetchone()["c"]
    assert nallele == EXPECTED_VARIANTS

    # hgvs_lookup indexes both the normalized HGVS name and the VCV accession.
    vcv_hit = conn.execute(
        "SELECT variation_id FROM hgvs_lookup WHERE hgvs_norm = ?",
        ("vcv000100001",),
    ).fetchone()
    assert vcv_hit is not None and vcv_hit["variation_id"] == 100001

    name_hit = conn.execute(
        "SELECT variation_id FROM hgvs_lookup WHERE hgvs_norm LIKE 'nm_007294.4(brca1):c.5266dupc%'"
    ).fetchone()
    assert name_hit is not None

    # gene_index maps uppercased gene symbol -> variant.
    n_brca1 = conn.execute(
        "SELECT COUNT(*) c FROM gene_index WHERE gene_symbol_upper = 'BRCA1'"
    ).fetchone()["c"]
    assert n_brca1 == 5

    conn.close()


def test_build_coordinates_differ_per_assembly(tmp_path):
    cfg = Settings(DATA_DIR=tmp_path, DB_FILENAME="t.sqlite")
    build_database(cfg, source_path=FIXTURE)
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT assembly, start, chromosome_accession FROM variant_coordinate "
        "WHERE variation_id = 100001 ORDER BY assembly"
    ).fetchall()
    assert {r["assembly"] for r in rows} == {"GRCh37", "GRCh38"}
    starts = {r["assembly"]: r["start"] for r in rows}
    assert starts["GRCh37"] != starts["GRCh38"]

    conn.close()


def test_build_creates_deferred_indexes(tmp_path):
    # All 9 secondary indexes must exist after the build, even though they are
    # created only after the bulk insert + FTS optimize.
    cfg = Settings(DATA_DIR=tmp_path, DB_FILENAME="t.sqlite")
    build_database(cfg, source_path=FIXTURE)
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert names == EXPECTED_INDEXES


def test_build_gene_summary_drops_detail_lists(tmp_path):
    # gene_summary JSON keeps aggregate fields but no longer carries the
    # never-read per-variant detail lists.
    cfg = Settings(DATA_DIR=tmp_path, DB_FILENAME="t.sqlite")
    build_database(cfg, source_path=FIXTURE)
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT summary_json FROM gene_summary WHERE gene_symbol_upper='BRCA1'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    blob = row["summary_json"]
    assert "protein_variants" not in blob
    assert "genomic_variants" not in blob

    summary = json.loads(blob)
    assert "protein_variants" not in summary
    assert "genomic_variants" not in summary
    assert "total_count" in summary
    assert "star_distribution" in summary
    assert "has_pathogenic" in summary


def test_build_fts_still_works_after_optimize(tmp_path):
    # Deferred indexes + FTS 'optimize' must not break free-text search.
    cfg = Settings(DATA_DIR=tmp_path, DB_FILENAME="t.sqlite")
    build_database(cfg, source_path=FIXTURE)
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    try:
        hit = conn.execute(
            "SELECT rowid FROM variant_fts WHERE variant_fts MATCH 'BRCA1' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert hit is not None


def test_build_uses_provided_source_sha256(tmp_path):
    # When source_sha256 is supplied, it is written verbatim to meta without
    # re-hashing the source file.
    cfg = Settings(DATA_DIR=tmp_path, DB_FILENAME="t.sqlite")
    build_database(cfg, source_path=FIXTURE, source_sha256="deadbeef")
    conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        meta = conn.execute("SELECT source_sha256 FROM meta WHERE id = 1").fetchone()
    finally:
        conn.close()
    assert meta["source_sha256"] == "deadbeef"


@respx.mock
def test_download_source_ok_then_not_modified(tmp_path):
    url = "https://example.test/variant_summary.txt.gz"
    dest = tmp_path / "variant_summary.txt.gz"
    cache = tmp_path / "download_cache.json"

    route = respx.get(url)

    # First call: 200 with validators -> "ok", body streamed, cache persisted.
    route.return_value = httpx.Response(
        200,
        content=b"FRESH-BODY",
        headers={"ETag": '"abc"', "Last-Modified": "Mon, 01 Jan 2026 00:00:00 GMT"},
    )
    first = download_source(url, dest, cache_path=cache)
    assert first["status"] == "ok"
    assert first["etag"] == '"abc"'
    assert dest.read_bytes() == b"FRESH-BODY"
    # SHA-256 is computed inline while streaming the body to disk.
    assert first["sha256"] == hashlib.sha256(b"FRESH-BODY").hexdigest()
    assert cache.exists()
    cached = json.loads(cache.read_text())
    assert cached[url]["etag"] == '"abc"'

    # Second call: conditional GET returns 304 -> "not_modified", body untouched.
    route.return_value = httpx.Response(304)
    second = download_source(url, dest, cache_path=cache)
    assert second["status"] == "not_modified"
    assert second["path"] == str(dest)
    assert dest.read_bytes() == b"FRESH-BODY"

    # The second request carried the cached validators as conditional headers.
    last_request = respx.calls.last.request
    assert last_request.headers.get("If-None-Match") == '"abc"'
    assert last_request.headers.get("If-Modified-Since") == "Mon, 01 Jan 2026 00:00:00 GMT"
