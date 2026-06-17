# clinvar-link 9-plus v2 — Track B (ingest / index) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two ingest-layer fixes that need the next index rebuild to take full effect: index the gene-stripped canonical HGVS key (so gene-less HGVS resolves by equality, not a LIKE scan), and emit a source `other_count` so gene-summary buckets reconcile at the source.

**Architecture:** Both changes live in the ingest layer (`ingest/builder.py`, `ingest/parsing.py`). They are fully testable in CI against the fixture-built database; they only change the **shipped** bundle after the maintainer runs `clinvar-link-data build && publish`. Track A's read-time `other_count` already guarantees API reconciliation regardless of bundle age, so B2 makes the stored data self-consistent for any non-service consumer.

**Tech Stack:** Python 3, SQLite, pytest. `make ci-local` must pass.

## Global Constraints

- Alpha; the schema version stays `1` (no DDL change — both fixes are additive to existing tables/JSON).
- `hgvs_lookup` has `UNIQUE (hgvs_norm, variation_id)`; inserts use `INSERT OR IGNORE`, so duplicate keys are safe no-ops.
- Tests are network-free and build from `tests/fixtures/variant_summary_sample.txt`.
- **Rollout:** after merge, the maintainer must `uv run clinvar-link-data build` then `clinvar-link-data publish` to ship the benefits; Track A degrades gracefully on the old bundle.
- Fixture facts: `VCV000100001` (VariationID 100001) name = `NM_007294.4(BRCA1):c.5266dupC (p.Gln1756fs)`; `100005` name = `NM_007294.4(BRCA1):c.5333-1G>A` (no protein suffix).

---

### Task 1: Index the gene-stripped canonical HGVS key

**Files:**
- Modify: `clinvar_link/ingest/builder.py:334-372` (`_emit_canonical`) and add a module helper near `_normalize_hgvs` (`:154-156`)
- Test: `tests/test_builder.py` (or `tests/test_repository.py` — use the fixture DB)

**Interfaces:**
- Produces: builder helper `_strip_gene_qualifier(expr: str) -> str | None`; `hgvs_lookup` additionally contains the gene-stripped form `<accession>:<change>` for each canonical name.

- [ ] **Step 1: Write the failing test** — append to `tests/test_repository.py`:

```python
def test_gene_stripped_hgvs_key_is_indexed(repo):
    # The gene-less canonical form must exist as an EQUALITY key (not just resolvable
    # via the LIKE fallback). Keys are stored lower-cased.
    for key in ("nm_007294.4:c.5266dupc", "nm_007294.4:c.5333-1g>a"):
        hit = repo._conn.execute(
            "SELECT 1 FROM hgvs_lookup WHERE hgvs_norm = ?", (key,)
        ).fetchone()
        assert hit is not None, f"missing gene-stripped key: {key}"
    # And it resolves to the right variant.
    assert repo.get_by_hgvs("NM_007294.4:c.5266dupC")["variation_id"] == 100001
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `uv run pytest tests/test_repository.py::test_gene_stripped_hgvs_key_is_indexed -v`
Expected: FAIL on the first assertion (key absent; only the LIKE fallback resolves it today).

- [ ] **Step 3: Implement** — add the helper after `_normalize_hgvs` in `builder.py`:

```python
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
```

In `_emit_canonical`, after the existing nucleotide/VCV hgvs appends (right before the `vcv = ...` block, i.e. after line 369), add the gene-stripped key derived from the canonical nucleotide expression:

```python
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
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `uv run pytest tests/test_repository.py -v`
Expected: PASS. (The `_get_by_hgvs_gene_insensitive` LIKE fallback stays as a safety net for any key not present.)

- [ ] **Step 5: Commit**

```bash
git add clinvar_link/ingest/builder.py tests/test_repository.py
git commit -m "feat(ingest): index gene-stripped canonical HGVS key for fast equality lookup"
```

---

### Task 2: Emit source `other_count` in the gene accumulator

**Files:**
- Modify: `clinvar_link/ingest/parsing.py:449-504` (`GeneAccumulator.finalize`)
- Test: `tests/test_parsing.py` (or `tests/test_ingest.py`)

**Interfaces:**
- Produces: `GeneAccumulator.finalize()` output gains `other_count = total − Σ(named buckets)`; the stored `gene_summary.summary_json` therefore reconciles. Track A's service still derives `other_count` independently, so old bundles remain correct via the API.

- [ ] **Step 1: Write the failing test** — append to `tests/test_parsing.py`:

```python
from clinvar_link.ingest.parsing import GeneAccumulator, load_star_map


def test_finalize_other_count_catches_unbucketed():
    acc = GeneAccumulator(load_star_map())
    acc.add_variant({"classification": "Pathogenic", "star_rating": 2})
    acc.add_variant({"classification": "risk factor", "star_rating": 1})  # outside named buckets
    stats = acc.finalize()
    known = (
        stats["pathogenic_count"] + stats["likely_pathogenic_count"] + stats["vus_count"]
        + stats["benign_count"] + stats["likely_benign_count"]
        + stats["conflicting_count"] + stats["not_provided_count"]
    )
    assert stats["other_count"] == stats["total_count"] - known
    assert stats["other_count"] == 1
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `uv run pytest tests/test_parsing.py::test_finalize_other_count_catches_unbucketed -v`
Expected: FAIL (`KeyError: 'other_count'`).

- [ ] **Step 3: Implement** — in `finalize`, just before building the `stats` dict, compute the catch-all, and add the key:

```python
        known_buckets = (
            self.pathogenic_count + self.likely_pathogenic_count + self.vus_count
            + self.benign_count + self.likely_benign_count
            + self.conflicting_count + self.not_provided_count
        )
        other_count = max(0, total - known_buckets)
```
then add `"other_count": other_count,` to the `stats` dict (next to `not_provided_count`).

- [ ] **Step 4: Run tests, expect PASS**

Run: `uv run pytest tests/test_parsing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add clinvar_link/ingest/parsing.py tests/test_parsing.py
git commit -m "feat(ingest): emit source other_count so stored gene summaries reconcile"
```

---

### Task 3: Rollout note + rebuild

**Files:** none (operational).

- [ ] **Step 1:** Confirm the full gate passes on the fixture build.

Run: `make ci-local`
Expected: PASS.

- [ ] **Step 2 (maintainer, manual):** Ship the benefits to production by rebuilding and republishing the bundle:

```bash
uv run clinvar-link-data build      # rebuild SQLite index from the weekly source
uv run clinvar-link-data publish    # pack + gh-release the new bundle-<DATE>
```
Expected: a new `bundle-<YYYY-MM-DD>` release; consumers `pull` it on next bootstrap. Until then Track A's read-time `other_count` keeps the API correct, and gene-less HGVS still resolves via the LIKE fallback (just slower).

---

## Self-Review

- **Spec coverage:** B1→Task 1 (SC8: gene-less HGVS equality), B2→Task 2 (source reconciliation). Rollout→Task 3.
- **Placeholder scan:** none — concrete code + commands throughout.
- **Type consistency:** `_strip_gene_qualifier(expr: str) -> str | None` defined and used in Task 1; `other_count: int` added to the `finalize()` dict (Task 2) mirrors the `GeneClinVarSummary.other_count` field added in Track A Task 9.
- **Cross-track note:** B2's stored value and Track A's read-time derivation agree by construction (same formula); the service derivation is the back-compat path for pre-Track-B bundles.
