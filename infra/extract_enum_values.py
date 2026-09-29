#!/usr/bin/env python3
"""AUDIT `atlas/data/enum_values.json` against the Atlas source tree and the live database.

This is a REPORT, not a generator. Automatic column -> enum mapping was tried and rejected:
Atlas stores these columns as plain ``String``, so the model never references the enum class, and
name-similarity matching is wrong often enough to be dangerous (it mapped
``hr_employees.status`` to ``ProjectStatus`` simply because the live values were a subset).
Corrupting a curated value list is worse than having none, so this script only tells a human what
needs attention and offers candidates to choose from.

Atlas' status/type columns are plain VARCHAR with no CHECK constraint, so the legal values live
only in Python `StrEnum` classes under `backend/app/modules/*/constants*`. The SQL generator needs
them (an unseen value is a value the model invents) and the result validator uses them to flag
illegal literals.

What it reports:
  MISSING   a categorical column with no curated values, plus candidate enums to choose from
  STALE     a curated list that does not cover the values actually present in the database
  ORPHANED  a curated entry whose column no longer exists

Exit code 1 when anything needs attention, so it can run in CI after an Atlas upgrade.

    python infra/extract_enum_values.py --atlas-dir ../atlas-erp
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

import psycopg

CATEGORICAL_RE = re.compile(
    r"(status|stage|_type$|^type$|_mode$|method|category|level|kind|basis|frequency"
    r"|normal_balance|priority|source|result|decision|partner_type|move_type)")


def collect_str_enums(atlas_dir: Path) -> dict[str, list[str]]:
    """Parse (not import) every Python file and return {EnumClassName: [values...]}.

    ast parsing avoids importing Atlas, so no database, settings or dependencies are needed.
    """
    enums: dict[str, list[str]] = {}
    for path in (atlas_dir / "backend" / "app").rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in node.bases}
            if not ({"StrEnum", "str", "Enum", "TextChoices"} & bases):
                continue
            values: list[str] = []
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant) \
                        and isinstance(stmt.value.value, str):
                    values.append(stmt.value.value)
            if values:
                enums.setdefault(node.name, values)
    return enums


def live_categorical_columns(dsn: str) -> dict[str, list[str]]:
    """{'table.column': [distinct values observed]} for varchar categorical columns."""
    out: dict[str, list[str]] = {}
    with psycopg.connect(dsn) as conn:
        cols = conn.execute(
            """SELECT table_name, column_name FROM information_schema.columns
               WHERE table_schema = 'public' AND data_type LIKE 'character%' ORDER BY 1, 2"""
        ).fetchall()
        for table, column in cols:
            if not CATEGORICAL_RE.search(column):
                continue
            rows = conn.execute(
                f'SELECT DISTINCT "{column}" FROM public."{table}" '
                f'WHERE "{column}" IS NOT NULL LIMIT 200').fetchall()
            out[f"{table}.{column}"] = sorted(r[0] for r in rows)
    return out


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if t and t not in {"fin", "sales",
            "inv", "proc", "mfg", "hr", "crm", "ps", "qm", "pm", "hsp", "core", "adm"}}


def candidate_enums(key: str, observed: list[str], enums: dict[str, list[str]],
                    limit: int = 4) -> list[str]:
    """Enums that could hold this column's values, best-named first. SUGGESTIONS ONLY."""
    table, column = key.split(".", 1)
    want = _tokens(table) | _tokens(column)
    scored: list[tuple[int, str]] = []
    for name, values in enums.items():
        if observed and not set(observed) <= set(values):
            continue  # cannot be right: it does not cover the live data
        have = _tokens(re.sub(r"(?<!^)(?=[A-Z])", "_", name))
        overlap = len(want & have)
        if overlap:
            scored.append((overlap, name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [f"{n} ({len(enums[n])} values)" for _, n in scored[:limit]]


EXCLUDED_HINT = "excluded from reporting"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--atlas-dir", default="../atlas-erp", type=Path)
    ap.add_argument("--dsn", default="postgresql://atlas:atlas@localhost:55433/atlas")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parents[1]
                    / "src/promptquery/atlas/data/enum_values.json")
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from promptquery.atlas import FREE_TEXT_CATEGORICALS
    from promptquery.atlas.setup_reporting import EXCLUDED_TABLES

    enums = collect_str_enums(args.atlas_dir)
    if not enums:
        print(f"no StrEnum classes found under {args.atlas_dir}/backend/app", file=sys.stderr)
        return 2
    columns = live_categorical_columns(args.dsn)
    curated: dict[str, list[str]] = json.loads(args.out.read_text()) if args.out.is_file() else {}

    missing, stale = [], []
    for key, observed in columns.items():
        table = key.split(".", 1)[0]
        if table in EXCLUDED_TABLES:
            continue  # no view exists, so the model never sees the column
        if key in FREE_TEXT_CATEGORICALS:
            continue  # documented free text: no value list by design
        if key not in curated:
            missing.append((key, observed, candidate_enums(key, observed, enums)))
        elif observed and not set(observed) <= set(curated[key]):
            stale.append((key, sorted(set(observed) - set(curated[key]))))
    orphaned = sorted(set(curated) - set(columns))

    print(f"{len(enums)} StrEnum classes in Atlas | {len(columns)} categorical columns in the DB "
          f"| {len(curated)} curated lists")
    for key, observed, cands in missing:
        print(f"  MISSING  {key}: live values {observed or '(no data yet)'}")
        print(f"           candidates: {', '.join(cands) or 'none — may be free text'}")
    for key, extra in stale:
        print(f"  STALE    {key}: database contains values absent from the curated list: {extra}")
    for key in orphaned:
        print(f"  ORPHANED {key}: curated list for a column that no longer exists")
    if not (missing or stale or orphaned):
        print("  OK — every reportable categorical column has an accurate value list.")
        return 0
    print("\nFix by editing src/promptquery/atlas/data/enum_values.json using the Atlas enum "
          "named above (verify it by reading backend/app/modules/<module>/constants*.py), then "
          "re-run `atlas-bot build-catalog`.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
