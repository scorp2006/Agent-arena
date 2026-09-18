"""Analyze agent_decisions.jsonl and (if available) the mock evaluation DB.

Prints, per task: intent, resolution, escalation, evidence count, and — when the
mock ground truth is available — whether it PASSED and exactly why it failed.

Usage:
    python analyze_log.py
"""

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "agent_decisions.jsonl"
DB = ROOT / "mock_simulator" / "mock_arena.db"


def load_traces():
    traces = {}
    if LOG.exists():
        for line in LOG.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                traces[r["task_id"]] = r  # last write wins
            except Exception:
                pass
    return traces


def load_evals():
    evals = {}
    if DB.exists():
        try:
            c = sqlite3.connect(str(DB))
            c.row_factory = sqlite3.Row
            for r in c.execute(
                "SELECT task_id, correct, actual_resolution, expected_resolution, "
                "actual_escalation, expected_escalation, missing_evidence, diff_explanation "
                "FROM mock_task_evaluations ORDER BY id DESC"
            ):
                if r["task_id"] not in evals:  # newest first
                    evals[r["task_id"]] = dict(r)
            c.close()
        except Exception:
            pass
    return evals


def main():
    traces = load_traces()
    evals = load_evals()
    if not traces:
        print("No traces found. Run the agent first (agent_decisions.jsonl is empty).")
        return

    passed = failed = unknown = 0
    fail_rows = []
    print("=" * 90)
    print(f"{'TASK':16} {'INTENT':12} {'RESOLUTION':13} {'ESC':4} {'EV':3} {'RESULT'}")
    print("-" * 90)
    for tid in sorted(traces):
        t = traces[tid]
        e = evals.get(tid)
        res = t.get("resolution", "?")
        esc = "Y" if t.get("escalation_required") else "N"
        evn = len(t.get("evidence", []))
        if e is None:
            result = "(no ground truth)"
            unknown += 1
        elif e["correct"]:
            result = "PASS"
            passed += 1
        else:
            result = "FAIL"
            failed += 1
            fail_rows.append((tid, t, e))
        print(f"{tid:16} {t.get('intent','?'):12} {res:13} {esc:4} {evn:<3} {result}")

    print("=" * 90)
    total = passed + failed
    if total:
        print(f"SUMMARY: {passed}/{total} passed ({passed/total*100:.1f}%)"
              + (f" | {unknown} without ground truth" if unknown else ""))
    else:
        print(f"SUMMARY: {unknown} tasks logged, no ground truth available (live/submission run).")

    # --- RISK REPORT (works even with NO ground truth, e.g. live submission) ---
    risky = [(tid, traces[tid]) for tid in sorted(traces)
             if traces[tid].get("risk_level") in ("high", "medium")]
    if risky:
        print("\n" + "=" * 90)
        print("  RISK REPORT — tasks most likely to be wrong (review these first)")
        print("  (Live submissions reveal no per-task correctness; this is our triage signal.)")
        print("=" * 90)
        # high first
        for level in ("high", "medium"):
            for tid, t in risky:
                if t.get("risk_level") != level:
                    continue
                print(f"\n[{level.upper()}] {tid}  intent={t.get('intent')}  resolution={t.get('resolution')}  "
                      f"conf={t.get('confidence')}")
                print(f"  message: {t.get('message_preview','')[:100]}")
                print(f"  flags  : {t.get('risk_flags')}")
                if t.get("uncertainties"):
                    print(f"  notes  : {t['uncertainties']}")
        counts = {}
        for _, t in risky:
            counts[t.get("risk_level")] = counts.get(t.get("risk_level"), 0) + 1
        print(f"\n  Risk summary: {counts.get('high',0)} high, {counts.get('medium',0)} medium, "
              f"{len(traces)-len(risky)} low")

    if fail_rows:
        print("\n" + "=" * 90)
        print("  FAILURE DETAIL — why each task lost points")
        print("=" * 90)
        for tid, t, e in fail_rows:
            print(f"\n[{tid}]  intent={t.get('intent')}  (kw={t.get('intent_kw')} llm={t.get('intent_llm')})")
            print(f"  message : {t.get('message_preview','')[:110]}")
            print(f"  decision: resolution={t.get('resolution')} escalate={t.get('escalation_required')}")
            print(f"  expected: resolution={e['expected_resolution']} escalate={bool(e['expected_escalation'])}")
            miss = json.loads(e["missing_evidence"]) if e.get("missing_evidence") else []
            if miss:
                print(f"  MISSING EVIDENCE: {miss}")
            print(f"  diff    : {e['diff_explanation']}")
            print(f"  retrieved: txns={t.get('retrieved_transactions')} sub={t.get('retrieved_subscription')} "
                  f"cases={t.get('retrieved_cases')}")
            if t.get("uncertainties"):
                print(f"  agent noted: {t['uncertainties']}")


if __name__ == "__main__":
    main()
