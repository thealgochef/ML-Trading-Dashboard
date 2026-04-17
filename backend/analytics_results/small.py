import csv
from collections import defaultdict
from pathlib import Path

CSV_PATH = Path("analytics_results/20260328_225724_prediction_analytics_detailed.csv")

def to_float(x: str) -> float:
    try:
        return float(x)
    except Exception:
        return 0.0

def summarize(grouped: dict[str, list[dict]], label: str) -> None:
    print(f"\n=== {label} ===")
    for key in sorted(grouped):
        grp = grouped[key]
        n = len(grp)
        wins = sum(1 for r in grp if r["tp15_sl30_exit_reason"] == "tp")
        losses = sum(1 for r in grp if r["tp15_sl30_exit_reason"] == "sl")
        session_end = sum(1 for r in grp if r["tp15_sl30_exit_reason"] == "session_end")
        denom = wins + losses
        wr = wins / denom if denom else 0.0
        ev = sum(to_float(r["tp15_sl30_pnl_points"]) for r in grp) / n if n else 0.0
        avg_rev_prob = sum(to_float(r["reversal_probability"]) for r in grp) / n if n else 0.0
        print(
            f"{key}: "
            f"n={n}, WR15/30={wr:.3f}, EV15/30={ev:.2f}, "
            f"wins={wins}, losses={losses}, session_end={session_end}, "
            f"avg_rev_prob={avg_rev_prob:.3f}"
        )

with CSV_PATH.open(newline="", encoding="utf-8") as f:
    all_rows = list(csv.DictReader(f))

rows = [
    r for r in all_rows
    if r["resolution_mode"] == "optimistic"
    and r["is_executable"] == "True"
]

print(f"CSV: {CSV_PATH}")
print(f"Total rows in file: {len(all_rows)}")
print(f"Executable optimistic rows: {len(rows)}")

by_level = defaultdict(list)
by_conf = defaultdict(list)
by_session = defaultdict(list)
by_session_level = defaultdict(list)

for r in rows:
    level = r.get("level_type") or "unknown"
    conf = r.get("confidence_bucket") or "unknown"
    session = r.get("session") or "unknown"
    by_level[level].append(r)
    by_conf[conf].append(r)
    by_session[session].append(r)
    by_session_level[f"{session} | {level}"].append(r)

summarize(by_level, "LEVEL TYPE")
summarize(by_conf, "CONFIDENCE BUCKET")
summarize(by_session, "SESSION")

print("\n=== SESSION x LEVEL TYPE (n >= 5) ===")
for key in sorted(by_session_level):
    grp = by_session_level[key]
    if len(grp) < 5:
        continue
    n = len(grp)
    wins = sum(1 for r in grp if r["tp15_sl30_exit_reason"] == "tp")
    losses = sum(1 for r in grp if r["tp15_sl30_exit_reason"] == "sl")
    session_end = sum(1 for r in grp if r["tp15_sl30_exit_reason"] == "session_end")
    denom = wins + losses
    wr = wins / denom if denom else 0.0
    ev = sum(to_float(r["tp15_sl30_pnl_points"]) for r in grp) / n if n else 0.0
    print(
        f"{key}: "
        f"n={n}, WR15/30={wr:.3f}, EV15/30={ev:.2f}, "
        f"wins={wins}, losses={losses}, session_end={session_end}"
    )