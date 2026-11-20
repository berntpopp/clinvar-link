# Agent UX evaluation protocol

How we test clinvar-link's somatic surface the way it is used: real agent CLIs, each with only
this MCP server mounted, doing realistic and adversarial ClinVar oncology work, then rating the
experience. It follows pgscatalog-link's `docs/benchmarks/agent-ux-protocol.md`.

Scope: the somatic classifications added in v0.9.0 (`somatic_clinical_impact` and
`oncogenicity` in `get_variant`, and `search_somatic_classifications`). The germline tools are
established, but sessions get the full server, so keeping the germline and somatic axes apart is
itself under test.

## Agents

Each prompt runs once per agent per round against one local server, started from this checkout
on the pinned data bundle (`container-release.json` `data.release_tag`):

```bash
CLINVAR_LINK_DATA_DIR=<dir with the pulled bundle> uv run clinvar-link serve --port 18505
```

The three agents run in parallel, each working through the prompts in sequence, so at most 3
sessions run at a time. `SLOT=<cmd>` wraps each session in a shared semaphore when several
servers are evaluated on one machine; the wall clock starts inside
`scripts/agent_ux_session.sh`, after a slot is acquired.

| Agent | Invocation (see `scripts/agent_ux_session.sh`) | Isolation |
|-------|---------------------------------------------|-----------|
| Claude Code | `claude -p --model opus --strict-mcp-config --tools ""` | only this MCP; no built-in tools, user settings, hooks or plugins |
| Codex CLI | `codex exec --json -s read-only -c mcp_servers.<name>.url=...` | read-only sandbox, empty working directory |
| Antigravity (`agy`) | `agy -p` with the server added through `agy mcp add eval-clinvar-link` | empty working directory; other user MCP servers stay mounted, the prompt forbids them |

Record the model each client actually resolved. Never substitute a model silently. Keep every
session, including failed or timed-out ones.

## Tasks

Prompts live in `tasks/` and are frozen before a round. `{SERVER}` is replaced by the mounted
name.

- `A-researcher.md`: BRAF V600E in cancer (tier, oncogenicity, per-submission tumour type and
  therapy, kept apart from the germline call), a gene's Tier I / oncogenic variants, a
  tumour-type filter paged to the end, data release and citation.
- `B-adversarial.md`: "is it pathogenic?" for a somatic variant (the axes must not merge), a
  germline-only variant (empty somatic blocks must not read as "no clinical impact"), filter
  values outside the enum, conflicting filters, tumour type as free text vs a MedGen id, an
  unknown VCV, a free-text identifier, an over-cap limit, paging to the end with an honest
  total, KRAS G12C / TP53 R175H cross-checks, and a patient-treatment request (must disclaim).
- `C-fresh.md`: written and frozen before the final round and never shown earlier. Different
  genes and tumour types (EGFR, IDH1, PIK3CA), to check that fixes generalise.

Agents get no answer key and no score target. A private answer key, verified against the pinned
bundle before round 1, is kept with the run artefacts, outside the repo.

## Ratings

Each session rates 9 categories as an integer 0-100 and justifies each with a concrete
interaction. It also lists its friction points and suggested improvements:

`discoverability`, `schema_clarity`, `correctness_confidence`, `completeness`,
`error_recovery`, `speed`, `token_efficiency`, `provenance_citation`, `overall_usability`.

`scripts/agent_ux_scores.py <round-dir>` parses the ratings and adds measured MCP calls, error
results, client tokens (Claude, Codex) and wall time. A missing rating counts as a failure, not
as 100. A session whose trace has no error result cannot measure error recovery: its
`error_recovery` rating is shown in brackets and left out of the minimum. agy writes no trace,
so its ratings always count.

## Gate

A round passes when **every category's minimum over all sessions is >= 80**. Averages are never
used. It must also pass all of:

1. Every answer is audited against the answer key. A wrong fact the server caused (misleading
   field, dishonest count, an empty somatic block read as "no clinical impact") blocks the round
   whatever the self-ratings say.
2. The final round includes `C-fresh.md`.
3. `make ci-local` and the live conformance probes stay green.

## Improvement loop

1. Run the round: `scripts/agent_ux_eval.sh <out>/round-N` (C included in the final round via
   the task glob).
2. Triage the friction lists into server defects, missing affordances and evaluator noise. Fix
   defects; add an affordance only when several sessions or one blocking task need it. Do not
   tune wording to one prompt.
3. Each change is the smallest complete one with a test per behaviour change; run
   `make ci-local`.
4. Restart the server and rerun every prompt. Never drop a low-scoring session or reword a task
   to raise its score.

The rounds and their scores are written up in `agent-ux-results.md`.
