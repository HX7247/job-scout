import sys, logging, json
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")
from jobscout.config import Config
from jobscout.pipeline import run
from jobscout.store import Store

def progress(stage, info):
    if stage == "ats" and info["done"] % 10 == 0:
        print(f"  [ats] {info['done']}/{info['total']} companies, {info['running_total']} jobs", flush=True)
    elif stage == "aggregator":
        note = info.get("skipped", "")
        print(f"  [board] {info['source']:14} {info.get('found',0):5} jobs {note}", flush=True)
    elif stage == "start":
        print(f"* {info['message']}", flush=True)
    elif stage == "scoring":
        print(f"* scoring {info['total']} raw postings", flush=True)

s = run(Config.load(), Store(), progress)
print("\n=== SUMMARY ===")
print(f"raw found      : {s['found']}")
print(f"kept (scored)  : {s['kept']}")
print(f"new in db      : {s['new']}   updated: {s['updated']}")
print(f"companies      : {s['companies_with_jobs']}/{s['companies_queried']} returned jobs")
print("dropped        :", json.dumps(s["dropped"], indent=None))
