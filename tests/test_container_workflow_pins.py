"""Trusted-builder workflow references must match the released control plane."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRUSTED_BUILDER_SHA = "31ea81cee5475fc3655c047c63a89739948f99a9"  # genefoundry-router v0.8.5


def test_all_router_reusable_workflows_use_the_exact_trusted_builder() -> None:
    """Reject valid-looking older router revisions across every workflow."""
    occurrences: list[str] = []
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            if "berntpopp/genefoundry-router/.github/workflows/" in line:
                occurrences.append(line.rsplit("@", 1)[-1].strip())

    assert occurrences
    assert set(occurrences) == {TRUSTED_BUILDER_SHA}
