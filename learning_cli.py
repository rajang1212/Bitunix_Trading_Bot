from __future__ import annotations

import json
from pathlib import Path
import sys

from config import load_config
from learning import run_daily_learning
from swing_learning import run_swing_daily_learning
from store import DayTraderStore, data_directory


def show_status() -> None:
    d = data_directory(); store = DayTraderStore(d / "daytrader.sqlite3")
    try:
        day = store.learning_counts(); swing = store.swing_learning_counts()
        print("Bitunix Multi-Agent v1.6 - LEARNING STATUS")
        print(f"Data: {d}")
        print(f"DAY   examples: {day['resolved']} resolved | {day['pending']} pending | {day['total']} total | horizon 24h")
        print(f"SWING examples: {swing['resolved']} resolved | {swing['pending']} pending | {swing['total']} total | horizon 7d")
        for label, key in (("DAY", "latest_learning_report"), ("SWING", "latest_swing_learning_report")):
            latest = store.meta_get(key)
            print(f"\n{label} latest report: {latest or 'none'}")
            if latest and Path(latest).exists():
                print(Path(latest).read_text(encoding="utf-8"))
    finally:
        store.close()


def export() -> None:
    d=data_directory(); store=DayTraderStore(d/"daytrader.sqlite3")
    try:
        outd=d/"exports"/"day_learning_examples.csv"; outs=d/"exports"/"swing_learning_examples.csv"
        nd=store.export_examples_csv(outd); ns=store.export_swing_examples_csv(outs)
        print(f"Exported DAY   {nd} examples to {outd}")
        print(f"Exported SWING {ns} examples to {outs}")
    finally:
        store.close()


def run_both(force: bool) -> dict:
    d=data_directory(); cfg=load_config(d/"settings.json")
    day=run_daily_learning(d,cfg,force=force)
    swing=run_swing_daily_learning(d,cfg,force=force) if cfg.swing_learning.enabled else {"status":"DISABLED"}
    return {"day":day.get("status"),"swing":swing.get("status"),"report_dir":str(d/"reports")}


def main() -> None:
    cmd=(sys.argv[1] if len(sys.argv)>1 else "daily").lower()
    if cmd=="daily": print(json.dumps(run_both(False),indent=2))
    elif cmd in {"now","force"}: print(json.dumps(run_both(True),indent=2))
    elif cmd=="status": show_status()
    elif cmd=="export": export()
    else: raise SystemExit("Usage: python learning_cli.py [daily|now|status|export]")


if __name__=="__main__": main()
