"""Enum definitions for ClinVar entities and request options.

Every closed vocabulary the tools accept is declared here ONCE and flows into three places:
the advertised MCP input schema (``mcp/params.py``), the runtime validation that rejects an
unrecognised value (``services/clinvar_service.py``), and the capabilities payload
(``mcp/resources.py``). A vocabulary that lives in only one of the three is how an undeclared
enum — and the silently-empty filter it produces — gets shipped.

Direction matters: the RUNTIME may accept more than the schema advertises (ClinVar's own human
wording, "Likely pathogenic", normalises onto ``likely_pathogenic``), because a model that reads
the enum can never guess wrong and a model that reaches for the upstream's published term still
lands on the right rows. The reverse — a schema that promises more than the runtime honours —
is the bug: it makes the model ask for something the server silently answers with nothing.
"""

from enum import Enum


class Classification(str, Enum):
    """Normalized ClinVar clinical classification categories.

    The canonical token set: exactly the values ``ingest.parsing.map_classification`` writes
    into ``variant.classification``, and exactly the distinct values present in the built index.
    """

    PATHOGENIC = "pathogenic"
    LIKELY_PATHOGENIC = "likely_pathogenic"
    VUS = "vus"
    LIKELY_BENIGN = "likely_benign"
    BENIGN = "benign"
    CONFLICTING = "conflicting"
    NOT_PROVIDED = "not_provided"
    OTHER = "other"


class Assembly(str, Enum):
    """Supported reference genome assemblies."""

    GRCH38 = "GRCh38"
    GRCH37 = "GRCh37"


class ResponseMode(str, Enum):
    """Payload verbosity modes for tool responses."""

    MINIMAL = "minimal"
    COMPACT = "compact"
    STANDARD = "standard"
    FULL = "full"


class IdType(str, Enum):
    """Identifier types accepted when resolving a ClinVar variant."""

    AUTO = "auto"
    VCV = "vcv"
    VARIATION_ID = "variation_id"
    RSID = "rsid"
    HGVS = "hgvs"
    ALLELE_ID = "allele_id"


CLASSIFICATION_VALUES: tuple[str, ...] = tuple(item.value for item in Classification)
ASSEMBLY_VALUES: tuple[str, ...] = tuple(item.value for item in Assembly)
RESPONSE_MODES: tuple[str, ...] = tuple(item.value for item in ResponseMode)
ID_TYPES: tuple[str, ...] = tuple(item.value for item in IdType)
MATCH_MODES: tuple[str, ...] = ("auto", "and", "or")
COUNT_MODES: tuple[str, ...] = ("exact", "none")
# Mirrors ClinVarRepository.SORT_ORDERS (a drift test pins the two together).
SORT_ORDERS: tuple[str, ...] = ("stars_desc", "stars_asc", "name", "variation_id")

# ClinVar's OWN published display terms, normalised (see _fold) onto the canonical token the
# index actually stores. These are the spellings a model reaches for because they are what
# ClinVar publishes; each one returned zero rows with success:true before this map existed.
# The composite terms fold exactly the way ingest.parsing.map_classification folds them, so a
# query can never ask for a bucket the index does not have.
_CLASSIFICATION_ALIASES: dict[str, str] = {
    "pathogenic_likely_pathogenic": "likely_pathogenic",
    "benign_likely_benign": "likely_benign",
    "uncertain_significance": "vus",
    "variant_of_uncertain_significance": "vus",
    "uncertain": "vus",
    "conflicting_interpretations_of_pathogenicity": "conflicting",
    "conflicting_classifications_of_pathogenicity": "conflicting",
    "conflicting_data_from_submitters": "conflicting",
    "no_classification_provided": "not_provided",
    "not_provided_by_submitter": "not_provided",
}

_ASSEMBLY_ALIASES: dict[str, str] = {
    "hg38": "GRCh38",
    "hg19": "GRCh37",
    "b38": "GRCh38",
    "b37": "GRCh37",
}


def _fold(value: str) -> str:
    """Case-, space- and separator-insensitive key: 'Pathogenic/Likely pathogenic' -> the token."""
    folded = value.strip().casefold()
    for char in (" ", "-", "/", ";", ",", "."):
        folded = folded.replace(char, "_")
    while "__" in folded:
        folded = folded.replace("__", "_")
    return folded.strip("_")


def normalize_classification(value: str) -> str | None:
    """Return the canonical classification token, or ``None`` when unrecognised.

    ``None`` is the signal to REJECT with ``invalid_input``. It is never a licence to drop the
    filter or to match nothing: a value the server does not understand has no truthful answer.
    """
    folded = _fold(value)
    if folded in CLASSIFICATION_VALUES:
        return folded
    return _CLASSIFICATION_ALIASES.get(folded)


def normalize_assembly(value: str) -> str | None:
    """Return the canonical assembly name, or ``None`` when unrecognised ('hg19' -> 'GRCh37')."""
    folded = _fold(value)
    for canonical in ASSEMBLY_VALUES:
        if folded == canonical.casefold():
            return canonical
    return _ASSEMBLY_ALIASES.get(folded)
