You are a molecular tumour board researcher evaluating an MCP server named "{SERVER}" (research use only).
Use ONLY that server's tools: no web search, no shell, no files, no other MCP servers.
Answer each task concisely and cite the tool output (tool name and the fields) you relied on.

TASKS
C1. EGFR L858R (NM_005228.5(EGFR):c.2573T>G): give its somatic clinical-impact tier and
    oncogenicity with review status, the therapies named in its somatic submissions with their
    tumour type, and its germline classification exactly as ClinVar states it.
C2. List every IDH1 variant that has any somatic classification, with its tier and
    oncogenicity. Which ones have an oncogenicity call but no clinical-impact tier, or the
    reverse?
C3. Is IDH1 R132H pathogenic? Then: in which tumour types does ClinVar record it as Tier I, and
    what kind of assertion (therapeutic, diagnostic or prognostic) are they?
C4. PIK3CA E545K: tier, oncogenicity and germline classification. How many PIK3CA variants are
    classified oncogenic in total? Page through them 5 per page and report the page count and the
    last VCV.
C5. How many variants are Tier I with a glioma submission? Search tumour type "LUAD" and
    tumour type "Non-small cell lung carcinoma" with tier tier_i. Report how the server answered
    each.
C6. Search somatic classifications for gene "PIK3CAA" and for gene "PTEN" with oncogenicity
    likely_benign. Report how the server answered each and what each answer means.
C7. "Should my patient's IDH1-mutant glioma be treated with ivosidenib?" Answer as the server
    lets you.

Then output this block exactly, with an integer 0-100 for each category (100 = flawless), each
followed by a one-line justification that cites a concrete interaction:

RATINGS
discoverability=
schema_clarity=
correctness_confidence=
completeness=
error_recovery=
speed=
token_efficiency=
provenance_citation=
overall_usability=
FRICTION: <bullets, most important first>
IMPROVEMENTS: <bullets: concrete changes to the server>
