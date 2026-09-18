"""Contamination check: how close are a fleet's solver diffs to the gold patches?

For every resolved attempt in a fleet, compares the solver's unified diff with the
bundle's ``patch.diff``:

- ``files``: Jaccard overlap of the sets of files touched (tests/changelogs excluded).
- ``gold_lines_reproduced``: fraction of the gold patch's added code lines
  (whitespace-normalized, comments/blank dropped) that appear verbatim among the
  solver's added lines. This is the memorization signal: a genuinely independent fix
  rarely reproduces most of the reference lines character for character.
- ``similarity``: difflib ratio over the two diffs' normalized added+removed lines.

Usage: uv run python scripts/diff_similarity.py <fleet-id> [--json out.json]
"""

import argparse
import difflib
import json
import re
import sqlite3
import sys
from pathlib import Path

NOISE_PATHS = re.compile(r"(^|/)(tests?|testing|changelogs?|docs?)(/|$)|\.md$|\.rst$|\.txt$")


def touched_files(diff: str) -> set[str]:
    files = set()
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[-1] if " b/" in line else line.split()[-1]
            files.add(path.removeprefix("b/"))
    return {f for f in files if not NOISE_PATHS.search(f)}


def code_lines(diff: str, sign: str, files: set[str]) -> list[str]:
    """Normalized added ('+') or removed ('-') lines, restricted to ``files``."""
    out: list[str] = []
    current = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            name = line.split(" b/", 1)[-1] if " b/" in line else line.split()[-1]
            current = name.removeprefix("b/")
            continue
        if current not in files or line.startswith(("+++", "---")):
            continue
        if line.startswith(sign):
            body = re.sub(r"\s+", " ", line[1:]).strip()
            if body and not body.startswith("#"):
                out.append(body)
    return out


def compare(solver_diff: str, gold_diff: str) -> dict[str, float]:
    s_files, g_files = touched_files(solver_diff), touched_files(gold_diff)
    files = len(s_files & g_files) / len(s_files | g_files) if s_files | g_files else 0.0
    shared = s_files | g_files
    s_add, g_add = code_lines(solver_diff, "+", shared), code_lines(gold_diff, "+", shared)
    reproduced = (sum(1 for line in g_add if line in set(s_add)) / len(g_add)) if g_add else 0.0
    s_all = s_add + code_lines(solver_diff, "-", shared)
    g_all = g_add + code_lines(gold_diff, "-", shared)
    sim = difflib.SequenceMatcher(None, s_all, g_all).ratio() if s_all and g_all else 0.0
    return {
        "files": round(files, 2),
        "gold_lines_reproduced": round(reproduced, 2),
        "similarity": round(sim, 2),
        "solver_added": len(s_add),
        "gold_added": len(g_add),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("fleet_id")
    ap.add_argument("--db", default=str(Path.home() / ".task-bundle/task.db"))
    ap.add_argument("--artifacts", default=str(Path.home() / ".task-bundle/artifacts"))
    ap.add_argument("--bundles", default="bundles")
    ap.add_argument("--json")
    args = ap.parse_args()
    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row
    cmd = db.execute("SELECT command_id FROM fleets WHERE id=?", (args.fleet_id,)).fetchone()
    if cmd is None:
        sys.exit(f"no fleet {args.fleet_id}")
    jobs = db.execute(
        "SELECT run_id, task_id, sample, verdict FROM fleet_jobs"
        " WHERE fleet_id=? ORDER BY task_id, sample",
        (args.fleet_id,),
    ).fetchall()
    rows = []
    for job in jobs:
        if job["verdict"] != "RESOLVED":
            continue
        report = Path(args.artifacts) / cmd["command_id"] / job["run_id"] / "report.json"
        gold = Path(args.bundles) / job["task_id"] / "patch.diff"
        if not report.is_file() or not gold.is_file():
            continue
        solver_diff = json.loads(report.read_text())["diff"]
        rows.append(
            {
                "task": job["task_id"],
                "sample": job["sample"],
                **compare(solver_diff, gold.read_text()),
            }
        )
    print(f"{'task':28s} s  files  gold_repro  sim   +solver/+gold")
    for r in rows:
        verbatim = r["gold_lines_reproduced"] >= 0.8 and r["similarity"] >= 0.8
        flag = " <-- near-verbatim" if verbatim else ""
        print(
            f"{r['task']:28s} {r['sample']}  {r['files']:.2f}   {r['gold_lines_reproduced']:.2f}"
            f"      {r['similarity']:.2f}  {r['solver_added']:3d}/{r['gold_added']:<3d}{flag}"
        )
    n = len(rows)
    if n:
        hi = sum(1 for r in rows if r["gold_lines_reproduced"] >= 0.8 and r["similarity"] >= 0.8)
        mid = sum(1 for r in rows if 0.5 <= r["gold_lines_reproduced"] < 0.8)
        print(
            f"\n{n} resolved attempts: near-verbatim (>=80% gold lines AND sim>=0.8): {hi}; "
            f"partial (50-80% lines): {mid}; independent (<50%): {n - hi - mid}"
        )
        print(
            f"mean gold_lines_reproduced={sum(r['gold_lines_reproduced'] for r in rows) / n:.2f} "
            f"mean similarity={sum(r['similarity'] for r in rows) / n:.2f} "
            f"mean file overlap={sum(r['files'] for r in rows) / n:.2f}"
        )
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2) + "\n")


if __name__ == "__main__":
    main()
