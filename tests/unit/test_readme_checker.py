"""Regression tests for README checker repository identity."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_checker() -> object:
    script = Path(__file__).parents[2] / "scripts" / "check_readme.py"
    spec = importlib.util.spec_from_file_location("check_readme", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repo_slug_uses_trusted_origin_in_a_differently_named_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checker = _load_checker()
    worktree = tmp_path / "clinvar-release-metadata-v056"
    worktree.mkdir()
    monkeypatch.setattr(checker, "ROOT", worktree)

    class Result:
        stdout = "https://github.com/berntpopp/clinvar-link.git\n"

    monkeypatch.setattr(checker.subprocess, "run", lambda *args, **kwargs: Result())

    assert checker.repo_slug() == "clinvar-link"
