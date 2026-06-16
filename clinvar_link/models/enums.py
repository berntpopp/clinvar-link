"""Enum definitions for ClinVar entities and request options."""

from enum import Enum


class Classification(str, Enum):
    """Normalized ClinVar clinical classification categories."""

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
