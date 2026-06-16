-- clinvar-link local index schema (built from ClinVar variant_summary.txt).
-- A VariationID appears once per assembly (GRCh38 + GRCh37). We keep one
-- canonical `variant` row (GRCh38 preferred) and BOTH assemblies' coordinates.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = OFF;

-- Single-row build provenance.
CREATE TABLE meta (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version        INTEGER,
    clinvar_release_date  TEXT,
    source_url            TEXT,
    source_etag           TEXT,
    source_last_modified  TEXT,
    source_sha256         TEXT,
    variant_count         INTEGER,
    gene_count            INTEGER,
    build_utc             TEXT,
    build_duration_s      REAL
);

-- One canonical row per VariationID. List/object fields are stored as JSON text.
CREATE TABLE variant (
    variation_id            INTEGER PRIMARY KEY,
    vcv_accession           TEXT,
    allele_id               INTEGER,
    rsid                    INTEGER,
    name                    TEXT,
    gene_symbol             TEXT,
    gene_id                 TEXT,
    hgnc_id                 TEXT,
    variant_type            TEXT,
    clinical_significance   TEXT,
    classification          TEXT,
    review_status           TEXT,
    star_rating             INTEGER,
    protein_change          TEXT,
    cdna_change             TEXT,
    molecular_consequence   TEXT,   -- JSON array
    traits                  TEXT,   -- JSON array
    rcv_accessions          TEXT,   -- JSON array
    number_submitters       INTEGER,
    last_evaluated          TEXT,
    origin                  TEXT,
    canonical_assembly      TEXT,
    chromosome              TEXT,
    cytogenetic             TEXT
);
CREATE INDEX idx_variant_gene ON variant (gene_symbol);
CREATE INDEX idx_variant_class ON variant (classification);
CREATE INDEX idx_variant_stars ON variant (star_rating);

-- Per-assembly coordinates (1-2 rows per variant: GRCh38 and/or GRCh37).
CREATE TABLE variant_coordinate (
    variation_id          INTEGER,
    assembly              TEXT,
    chromosome_accession  TEXT,
    chromosome            TEXT,
    start                 INTEGER,
    stop                  INTEGER,
    reference_allele      TEXT,
    alternate_allele      TEXT,
    position_vcf          INTEGER,
    reference_allele_vcf  TEXT,
    alternate_allele_vcf  TEXT
);
CREATE INDEX idx_coord_vid ON variant_coordinate (variation_id);
CREATE INDEX idx_coord_assembly ON variant_coordinate (assembly, chromosome, start);

-- Resolution index: dbSNP rsid -> variation_id.
CREATE TABLE rsid_lookup (
    rsid          INTEGER,
    variation_id  INTEGER
);
CREATE INDEX idx_rsid ON rsid_lookup (rsid);

-- Resolution index: ClinVar AlleleID -> variation_id.
CREATE TABLE allele_id_lookup (
    allele_id     INTEGER,
    variation_id  INTEGER
);
CREATE INDEX idx_allele_id ON allele_id_lookup (allele_id);

-- Resolution index: normalized HGVS string -> variation_id.
CREATE TABLE hgvs_lookup (
    hgvs_norm     TEXT,
    variation_id  INTEGER
);
CREATE INDEX idx_hgvs_norm ON hgvs_lookup (hgvs_norm);

-- Gene -> variant membership (uppercased symbol for case-insensitive lookup).
CREATE TABLE gene_index (
    gene_symbol_upper  TEXT,
    variation_id       INTEGER
);
CREATE INDEX idx_gene_index ON gene_index (gene_symbol_upper);

-- Per-gene aggregate summary (precomputed JSON blob).
CREATE TABLE gene_summary (
    gene_symbol_upper  TEXT PRIMARY KEY,
    gene_symbol        TEXT,
    summary_json       TEXT
);

-- Free-text search over variant name, gene symbol, and traits.
-- Contentless (external-content) FTS5 keyed to variation_id: the builder MUST
-- insert each row with rowid = variation_id so MATCH results map back to a
-- variant. e.g. INSERT INTO variant_fts(rowid, name, gene_symbol, traits)
-- VALUES (<variation_id>, ...).
CREATE VIRTUAL TABLE variant_fts USING fts5 (
    name,
    gene_symbol,
    traits,
    content = '',
    tokenize = 'unicode61'
);
