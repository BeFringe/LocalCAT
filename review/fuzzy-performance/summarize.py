"""离线复算本次评审摘录；不执行产品 Gate，不签发资格。仅依赖标准库。"""
import csv
import math
from collections import defaultdict
from pathlib import Path
from statistics import median

root = Path(__file__).resolve().parent
runs = defaultdict(dict)
with (root / "query-samples.csv").open(encoding="utf-8", newline="") as source:
    for row in csv.DictReader(source):
        key = row["experiment"], row["machine"], row["path"]
        query_id = int(row["query_id"])
        assert query_id not in runs[key]
        runs[key][query_id] = int(row["elapsed_ns"]) / 1_000_000
assert len(runs) == 7

def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * fraction) - 1]

for key, samples in runs.items():
    assert set(samples) == set(range(1, 241))
    values = list(samples.values())
    print("/".join(key), f"n={len(values)} p50={percentile(values, .5):.4f}ms",
          f"p95={percentile(values, .95):.4f}ms max={max(values):.4f}ms",
          f"over500={sum(value > 500 for value in values)}")

for path in ("fts5", "fallback"):
    dev = runs["CANDIDATE", "DEV", path]
    val = runs["CANDIDATE", "VAL", path]
    for label, ids in (("all", range(1, 241)), ("near-edit", range(1, 201)),
                       ("miss", range(201, 241))):
        ratios = [val[i] / dev[i] for i in ids]
        print(path, label, f"paired VAL/DEV median={median(ratios):.6f}",
              f"p10={percentile(ratios, .1):.6f} p90={percentile(ratios, .9):.6f}")

first = runs["QOS_default-1", "VAL", "fts5"]
high = runs["QOS_high-qos", "VAL", "fts5"]
last = runs["QOS_default-2", "VAL", "fts5"]
ratios = [high[i] / ((first[i] + last[i]) / 2) for i in first]
print(f"QoS paired high/default-mean median={median(ratios):.9f}")
print(f"QoS paired default2/default1 median={median(last[i]/first[i] for i in first):.9f}")

with (root / "phase1-profile.csv").open(encoding="utf-8", newline="") as source:
    profiles = list(csv.DictReader(source))
assert len(profiles) == 8
assert all(row["results_equal"] == "True" for row in profiles)
ratios = [float(row["frontier_ms"]) / float(row["instrumented_total_ms"]) for row in profiles]
print(f"DEV source frontier/instrumented total: {min(ratios):.2%}..{max(ratios):.2%}")
