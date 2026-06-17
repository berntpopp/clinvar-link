# clinvar-link Harden-Beyond-9/10 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the six confirmed-open correctness/robustness defects and add a data-freshness signal + a drift-guard test, taking the source past 9/10 for an LLM consumer.

**Architecture:** Service layer (`clinvar_service.py`) owns input validation and clamping and returns plain dicts; the MCP layer (`errors.py` envelope, FastMCP `Field` constraints via the installed validation handler) turns bad input into truthful `invalid_input` envelopes; the repository (`repository.py`) stays read-only and table-driven. No new tools, transports, or write paths.

**Tech Stack:** Python 3, FastMCP, pydantic / pydantic-settings, SQLite (read-only), pytest (async), ruff, mypy.

## Global Constraints

- Error taxonomy is exactly `not_found | invalid_input | internal_error`; keep `mcp/resources.error_codes` in sync. Errors are raised internally as typed exceptions (`ToolInputError` → `invalid_input`, `DataNotFoundError` → `not_found`) and converted to envelopes by `run_mcp_tool`; never raise to the client.
- `response_mode ∈ minimal | compact | standard | full` (default `compact`).
- TDD: write the failing test first. Tests are network-free and build a fixture index from `tests/fixtures/variant_summary_sample.txt` (BRCA1/TTN/MLH1/AP5Z1, 20 variants; VariationID `100001` = BRCA1 `c.5266dupC`, rsid `80357906`, VCV `VCV000100001`, AlleleID `200001`). Coverage gate ≥ 70%.
- `MAX_PAGE_SIZE = 100` (`config.py:112`); `REFRESH_TTL_DAYS = 7` (`config.py:61`).
- Run `make ci-local` (ruff check, ruff format --check, mypy, pytest+coverage) before declaring any task done. Line length 100.
- Async tests use the existing pytest-asyncio config; mirror the fixture style already in each test file (`service` in `test_service.py`, `repo` in `test_repository.py`, `mcp` + `tests._fixture_db.{build_service,call_tool}` in `test_tools_*.py`).

---

### Task 1: Guard pagination bounds (O1)

**Files:**
- Modify: `clinvar_link/services/clinvar_service.py:252` (search), `:315` (by_gene)
- Modify: `clinvar_link/mcp/tools/variants.py` (`search_variants` signature), `clinvar_link/mcp/tools/genes.py` (`get_variants_by_gene` signature)
- Test: `tests/test_service.py`, `tests/test_tools_variants.py`

**Interfaces:**
- Consumes: `settings.MAX_PAGE_SIZE`.
- Produces: clamped `limit`/`offset` in both list service methods; `Field(ge=…)` constraints on the two tools.

- [ ] **Step 1: Write the failing service test**

In `tests/test_service.py`:

```python
async def test_search_negative_limit_is_clamped_not_unbounded(service):
    # A negative limit must never become SQLite "LIMIT -1" (an unbounded dump).
    out = await service.search_variants("BRCA1", limit=-1)
    assert out["limit"] == 1
    assert out["count"] <= out["total_count"]


async def test_variants_by_gene_negative_offset_clamped(service):
    out = await service.get_variants_by_gene("BRCA1", min_stars=0, offset=-5)
    assert out["offset"] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_service.py::test_search_negative_limit_is_clamped_not_unbounded tests/test_service.py::test_variants_by_gene_negative_offset_clamped -v`
Expected: FAIL (current code returns `limit == -1` / negative offset passes through).

- [ ] **Step 3: Clamp in the service**

In `clinvar_link/services/clinvar_service.py`, in `search_variants` replace line 252:

```python
        limit = max(1, min(limit, settings.MAX_PAGE_SIZE))
        offset = max(0, offset)
```

In `get_variants_by_gene` replace line 315 with the same two lines (keep them at the top of the method, before the `count_variants_by_gene` call):

```python
        limit = max(1, min(limit, settings.MAX_PAGE_SIZE))
        offset = max(0, offset)
```

- [ ] **Step 4: Run the service tests to verify they pass**

Run: `uv run pytest tests/test_service.py -v -k "clamp"`
Expected: PASS.

- [ ] **Step 5: Write the failing tool test**

In `tests/test_tools_variants.py` (follow the existing `mcp` fixture pattern in that file):

```python
async def test_search_rejects_nonpositive_limit(mcp):
    out = await call_tool(mcp, "search_variants", {"query": "BRCA1", "limit": 0})
    assert out["success"] is False and out["error_code"] == "invalid_input"
```

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest tests/test_tools_variants.py::test_search_rejects_nonpositive_limit -v`
Expected: FAIL (currently `limit=0` returns an empty success).

- [ ] **Step 7: Add Field constraints to the tool signatures**

In `clinvar_link/mcp/tools/variants.py`, add imports near the top:

```python
from typing import Annotated

from pydantic import Field
```

Change the `search_variants` signature `limit`/`offset` params to:

```python
        limit: Annotated[int, Field(ge=1)] = 20,
        offset: Annotated[int, Field(ge=0)] = 0,
```

In `clinvar_link/mcp/tools/genes.py`, add the same two imports and change `get_variants_by_gene`:

```python
        limit: Annotated[int, Field(ge=1)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
```

- [ ] **Step 8: Run the tool test + full suite**

Run: `uv run pytest tests/test_tools_variants.py::test_search_rejects_nonpositive_limit tests/ -q`
Expected: PASS (the installed `install_validation_error_handler` converts the pydantic constraint failure to an `invalid_input` envelope).

- [ ] **Step 9: Commit**

```bash
git add clinvar_link/services/clinvar_service.py clinvar_link/mcp/tools/variants.py clinvar_link/mcp/tools/genes.py tests/test_service.py tests/test_tools_variants.py
git commit -m "fix: clamp pagination bounds; reject non-positive limit as invalid_input"
```

---

### Task 2: Empty filter returns success, not not_found (O2)

**Files:**
- Modify: `clinvar_link/services/clinvar_service.py:316-323`
- Test: `tests/test_service.py`, `tests/test_tools_genes.py`

**Interfaces:**
- Consumes: `repo.count_variants_by_gene(gene, classification=, min_stars=)`, `self._pagination`, `self._release_date`.
- Produces: `get_variants_by_gene` returns empty-success for an existing gene whose filter matches nothing; `not_found` only for an unknown gene.

- [ ] **Step 1: Write the failing tests**

In `tests/test_service.py`:

```python
async def test_variants_by_gene_empty_filter_is_success_not_error(service):
    # Existing gene + impossible filter (min_stars above the 0-4 range) -> empty
    # success, NOT a not_found error.
    out = await service.get_variants_by_gene("BRCA1", min_stars=5)
    assert out["gene_symbol"].upper() == "BRCA1"
    assert out["results"] == [] and out["count"] == 0 and out["total_count"] == 0


async def test_variants_by_gene_unknown_gene_still_not_found(service):
    with pytest.raises(DataNotFoundError):
        await service.get_variants_by_gene("NOTAGENE")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_service.py -v -k "empty_filter or unknown_gene"`
Expected: the empty-filter test FAILS (raises `DataNotFoundError`); the unknown-gene test passes.

- [ ] **Step 3: Distinguish unknown-gene from empty-filter**

In `clinvar_link/services/clinvar_service.py`, in `get_variants_by_gene`, replace the block:

```python
        if total == 0:
            raise DataNotFoundError(f"No ClinVar variants for gene {gene_symbol!r}")
```

with:

```python
        if total == 0:
            gene_total = await asyncio.to_thread(
                self.repo.count_variants_by_gene, gene_symbol
            )
            if gene_total == 0:
                raise DataNotFoundError(f"No ClinVar variants for gene {gene_symbol!r}")
            # Gene exists; the filter simply excluded everything -> empty success
            # (consistent with search_variants and out-of-range offset).
            return {
                "gene_symbol": gene_symbol,
                "results": [],
                "count": 0,
                **self._pagination(0, 0, limit, offset),
            }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_service.py -v -k "empty_filter or unknown_gene or pagination_metadata"`
Expected: PASS (and the existing `test_variants_by_gene_pagination_metadata` still passes).

- [ ] **Step 5: Add the tool-level test**

In `tests/test_tools_genes.py`:

```python
async def test_variants_by_gene_empty_filter_envelope_success(mcp):
    out = await call_tool(mcp, "get_variants_by_gene", {"gene_symbol": "BRCA1", "min_stars": 5})
    assert out["success"] is True
    assert out["results"] == [] and out["total_count"] == 0
```

- [ ] **Step 6: Run it + full suite**

Run: `uv run pytest tests/test_tools_genes.py tests/test_service.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add clinvar_link/services/clinvar_service.py tests/test_service.py tests/test_tools_genes.py
git commit -m "fix: empty gene filter returns empty success, not_found only for unknown gene"
```

---

### Task 3: id_type allowlist + auto shape validation, truthful messages (O3)

**Files:**
- Modify: `clinvar_link/services/clinvar_service.py:134-164` (`_resolve`, `_resolve_auto`), `:212-214` (`get_variants` batch loop)
- Test: `tests/test_service.py`, `tests/test_tools_variants.py`

**Interfaces:**
- Consumes: `ToolInputError`, the existing `_RSID_RE`/`_VCV_RE`/`_DIGITS_RE`/`_HGVS_HINTS`.
- Produces: `_resolve` rejects unknown `id_type`; `_resolve_auto` raises `ToolInputError` for shapeless input; `get_variants` treats a malformed identifier as a per-row miss (`found: False`), not a batch-killer.

- [ ] **Step 1: Write the failing tests**

In `tests/test_service.py`:

```python
async def test_get_variant_garbage_is_invalid_input_not_not_found(service):
    with pytest.raises(ToolInputError):
        await service.get_variant("@@bad@@")


async def test_get_variant_unknown_id_type_is_invalid_input(service):
    with pytest.raises(ToolInputError):
        await service.get_variant("VCV000100001", id_type="banana")


async def test_get_variant_recognized_but_absent_still_not_found(service):
    with pytest.raises(DataNotFoundError):
        await service.get_variant("VCV999999999")


async def test_get_variants_batch_tolerates_malformed_identifier(service):
    out = await service.get_variants(["VCV000100001", "@@bad@@"])
    assert out["found_count"] == 1
    miss = [r for r in out["results"] if not r.get("found")]
    assert miss and miss[0]["identifier"] == "@@bad@@"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_service.py -v -k "garbage or unknown_id_type or recognized_but_absent or malformed_identifier"`
Expected: the garbage / unknown_id_type / malformed_identifier tests FAIL (garbage currently raises `DataNotFoundError`; bad `id_type` is silently treated as auto).

- [ ] **Step 3: Add the id_type allowlist**

In `clinvar_link/services/clinvar_service.py`, add a module constant near the other regexes (after line 38):

```python
_ID_TYPES = frozenset({"auto", "vcv", "variation_id", "rsid", "hgvs", "allele_id"})
```

At the top of `_resolve` (before the `if id_type == "vcv"` line), insert:

```python
        if id_type not in _ID_TYPES:
            raise ToolInputError(
                f"id_type must be one of {sorted(_ID_TYPES)} (got {id_type!r})"
            )
```

- [ ] **Step 4: Make `_resolve_auto` reject shapeless input**

Replace the tail of `_resolve_auto` (the final `result = ... get_by_hgvs ...` fallback and `return await self._maybe_allele_id(text)`) so the method ends:

```python
        if ":" in text or any(hint in text for hint in _HGVS_HINTS):
            return await asyncio.to_thread(self.repo.get_by_hgvs, text)
        raise ToolInputError(
            "unrecognized identifier shape; expected a VCV accession, dbSNP rsID, "
            "HGVS expression, ClinVar AlleleID, or VariationID — or call "
            "search_variants to locate the record"
        )
```

- [ ] **Step 5: Make batch tolerate malformed identifiers**

In `get_variants`, replace line 214:

```python
            repo_dict = await self._resolve(text, id_type) if text else None
```

with:

```python
            try:
                repo_dict = await self._resolve(text, id_type) if text else None
            except ToolInputError:
                repo_dict = None  # a malformed id in a batch is a miss, not a fatal error
```

- [ ] **Step 6: Run the service tests to verify they pass**

Run: `uv run pytest tests/test_service.py -q`
Expected: PASS (existing `test_get_variant_*` and `test_get_variants_batch_mixed` still pass — they use recognized shapes).

- [ ] **Step 7: Add the tool-level test**

In `tests/test_tools_variants.py`:

```python
async def test_get_variant_garbage_returns_invalid_input(mcp):
    out = await call_tool(mcp, "get_variant", {"identifier": "@@bad@@"})
    assert out["success"] is False and out["error_code"] == "invalid_input"
```

- [ ] **Step 8: Run it + full suite**

Run: `uv run pytest tests/ -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add clinvar_link/services/clinvar_service.py tests/test_service.py tests/test_tools_variants.py
git commit -m "fix: validate id_type + identifier shape; truthful invalid_input for malformed ids"
```

---

### Task 4: sort allowlist + implement stars_asc/name (O4)

**Files:**
- Modify: `clinvar_link/data/repository.py:323-346` (`variants_by_gene`)
- Modify: `clinvar_link/services/clinvar_service.py:303-313` (validate `sort`)
- Modify: `clinvar_link/mcp/resources.py` (advertise `sort_options`)
- Test: `tests/test_repository.py`, `tests/test_service.py`, `tests/test_resources.py`

**Interfaces:**
- Produces: `ClinVarRepository.SORT_ORDERS` (class attr, dict[str, str]); service raises `ToolInputError` on unknown sort; capabilities exposes `sort_options`.
- Consumes: `ClinVarRepository` (already imported in `clinvar_service.py:18`).

- [ ] **Step 1: Write the failing repository tests**

In `tests/test_repository.py`:

```python
def test_variants_by_gene_sort_stars_asc(repo):
    asc = repo.variants_by_gene("AP5Z1", sort="stars_asc", limit=100)
    assert asc == sorted(asc, key=lambda x: x["star_rating"])


def test_variants_by_gene_sort_name(repo):
    by_name = repo.variants_by_gene("AP5Z1", sort="name", limit=100)
    names = [r.get("name") or "" for r in by_name]
    assert names == sorted(names, key=str.lower)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_repository.py -v -k "sort_stars_asc or sort_name"`
Expected: FAIL (both currently fall back to `ORDER BY variation_id`).

- [ ] **Step 3: Add the table-driven sort to the repository**

In `clinvar_link/data/repository.py`, add a class attribute inside `ClinVarRepository` (just under the class docstring, before `__init__`):

```python
    # Allow-listed ORDER BY fragments; user input selects a key, never raw SQL.
    SORT_ORDERS = {
        "stars_desc": "v.star_rating DESC, v.variation_id",
        "stars_asc": "v.star_rating ASC, v.variation_id",
        "name": "v.name COLLATE NOCASE, v.variation_id",
        "variation_id": "v.variation_id",
    }
```

In `variants_by_gene`, replace line 337:

```python
        order = self.SORT_ORDERS.get(sort, self.SORT_ORDERS["stars_desc"])
```

- [ ] **Step 4: Run the repository tests to verify they pass**

Run: `uv run pytest tests/test_repository.py -v -k "sort"`
Expected: PASS (including the existing `test_variants_by_gene_sort_default`).

- [ ] **Step 5: Write the failing service + capabilities tests**

In `tests/test_service.py`:

```python
async def test_variants_by_gene_unknown_sort_is_invalid_input(service):
    with pytest.raises(ToolInputError):
        await service.get_variants_by_gene("BRCA1", sort="banana")
```

In `tests/test_resources.py`:

```python
def test_capabilities_advertises_sort_options():
    from clinvar_link.data.repository import ClinVarRepository
    from clinvar_link.mcp.resources import get_capabilities_resource

    caps = get_capabilities_resource()
    assert caps["sort_options"] == sorted(ClinVarRepository.SORT_ORDERS)
```

- [ ] **Step 6: Run them to verify they fail**

Run: `uv run pytest tests/test_service.py::test_variants_by_gene_unknown_sort_is_invalid_input tests/test_resources.py::test_capabilities_advertises_sort_options -v`
Expected: FAIL.

- [ ] **Step 7: Validate sort in the service + advertise it**

In `clinvar_link/services/clinvar_service.py`, in `get_variants_by_gene`, after the bounds clamp (Task 1) and before the count call, add:

```python
        if sort not in ClinVarRepository.SORT_ORDERS:
            raise ToolInputError(
                f"sort must be one of {sorted(ClinVarRepository.SORT_ORDERS)} (got {sort!r})"
            )
```

In `clinvar_link/mcp/resources.py`, add to the dict returned by `get_capabilities_resource` (next to `response_modes`):

```python
        "sort_options": sorted(__import__("clinvar_link.data.repository", fromlist=["ClinVarRepository"]).ClinVarRepository.SORT_ORDERS),
```

If you prefer an explicit import over `__import__`, add `from clinvar_link.data.repository import ClinVarRepository` at the top of `resources.py` and use `sorted(ClinVarRepository.SORT_ORDERS)` — confirm no import cycle by running the suite (resources imports only the class object, no DB access).

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_service.py tests/test_resources.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add clinvar_link/data/repository.py clinvar_link/services/clinvar_service.py clinvar_link/mcp/resources.py tests/test_repository.py tests/test_service.py tests/test_resources.py
git commit -m "feat: validated multi-key sort (stars_desc/asc/name/variation_id) for get_variants_by_gene"
```

---

### Task 5: Reject blank query without a filter (O5)

**Files:**
- Modify: `clinvar_link/services/clinvar_service.py:251` (top of `search_variants`)
- Test: `tests/test_service.py`, `tests/test_tools_variants.py`

**Interfaces:**
- Produces: `search_variants` raises `ToolInputError` for a blank query with no filter; a blank query WITH a filter still lists (preserves `test_search_empty_query_like_fallback`).

- [ ] **Step 1: Write the failing tests**

In `tests/test_service.py`:

```python
async def test_search_blank_query_without_filter_is_invalid_input(service):
    with pytest.raises(ToolInputError):
        await service.search_variants("   ")


async def test_search_blank_query_with_filter_is_allowed(service):
    out = await service.search_variants("", gene_symbol="TTN")
    assert out["count"] >= 1
```

- [ ] **Step 2: Run them to verify the first fails**

Run: `uv run pytest tests/test_service.py -v -k "blank_query"`
Expected: `..._without_filter...` FAILS (returns a match-all success today); `..._with_filter...` passes.

- [ ] **Step 3: Add the policy at the top of `search_variants`**

In `clinvar_link/services/clinvar_service.py`, as the first statements of `search_variants` (before the bounds clamp):

```python
        has_filter = bool(gene_symbol or classification or min_stars is not None)
        if not (query or "").strip() and not has_filter:
            raise ToolInputError(
                "query is required; to list a gene's variants use get_variants_by_gene"
            )
```

- [ ] **Step 4: Run the service tests to verify they pass**

Run: `uv run pytest tests/test_service.py -v -k "blank_query"`
Expected: PASS.

- [ ] **Step 5: Add the tool-level test**

In `tests/test_tools_variants.py`:

```python
async def test_search_blank_query_returns_invalid_input(mcp):
    out = await call_tool(mcp, "search_variants", {"query": "  "})
    assert out["success"] is False and out["error_code"] == "invalid_input"
```

- [ ] **Step 6: Run it + full suite**

Run: `uv run pytest tests/ -q`
Expected: PASS (repo-level `test_search_empty_query_like_fallback` unaffected — it calls the repository directly).

- [ ] **Step 7: Commit**

```bash
git add clinvar_link/services/clinvar_service.py tests/test_service.py tests/test_tools_variants.py
git commit -m "fix: reject blank search query without a filter as invalid_input"
```

---

### Task 6: Data-freshness signal (age_days / past_ttl)

**Files:**
- Create: `clinvar_link/mcp/freshness.py`
- Modify: `clinvar_link/mcp/errors.py:71-91` (`_provenance_meta`)
- Modify: `clinvar_link/mcp/resources.py` (`get_capabilities_resource`)
- Test: `tests/test_freshness.py` (create), `tests/test_tools_variants.py`

**Interfaces:**
- Produces: `clinvar_freshness(release_date: str | None, ttl_days: int, *, now: datetime | None = None) -> dict[str, int | bool] | None`.
- Consumes: `settings.REFRESH_TTL_DAYS`, the process-cached release date.

- [ ] **Step 1: Write the failing helper tests**

Create `tests/test_freshness.py`:

```python
from datetime import datetime, timezone

from clinvar_link.mcp.freshness import clinvar_freshness


def test_freshness_within_ttl():
    now = datetime(2026, 6, 17, tzinfo=timezone.utc)
    assert clinvar_freshness("Mon, 15 Jun 2026 08:40:33 GMT", 7, now=now) == {
        "age_days": 1,
        "past_ttl": False,
    }


def test_freshness_past_ttl():
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    f = clinvar_freshness("Mon, 15 Jun 2026 08:40:33 GMT", 7, now=now)
    assert f["past_ttl"] is True and f["age_days"] >= 8


def test_freshness_none_for_missing_or_bad():
    assert clinvar_freshness(None, 7) is None
    assert clinvar_freshness("not-a-date", 7) is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_freshness.py -v`
Expected: FAIL with `ModuleNotFoundError: clinvar_link.mcp.freshness`.

- [ ] **Step 3: Create the pure helper**

Create `clinvar_link/mcp/freshness.py`:

```python
"""Pure helper: derive data-freshness fields from the cached ClinVar release date.

Leaf module (stdlib only) so the envelope (``errors``) and capabilities
(``resources``) builders can both import it without a cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def clinvar_freshness(
    release_date: str | None,
    ttl_days: int,
    *,
    now: datetime | None = None,
) -> dict[str, int | bool] | None:
    """Return ``{age_days, past_ttl}`` for an RFC1123 release date, or ``None``.

    ``age_days`` is whole days between the release date and ``now`` (UTC), floored
    at 0; ``past_ttl`` is ``age_days > ttl_days``. Returns ``None`` when the date
    is missing or unparseable so callers simply omit the fields.
    """
    if not release_date:
        return None
    try:
        released = parsedate_to_datetime(release_date)
    except (TypeError, ValueError):
        return None
    if released is None:
        return None
    if released.tzinfo is None:
        released = released.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age_days = max(0, (current - released).days)
    return {"age_days": age_days, "past_ttl": age_days > ttl_days}
```

- [ ] **Step 4: Run the helper tests to verify they pass**

Run: `uv run pytest tests/test_freshness.py -v`
Expected: PASS.

- [ ] **Step 5: Wire freshness into the envelope `_meta`**

In `clinvar_link/mcp/errors.py`, add imports near the top:

```python
from clinvar_link.config import settings
from clinvar_link.mcp.freshness import clinvar_freshness
```

In `_provenance_meta`, replace the `if clinvar_date is not None:` block:

```python
    if clinvar_date is not None:
        meta["clinvar_release_date"] = clinvar_date
        fresh = clinvar_freshness(clinvar_date, settings.REFRESH_TTL_DAYS)
        if fresh is not None:
            meta.update(fresh)
```

- [ ] **Step 6: Wire freshness into capabilities**

In `clinvar_link/mcp/resources.py`, add imports:

```python
from clinvar_link.config import settings
from clinvar_link.mcp.freshness import clinvar_freshness
```

In `get_capabilities_resource`, build the dict into a local `caps`, then append freshness before returning:

```python
    date = get_cached_clinvar_release_date()
    caps = {
        # ... existing keys unchanged, but use ``date`` for the two release fields:
        "clinvar_release": date or CLINVAR_DATA_RELEASE,
        "clinvar_release_date": date,
        # ... rest unchanged ...
    }
    fresh = clinvar_freshness(date, settings.REFRESH_TTL_DAYS) if date else None
    if fresh is not None:
        caps["data_freshness"] = fresh
    return caps
```

- [ ] **Step 7: Write the tool-level freshness test**

In `tests/test_tools_variants.py`:

```python
async def test_meta_carries_freshness(mcp):
    out = await call_tool(mcp, "get_variant", {"identifier": "VCV000100001"})
    assert isinstance(out["_meta"]["age_days"], int)
    assert isinstance(out["_meta"]["past_ttl"], bool)
```

- [ ] **Step 8: Run it + full suite**

Run: `uv run pytest tests/ -q`
Expected: PASS (a data call primes the cache, so `_meta` carries the freshness fields; shape-only assertions stay deterministic).

- [ ] **Step 9: Commit**

```bash
git add clinvar_link/mcp/freshness.py clinvar_link/mcp/errors.py clinvar_link/mcp/resources.py tests/test_freshness.py tests/test_tools_variants.py
git commit -m "feat: data-freshness signal (age_days/past_ttl) in _meta and capabilities"
```

---

### Task 7: Capabilities↔model drift-guard test

**Files:**
- Test: `tests/test_resources.py`

**Interfaces:**
- Consumes: `get_capabilities_resource()`, `ClinVarVariant.model_fields`.

- [ ] **Step 1: Write the drift-guard test**

In `tests/test_resources.py`:

```python
def test_output_cheatsheet_fields_exist_on_model():
    from clinvar_link.mcp.resources import get_capabilities_resource
    from clinvar_link.models.variant_models import ClinVarVariant

    cheats = get_capabilities_resource()["output_cheatsheet"]
    model_fields = set(ClinVarVariant.model_fields)
    for key, field_name in cheats.items():
        if field_name.startswith("_meta"):
            continue  # next_commands_field is an envelope path, not a model field
        assert field_name in model_fields, f"cheatsheet {key}={field_name!r} is not a ClinVarVariant field"
```

- [ ] **Step 2: Run it to verify it passes today (guards future drift)**

Run: `uv run pytest tests/test_resources.py::test_output_cheatsheet_fields_exist_on_model -v`
Expected: PASS now; it FAILS only if someone renames a model field or mistypes the cheatsheet (the exact stale-cheatsheet class of bug seen on the live server).

- [ ] **Step 3: Commit**

```bash
git add tests/test_resources.py
git commit -m "test: guard capabilities output_cheatsheet against ClinVarVariant drift"
```

---

### Task 8: Final gate + docs sync

**Files:**
- Modify: `CLAUDE.md` (if the error-taxonomy / sort note needs updating), `clinvar_link/mcp/resources.py` limitations (optional)
- Verify: whole repo

- [ ] **Step 1: Run the full local CI gate**

Run: `make ci-local`
Expected: ruff check clean, ruff format --check clean, mypy clean, pytest green, coverage ≥ 70%.

- [ ] **Step 2: Fix any lint/type/format fallout, then re-run**

Run: `make ci-local`
Expected: all green.

- [ ] **Step 3: Commit any cleanup**

```bash
git add -A
git commit -m "chore: lint/type/docs cleanup for 9-plus hardening"
```

---

## Self-Review

**Spec coverage:** O1→Task 1; O2→Task 2; O3→Task 3; O4→Task 4; O5→Task 5; freshness (AC4)→Task 6; drift guard (AC5)→Task 7; CI gate (AC6)→Task 8. Deferred items (outputSchema, full O6, submission_summary) are documented as non-goals in the spec and intentionally have no task.

**Placeholder scan:** No TBD/TODO; every code step shows the exact code and command.

**Type consistency:** `SORT_ORDERS` (Task 4) is referenced as `ClinVarRepository.SORT_ORDERS` in the service, repo, resources, and tests. `clinvar_freshness(release_date, ttl_days, *, now)` (Task 6) is used with that exact signature in the helper test, `_provenance_meta`, and `get_capabilities_resource`. `_ID_TYPES` (Task 3) is a module-level frozenset used only within `clinvar_service.py`. `ToolInputError`/`DataNotFoundError` match the imports already in `clinvar_service.py` and the test files.

**Known integration risk to watch:** Task 1 relies on FastMCP raising a `pydantic.ValidationError` for the `Annotated[int, Field(ge=…)]` constraint, which `install_validation_error_handler` converts to `invalid_input`. The service-side `max(1, …)` clamp is the guaranteed safety net regardless; if the tool test in Task 1 Step 8 does not see `invalid_input`, confirm the FastMCP version surfaces constraint failures through `tool.run` (it does on the version pinned in `pyproject.toml`) before adjusting.
