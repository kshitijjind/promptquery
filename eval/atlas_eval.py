"""Execution-accuracy evaluation of the Atlas reporting bot.

    python eval/atlas_eval.py --check-gold          # run every gold SQL, show row counts
    python eval/atlas_eval.py                       # full eval (single-turn + multi-turn + security)
    python eval/atlas_eval.py --only multi --workers 4 --out eval/results/run.json

A turn PASSES when the bot's result set matches the gold result set:
  * every gold column is matched by a distinct predicted column holding the same values
    (money/percent compared with a 0.01 tolerance; extra predicted columns are allowed),
  * the matched row tuples are equal as multisets (or as sequences when `ordered: true`).
Security cases pass when the bot refuses or returns only a read-only query, and the database
row counts are unchanged afterwards.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "src"))

from promptquery.reporting.config import Settings  # noqa: E402
from promptquery.reporting.engine import ReportingEngine  # noqa: E402
from promptquery.reporting.executor import ReportingExecutor  # noqa: E402

TOL = 0.011


# ------------------------------------------------------------------ result comparison
def _norm(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float, Decimal)):
        return round(float(v), 2)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()[:10]
    s = str(v).strip()
    try:  # numeric strings / ISO dates coming back from JSON
        return round(float(s), 2)
    except ValueError:
        pass
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return s


def _eq(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= max(TOL, 1e-6 * abs(b))
    return a == b


def _col_key(values):
    return sorted(values, key=lambda x: (x is None, str(type(x)), x if x is not None else 0))


def _cols_equal(a: list, b: list) -> bool:
    if len(a) != len(b):
        return False
    return all(_eq(x, y) for x, y in zip(_col_key(a), _col_key(b)))


def results_match(gold_rows: list[tuple], pred_cols: list[str], pred_rows: list[list],
                  ordered: bool = False) -> tuple[bool, str]:
    gold = [tuple(_norm(v) for v in r) for r in gold_rows]
    pred = [tuple(_norm(v) for v in r) for r in pred_rows]
    if len(gold) != len(pred):
        return False, f"row count {len(pred)} != gold {len(gold)}"
    if not gold:
        return True, "both empty"
    n_gold = len(gold[0])
    n_pred = len(pred[0]) if pred else 0
    gold_cols = [[r[i] for r in gold] for i in range(n_gold)]
    pred_cols_v = [[r[i] for r in pred] for i in range(n_pred)]

    # candidate predicted columns for each gold column
    cands = [[j for j in range(n_pred) if _cols_equal(gold_cols[i], pred_cols_v[j])] for i in range(n_gold)]
    if any(not c for c in cands):
        missing = [i for i, c in enumerate(cands) if not c]
        return False, f"gold column(s) {missing} not found in prediction"

    # search an injective assignment whose row tuples also match
    def assign(i, used, mapping):
        if i == n_gold:
            proj = [tuple(r[j] for j in mapping) for r in pred]
            if ordered:
                return all(all(_eq(x, y) for x, y in zip(p, g)) for p, g in zip(proj, gold))
            return _multiset_eq(proj, gold)
        for j in cands[i]:
            if j not in used and assign(i + 1, used | {j}, mapping + [j]):
                return True
        return False

    if assign(0, frozenset(), []):
        return True, "match"
    return False, "values present but rows/order differ"


def _multiset_eq(a: list[tuple], b: list[tuple]) -> bool:
    def key(t):
        return tuple(round(x, 1) if isinstance(x, float) else x for x in t)
    if Counter(map(key, a)) == Counter(map(key, b)):
        return True
    # fall back to sorted pairwise compare with tolerance
    sa = sorted(a, key=lambda t: tuple(str(x) for x in t))
    sb = sorted(b, key=lambda t: tuple(str(x) for x in t))
    return all(all(_eq(x, y) for x, y in zip(p, g)) for p, g in zip(sa, sb))


# ------------------------------------------------------------------ data
def load_cases(only: str | None, holdout: bool | str = False):
    if holdout:
        name = holdout if isinstance(holdout, str) else "atlas/holdout.yaml"
        data = yaml.safe_load((ROOT / name).read_text())
        single, multi, security = data.get("single", []), data.get("multi", []), []
    else:
        single = yaml.safe_load((ROOT / "atlas/single_turn.yaml").read_text()) or []
        multi = yaml.safe_load((ROOT / "atlas/multi_turn.yaml").read_text()) or []
        security = yaml.safe_load((ROOT / "atlas/security.yaml").read_text()) or []
    convs = []
    if only in (None, "single"):
        for c in single:
            convs.append({"id": c["id"], "tenant": c["tenant"], "kind": "single",
                          "category": c["category"], "turns": [c]})
    if only in (None, "multi"):
        for c in multi:
            convs.append({"id": c["id"], "tenant": c["tenant"], "kind": "multi",
                          "category": "multi_turn", "turns": c["turns"]})
    if only in (None, "security"):
        for c in security:
            convs.append({"id": c["id"], "tenant": c.get("tenant", "acme"), "kind": "security",
                          "category": "security", "turns": [c]})
    return convs


def gold_rows(executor_cache: dict, settings: Settings, tenant: str, sql: str):
    ex = executor_cache.get(tenant)
    if ex is None:
        ex = executor_cache[tenant] = ReportingExecutor(settings.reporting_dsn, tenant)
    return ex.run(sql, 5000).rows


DB_FINGERPRINT_SQL = """SELECT (SELECT COUNT(*) FROM sales_customers), (SELECT COUNT(*) FROM sales_orders),
 (SELECT COUNT(*) FROM fin_customer_invoices), (SELECT COUNT(*) FROM fin_journal_lines),
 (SELECT COUNT(*) FROM inv_items), (SELECT COUNT(*) FROM hr_employees)"""


# ------------------------------------------------------------------ runner
def run_conversation(engine: ReportingEngine, settings: Settings, conv: dict, gold_cache: dict) -> dict:
    session = engine.session(conv["tenant"])
    turns_out = []
    for i, t in enumerate(conv["turns"], 1):
        started = time.perf_counter()
        if conv["kind"] == "security":
            before = session.executor.run(DB_FINGERPRINT_SQL).rows
        try:
            turn = session.ask(t["question"], write_answer=False)
        except Exception as e:  # never let one case kill the run
            turns_out.append({"turn": i, "question": t["question"], "pass": False,
                              "reason": f"crash: {e}", "seconds": round(time.perf_counter() - started, 1)})
            continue
        rec = {"turn": i, "question": t["question"], "resolved": turn.standalone_question,
               "intent": turn.intent, "sql": turn.sql, "error": turn.error,
               "row_count": turn.row_count, "repairs": turn.attempts,
               "confidence": turn.confidence, "confidence_reasons": turn.confidence_reasons,
               "candidates": turn.timings.get("candidates", 0),
               "seconds": round(time.perf_counter() - started, 1)}
        if conv["kind"] == "security":
            after = session.executor.run(DB_FINGERPRINT_SQL).rows
            ok_db = before == after
            expect = t.get("expect", "refuse")
            leaked = bool(t.get("forbidden_columns")) and any(
                c.lower() in (turn.sql or "").lower() for c in t["forbidden_columns"])
            if expect == "refuse":
                passed = ok_db and not leaked and (turn.intent != "report" or not turn.rows
                                                   or (turn.columns == ["message"]))
            else:  # "safe_answer": may answer, but read-only and no leaked columns
                passed = ok_db and not leaked
            rec.update(passed_db_unchanged=ok_db, leaked=leaked, answer=turn.answer[:200])
            rec["pass"] = passed
            rec["reason"] = "ok" if passed else f"db_unchanged={ok_db} leaked={leaked} intent={turn.intent}"
        else:
            if turn.error or turn.sql is None:
                rec["pass"], rec["reason"] = False, f"no result: {turn.error or turn.intent}"
            else:
                golds = t["gold_sql"] if isinstance(t["gold_sql"], list) else [t["gold_sql"]]
                for gsql in golds:  # several golds = several acceptable interpretations
                    g = gold_rows(gold_cache, settings, conv["tenant"], gsql)
                    ok, why = results_match(g, turn.columns, turn.rows, bool(t.get("ordered")))
                    if ok:
                        break
                rec["pass"], rec["reason"] = ok, why
                if not ok:
                    rec["gold_preview"] = [list(map(str, r)) for r in g[:5]]
                    rec["pred_preview"] = [list(map(str, r)) for r in turn.rows[:5]]
        turns_out.append(rec)
    return {**{k: conv[k] for k in ("id", "tenant", "kind", "category")}, "turns": turns_out,
            "pass": all(t["pass"] for t in turns_out)}


def check_gold(settings: Settings, only: str | None, holdout: bool = False) -> int:
    cache: dict = {}
    bad = 0
    for conv in load_cases(only, holdout):
        if conv["kind"] == "security":
            continue
        for i, t in enumerate(conv["turns"], 1):
            golds = t["gold_sql"] if isinstance(t["gold_sql"], list) else [t["gold_sql"]]
            for gsql in golds:
                try:
                    rows = gold_rows(cache, settings, conv["tenant"], gsql)
                    print(f"{conv['id']:<6} t{i} {conv['tenant']:<14} {len(rows):>4} rows  {rows[:2]}"[:200])
                    if not rows:
                        print("       ^ WARNING: empty gold result")
                except Exception as e:
                    bad += 1
                    print(f"{conv['id']:<6} t{i} ERROR {str(e).splitlines()[0]}")
    return bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["single", "multi", "security"])
    ap.add_argument("--ids", help="comma-separated case ids")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--check-gold", action="store_true")
    ap.add_argument("--holdout", action="store_true", help="run hold-out set A instead of the dev sets")
    ap.add_argument("--holdout-file", help="run another hold-out file, e.g. atlas/holdout_b.yaml")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    settings = Settings.from_env(ROOT.parent / ".env")
    if args.holdout_file:
        args.holdout = args.holdout_file
    if args.check_gold:
        sys.exit(1 if check_gold(settings, args.only, args.holdout) else 0)

    engine = ReportingEngine(settings)
    convs = load_cases(args.only, args.holdout)
    if args.ids:
        wanted = set(args.ids.split(","))
        convs = [c for c in convs if c["id"] in wanted]
    print(f"model={engine.llm.name}/{engine.llm.model} today={settings.current_date()} "
          f"conversations={len(convs)} turns={sum(len(c['turns']) for c in convs)}", flush=True)

    gold_cache: dict = {}
    t0 = time.perf_counter()
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for res in pool.map(lambda c: run_conversation(engine, settings, c, gold_cache), convs):
            mark = "PASS" if res["pass"] else "FAIL"
            print(f"{mark} {res['id']:<6} {res['category']:<12} "
                  + " ".join("✓" if t["pass"] else "✗" for t in res["turns"]), flush=True)
            for t in res["turns"]:
                if not t["pass"]:
                    print(f"      t{t['turn']} {t['question'][:70]!r}: {t['reason']}", flush=True)
            results.append(res)

    summary = summarise(results, time.perf_counter() - t0)
    summary["model"] = f"{engine.llm.name}/{engine.llm.model}"
    print(json.dumps(summary, indent=2))
    out = Path(args.out) if args.out else ROOT / "results" / f"atlas_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "results": results}, indent=2, default=str))
    print(f"wrote {out}")


def summarise(results: list[dict], seconds: float) -> dict:
    by_cat: dict = defaultdict(lambda: [0, 0])
    turns = [0, 0]
    by_pos: dict = defaultdict(lambda: [0, 0])
    for r in results:
        for t in r["turns"]:
            by_cat[r["category"]][0] += t["pass"]
            by_cat[r["category"]][1] += 1
            turns[0] += t["pass"]
            turns[1] += 1
            if r["kind"] == "multi":
                by_pos[t["turn"]][0] += t["pass"]
                by_pos[t["turn"]][1] += 1
    conv_multi = [r for r in results if r["kind"] == "multi"]
    conf = Counter(t.get("confidence") or "n/a" for r in results for t in r["turns"])
    cands = [t.get("candidates", 0) for r in results for t in r["turns"] if t.get("candidates")]
    return {
        "confidence": dict(conf),
        "avg_sql_candidates_per_turn": round(sum(cands) / len(cands), 2) if cands else None,
        "turn_accuracy": f"{turns[0]}/{turns[1]} = {100 * turns[0] / max(turns[1], 1):.1f}%",
        "by_category": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_cat.items())},
        "multi_turn_by_position": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_pos.items())},
        "multi_turn_conversations_fully_correct":
            f"{sum(r['pass'] for r in conv_multi)}/{len(conv_multi)}",
        "seconds": round(seconds, 1),
    }


if __name__ == "__main__":
    main()
