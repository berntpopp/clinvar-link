"""`python -m clinvar_link.data_probe` is the fleet controller's semantic observation.

The controller execs it inside the running container and compares the JSON it prints
against a reviewed record, so the output shape is a contract: exactly three keys, exactly
those names, and byte-identical output for the same data. It must also be read-only —
opening the index for writing would create a ``-wal`` sidecar and invalidate the runtime
data identity of the whole volume.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from clinvar_link.data_probe import DataProbeError, probe

PROBE_KEYS = {"data_schema_version", "record_count", "query_result_sha256"}
# sha256("100001"), the fixture index's lowest variation_id.
FIXTURE_FIRST_KEY_SHA256 = "97c489b6c1231ecd9fac99df40e60cec000a70a057d5971fb520c578da8e8841"


def test_probe_reports_exactly_the_contract_keys(built_db: Path) -> None:
    observation = probe(built_db)
    assert set(observation) == PROBE_KEYS
    assert isinstance(observation["data_schema_version"], str)
    assert isinstance(observation["record_count"], int)
    assert observation["record_count"] > 0
    assert len(observation["query_result_sha256"]) == 64
    assert observation["query_result_sha256"] == FIXTURE_FIRST_KEY_SHA256


def test_probe_is_deterministic(built_db: Path) -> None:
    assert probe(built_db) == probe(built_db)


def test_probe_does_not_write_to_the_data_directory(built_db: Path) -> None:
    """An immutable open must not leave a -wal/-shm sidecar behind."""
    before = sorted(p.name for p in built_db.parent.iterdir())
    probe(built_db)
    assert sorted(p.name for p in built_db.parent.iterdir()) == before


def test_probe_fails_closed_on_a_missing_index(tmp_path: Path) -> None:
    with pytest.raises(DataProbeError, match="missing"):
        probe(tmp_path / "absent.sqlite")


def test_module_entry_point_prints_one_json_object(built_db: Path) -> None:
    """The exact command the fleet controller execs must print one parseable line."""
    environment = {
        **os.environ,
        "CLINVAR_LINK_DATA_DIR": str(built_db.parent),
        "CLINVAR_LINK_DB_FILENAME": built_db.name,
        "CLINVAR_LINK_ENVIRONMENT": "development",
    }
    completed = subprocess.run(  # fixed argv, no shell
        [sys.executable, "-m", "clinvar_link.data_probe"],
        capture_output=True,
        check=True,
        env=environment,
        text=True,
    )
    (line,) = completed.stdout.splitlines()
    assert set(json.loads(line)) == PROBE_KEYS
    assert json.loads(line) == probe(built_db)
