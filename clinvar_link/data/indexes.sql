-- Secondary B-tree indexes for the clinvar-link local index.
-- Applied AFTER the bulk insert (and after the FTS5 'optimize') so the inserts
-- do not pay index-maintenance cost per row. The temp DB is atomically swapped
-- into place once these exist. Mirrors the tables defined in schema.sql.
--
-- NOTE: idx_hgvs_norm is intentionally NOT here. It is a UNIQUE index on
-- (hgvs_norm, variation_id) created up front in builder.build_database (before
-- any hgvs_lookup inserts) so that INSERT OR IGNORE dedups exact pairs as the
-- build streams. A UNIQUE (hgvs_norm, variation_id) index still serves
-- ``WHERE hgvs_norm = ?`` as a leftmost-prefix scan.
CREATE INDEX idx_variant_gene ON variant (gene_symbol);
CREATE INDEX idx_variant_class ON variant (classification);
CREATE INDEX idx_variant_stars ON variant (star_rating);
CREATE INDEX idx_coord_vid ON variant_coordinate (variation_id);
CREATE INDEX idx_coord_assembly ON variant_coordinate (assembly, chromosome, start);
CREATE INDEX idx_rsid ON rsid_lookup (rsid);
CREATE INDEX idx_allele_id ON allele_id_lookup (allele_id);
CREATE INDEX idx_gene_index ON gene_index (gene_symbol_upper);
