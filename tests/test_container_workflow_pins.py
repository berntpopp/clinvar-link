"""Trusted-builder workflow references must match the released control plane."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRUSTED_BUILDER_SHA = "db47bd3357cebf33e6722615c4f0e7419a64857e"


def test_all_router_reusable_workflows_use_the_exact_trusted_builder() -> None:
    """Reject valid-looking older router revisions across every workflow."""
    occurrences: list[str] = []
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if "berntpopp/genefoundry-router/.github/workflows/" in line:
                occurrences.append(line.rsplit("@", 1)[-1].strip())

    assert occurrences
    assert set(occurrences) == {TRUSTED_BUILDER_SHA}
