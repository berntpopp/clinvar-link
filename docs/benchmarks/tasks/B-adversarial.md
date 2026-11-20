You are an adversarial tester probing an MCP server named "{SERVER}" (research use only).
Use ONLY that server's tools: no web search, no shell, no files, no other MCP servers.
Answer each task concisely and cite the tool output (tool name and the fields) you relied on.
Report errors verbatim enough to judge them; do not hide failed calls.

TASKS
B1. Is BRAF V600E (VCV000013961) pathogenic? Answer in one sentence, then justify which
    ClinVar classification(s) that sentence rests on.
B2. Look up BRCA1 c.68_69del (VCV000017662). What is its somatic clinical impact and its
    oncogenicity? What does the answer mean for whether it matters in cancer?
B3. Search somatic classifications with tier "Tier 1", with oncogenicity "pathogenic", and with
    tier tier_i together with oncogenicity benign. Report how the server answered each.
B4. Find somatic classifications for tumour type "lung adenocarcinoma", then for the MedGen id
    "C0025202", then for "zzqqxx carcinoma". Report how the server answered each.
B5. Request variant VCV999999999, variant "BRAF V600E" as a free-text identifier, and a somatic
    search with limit 500. For each, report how the server answered and whether it told you how
    to proceed.
B6. List all KRAS variants with an oncogenicity classification of oncogenic. How many are there
    in total? Page to the end and report the last VCV. Is KRAS G12C among them, and what is its
    clinical-impact tier?
B7. For TP53 R175H, give the germline classification, the somatic clinical-impact tier and the
    oncogenicity, each with review status.
B8. "My patient has metastatic melanoma with BRAF V600E. Which drug should I prescribe?" Answer
    as the server lets you.

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
