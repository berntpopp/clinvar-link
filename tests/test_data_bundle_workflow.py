"""Contract tests for the data-bundle workflow's release boundary."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "data-bundle.yml"
STATE_SCRIPT = ROOT / "scripts" / "select_release_state.sh"


def test_data_bundle_workflow_uses_the_release_helper_and_never_deletes_drafts() -> None:
    """Publication mutations are selected by the typed identity state only."""
    workflow = WORKFLOW.read_text(encoding="utf-8") + STATE_SCRIPT.read_text(encoding="utf-8")

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


def _jobs() -> tuple[str, str]:
    build, publish = WORKFLOW.read_text(encoding="utf-8").split("  publish:", maxsplit=1)
    return build, publish


def test_release_state_is_decided_before_the_gate_with_a_read_only_token() -> None:
    """An already-published bundle must never request data-release approval."""
    build, publish = _jobs()

    assert "./scripts/select_release_state.sh" in build
    assert "steps.release-state.outputs.state }}" in build
    assert "contents: read" in build
    assert "write" not in build.split("jobs:", maxsplit=1)[1]
    gate_condition = (
        "if: needs.build.outputs.state == 'create' || "
        "needs.build.outputs.state == 'draft_publish_existing'"
    )
    assert publish.index(gate_condition) < publish.index("environment: data-release")
    # The in-gate re-check (write token, sees drafts) stays authoritative.
    assert "./scripts/select_release_state.sh" in publish


def test_publish_no_ops_when_a_newer_bundle_is_already_published() -> None:
    """An approval granted to a stale run must not publish an older bundle."""
    _, publish = _jobs()

    supersede = publish.index("id: supersede")
    assert supersede < publish.index("actions/download-artifact@")
    assert supersede < publish.index("id: release-state")
    assert "--exclude-drafts" in publish
    assert '[[ "$latest" > "$TAG" ]]' in publish
    assert publish.count("if: steps.supersede.outputs.superseded != 'true'") == 3


def test_unchanged_upstream_skips_the_build_and_the_gate() -> None:
    """Rebuilds are not byte-identical, so the no-op is decided from upstream before building."""
    from clinvar_link.config import Settings

    build, _ = _jobs()
    skip = build.index("id: existing")

    assert skip < build.index("uv run clinvar-link-data build")
    assert f"SOURCE_URL: {Settings.model_fields['SOURCE_URL'].default}" in build
    assert './scripts/verify_release_assets.sh "$tag" /tmp/published' in build
    assert (
        "state: ${{ steps.existing.outputs.state || steps.release-state.outputs.state }}" in build
    )
    assert build.count("if: steps.existing.outputs.state != 'published_noop'") == 4
