You are a cancer genomics researcher evaluating an MCP server named "{SERVER}" (research use only).
Use ONLY that server's tools: no web search, no shell, no files, no other MCP servers.
Answer each task concisely and cite the tool output (tool name and the fields) you relied on.

TASKS
A1. What does ClinVar say about BRAF V600E (VCV000013961) in cancer? Report its somatic
    clinical-impact tier and its oncogenicity classification, each with review status and
    last-evaluated date, and its germline classification. Are these one call or separate calls?
A2. For the somatic submissions on that variant, list each submitter's tumour type and any
    therapy named, and say whether that text is ClinVar's curated data or submitter-provided.
A3. How many BRAF variants does ClinVar classify as Tier I, and how many as oncogenic? Give the
    VCV accessions of the Tier I ones.
A4. Across all genes, how many variants carry a Tier I clinical-impact classification with a
    melanoma submission? Page through all of them 10 per page: how many pages, and what is the
    last VCV on the last page?
A5. Which ClinVar data release did your answers come from, and how should the source be cited
    for VCV000013961?

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
