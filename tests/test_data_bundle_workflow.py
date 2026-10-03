"""Contract tests for the data-bundle workflow's release boundary."""

from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "data-bundle.yml"


def test_data_bundle_workflow_uses_the_release_helper_and_never_deletes_drafts() -> None:
    """Publication mutations are selected by the typed identity state only."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "clinvar_link.ingest.release_metadata" in workflow
    assert 'str(meta["clinvar_release_date"])[:10]' not in workflow
    assert ".read_bytes()" not in workflow
    assert "gh release delete" not in workflow
    assert "state=create" in workflow
    assert "published_noop" in workflow
    assert "draft_publish_existing" in workflow
    assert "collision" in workflow
    assert "steps.release-state.outputs.state == 'create'" in workflow
    assert "steps.release-state.outputs.state == 'draft_publish_existing'" in workflow
    assert 'gh release verify "$TAG"' not in workflow
    assert "published_noop)" in workflow
    assert (
        './scripts/verify_release_assets.sh "$TAG" "/tmp/existing" "$GITHUB_REPOSITORY"' in workflow
    )
    assert "draft_publish_existing)" in workflow
    assert "./scripts/verify_draft_release_assets.sh" in workflow
    assert "git/ref/tags/$TAG" in workflow
    assert "draft recovery requires an existing tag that resolves to a source commit" in workflow
    publication = workflow.split("- name: Publish once and verify immutable assets", maxsplit=1)[1]
    assert publication.index('gh release edit "$TAG"') < publication.index(
        "./scripts/verify_release_assets.sh"
    )
    assert 'steps.release-state.outputs.state }}" == draft_publish_existing' in publication
    assert './scripts/verify_release_assets.sh "$TAG" "/tmp/existing"' in publication
    assert './scripts/verify_release_assets.sh "$TAG" "dist"' in publication


def test_publish_job_installs_the_release_state_helper() -> None:
    """Existing releases must use the same checked-out immutable-state helper."""
    publish_job = WORKFLOW.read_text(encoding="utf-8").split("  publish:", maxsplit=1)[1]

    assert "actions/checkout@" in publish_job
    assert "actions/setup-python@" in publish_job
    assert "astral-sh/setup-uv@" in publish_job
    assert "uv sync --frozen" in publish_job
