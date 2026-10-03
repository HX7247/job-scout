import sys, logging, json, time
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")
from jobscout.config import Config
from jobscout.pipeline import run
from jobscout.store import Store

t0 = time.time()
def progress(stage, info):
    if stage == "ats" and info["done"] % 10 == 0:
        print(f"  [ats] {info['done']}/{info['total']} companies, {info['running_total']} jobs", flush=True)
    elif stage == "aggregator":
        print(f"  [board] {info['source']:14} {info.get('found',0):5} {info.get('skipped','')}", flush=True)
    elif stage == "start":
        print(f"* {info['message']}", flush=True)
    elif stage == "scoring":
        print(f"* scoring {info['total']} raw postings", flush=True)

s = run(Config.load(), Store(), progress)
print("\n=== SUMMARY ===")
for k in ("found","kept","new","updated","companies_queried","companies_with_jobs","sponsored"):
    print(f"{k:22}: {s.get(k)}")
print("dropped:", json.dumps(s["dropped"]))
print("per_source:", json.dumps(s["per_source"]))
print(f"elapsed: {time.time()-t0:.0f}s")
