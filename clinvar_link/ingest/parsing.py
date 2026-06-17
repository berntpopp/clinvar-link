"""Pure utility functions for parsing ClinVar variant_summary.txt data.

Ported from kidney-genetics-db ``clinvar_utils.py`` with the parsing logic kept
identical.  The one deliberate divergence is the review-status mapping: instead
of the kidney-confidence scale we use the OFFICIAL ClinVar gold-star convention
(see ``clinvar_link/data/review_status_stars.yaml``).

All parsing functions are stateless with no DB/session dependencies, making them
easy to test in isolation.  The only non-stdlib dependency is PyYAML, used by the
star-map loader.
"""

import re
from importlib.resources import files
from typing import Any

import yaml

# Pre-compiled regexes
_PROTEIN_CHANGE_RE = re.compile(r"\((p\..*?)\)")
_PROTEIN_POSITION_RE = re.compile(r"[A-Za-z]{3}(\d+)")
_SPLICE_CANONICAL_RE = re.compile(r"c\.\d+[+-][12][^\d]|c\.\d+[+-][12]$")
_SPLICE_INTRONIC_RE = re.compile(r"c\.\d+[+-]\d+")

# Three-letter amino acid codes (for missense detection)
_AMINO_ACIDS_3 = {
    "Ala",
    "Arg",
    "Asn",
    "Asp",
    "Cys",
    "Gln",
    "Glu",
    "Gly",
    "His",
    "Ile",
    "Leu",
    "Lys",
    "Met",
    "Phe",
    "Pro",
    "Ser",
    "Thr",
    "Trp",
    "Tyr",
    "Val",
    "Sec",
    "Pyl",
}

# Missense pattern: p.Xxx###Yyy where Xxx and Yyy are amino acids
_MISSENSE_RE = re.compile(r"p\.([A-Z][a-z]{2})(\d+)([A-Z][a-z]{2})")


def parse_protein_change(name: str) -> str:
    """Extract p.Xxx###Yyy protein change from HGVS Name.

    Args:
        name: Full HGVS notation string (e.g. "NM_000297.4:c.12616C>T (p.Arg4206Trp)")

    Returns:
        Protein change string (e.g. "p.Arg4206Trp") or empty string if not found.
    """
    if not name:
        return ""
    match = _PROTEIN_CHANGE_RE.search(name)
    return match.group(1) if match else ""


def parse_protein_position(protein_change: str) -> int | None:
    """Extract numeric position from HGVS protein change notation.

    Args:
        protein_change: e.g. "p.Arg4206Trp", "p.Gly1234*", "p.Ter1234Cys"

    Returns:
        Integer position or None if cannot be parsed.
    """
    if not protein_change:
        return None
    # Strip leading "p." for matching
    cleaned = protein_change
    if cleaned.startswith("p."):
        cleaned = cleaned[2:]
    match = _PROTEIN_POSITION_RE.search(cleaned)
    if match:
        return int(match.group(1))
    return None


def infer_effect_category(name: str) -> str:
    """Infer variant effect category from HGVS Name notation.

    Rules (in priority order):
    - p.*Ter* (not ext) -> truncating (nonsense)
    - p.*fs* -> truncating (frameshift)
    - p.Xxx###Yyy (Xxx!=Yyy, Yyy!=Ter) -> missense
    - del/dup/ins without fs -> inframe
    - p.*= -> synonymous
    - c.NNN+1/+2/-1/-2 -> splice_region (canonical)
    - c.NNN+N/-N (N>2) -> intronic
    - anything else -> other

    Args:
        name: Full HGVS notation string.

    Returns:
        Effect category string.
    """
    if not name:
        return "other"

    protein = parse_protein_change(name)

    # Check protein-level changes first
    if protein:
        # Frameshift (check before nonsense since fs can include Ter)
        if "fs" in protein:
            return "truncating"

        # Nonsense: contains Ter but not ext (readthrough/extension)
        if "Ter" in protein and "ext" not in protein:
            # p.Xxx###Ter or p.Ter###Xxx (stop gain)
            # But not p.Ter###XxxextTer### (extension)
            return "truncating"

        # Stop notation with *
        if "*" in protein and "ext" not in protein and "fs" not in protein:
            return "truncating"

        # Synonymous: p.Xxx###= (silent mutation)
        if protein.endswith("="):
            return "synonymous"

        # Missense: p.Xxx###Yyy where both are amino acids and different
        m = _MISSENSE_RE.search(protein)
        if m:
            aa_from, _, aa_to = m.group(1), m.group(2), m.group(3)
            if aa_from != aa_to and aa_to != "Ter":
                return "missense"

    # Check for inframe indels (del/dup/ins without fs in protein)
    if (
        protein
        and ("del" in protein or "dup" in protein or "ins" in protein)
        and "fs" not in protein
    ):
        return "inframe"

    # Splice site detection from cDNA notation
    if _SPLICE_CANONICAL_RE.search(name):
        return "splice_region"

    if _SPLICE_INTRONIC_RE.search(name):
        return "intronic"

    # Check for genomic-level del/dup/ins (no protein change available)
    if not protein:
        for pattern in ("del", "dup", "ins"):
            if pattern in name.lower():
                return "inframe"

    return "other"


def infer_molecular_consequences(name: str, effect_category: str) -> list[str]:
    """Map effect_category to Sequence Ontology term labels.

    Args:
        name: HGVS notation (used for splice sub-typing).
        effect_category: Result from infer_effect_category().

    Returns:
        List of SO term label strings.
    """
    if effect_category == "truncating":
        protein = parse_protein_change(name)
        if protein and "fs" in protein:
            return ["frameshift variant"]
        return ["nonsense"]

    if effect_category == "missense":
        return ["missense variant"]

    if effect_category == "inframe":
        return ["inframe_indel"]

    if effect_category == "synonymous":
        return ["synonymous variant"]

    if effect_category == "splice_region":
        # Distinguish donor vs acceptor
        if re.search(r"c\.\d+\+[12]([^\d]|$)", name):
            return ["splice donor variant"]
        if re.search(r"c\.\d+-[12]([^\d]|$)", name):
            return ["splice acceptor variant"]
        return ["splice donor variant"]

    if effect_category == "intronic":
        return ["intron variant"]

    return ["other"]


def map_classification(clinical_significance: str) -> str:
    """Map ClinicalSignificance text to a normalized category.

    Args:
        clinical_significance: Raw text from ClinVar (e.g. "Pathogenic",
            "Pathogenic/Likely pathogenic", "Uncertain significance; other").

    Returns:
        Normalized category string.
    """
    if not clinical_significance:
        return "not_provided"

    sig = clinical_significance.lower().strip()

    # Handle combined classifications with "/" or ";"
    # "Pathogenic/Likely pathogenic" -> pathogenic
    if "conflicting" in sig:
        return "conflicting"

    if "pathogenic" in sig:
        if "benign" in sig:
            # "Pathogenic; Likely benign" or similar mixed -> conflicting
            return "conflicting"
        # Combined "Pathogenic/Likely pathogenic" and any "likely pathogenic"
        # collapse to likely_pathogenic; bare "Pathogenic" stays pathogenic.
        if "likely" in sig:
            return "likely_pathogenic"
        return "pathogenic"

    if "benign" in sig:
        if "likely" in sig:
            return "likely_benign"
        return "benign"

    if "uncertain" in sig or "vus" in sig:
        return "vus"

    if "not provided" in sig or sig == "-" or not sig:
        return "not_provided"

    return "other"


def load_star_map() -> dict[str, int]:
    """Load the ClinVar review-status -> gold-star mapping from package data.

    Returns:
        Mapping of lowercase/stripped review-status text -> star rating (0..4).
    """
    text = (files("clinvar_link.data") / "review_status_stars.yaml").read_text()
    raw: dict[str, Any] = yaml.safe_load(text) or {}
    return {str(key).lower().strip(): int(value) for key, value in raw.items()}


def map_star_rating(review_status: str, star_map: dict[str, int]) -> int:
    """Map a ClinVar ReviewStatus string to a 0-4 gold-star rating.

    Args:
        review_status: Raw ReviewStatus text from ClinVar.
        star_map: Mapping from ``load_star_map()``.

    Returns:
        Integer star rating (0-4); 0 for empty, "-", or unknown statuses.
    """
    if not review_status or review_status.strip() == "-":
        return 0
    return star_map.get(review_status.lower().strip(), 0)


def format_accession(variation_id: str) -> str:
    """Format a VariationID into a ClinVar VCV accession.

    Args:
        variation_id: Numeric string (e.g. "12345").

    Returns:
        Formatted accession (e.g. "VCV000012345").
    """
    try:
        return f"VCV{int(variation_id):09d}"
    except (ValueError, TypeError):
        return f"VCV{variation_id}"


def _safe_int(val: str | None) -> int | None:
    """Convert string to int, returning None for non-numeric/sentinel values."""
    if not val:
        return None
    cleaned = val.strip().lower()
    if cleaned in ("", "-", "na"):
        return None
    try:
        return int(val.strip())
    except (ValueError, TypeError):
        return None


class GeneAccumulator:
    """Incrementally accumulate variant statistics for one gene.

    Memory-efficient alternative to collecting all variant dicts and then
    aggregating: only running aggregates are kept, never per-variant detail.

    Star ratings drive the "high confidence" notion (star_rating >= 3) and the
    ``star_distribution`` histogram, replacing the kidney-confidence map.

    Usage::

        acc = GeneAccumulator(star_map)
        for variant in stream:
            acc.add_variant(variant)
        stats = acc.finalize()
    """

    __slots__ = (
        "_star_map",
        "benign_count",
        "conflicting_count",
        "consequence_categories",
        "high_confidence_count",
        "likely_benign_count",
        "likely_pathogenic_count",
        "molecular_consequences",
        "not_provided_count",
        "pathogenic_count",
        "star_distribution",
        "total_count",
        "traits_summary",
        "variant_type_counts",
        "vus_count",
    )

    _TRUNCATING: frozenset[str] = frozenset({"nonsense", "frameshift variant", "start lost"})
    _SPLICE: frozenset[str] = frozenset(
        {"splice donor variant", "splice acceptor variant", "splice region variant"}
    )

    def __init__(self, star_map: dict[str, int]) -> None:
        self.total_count = 0
        self.pathogenic_count = 0
        self.likely_pathogenic_count = 0
        self.vus_count = 0
        self.benign_count = 0
        self.likely_benign_count = 0
        self.conflicting_count = 0
        self.not_provided_count = 0
        self.high_confidence_count = 0
        self.variant_type_counts: dict[str, int] = {}
        self.traits_summary: dict[str, int] = {}
        self.molecular_consequences: dict[str, int] = {}
        self.consequence_categories: dict[str, int] = {
            "truncating": 0,
            "missense": 0,
            "inframe": 0,
            "splice_region": 0,
            "regulatory": 0,
            "intronic": 0,
            "synonymous": 0,
            "other": 0,
        }
        self.star_distribution: dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
        self._star_map = star_map

    # ------------------------------------------------------------------ #

    def _star_rating(self, variant: dict[str, Any]) -> int:
        """Resolve a variant's star rating, preferring the parsed value."""
        rating = variant.get("star_rating")
        if rating is None:
            rating = map_star_rating(variant.get("review_status", ""), self._star_map)
        return int(rating)

    def add_variant(self, variant: dict[str, Any]) -> None:
        """Incorporate one parsed variant dict into running totals."""
        self.total_count += 1
        classification = variant["classification"].lower()

        # --- Classification counting (pathogenic / likely pathogenic) ---
        is_pathogenic = False
        if "pathogenic" in classification:
            is_pathogenic = True
            if "likely" in classification:
                self.likely_pathogenic_count += 1
            elif "/" not in classification or "pathogenic/likely pathogenic" in classification:
                self.pathogenic_count += 1

        star_rating = self._star_rating(variant)
        mol_consequences = variant.get("molecular_consequences", [])

        # --- Other classification counts ---
        if not is_pathogenic and "benign" in classification:
            if "likely" in classification:
                self.likely_benign_count += 1
            elif "/" not in classification:
                self.benign_count += 1
        elif "uncertain" in classification or "vus" in classification:
            self.vus_count += 1
        elif "conflicting" in classification:
            self.conflicting_count += 1
        elif "not provided" in classification or "not_provided" in classification:
            self.not_provided_count += 1

        # --- Star distribution + high-confidence counting ---
        bucket = star_rating if star_rating in self.star_distribution else 0
        self.star_distribution[bucket] += 1
        if star_rating >= 3:
            self.high_confidence_count += 1

        # --- Variant type ---
        vtype = variant.get("variant_type", "")
        self.variant_type_counts[vtype] = self.variant_type_counts.get(vtype, 0) + 1

        # --- Traits ---
        for trait in variant.get("traits", []):
            name = trait.get("name")
            if name:
                self.traits_summary[name] = self.traits_summary.get(name, 0) + 1

        # --- Molecular consequence counts + consequence categories ---
        for consequence in mol_consequences:
            self.molecular_consequences[consequence] = (
                self.molecular_consequences.get(consequence, 0) + 1
            )
            consequence_lower = consequence.lower()
            if consequence_lower in self._TRUNCATING:
                self.consequence_categories["truncating"] += 1
            elif consequence_lower in self._SPLICE or "splice" in consequence_lower:
                self.consequence_categories["splice_region"] += 1
            elif "missense" in consequence_lower:
                self.consequence_categories["missense"] += 1
            elif "synonymous" in consequence_lower:
                self.consequence_categories["synonymous"] += 1
            elif "inframe" in consequence_lower:
                self.consequence_categories["inframe"] += 1
            elif "UTR" in consequence:
                self.consequence_categories["regulatory"] += 1
            elif "intron" in consequence_lower:
                self.consequence_categories["intronic"] += 1
            else:
                self.consequence_categories["other"] += 1

    # ------------------------------------------------------------------ #

    def finalize(self) -> dict[str, Any]:
        """Return the aggregated statistics dict for the gene."""
        total = self.total_count

        known_buckets = (
            self.pathogenic_count
            + self.likely_pathogenic_count
            + self.vus_count
            + self.benign_count
            + self.likely_benign_count
            + self.conflicting_count
            + self.not_provided_count
        )
        other_count = max(0, total - known_buckets)

        top_consequences = sorted(
            self.molecular_consequences.items(), key=lambda x: x[1], reverse=True
        )[:10]
        top_molecular_consequences = [
            {"consequence": c[0], "count": c[1]} for c in top_consequences
        ]

        percentages: dict[str, float] = {}
        if total > 0:
            for cat_name in self.consequence_categories:
                percentages[f"{cat_name}_percentage"] = round(
                    (self.consequence_categories[cat_name] / total) * 100, 1
                )

        top_traits_sorted = sorted(self.traits_summary.items(), key=lambda x: x[1], reverse=True)[
            :5
        ]
        top_traits = [{"trait": t[0], "count": t[1]} for t in top_traits_sorted]

        high_confidence_percentage = 0.0
        pathogenic_percentage = 0.0
        if total > 0:
            high_confidence_percentage = round((self.high_confidence_count / total) * 100, 1)
            pathogenic_percentage = round(
                ((self.pathogenic_count + self.likely_pathogenic_count) / total) * 100,
                1,
            )

        has_pathogenic = self.pathogenic_count > 0 or self.likely_pathogenic_count > 0

        stats: dict[str, Any] = {
            "total_count": total,
            "pathogenic_count": self.pathogenic_count,
            "likely_pathogenic_count": self.likely_pathogenic_count,
            "vus_count": self.vus_count,
            "benign_count": self.benign_count,
            "likely_benign_count": self.likely_benign_count,
            "conflicting_count": self.conflicting_count,
            "not_provided_count": self.not_provided_count,
            "other_count": other_count,
            "high_confidence_count": self.high_confidence_count,
            "variant_type_counts": self.variant_type_counts,
            "molecular_consequences": self.molecular_consequences,
            "consequence_categories": self.consequence_categories,
            "star_distribution": self.star_distribution,
            "top_molecular_consequences": top_molecular_consequences,
            "top_traits": top_traits,
            "high_confidence_percentage": high_confidence_percentage,
            "pathogenic_percentage": pathogenic_percentage,
            "has_pathogenic": has_pathogenic,
        }
        stats.update(percentages)
        return stats


def _get_col(row: dict[str, str], *names: str) -> str:
    """Return the first present column value among *names* (handles #-prefixes)."""
    for name in names:
        if name in row:
            return row[name]
    return ""


def parse_variant_row(row: dict[str, str], star_map: dict[str, int]) -> dict[str, Any]:
    """Parse one TSV row from variant_summary.txt into a normalized variant dict.

    Columns are accessed by name (handling both ``#AlleleID`` and ``AlleleID``).

    Args:
        row: Dict with TSV column names as keys.
        star_map: ReviewStatus -> gold-star mapping from ``load_star_map()``.

    Returns:
        Normalized variant dictionary.
    """
    name = _get_col(row, "Name")
    clinical_sig = _get_col(row, "ClinicalSignificance")
    review_status = _get_col(row, "ReviewStatus")
    variation_id = _get_col(row, "VariationID")

    protein_change = parse_protein_change(name)
    effect_category = infer_effect_category(name)
    mol_consequences = infer_molecular_consequences(name, effect_category)

    # Parse traits from PhenotypeList (pipe-separated), filtering placeholders.
    phenotype_list = _get_col(row, "PhenotypeList")
    traits: list[dict[str, Any]] = []
    if phenotype_list and phenotype_list not in ("-", "not provided", "not specified"):
        for pheno_raw in phenotype_list.split("|"):
            pheno = pheno_raw.strip()
            if pheno and pheno not in ("not provided", "not specified"):
                traits.append({"name": pheno, "omim_id": None, "medgen_id": None})

    # Parse RCV accessions (pipe-separated).
    rcv_raw = _get_col(row, "RCVaccession")
    rcv_accessions: list[str] = []
    if rcv_raw and rcv_raw != "-":
        rcv_accessions = [r.strip() for r in rcv_raw.split("|") if r.strip()]

    # Parse rsID (RS# (dbSNP)); -1 / - mean "no rsID".
    rsid = _safe_int(_get_col(row, "RS# (dbSNP)"))
    if rsid is not None and rsid < 0:
        rsid = None

    return {
        "variation_id": variation_id,
        "allele_id": _get_col(row, "#AlleleID", "AlleleID"),
        "accession": format_accession(variation_id),
        "rsid": rsid,
        "name": name,
        "gene_symbol": _get_col(row, "GeneSymbol"),
        "gene_id": _get_col(row, "GeneID"),
        "hgnc_id": _get_col(row, "HGNC_ID"),
        "variant_type": _get_col(row, "Type"),
        "clinical_significance": clinical_sig,
        "classification": map_classification(clinical_sig),
        "review_status": review_status,
        "star_rating": map_star_rating(review_status, star_map),
        "protein_change": protein_change,
        "cdna_change": name,
        "molecular_consequences": mol_consequences,
        "traits": traits,
        "rcv_accessions": rcv_accessions,
        "assembly": _get_col(row, "Assembly"),
        "chromosome": _get_col(row, "Chromosome"),
        "chromosome_accession": _get_col(row, "ChromosomeAccession"),
        "start": _safe_int(_get_col(row, "Start")),
        "stop": _safe_int(_get_col(row, "Stop")),
        "reference_allele": _get_col(row, "ReferenceAllele"),
        "alternate_allele": _get_col(row, "AlternateAllele"),
        "position_vcf": _safe_int(_get_col(row, "PositionVCF")),
        "reference_allele_vcf": _get_col(row, "ReferenceAlleleVCF"),
        "alternate_allele_vcf": _get_col(row, "AlternateAlleleVCF"),
        "cytogenetic": _get_col(row, "Cytogenetic"),
        "number_submitters": _safe_int(_get_col(row, "NumberSubmitters")),
        "last_evaluated": _get_col(row, "LastEvaluated"),
        "origin": _get_col(row, "Origin"),
    }
