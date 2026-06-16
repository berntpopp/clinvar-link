import sqlite3
from importlib.resources import files


def _schema_sql() -> str:
    return (files("clinvar_link.data") / "schema.sql").read_text()


def test_schema_creates_all_tables():
    conn = sqlite3.connect(":memory:")
    conn.executescript(_schema_sql())
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {
        "meta",
        "variant",
        "variant_coordinate",
        "rsid_lookup",
        "allele_id_lookup",
        "hgvs_lookup",
        "gene_index",
        "gene_summary",
        "variant_fts",
    }
    assert expected <= names


def test_fts_table_present():
    conn = sqlite3.connect(":memory:")
    conn.executescript(_schema_sql())
    # FTS5 must be available and queryable
    conn.execute(
        "INSERT INTO variant_fts(rowid, name, gene_symbol, traits) VALUES (1,'NM_x:c.1A>T','BRCA1','breast cancer')"
    )
    rows = list(conn.execute("SELECT rowid FROM variant_fts WHERE variant_fts MATCH 'BRCA1'"))
    assert rows and rows[0][0] == 1
