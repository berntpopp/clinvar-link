"""Summarise one agent UX round: ratings, MCP calls, tokens and wall time per session.

    python3 scripts/agent_ux_scores.py <round-dir>

Writes <round-dir>/scores.json and prints a Markdown table plus the per-category minimum
(the gate: every category's minimum must be >= 80). See docs/benchmarks/agent-ux-protocol.md.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CATEGORIES = [
    "discoverability",
    "schema_clarity",
    "correctness_confidence",
    "completeness",
    "error_recovery",
    "speed",
    "token_efficiency",
    "provenance_citation",
    "overall_usability",
]
GATE = 80


def _events(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(errors="replace").splitlines() if path.exists() else []:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def session(d: Path) -> dict:
    agent = d.name.rsplit("-", 1)[1]
    final, calls, tokens, errors = "", None, None, None
    if agent == "claude":
        ev = _events(d / "trace.jsonl")
        calls = sum(
            1
            for e in ev
            if e.get("type") == "assistant"
            for c in e["message"].get("content", [])
            if c.get("type") == "tool_use" and c.get("name", "").startswith("mcp__")
        )
        errors = sum(
            1
            for e in ev
            if e.get("type") == "user"
            for c in e["message"].get("content", [])
            if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error")
        )
        res = next((e for e in ev if e.get("type") == "result"), {})
        final = res.get("result") or ""
        u = res.get("usage") or {}
        tokens = (
            sum(
                u.get(k, 0)
                for k in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                    "output_tokens",
                )
            )
            or None
        )
    else:
        final = (d / "final.txt").read_text(errors="replace") if (d / "final.txt").exists() else ""
    if agent == "codex":
        ev = _events(d / "trace.jsonl")
        calls = sum(
            1
            for e in ev
            if e.get("type") == "item.completed" and e["item"].get("type") == "mcp_tool_call"
        )
        errors = sum(
            1
            for e in ev
            if e.get("type") == "item.completed"
            and e["item"].get("type") == "mcp_tool_call"
            and e["item"].get("status") == "failed"
        )
        u = next((e["usage"] for e in ev if e.get("type") == "turn.completed"), {})
        tokens = (u.get("input_tokens", 0) + u.get("output_tokens", 0)) or None
    run = (d / "run.txt").read_text() if (d / "run.txt").exists() else ""
    wall = re.search(r"wall_s=([\d.]+)", run)
    ratings = {}
    for cat in CATEGORIES:
        m = re.search(rf"{cat}\**\s*[=:]\s*\**\s*(\d{{1,3}})", final)
        ratings[cat] = int(m.group(1)) if m else None
    untested = []
    if errors == 0:  # no error was observed: its error_recovery rating measures nothing
        untested.append("error_recovery")
    return {
        "session": d.name,
        "rc": (re.search(r"rc=(\d+)", run) or [None, None])[1],
        "wall_s": round(float(wall.group(1))) if wall else None,
        "mcp_calls": calls,
        "client_tokens": tokens,
        "error_results": errors,
        "untested": untested,
        "ratings": ratings,
    }


def main(root: Path) -> None:
    rows = [session(d) for d in sorted(root.iterdir()) if (d / "prompt.txt").exists()]
    mins = {
        c: min(
            (
                r["ratings"][c]
                for r in rows
                if r["ratings"][c] is not None and c not in r["untested"]
            ),
            default=None,
        )
        for c in CATEGORIES
    }
    (root / "scores.json").write_text(json.dumps({"sessions": rows, "min": mins}, indent=1))
    short = [c.split("_")[0][:6] for c in CATEGORIES]
    print("| session | rc | wall s | calls | tokens | " + " | ".join(short) + " |")
    print("|---" * (5 + len(CATEGORIES)) + "|")
    for r in rows:
        vals = " | ".join(
            f"({r['ratings'][c]})" if c in r["untested"] else str(r["ratings"][c])
            for c in CATEGORIES
        )
        print(
            f"| {r['session']} | {r['rc']} | {r['wall_s']} | {r['mcp_calls']} | "
            f"{r['client_tokens']} | {vals} |"
        )
    print("| **min** | | | | | " + " | ".join(f"**{mins[c]}**" for c in CATEGORIES) + " |")
    missing = [r["session"] for r in rows if None in r["ratings"].values()]
    failed = [c for c in CATEGORIES if mins[c] is None or mins[c] < GATE]
    print("\n(n) = category not exercised in that session (no error result): excluded")
    print(f"incomplete ratings: {missing or 'none'}")
    print(
        f"gate (every min >= {GATE}): {'PASS' if not failed and not missing else 'FAIL ' + str(failed)}"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
