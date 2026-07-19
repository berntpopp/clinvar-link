# clinvar-link 9-plus v2 — Track A (MCP / read-time) Implementation Plan

> Historical record — This plan records completed implementation work; the live MCP registry is authoritative.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Push the ClinVar-Link MCP past 9/10 with MCP/read-time fixes that ship without rebuilding the index: drift-proof + version-stamped surface, precise AND search with OR-fallback and tiered count, per-tool error UX, and gene-bucket reconciliation + envelope/token cleanup.

**Architecture:** Changes stay in the MCP/service/repository layers. The repository gains an AND/OR-parameterized FTS query and a capped count; the service orchestrates AND→OR fallback, `limit+1` `has_more`, and `count_mode`; `mcp/errors.py` gains version stamping + per-tool recovery prose; the gene summary gains a read-time `other_count`. No ingest or schema changes (those are Track B).

**Tech Stack:** Python 3, FastMCP, pydantic v2, SQLite FTS5, pytest (`make ci-local`: ruff check, ruff format --check, mypy, pytest w/ coverage gate ≥ 70%).

## Global Constraints

- Project is **alpha — breaking changes permitted**; reflect them by bumping the package version (`0.1.0 → 0.2.0` via `pyproject.toml [project] version`).
- Errors are **returned, not raised** to the client (envelope dicts via `run_mcp_tool`).
- Every response carries `_meta.next_commands`; every result keeps `recommended_citation` + the ClinVar release in `_meta`.
- Line length 100; type all new code (mypy); TDD (tests first); network-free tests build from `tests/fixtures/variant_summary_sample.txt`.
- Keep the six-tool surface in lockstep across `mcp/tools/`, `mcp/facade.py`, `mcp/resources.py`.
- Run `make ci-local` green before every commit.
- Fixture facts (for tests): genes BRCA1 (VCV000100001–100005), TTN (…06–10), MLH1 (…11–15), AP5Z1 (…16–20). `100002` name = `NM_007294.4(BRCA1):c.181T>G (p.Cys61Gly)`; `100011` trait = `Lynch syndrome`.

---

### Task 1: Stamp `server_version` into every `_meta`

**Files:**
- Modify: `clinvar_link/mcp/resources.py:40-44` (rename `_server_version` → `server_version`)
- Modify: `clinvar_link/mcp/resources.py:51` (caller)
- Modify: `clinvar_link/mcp/errors.py:30` (import) and `:84-96` (`_provenance_meta`)
- Test: `tests/test_tools_metadata.py`

**Interfaces:**
- Produces: `clinvar_link.mcp.resources.server_version() -> str`; every `_meta` now contains `server_version: str`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_tools_metadata.py`:

```python
async def test_meta_carries_server_version(mcp):
    out = await call_tool(mcp, "get_variant", {"identifier": "VCV000100001"})
    assert isinstance(out["_meta"]["server_version"], str)
    assert out["_meta"]["server_version"]  # non-empty
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `uv run pytest tests/test_tools_metadata.py::test_meta_carries_server_version -v`
Expected: FAIL (`KeyError: 'server_version'`).

- [ ] **Step 3: Implement** — in `resources.py` rename the function and its call:

```python
def server_version() -> str:
    try:
        return version("clinvar-link")
    except PackageNotFoundError:
        return "unknown"
```
and at line 51 change `"server_version": _server_version(),` → `"server_version": server_version(),`.

In `errors.py`, change the import at line 30 to also import it, and add the field in `_provenance_meta`:

```python
from clinvar_link.mcp.resources import CLINVAR_DATA_RELEASE, server_version
```
```python
    meta: dict[str, Any] = {
        "unsafe_for_clinical_use": True,
        "server_version": server_version(),
        "clinvar_release": clinvar_date if clinvar_date else CLINVAR_DATA_RELEASE,
    }
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `uv run pytest tests/test_tools_metadata.py -v`
Expected: PASS (all, including the new test).

- [ ] **Step 5: Commit**

```bash
git add clinvar_link/mcp/resources.py clinvar_link/mcp/errors.py tests/test_tools_metadata.py
git commit -m "feat(mcp): stamp server_version into every _meta envelope"
```

---

### Task 2: Strict tool-surface drift guard

**Files:**
- Modify: `tests/test_tools_metadata.py:38-41` (replace `issubset` with equality vs the live registry)

**Interfaces:**
- Consumes: `tests._fixture_db.call_tool`, `fastmcp.Client`.

- [ ] **Step 1: Write the failing test** — replace `test_capabilities_lists_all_tools` with:

```python
async def test_capabilities_tools_equal_registered_tools(mcp):
    from fastmcp import Client

    out = await call_tool(mcp, "get_server_capabilities", {})
    async with Client(mcp) as client:
        registered = {t.name for t in await client.list_tools()}
    assert set(out["tools"]) == registered  # equality: no over/under-reporting
    assert registered == _EXPECTED_TOOLS
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_tools_metadata.py::test_capabilities_tools_equal_registered_tools -v`
Expected: PASS (code is currently in lockstep — this test now *locks* that invariant; if it fails, `_TOOLS` and the registry have drifted and must be reconciled).

- [ ] **Step 3: (no impl needed)** — this task is a guard. If the test fails, fix `clinvar_link/mcp/resources.py:_TOOLS` to match the registered set.

- [ ] **Step 4: Run the metadata suite**

Run: `uv run pytest tests/test_tools_metadata.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_tools_metadata.py
git commit -m "test(mcp): assert capabilities.tools equals the registered tool set"
```

---

### Task 3: Add the `clinvar://version` resource

**Files:**
- Modify: `clinvar_link/mcp/resources.py` (add `get_version_resource`, add to capabilities `resources` map at `:104-109`)
- Modify: `clinvar_link/mcp/tools/metadata.py` (register the resource; import the getter)
- Test: `tests/test_resources.py` (create if absent)

**Interfaces:**
- Produces: `resources.get_version_resource() -> dict` = `{server, server_version, mcp_protocol_version, clinvar_release_date}`; new MCP resource URI `clinvar://version`.

- [ ] **Step 1: Write the failing test** — create/append `tests/test_resources.py`:

```python
from clinvar_link.mcp.resources import get_version_resource


def test_version_resource_shape():
    v = get_version_resource()
    assert v["server"] == "clinvar-link"
    assert isinstance(v["server_version"], str) and v["server_version"]
    assert "mcp_protocol_version" in v
    assert "clinvar_release_date" in v  # may be None until the date cache primes
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `uv run pytest tests/test_resources.py -v`
Expected: FAIL (`ImportError: cannot import name 'get_version_resource'`).

- [ ] **Step 3: Implement** — in `resources.py` add:

```python
def get_version_resource() -> dict[str, Any]:
    return {
        "server": "clinvar-link",
        "server_version": server_version(),
        "mcp_protocol_version": MCP_PROTOCOL_VERSION,
        "clinvar_release_date": get_cached_clinvar_release_date(),
    }
```
and add to the capabilities `resources` dict (after the `clinvar://research-use` entry):
```python
            "clinvar://version": "server + protocol + data-release versions",
```

In `metadata.py`, import `get_version_resource` alongside the others and register:
```python
    @mcp.resource(
        "clinvar://version",
        annotations=_RESOURCE_ANNOTATIONS,
        mime_type="application/json",
    )
    def version_resource() -> dict[str, Any]:
        return get_version_resource()
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `uv run pytest tests/test_resources.py tests/test_tools_metadata.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add clinvar_link/mcp/resources.py clinvar_link/mcp/tools/metadata.py tests/test_resources.py
git commit -m "feat(mcp): add clinvar://version resource for staleness detection"
```

---

### Task 4: FTS search defaults to AND (repository)

**Files:**
- Modify: `clinvar_link/data/repository.py:171-179` (`_fts_query`), `:181-229` (`search` gains `match_mode`)
- Test: `tests/test_repository.py`

**Interfaces:**
- Produces: `ClinVarRepository._fts_query(text: str, operator: str = "AND") -> str`; `ClinVarRepository.search(..., match_mode: str = "and", ...)` where `match_mode ∈ {"and","or"}`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_repository.py`:

```python
def test_search_and_is_more_selective_than_or(repo):
    # "BRCA1" + "Cys61Gly" co-occur only in VariationID 100002.
    and_rows = repo.search("BRCA1 Cys61Gly", match_mode="and", limit=50)
    or_rows = repo.search("BRCA1 Cys61Gly", match_mode="or", limit=50)
    and_ids = {r["variation_id"] for r in and_rows}
    or_ids = {r["variation_id"] for r in or_rows}
    assert and_ids == {100002}
    assert and_ids < or_ids  # OR is a strict superset (all BRCA1 rows, etc.)
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `uv run pytest tests/test_repository.py::test_search_and_is_more_selective_than_or -v`
Expected: FAIL (`search() got an unexpected keyword argument 'match_mode'`).

- [ ] **Step 3: Implement** — replace `_fts_query`:

```python
    @staticmethod
    def _fts_query(text: str, operator: str = "AND") -> str:
        """Build a safe FTS5 MATCH string (tokens joined by AND/OR, last prefix-matched)."""
        tokens = _FTS_TOKEN_RE.findall(text or "")
        if not tokens:
            return '""'
        quoted = [f'"{tok}"' for tok in tokens[:-1]]
        quoted.append(f'"{tokens[-1]}"*')
        joiner = " OR " if operator.upper() == "OR" else " AND "
        return joiner.join(quoted)
```

In `search`, add the `match_mode` parameter (default `"and"`) and pass the operator:
```python
    def search(
        self,
        query: str,
        *,
        gene_symbol: str | None = None,
        classification: str | None = None,
        min_stars: int | None = None,
        assembly: str | None = None,
        match_mode: str = "and",
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
```
and inside, change `match = self._fts_query(query)` to:
```python
            match = self._fts_query(query, operator="OR" if match_mode == "or" else "AND")
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `uv run pytest tests/test_repository.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add clinvar_link/data/repository.py tests/test_repository.py
git commit -m "feat(repo): FTS search joins terms with AND (default) + OR mode"
```

---

### Task 5: Service-level AND→OR fallback + `has_more` via limit+1

**Files:**
- Modify: `clinvar_link/services/clinvar_service.py:257-304` (`search_variants`), `:385-401` (`_pagination`), `:327-381` (`get_variants_by_gene` call sites of `_pagination`)
- Modify: `clinvar_link/mcp/tools/variants.py:108-160` (`search_variants` tool gains `match_mode`)
- Test: `tests/test_service.py`

**Interfaces:**
- Consumes: `repo.search(..., match_mode=...)` (Task 4).
- Produces: `ClinVarService.search_variants(..., match_mode: str = "auto")`; response carries `match_mode ∈ {"and","or","or_fallback"}`. `_pagination(total: int | None, has_more: bool, limit: int, offset: int, *, capped: bool = False)`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_service.py`:

```python
async def test_search_auto_falls_back_to_or(service):
    # "BRCA1" AND "Lynch" co-occur in NO variant; OR finds both gene sets.
    out = await service.search_variants("BRCA1 Lynch")
    assert out["match_mode"] == "or_fallback"
    assert out["count"] > 0


async def test_search_auto_uses_and_when_it_matches(service):
    out = await service.search_variants("BRCA1 Cys61Gly")
    assert out["match_mode"] == "and"
    assert {r["variation_id"] for r in out["results"]} == {100002}


async def test_search_has_more_without_relying_on_count(service):
    out = await service.search_variants("BRCA1", limit=2, count_mode="none")
    assert out["total_count"] is None
    assert out["has_more"] is True
    assert out["next_offset"] == 2
```

- [ ] **Step 2: Run them, expect FAIL**

Run: `uv run pytest tests/test_service.py -k "search_auto or has_more" -v`
Expected: FAIL (`unexpected keyword 'count_mode'` / `KeyError: 'match_mode'`).

- [ ] **Step 3: Implement** — add module constants near line 41:

```python
_MATCH_MODES = frozenset({"auto", "and", "or"})
_COUNT_MODES = frozenset({"exact", "none"})
# Cap the search count scan; beyond this we report total_count_capped=True.
_SEARCH_COUNT_EXACT_MAX = 1000
```

Replace `_pagination` with a `has_more`-driven version:
```python
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
```

Rewrite `search_variants` (count wiring lands fully in Task 6; here add the mode/fallback + limit+1):
```python
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
            raise ToolInputError(f"match_mode must be one of {sorted(_MATCH_MODES)} (got {match_mode!r})")
        if count_mode not in _COUNT_MODES:
            raise ToolInputError(f"count_mode must be one of {sorted(_COUNT_MODES)} (got {count_mode!r})")
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
```

Add a placeholder `_count_for_search` (Task 6 fills the body); for now return exact via repo:
```python
    async def _count_for_search(self, query, *, gene_symbol, classification, min_stars,
                                assembly, match_mode, count_mode, has_more, returned, offset):
        if count_mode == "none":
            return None, False
        total = await asyncio.to_thread(
            self.repo.count_search, query, gene_symbol=gene_symbol,
            classification=classification, min_stars=min_stars, assembly=assembly,
        )
        return total, False
```

Update `get_variants_by_gene`'s two `_pagination(...)` calls to the new signature:
- empty-filter branch: `**self._pagination(0, False, limit, offset)`
- main branch: compute `has_more = (offset + len(results)) < total` then `**self._pagination(total, has_more, limit, offset)`

Wire the tool param in `variants.py` `search_variants` — add to the signature and the service call:
```python
        match_mode: str = "auto",
        count_mode: str = "exact",
```
and pass `match_mode=match_mode, count_mode=count_mode,` into `service_factory().search_variants(...)`.

- [ ] **Step 4: Run tests, expect PASS**

Run: `uv run pytest tests/test_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add clinvar_link/services/clinvar_service.py clinvar_link/mcp/tools/variants.py tests/test_service.py
git commit -m "feat(search): AND default with OR fallback + has_more via limit+1"
```

---

### Task 6: Tiered count (`count_mode` + `total_count_capped`)

**Files:**
- Modify: `clinvar_link/data/repository.py:253-298` (`count_search` returns `tuple[int, bool]`, caps the scan)
- Modify: `clinvar_link/services/clinvar_service.py` (`_count_for_search` body)
- Test: `tests/test_repository.py`, `tests/test_service.py`

**Interfaces:**
- Produces: `ClinVarRepository.count_search(..., match_mode: str = "and", count_exact_max: int | None = None) -> tuple[int, bool]` (count, capped).

- [ ] **Step 1: Write the failing tests**

`tests/test_repository.py`:
```python
def test_count_search_caps_at_exact_max(repo):
    full, capped_full = repo.count_search("BRCA1")
    assert full == 5 and capped_full is False
    n, capped = repo.count_search("BRCA1", count_exact_max=2)
    assert n == 2 and capped is True
```
`tests/test_service.py`:
```python
async def test_search_reports_capped_total(service):
    out = await service.search_variants("BRCA1", count_mode="exact", limit=2)
    assert out["total_count"] in (5,)  # fixture is small; not capped here
    assert "total_count_capped" not in out  # only present when capped
```

- [ ] **Step 2: Run them, expect FAIL**

Run: `uv run pytest tests/test_repository.py::test_count_search_caps_at_exact_max tests/test_service.py::test_search_reports_capped_total -v`
Expected: FAIL (`count_search() got an unexpected keyword 'count_exact_max'` / tuple-unpack error).

- [ ] **Step 3: Implement** — replace `count_search` with a capped, tuple-returning version:

```python
    def count_search(
        self,
        query: str,
        *,
        gene_symbol: str | None = None,
        classification: str | None = None,
        min_stars: int | None = None,
        assembly: str | None = None,
        match_mode: str = "and",
        count_exact_max: int | None = None,
    ) -> tuple[int, bool]:
        """Return (match_count, capped). When ``count_exact_max`` is set, the scan
        stops after that many rows and ``capped`` is True if more exist."""
        filter_sql, filter_params = self._search_filters(
            gene_symbol=gene_symbol, classification=classification,
            min_stars=min_stars, assembly=assembly,
        )

        def _capped(n: int) -> tuple[int, bool]:
            if count_exact_max is not None and n > count_exact_max:
                return count_exact_max, True
            return n, False

        tokens = _FTS_TOKEN_RE.findall(query or "")
        if tokens:
            match = self._fts_query(query, operator="OR" if match_mode == "or" else "AND")
            base = (
                "SELECT 1 FROM variant_fts f "  # noqa: S608
                "JOIN variant v ON v.variation_id = f.rowid "
                "WHERE variant_fts MATCH ?" f"{filter_sql}"
            )
            params: list[Any] = [match, *filter_params]
            sql, params = self._wrap_count(base, params, count_exact_max)
            try:
                row = self._conn.execute(sql, tuple(params)).fetchone()
                return _capped(int(row["n"]) if row is not None else 0)
            except sqlite3.Error:
                pass
        cleaned = (query or "").replace("%", "").replace("_", "").strip().upper()
        pattern = f"%{cleaned}%"
        base = (
            "SELECT 1 FROM variant v "  # noqa: S608
            "WHERE (UPPER(v.name) LIKE ? OR UPPER(v.gene_symbol) LIKE ?)" f"{filter_sql}"
        )
        params = [pattern, pattern, *filter_params]
        sql, params = self._wrap_count(base, params, count_exact_max)
        row = self._conn.execute(sql, tuple(params)).fetchone()
        return _capped(int(row["n"]) if row is not None else 0)

    @staticmethod
    def _wrap_count(base: str, params: list[Any], count_exact_max: int | None) -> tuple[str, list[Any]]:
        """Wrap a row-yielding query in COUNT(*), bounding the scan when capping."""
        if count_exact_max is not None:
            return f"SELECT COUNT(*) AS n FROM ({base} LIMIT ?)", [*params, count_exact_max + 1]  # noqa: S608
        return f"SELECT COUNT(*) AS n FROM ({base})", params  # noqa: S608
```

Fill `_count_for_search` in the service:
```python
    async def _count_for_search(self, query, *, gene_symbol, classification, min_stars,
                                assembly, match_mode, count_mode, has_more, returned, offset):
        if count_mode == "none":
            return None, False
        return await asyncio.to_thread(
            self.repo.count_search, query, gene_symbol=gene_symbol,
            classification=classification, min_stars=min_stars, assembly=assembly,
            match_mode=match_mode, count_exact_max=_SEARCH_COUNT_EXACT_MAX,
        )
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `uv run pytest tests/test_repository.py tests/test_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add clinvar_link/data/repository.py clinvar_link/services/clinvar_service.py tests/test_repository.py tests/test_service.py
git commit -m "feat(search): tiered count_mode with total_count_capped guard"
```

---

### Task 7: Per-tool error recovery prose + blank-input hygiene

**Files:**
- Modify: `clinvar_link/mcp/errors.py:105-123` (`_fallback_for`), `:167-186` (`_recovery_text`)
- Test: `tests/test_errors.py` (create if absent) or `tests/test_tools_*`

**Interfaces:**
- Produces: `_recovery_text(error_code, fallback_tool, tool_name)` branches on tool family; `_fallback_for` treats whitespace-only context values as absent.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_tools_genes.py` and `tests/test_tools_variants.py` (use existing helpers `build_service`/`call_tool`):

```python
# tests/test_tools_genes.py
async def test_gene_not_found_recovery_is_gene_specific(mcp):
    out = await call_tool(mcp, "get_gene_clinvar_summary", {"gene_symbol": "NOSUCHGENE"})
    assert out["success"] is False and out["error_code"] == "not_found"
    assert "VCV" not in out["recovery"] and "rsID" not in out["recovery"]
    assert "gene symbol" in out["recovery"].lower()
```
```python
# tests/test_tools_variants.py
async def test_blank_identifier_does_not_echo_blank(mcp):
    out = await call_tool(mcp, "get_variant", {"identifier": "   "})
    assert out["success"] is False and out["error_code"] == "invalid_input"
    assert out["fallback_args"] in (None, {})  # no blank query echoed
    for cmd in out["_meta"]["next_commands"]:
        assert cmd["arguments"].get("query", "").strip() != "" or "query" not in cmd["arguments"]
```
(If a `mcp` fixture is not already in those modules, copy the one from `tests/test_tools_metadata.py:24-35`.)

- [ ] **Step 2: Run them, expect FAIL**

Run: `uv run pytest tests/test_tools_genes.py::test_gene_not_found_recovery_is_gene_specific tests/test_tools_variants.py::test_blank_identifier_does_not_echo_blank -v`
Expected: FAIL.

- [ ] **Step 3: Implement** — add a tool-family set and branch `_recovery_text`:

```python
_GENE_TOOLS = frozenset({"get_gene_clinvar_summary", "get_variants_by_gene"})


def _recovery_text(error_code: str, fallback_tool: str | None, tool_name: str | None = None) -> str:
    is_gene = tool_name in _GENE_TOOLS
    if error_code == "not_found":
        if is_gene:
            return (
                "No ClinVar record for that gene in the local index. Confirm the HGNC "
                "gene symbol (e.g. COL4A5); or call search_variants to discover variants."
            )
        resolver = fallback_tool or "search_variants"
        return (
            "Identifier well-formed but absent in the local ClinVar index. This is a "
            "reformulate, not a retry: confirm the VCV / rsID / HGVS / AlleleID "
            "(e.g. VCV000024455 | rs104886142 | NM_033380.3(COL4A5):c.1871G>A), or call "
            f"{resolver} to locate the matching record, then retry."
        )
    if error_code == "invalid_input":
        if is_gene:
            return (
                "The request was rejected as malformed. Pass a single HGNC gene symbol "
                "(e.g. COL4A5) and a valid sort/filter; do not retry unchanged."
            )
        resolver = fallback_tool or "get_server_capabilities"
        return (
            "The request was rejected as malformed (the identifier or query shape is "
            "wrong for this tool). Do not retry unchanged. Provide a valid id "
            "(e.g. VCV000024455 | rs104886142 | NM_033380.3(COL4A5):c.1871G>A) or call "
            f"{resolver}."
        )
    return (
        f"Unexpected failure. Call {fallback_tool} for a safe entry point."
        if fallback_tool
        else "Unexpected failure."
    )
```

Harden `_fallback_for` against whitespace-only echoes:
```python
def _fallback_for(context: McpErrorContext) -> tuple[str, dict[str, Any] | None]:
    query = (context.query or "").strip() or None
    variant_id = (context.variant_id or "").strip() or None
    if context.tool_name == "get_variant":
        if query:
            return "search_variants", {"query": query}
        if variant_id:
            return "search_variants", {"query": variant_id}
        return "search_variants", None
    if context.gene_symbol and context.gene_symbol.strip():
        return "get_gene_clinvar_summary", {"gene_symbol": context.gene_symbol.strip()}
    if query:
        return "search_variants", {"query": query}
    return "get_server_capabilities", None
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `uv run pytest tests/test_tools_genes.py tests/test_tools_variants.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add clinvar_link/mcp/errors.py tests/test_tools_genes.py tests/test_tools_variants.py
git commit -m "fix(errors): tool-family recovery prose + no blank-input echo"
```

---

### Task 8: Forced `id_type` shape validation

**Files:**
- Modify: `clinvar_link/services/clinvar_service.py:144-157` (`_resolve` — validate shape when `id_type != auto`)
- Test: `tests/test_service.py`

**Interfaces:**
- Produces: `_resolve` raises `ToolInputError` (→ `invalid_input`) when an explicit `id_type` does not match the identifier shape.

- [ ] **Step 1: Write the failing test**:

```python
import pytest
from clinvar_link.exceptions import ToolInputError

async def test_forced_id_type_mismatch_is_invalid_input(service):
    with pytest.raises(ToolInputError):
        await service.get_variant("VCV000100001", id_type="variation_id")
    with pytest.raises(ToolInputError):
        await service.get_variant("rs28897672", id_type="hgvs")
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `uv run pytest tests/test_service.py -k forced_id_type -v`
Expected: FAIL (raises `DataNotFoundError`, not `ToolInputError`).

- [ ] **Step 3: Implement** — add a shape guard and call it in `_resolve`:

```python
    @staticmethod
    def _validate_shape(text: str, id_type: str) -> None:
        """Reject a value whose shape cannot match an explicitly forced id_type."""
        if id_type == "vcv" and not _VCV_RE.match(text):
            raise ToolInputError(f"id_type='vcv' requires a VCV accession (got {text!r})")
        if id_type == "rsid" and not (_RSID_RE.match(text) or _DIGITS_RE.match(text)):
            raise ToolInputError(f"id_type='rsid' requires an rsID (got {text!r})")
        if id_type in {"variation_id", "allele_id"} and not _DIGITS_RE.match(text):
            raise ToolInputError(f"id_type={id_type!r} requires a numeric id (got {text!r})")
        if id_type == "hgvs" and ":" not in text and not any(h in text for h in _HGVS_HINTS):
            raise ToolInputError(f"id_type='hgvs' requires an HGVS expression (got {text!r})")
```
and in `_resolve`, right after `_ensure_id_type(id_type)`:
```python
        if id_type != "auto":
            self._validate_shape(text, id_type)
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `uv run pytest tests/test_service.py -v`
Expected: PASS. (Note: the batch `get_variants` already swallows `ToolInputError` per-row as a miss, so batch behavior is unchanged.)

- [ ] **Step 5: Commit**

```bash
git add clinvar_link/services/clinvar_service.py tests/test_service.py
git commit -m "fix(service): forced id_type shape mismatch -> invalid_input"
```

---

### Task 9: Gene summary `other_count` reconciliation (read-time)

**Files:**
- Modify: `clinvar_link/models/gene_models.py:19-21` (add `other_count` field)
- Modify: `clinvar_link/services/clinvar_service.py:308-325` (`get_gene_clinvar_summary` derives it)
- Test: `tests/test_service.py`

**Interfaces:**
- Produces: `GeneClinVarSummary.other_count: int`; gene summary payload satisfies `Σ(significance buckets) + other_count == total_count`.

- [ ] **Step 1: Write the failing test**:

```python
async def test_gene_summary_buckets_reconcile_to_total(service):
    out = await service.get_gene_clinvar_summary("BRCA1")
    buckets = (
        out["pathogenic_count"] + out["likely_pathogenic_count"] + out["vus_count"]
        + out["likely_benign_count"] + out["benign_count"] + out["conflicting_count"]
        + out["not_provided_count"] + out["other_count"]
    )
    assert buckets == out["total_count"]
    assert out["other_count"] >= 0
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `uv run pytest tests/test_service.py -k reconcile -v`
Expected: FAIL (`KeyError: 'other_count'`).

- [ ] **Step 3: Implement** — add the model field after `not_provided_count`:

```python
    other_count: int = Field(
        0, description="Variants whose classification falls outside the named buckets."
    )
```
and derive it in `get_gene_clinvar_summary` before returning:
```python
        payload = model.model_dump()
        known = (
            payload["pathogenic_count"] + payload["likely_pathogenic_count"]
            + payload["vus_count"] + payload["likely_benign_count"]
            + payload["benign_count"] + payload["conflicting_count"]
            + payload["not_provided_count"]
        )
        payload["other_count"] = max(0, payload["total_count"] - known)
        if response_mode == "minimal":
            for key in ("consequence_categories", "top_traits", "star_distribution"):
                payload.pop(key, None)
        return payload
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `uv run pytest tests/test_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add clinvar_link/models/gene_models.py clinvar_link/services/clinvar_service.py tests/test_service.py
git commit -m "feat(gene): read-time other_count so buckets reconcile to total"
```

---

### Task 10: Remove the duplicate `clinvar_release` (breaking, alpha)

**Files:**
- Modify: `clinvar_link/mcp/errors.py:84-96` (`_provenance_meta` — drop `clinvar_release`)
- Modify: `clinvar_link/mcp/resources.py:55, 162` (capabilities + license — drop `clinvar_release`)
- Modify: `clinvar_link/mcp/resources.py:117-146` (usage text) and `clinvar_link/mcp/facade.py:32-34` (instructions) — reference only `clinvar_release_date`
- Modify: `tests/test_tools_metadata.py:51-79` (update assertions)
- Modify: `pyproject.toml` (`version = "0.2.0"`)

**Interfaces:**
- Produces: a single canonical `clinvar_release_date` everywhere; `clinvar_release` removed.

- [ ] **Step 1: Update the tests first (they encode the new contract)** — in `tests/test_tools_metadata.py`:
  - `test_capabilities_clinvar_release_is_populated_not_unknown` → assert on `out["clinvar_release_date"]`:
    ```python
    async def test_capabilities_release_date_is_populated(mcp):
        out = await call_tool(mcp, "get_server_capabilities", {})
        date = get_cached_clinvar_release_date()
        assert date is not None
        assert out["clinvar_release_date"] == date
        assert "clinvar_release" not in out
    ```
  - `test_success_envelope_meta_carries_release_and_request_id` → replace `meta["clinvar_release"]` assertion with `assert "clinvar_release" not in meta` and keep `meta["clinvar_release_date"] == date`.
  - `test_cold_get_variant_carries_release_without_capabilities` → assert `out["_meta"]["clinvar_release_date"] != "unknown"` and `"clinvar_release" not in out["_meta"]` (drop the equality-of-two-fields assertion).

- [ ] **Step 2: Run them, expect FAIL**

Run: `uv run pytest tests/test_tools_metadata.py -v`
Expected: FAIL (`clinvar_release` still present).

- [ ] **Step 3: Implement**
  - `_provenance_meta`: drop the `clinvar_release` key; set `clinvar_release_date` only when known:
    ```python
        clinvar_date = get_cached_clinvar_release_date()
        meta: dict[str, Any] = {
            "unsafe_for_clinical_use": True,
            "server_version": server_version(),
        }
        if clinvar_date is not None:
            meta["clinvar_release_date"] = clinvar_date
            fresh = clinvar_freshness(clinvar_date, settings.REFRESH_TTL_DAYS)
            if fresh is not None:
                meta.update(fresh)
        if context is not None and context.request_id:
            meta["request_id"] = context.request_id
        return meta
    ```
  - `resources.py` capabilities: delete the `"clinvar_release": date or CLINVAR_DATA_RELEASE,` line (keep `clinvar_release_date`). In `get_license_resource` delete the `"clinvar_release": ...` line.
  - `resources.py` usage text (`get_usage_resource`): change `_meta.clinvar_release / _meta.clinvar_release_date` → `_meta.clinvar_release_date`.
  - `facade.py` instructions: change `_meta (clinvar_release / clinvar_release_date)` → `_meta.clinvar_release_date`.
  - `pyproject.toml`: bump `version = "0.2.0"`.

- [ ] **Step 4: Run the full suite, expect PASS**

Run: `uv run pytest -q`
Expected: PASS. (Grep guard: `rg -n "clinvar_release\"" clinvar_link` should return nothing except `clinvar_release_date`.)

- [ ] **Step 5: Commit**

```bash
git add clinvar_link/mcp/errors.py clinvar_link/mcp/resources.py clinvar_link/mcp/facade.py tests/test_tools_metadata.py pyproject.toml
git commit -m "refactor(meta)!: drop duplicate clinvar_release; bump 0.2.0 (alpha breaking)"
```

---

### Task 11: Trim null/`"na"` noise in `full` mode

**Files:**
- Modify: `clinvar_link/services/clinvar_service.py:436-463` (`_project`, `full` branch)
- Test: `tests/test_service.py`

**Interfaces:**
- Produces: `full`-mode payload omits null `omim_id/medgen_id/mondo_id` in traits and `"na"` `reference_allele/alternate_allele` in coordinates.

- [ ] **Step 1: Write the failing test**:

```python
async def test_full_mode_trims_null_and_na(service):
    out = await service.get_variant("VCV000100001", response_mode="full")
    for trait in out["traits"]:
        assert all(v is not None for v in trait.values())  # no null id keys
    for coord in out["coordinates"]:
        assert coord.get("reference_allele") != "na"
        assert coord.get("alternate_allele") != "na"
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `uv run pytest tests/test_service.py -k trims_null -v`
Expected: FAIL (null trait ids / `"na"` alleles present).

- [ ] **Step 3: Implement** — replace the `full` branch of `_project`:

```python
        if mode == "full":
            return self._trim_full(payload)
```
and add the helper:
```python
    @staticmethod
    def _trim_full(payload: dict[str, Any]) -> dict[str, Any]:
        """Drop information-free keys (null trait ids, 'na' alleles) from full payloads."""
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
```

- [ ] **Step 4: Run tests, expect PASS**

Run: `uv run pytest tests/test_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add clinvar_link/services/clinvar_service.py tests/test_service.py
git commit -m "perf(tokens): trim null trait ids and 'na' alleles in full mode"
```

---

### Task 12: Docs + capabilities lockstep + memory note

**Files:**
- Modify: `clinvar_link/mcp/resources.py` (capabilities: document new params/fields), `get_usage_resource` (mention `match_mode`/`count_mode`)
- Modify: `CLAUDE.md`, `AGENTS.md` (note AND-default search, `match_mode`/`count_mode`, `other_count`, `server_version`, `total_count_capped`)
- Modify: memory `mcp-quality-rubric.md`
- Test: `tests/test_tools_metadata.py` (assert the new capabilities keys exist)

**Interfaces:** none (docs).

- [ ] **Step 1: Write the failing test** — append to `tests/test_tools_metadata.py`:

```python
async def test_capabilities_advertise_search_controls(mcp):
    out = await call_tool(mcp, "get_server_capabilities", {})
    assert "search_controls" in out
    assert set(out["search_controls"]["match_mode"]) >= {"auto", "and", "or"}
    assert set(out["search_controls"]["count_mode"]) >= {"exact", "none"}
```

- [ ] **Step 2: Run it, expect FAIL**

Run: `uv run pytest tests/test_tools_metadata.py -k search_controls -v`
Expected: FAIL (`KeyError: 'search_controls'`).

- [ ] **Step 3: Implement** — in `get_capabilities_resource`, add after `sort_options`:
```python
        "search_controls": {
            "match_mode": ["auto", "and", "or"],
            "count_mode": ["exact", "none"],
            "default_match_mode": "auto",
            "note": "auto = AND with automatic OR fallback when AND returns nothing.",
        },
```
Update the `output_cheatsheet` to add `"other_count_field": "other_count"` and `"capped_total_flag": "total_count_capped"`. Update `get_usage_resource` search section to mention `match_mode` / `count_mode`. Update `CLAUDE.md` (Conventions → search/pagination) and `AGENTS.md` accordingly. Append a dated note to the memory `mcp-quality-rubric.md` recording the v2 Track A landing.

- [ ] **Step 4: Run the full gate**

Run: `make ci-local`
Expected: PASS (ruff, format, mypy, pytest + coverage ≥ 70%).

- [ ] **Step 5: Commit**

```bash
git add clinvar_link/mcp/resources.py CLAUDE.md AGENTS.md tests/test_tools_metadata.py
git commit -m "docs(capabilities): advertise search_controls + reconcile field docs"
```

---

## Self-Review

- **Spec coverage:** A1→Tasks 1–3; A2→Tasks 4–6; A3→Tasks 7–8; A4→Tasks 9–11; cross-cutting docs/version→Tasks 10,12. SC1→Task 2; SC2→Tasks 1,3; SC3/SC4→Tasks 4–5; SC5→Task 6; SC6→Tasks 7–8; SC7→Task 9; SC9→every task's `make ci-local`. SC8 is Track B.
- **Placeholder scan:** none — every step has concrete code/commands.
- **Type consistency:** `count_search -> tuple[int, bool]` (Task 6) consumed via `total, capped = ...` (Tasks 5–6); `_pagination(total: int | None, has_more, limit, offset, *, capped=False)` defined Task 5 and used by both search and gene listing; `match_mode` strings `{"auto","and","or"}` (service) / `{"and","or"}` (repo) consistent; `_SEARCH_COUNT_EXACT_MAX` defined Task 5, used Task 6.
- **Note:** `estimated` count mode from the spec is intentionally dropped — the self-capping `exact` mode supersedes it; the spec's count-mode enum is narrowed to `{exact, none}` (recorded in Task 12 docs).
