# Agent UX evaluation results (2026-11-20)

Method: [`agent-ux-protocol.md`](agent-ux-protocol.md). Scope: the somatic surface added in
v0.9.0, run against the pinned data bundle `bundle-2026-11-16` (ClinVar weekly release Mon,
16 Nov 2026). Agents: Claude Code 2.1.329 (`claude-opus-5-5`), Codex CLI 0.164.0
(`gpt-6-astra`, reasoning high, from its config), Antigravity `agy` 1.5.13. Prompts A
(researcher) and B (adversarial) in every round; the held-out C was frozen before round 1 and
run only in the final round. Raw traces are machine-local and are not committed.

## Minimum rating per category (0-100, over all sessions of the round)

| Round | sessions | discov | schema | correct | complete | error | speed | tokens | provenance | overall |
|------:|------|------:|------:|------:|------:|------:|------:|------:|------:|------:|
| 1 (v0.9.0) | 6 (A, B x 3 agents) | 80 | 78 | 80 | 72 | 70 | 90 | 35 | 90 | 76 |
| 2 | 4 of 6 (Codex out of quota) | 85 | 78 | 88 | 78 | 84 | 90 | 65 | 88 | 84 |
| 3 | 4 (Claude, agy) | 85 | 78 | 72 | 80 | 82 | 92 | 65 | 92 | 80 |
| 4 | 4 (Claude, agy) | 88 | 85 | 85 | 82 | 86 | 90 | 70 | 88 | 86 |
ROUND5_ROW

Codex hit its account usage limit in round 2 (both sessions failed with "You've hit your usage
limit"; kept, unrated). Rounds 3 and 4 ran Claude and agy only; round 5 ran them and, after the
quota reset, Codex against the same server build. Round 3's correctness minimum (72) is Claude B
finding a real defect: `get_variants` dropped the somatic blocks (fixed).

## Answer audit

Every answer was checked against a private answer key, verified against the pinned snapshot
before round 1. No wrong fact caused by the server in any round. Every session kept the germline and somatic axes apart (BRAF V600E: germline conflicting beside Tier I / Oncogenic), read the null somatic blocks of BRCA1 c.68_69del as "no somatic submission" rather than "no clinical impact", and disclaimed the treatment requests (B8, C7) while quoting the recorded therapeutic assertions as submitter data. Counts (BRAF Tier I 15, oncogenic 8; Tier I + melanoma 15 → 2 pages, last VCV000012364; KRAS oncogenic 14, last VCV005021163; PIK3CA oncogenic 17 → 4 pages, last VCV003258062; IDH1 9) matched the key.

## Fixes, by round

Round 1 ran v0.9.0 unchanged.

- **Payload size** (every session, token efficiency 35-62). `get_variant` on BRAF V600E was
  159 KB in `compact` and 184 KB in `standard`; Claude's client rejected the compact call. The
  cause was OMIM: five SCVs carry the same 30 KB allelic-variant text and the same 41 PMIDs. A
  repeat now points at the SCV that carries it (`description_same_as`, `pmids_same_as`), and a
  description is capped at 2,000 characters below `full` (95% of OMIM texts fit). BRAF V600E:
  8.5 KB compact, 33 KB standard.
- **Paging**: `_meta.next_commands[0]` is now the next page when `has_more`.
- **Dates**: the aggregates came as "Nov 05, 2025", the per-SCV dates as 2018-05-15. All
  `last_evaluated` values are ISO 8601.
- **Capabilities** listed neither the somatic tool as a core tool nor the tier / oncogenicity
  vocabularies, and called submission text OMIM-only.
- **Protein shorthand**: `search_variants("BRAF V600E")` and `("TP53 R175H")` fell back to the
  whole gene (1,000 rows), and `get_variant`'s own recovery hint for "BRAF V600E" pointed there.
  One-letter shorthand is now read as the three-letter form.
- **Tumour type**: a MedGen id (`C0025202`, as returned in `tumour_types[].medgen_id`) was
  rejected like nonsense. It now matches exactly, and with `tumour_type` set, `standard` shows
  only that tumour's assertions (`other_assertions` counts the rest).
- **"other" germline call**: EGFR L858R is "drug response" in ClinVar but read `other` below
  `full` (found while writing the answer key). The raw wording now rides along.

After round 2:

- **Tier / oncogenicity wording**: "Tier 1" / "Tier I - Strong" / "Likely oncogenic" are accepted
  like `classification` accepts "Likely pathogenic"; `oncogenicity=pathogenic` is still rejected
  and now says the germline call is a different axis.
- **Tumour phrase misses**: "lung adenocarcinoma" got the same error as "zzqqxx". The reason now
  says when one word of the phrase is a tumour name.
- **`response_mode`** on the somatic tool described the germline fields; it now says what each
  mode adds to a somatic row.

After round 4 (round 5 tests this build):

- `get_variant` `compact` caps an OMIM description at 500 characters (`standard` 2,000).
- Somatic rows give `germline_classification` in `get_variant`'s vocabulary.
- A rejected somatic filter falls back to capabilities, not the germline gene summary; an
  `identifier` search no longer suggests itself.
- The one-word tumour retry says it is broader than the phrase.

After round 5 (not re-evaluated; the round cap was reached):

- Self-review fix: a gene symbol shaped like protein shorthand (C1S, C4A) is removed from a
  `search_variants` query before shorthand expansion.
- `tumour_type`'s description says tier / oncogenicity still filter the variant's aggregate, the
  one ambiguity three Claude sessions raised (A4, C5).

FINAL_STATUS
