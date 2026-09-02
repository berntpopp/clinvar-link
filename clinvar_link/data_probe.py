"""Deterministic, read-only semantic probe of the materialized ClinVar index.

The fleet controller (``strato_v6_docker_npm``) execs this module inside the running
application container to observe *what the data actually is*, independently of what the
deployment claims::

    python -m clinvar_link.data_probe

It prints exactly one JSON object with exactly these keys::

    {"data_schema_version": "<str>", "record_count": <int>, "query_result_sha256": "<64 hex>"}

``record_count`` counts the primary entity (one canonical row per ClinVar VariationID) and
``query_result_sha256`` is the SHA-256 of the UTF-8 text of the canonical first key
(``SELECT variation_id FROM variant ORDER BY variation_id LIMIT 1``). Two containers
serving the same data release must print byte-identical output.

The database is opened ``mode=ro&immutable=1``: observing the data can neither modify it
nor create a ``-wal``/``-shm`` sidecar file, which would invalidate the runtime data
identity of the volume. The probe needs no network and runs as the image's non-root user.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

_SCHEMA_QUERY = "SELECT schema_version FROM meta WHERE id = 1"
_COUNT_QUERY = "SELECT COUNT(*) FROM variant"
_FIRST_KEY_QUERY = "SELECT variation_id FROM variant ORDER BY variation_id LIMIT 1"


class DataProbeError(RuntimeError):
    """The materialized index cannot answer the reviewed probe query."""


def probe(db_path: Path) -> dict[str, Any]:
    """Return the reviewed observation of one materialized ClinVar index."""
    if not db_path.is_file():
        raise DataProbeError(f"ClinVar index is missing at {db_path}")
    connection = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    try:
        schema_row = connection.execute(_SCHEMA_QUERY).fetchone()
        count_row = connection.execute(_COUNT_QUERY).fetchone()
        key_row = connection.execute(_FIRST_KEY_QUERY).fetchone()
    except sqlite3.Error as exc:
        raise DataProbeError(f"ClinVar index at {db_path} is not readable: {exc}") from exc
    finally:
        connection.close()
    if schema_row is None or schema_row[0] is None:
        raise DataProbeError("ClinVar index has no readable schema identity")
    if count_row is None or key_row is None:
        raise DataProbeError("ClinVar index has no variant rows to observe")
    return {
        "data_schema_version": str(schema_row[0]),
        "record_count": int(count_row[0]),
        "query_result_sha256": hashlib.sha256(str(key_row[0]).encode("utf-8")).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    """Print the probe observation as one line of JSON; non-zero on any failure."""
    if argv:
        print("usage: python -m clinvar_link.data_probe", file=sys.stderr)
        return 2
    # Imported here so a configuration problem is reported as a probe failure rather than
    # an import-time traceback, and so the module stays importable for unit tests.
    from clinvar_link.config import settings

    try:
        observation = probe(settings.db_path)
    except DataProbeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(observation, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
