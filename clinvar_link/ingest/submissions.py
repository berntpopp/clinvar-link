"""Stream allow-listed submitters' SCV records out of the monthly ClinVar VCV XML dump.

The dump (``ClinVarVCVRelease_00-latest.xml.gz``, ~6 GB gz / ~60 GB XML) is the only bulk
source of submission free text: ``submission_summary.txt`` has ``Description = -`` for every
OMIM SCV (#119, #122). It is parsed once per bundle build with ``lxml.etree.iterparse`` on
``VariationArchive``; each record is cleared together with its already-seen siblings, so
memory stays flat however long the stream is. Only SCVs whose ``SubmitterName`` is in
:data:`SUBMITTER_ALLOWLIST` are kept.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import re
import sqlite3
import time
import zlib
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, cast

import httpx
from lxml import etree

from clinvar_link.exceptions import ClinVarDataError, DownloadError
from clinvar_link.ingest.download_security import open_source_text
from clinvar_link.models.enums import SUBMITTERS

if TYPE_CHECKING:
    from clinvar_link.config import Settings

SUBMITTER_ALLOWLIST = SUBMITTERS

# The independent submission_summary count may differ from the parsed one: the XML dump is
# monthly and the TSV weekly. A larger gap means the parse lost records - fail the build.
MAX_COUNT_DRIFT = 0.05

_INSERT_BATCH = 2000
_SEP = "\n\n"


def _text(element: Any) -> str:
    return " ".join("".join(element.itertext()).split())


def _unique(values: Iterator[str]) -> list[str]:
    return list(dict.fromkeys(v for v in values if v))


def _vcv_genes(record: Any) -> list[str]:
    """ClinVar's own (VCV-level) gene symbols, excluding the per-submission ones."""
    return _unique(
        gene.get("Symbol", "")
        for child in record.iterfind("ClassifiedRecord/*")
        if child.tag in ("SimpleAllele", "Haplotype", "Genotype")
        for gene in child.iter("Gene")
    )


def _submission(assertion: Any, record: Any, vcv_genes: list[str]) -> dict[str, Any]:
    accession = assertion.find("ClinVarAccession")
    descriptions = [
        _text(attr) for attr in assertion.iterfind(".//ObservedData/Attribute[@Type='Description']")
    ] + [_text(comment) for comment in assertion.iter("Comment")]
    pmids = _unique(
        (cid.text or "").strip() for cid in assertion.iterfind(".//Citation/ID[@Source='PubMed']")
    )
    submitted = _unique(gene.get("Symbol", "") for gene in assertion.iter("Gene"))
    return {
        "scv": accession.get("Accession"),
        "scv_version": int(accession.get("Version") or 0) or None,
        "variation_id": int(record.get("VariationID")),
        "vcv_accession": record.get("Accession"),
        "variation_name": record.get("VariationName"),
        "submitter": accession.get("SubmitterName"),
        "submitted_genes": submitted,
        "genes": _unique(iter(submitted + vcv_genes)),
        "conditions": _unique(
            _text(value)
            for value in assertion.iterfind("TraitSet/Trait/Name/ElementValue[@Type='Preferred']")
        ),
        "description": _SEP.join(_unique(iter(descriptions))) or None,
        "pmids": sorted(pmids, key=lambda p: (len(p), p)),
    }


def iter_submissions(
    source: IO[bytes], *, allowlist: tuple[str, ...] = SUBMITTER_ALLOWLIST, stats: Counter[str]
) -> Iterator[dict[str, Any]]:
    """Yield one dict per current SCV of an allow-listed submitter in a VCV XML stream.

    ``source`` is the DECOMPRESSED XML byte stream. A record or assertion missing its
    identifiers is skipped and counted in ``stats["skipped"]``; a stream that is not
    well-formed (e.g. truncated) raises :class:`lxml.etree.XMLSyntaxError`.
    """
    wanted = frozenset(allowlist)
    for _, record in etree.iterparse(
        source, events=("end",), tag="VariationArchive", resolve_entities=False, no_network=True
    ):
        stats["records"] += 1
        try:
            vid_text = record.get("VariationID") or ""
            assertions = [
                a
                for a in record.iterfind("ClassifiedRecord/ClinicalAssertionList/ClinicalAssertion")
                if (acc := a.find("ClinVarAccession")) is not None
                and acc.get("SubmitterName") in wanted
            ]
            if assertions and not vid_text.isdigit():
                stats["skipped"] += len(assertions)
                assertions = []
            vcv_genes = _vcv_genes(record) if assertions else []
            for assertion in assertions:
                if (assertion.findtext("RecordStatus") or "current").strip() != "current":
                    stats["not_current"] += 1
                    continue
                if not (assertion.find("ClinVarAccession").get("Accession") or "").startswith(
                    "SCV"
                ):
                    stats["skipped"] += 1
                    continue
                yield _submission(assertion, record, vcv_genes)
        finally:
            # Drop the record and every already-processed sibling: flat memory on a 60 GB stream.
            record.clear(keep_tail=False)
            parent = record.getparent()
            if parent is not None:
                while record.getprevious() is not None:
                    del parent[0]


def load_submissions(conn: sqlite3.Connection, source: IO[bytes]) -> Counter[str]:
    """Insert the allow-listed submissions from ``source`` into ``submission`` tables.

    Returns the stats counter: per-submitter kept counts plus ``records`` / ``skipped`` /
    ``not_current``.
    """
    stats: Counter[str] = Counter()
    rows: list[tuple[Any, ...]] = []
    genes: list[tuple[str, str]] = []

    def flush() -> None:
        conn.executemany(
            "INSERT OR REPLACE INTO submission VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
        conn.executemany("INSERT OR IGNORE INTO submission_gene VALUES (?, ?)", genes)
        rows.clear()
        genes.clear()

    for sub in iter_submissions(source, stats=stats):
        stats[f"submitter:{sub['submitter']}"] += 1
        rows.append(
            (
                sub["scv"],
                sub["scv_version"],
                sub["variation_id"],
                sub["vcv_accession"],
                sub["variation_name"],
                sub["submitter"],
                json.dumps(sub["submitted_genes"]),
                json.dumps(sub["conditions"]),
                sub["description"],
                json.dumps(sub["pmids"]),
            )
        )
        genes.extend((symbol.upper(), sub["scv"]) for symbol in sub["genes"])
        if len(rows) >= _INSERT_BATCH:
            flush()
    flush()
    return stats


def count_summary_scvs(
    path: Path, *, allowlist: tuple[str, ...] = SUBMITTER_ALLOWLIST, max_expanded_bytes: int
) -> Counter[str]:
    """Count SCVs per allow-listed submitter in ``submission_summary.txt.gz`` (independent check)."""
    wanted = frozenset(allowlist)
    counts: Counter[str] = Counter()
    submitter_col: int | None = None
    with open_source_text(path, max_expanded_bytes=max_expanded_bytes) as handle:
        for fields in csv.reader(handle, delimiter="\t", quoting=csv.QUOTE_NONE):
            if not fields:
                continue
            if fields[0].startswith("#"):
                header = [f.lstrip("#").strip() for f in fields]
                if "Submitter" in header:
                    submitter_col = header.index("Submitter")
                continue
            if submitter_col is None:
                raise ClinVarDataError("submission_summary lacks a #VariationID ... header row")
            if len(fields) > submitter_col and fields[submitter_col] in wanted:
                counts[fields[submitter_col]] += 1
    return counts


def check_counts(parsed: Counter[str], expected: Counter[str]) -> None:
    """Fail the build when any allow-listed submitter's parsed count drifts > MAX_COUNT_DRIFT."""
    for submitter in SUBMITTER_ALLOWLIST:
        want, got = expected[submitter], parsed[f"submitter:{submitter}"]
        if want == 0 or abs(got - want) / want > MAX_COUNT_DRIFT:
            raise ClinVarDataError(
                f"{submitter} SCV count {got} from the VCV XML differs from {want} in "
                f"submission_summary by more than {MAX_COUNT_DRIFT:.0%}"
            )


_MD5_RE = re.compile(r"[0-9a-f]{32}")
_TIMEOUT = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0)
_HEADERS = {"User-Agent": "clinvar-link/ingest (+https://github.com/berntpopp/clinvar-link)"}


class _HashingReader(io.RawIOBase):
    """Raw stream over an HTTP body that hashes (MD5) and bounds the compressed bytes."""

    def __init__(self, chunks: Iterator[bytes], *, max_bytes: int, max_seconds: float) -> None:
        self._chunks = chunks
        self._pending = b""
        self._deadline = time.monotonic() + max_seconds
        self._max_bytes = max_bytes
        self.md5 = hashlib.md5(usedforsecurity=False)
        self.size = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if not self._pending:
            self._pending = next(self._chunks, b"")
            self.size += len(self._pending)
            self.md5.update(self._pending)
            if self.size > self._max_bytes:
                raise DownloadError(f"VCV XML exceeded {self._max_bytes} bytes")
            if time.monotonic() > self._deadline:
                raise DownloadError("VCV XML stream exceeded its time limit")
        n = min(len(buffer), len(self._pending))
        buffer[:n], self._pending = self._pending[:n], self._pending[n:]
        return n


@contextmanager
def open_vcv_xml(
    url: str, *, max_bytes: int, max_seconds: float
) -> Iterator[tuple[IO[bytes], dict[str, Any]]]:
    """Stream ``url`` (gzip XML) without touching disk; verify NCBI's published MD5 at the end.

    Yields the decompressed byte stream and a provenance dict that is completed (``md5``,
    ``size``) only after the consumer has read the stream to EOF and the MD5 matched.
    """
    try:
        with httpx.Client(follow_redirects=False, timeout=_TIMEOUT) as client:
            md5_response = client.get(f"{url}.md5", headers=_HEADERS)
            md5_response.raise_for_status()
            expected = md5_response.text[:64].split()[0].lower() if md5_response.text else ""
            if not _MD5_RE.fullmatch(expected):
                raise DownloadError(f"{url}.md5 does not hold an MD5 digest")
            with client.stream("GET", url, headers=_HEADERS) as response:
                response.raise_for_status()
                raw = _HashingReader(
                    response.iter_bytes(1 << 20), max_bytes=max_bytes, max_seconds=max_seconds
                )
                info: dict[str, Any] = {
                    "url": url,
                    "last_modified": response.headers.get("Last-Modified"),
                }
                with gzip.GzipFile(fileobj=io.BufferedReader(raw, 1 << 20)) as xml:
                    yield cast(IO[bytes], xml), info
                    while xml.read(1 << 20):  # to EOF: gzip checks its CRC32 + length trailer
                        pass
                while raw.read(1 << 20):
                    pass
    except httpx.HTTPError as exc:
        raise DownloadError(f"VCV XML download failed: {exc}") from exc
    except (EOFError, zlib.error, etree.XMLSyntaxError) as exc:  # truncated / corrupt stream
        raise DownloadError(f"VCV XML stream is truncated or corrupt: {exc}") from exc
    if raw.md5.hexdigest() != expected:
        raise DownloadError(f"VCV XML MD5 {raw.md5.hexdigest()} != published {expected}")
    info.update(md5=expected, size=raw.size)


def load_hgnc_aliases(conn: sqlite3.Connection, path: Path) -> int:
    """Load approved / previous / alias symbols from ``hgnc_complete_set.txt``; return genes."""
    rows: list[tuple[str, str, str, str]] = []
    genes = 0
    with path.open(encoding="utf-8", newline="") as handle:
        for rec in csv.DictReader(handle, delimiter="\t"):
            hgnc_id, symbol = rec.get("hgnc_id") or "", rec.get("symbol") or ""
            if not hgnc_id or not symbol:
                continue
            genes += 1
            rows.append((symbol.upper(), hgnc_id, symbol, "approved"))
            for kind, column in (("previous", "prev_symbol"), ("alias", "alias_symbol")):
                for other in (rec.get(column) or "").split("|"):
                    if other.strip():
                        rows.append((other.strip().upper(), hgnc_id, symbol, kind))
    conn.executemany("INSERT INTO gene_alias VALUES (?, ?, ?, ?)", rows)
    if genes == 0:
        raise ClinVarDataError(f"{path.name} holds no HGNC genes")
    return genes


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def make_loader(
    config: Settings,
    *,
    summary: dict[str, Any],
    hgnc: dict[str, Any],
    report: dict[str, Any],
) -> Callable[[sqlite3.Connection], None]:
    """Return the builder hook that fills ``gene_alias``, ``submission*`` and ``data_source``.

    The independent submission_summary count is taken up front (fail fast, before the long
    build); the VCV XML is streamed inside the build and its count checked against it.
    ``summary`` / ``hgnc`` are the :func:`~clinvar_link.ingest.downloader.download_source`
    results for those inputs; ``report`` receives the parse stats and wall time.
    """
    summary_path, hgnc_path = Path(summary["path"]), Path(hgnc["path"])
    expected = count_summary_scvs(summary_path, max_expanded_bytes=config.SOURCE_MAX_EXPANDED_BYTES)

    def load(conn: sqlite3.Connection) -> None:
        started = time.perf_counter()
        genes = load_hgnc_aliases(conn, hgnc_path)
        with open_vcv_xml(
            config.VCV_XML_URL,
            max_bytes=config.VCV_XML_MAX_BYTES,
            max_seconds=config.VCV_XML_MAX_SECONDS,
        ) as (xml, info):
            stats = load_submissions(conn, xml)
        check_counts(stats, expected)
        retrieved = datetime.now(UTC).isoformat().replace("+00:00", "Z")  # stream end
        kept = sum(n for key, n in stats.items() if key.startswith("submitter:"))
        conn.executemany(
            "INSERT INTO data_source VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("vcv_xml", info["url"], info["last_modified"], info["md5"], None, retrieved, kept),
                (
                    "submission_summary",
                    config.SUBMISSION_SUMMARY_URL,
                    summary.get("last_modified"),
                    None,
                    _sha256(summary_path),
                    summary.get("retrieved_at"),
                    sum(expected.values()),
                ),
                (
                    "hgnc",
                    config.HGNC_URL,
                    hgnc.get("last_modified"),
                    None,
                    _sha256(hgnc_path),
                    hgnc.get("retrieved_at"),
                    genes,
                ),
            ],
        )
        report.update(
            stats=dict(stats),
            expected=dict(expected),
            vcv_xml_bytes=info["size"],
            seconds=round(time.perf_counter() - started, 1),
        )

    return load
