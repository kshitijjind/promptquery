"""Baseline: the ORIGINAL PromptQuery pipeline (cli.run_question) on the dev single-turn set.

It connects the way the prototype would: directly to the Atlas database (public schema, owner
DSN, read-only session), with no semantic layer, no tenant scoping and no conversation state.
The tenant is named in the question ("For the company with slug 'acme': ...") so the model at
least has a chance to filter on tenant_id. Scored with the same comparator as atlas_eval.py.

    python eval/atlas_baseline.py --workers 3
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "src"))

from atlas_eval import gold_rows, results_match  # noqa: E402

from promptquery.cli import Outcome, run_question  # noqa: E402
from promptquery.db import Database  # noqa: E402
from promptquery.llm import make_client  # noqa: E402
from promptquery.reporting.config import Settings  # noqa: E402
from promptquery.retrieval import TfIdfRetriever  # noqa: E402
from promptquery.schema import introspect  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    settings = Settings.from_env(ROOT.parent / ".env")
    llm = make_client(settings.llm)

    with Database(settings.admin_dsn) as db:
        schema = introspect(db)
    schema.tables = [t for t in schema.tables if t.schema == "public"]
    retriever = TfIdfRetriever(schema)
    cases = yaml.safe_load((ROOT / "atlas/single_turn.yaml").read_text())
    gold_cache: dict = {}

    def run(case):
        q = f"For the company with slug '{case['tenant']}' (adm_tenants.slug): {case['question']}"
        with Database(settings.admin_dsn) as db:
            res = run_question(q, schema, retriever, llm, llm, db, top_k=50, select_n=15,
                               max_tables=25, confirm=False, max_repair=1)
        if res.outcome != Outcome.OK:
            return case["id"], False, f"{res.outcome.value}: {(res.error or '')[:100]}"
        golds = case["gold_sql"] if isinstance(case["gold_sql"], list) else [case["gold_sql"]]
        for g in golds:
            ok, why = results_match(gold_rows(gold_cache, settings, case["tenant"], g),
                                    res.cols, [list(r) for r in res.rows], bool(case.get("ordered")))
            if ok:
                return case["id"], True, "match"
        return case["id"], False, why

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run, cases))
    for cid, ok, why in results:
        print(("PASS " if ok else "FAIL ") + cid + ("" if ok else f"  {why}"))
    passed = sum(ok for _, ok, _ in results)
    summary = {"baseline_turn_accuracy": f"{passed}/{len(results)} = {100 * passed / len(results):.1f}%"}
    print(json.dumps(summary))
    out = ROOT / "results" / "baseline_promptquery.json"
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))


if __name__ == "__main__":
    main()
