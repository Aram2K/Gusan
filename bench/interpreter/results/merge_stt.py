"""Merge several stt_*.json results (e.g. a clean run and a noisy,speakers run) into one, with the summary
recomputed, so report.py can show every variant side by side.

    python results/merge_stt.py results/stt_A.json results/stt_B.json [-o results/stt_<stamp>.json]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import run_stt  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("-o", "--out")
    a = ap.parse_args()
    runs = [json.load(open(f, encoding="utf-8")) for f in a.files]
    convs = {}
    order = []
    for run in runs:
        for c in run["conversations"]:
            if c["id"] not in convs:
                convs[c["id"]] = dict(c, jobs=[])
                order.append(c["id"])
            convs[c["id"]]["jobs"].extend(c["jobs"])
    merged_convs = [convs[i] for i in order]
    cfg = dict(runs[0]["config"])
    for key in ("engines", "inputs", "modes", "variants"):
        seen = []
        for run in runs:
            for v in run["config"].get(key) or []:
                if v not in seen:
                    seen.append(v)
        cfg[key] = seen
    cfg["merged_from"] = [os.path.basename(f) for f in a.files]
    cfg["wall_s"] = round(sum(float(r["config"].get("wall_s") or 0) for r in runs), 1)
    mins = {}
    for run in runs:
        for e, m in (run["config"].get("audio_min_by_engine") or {}).items():
            mins[e] = round(mins.get(e, 0) + m, 1)
    cfg["audio_min_by_engine"] = mins
    deltas = [((r["config"].get("elevenlabs_usage") or {}).get("delta")) for r in runs]
    if all(isinstance(d, (int, float)) for d in deltas):
        last = (runs[-1]["config"].get("elevenlabs_usage") or {})
        cfg["elevenlabs_usage"] = {"before": (runs[0]["config"].get("elevenlabs_usage") or {}).get("before"),
                                   "after": last.get("after"), "delta": sum(deltas)}
    data = {"kind": "stt", "stamp": None, "git": runs[-1].get("git"), "config": cfg,
            "conversations": merged_convs, "summary": run_stt.summarize(merged_convs)}
    if a.out:
        data["stamp"] = os.path.splitext(os.path.basename(a.out))[0].split("_", 1)[-1]
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        path = a.out
    else:
        path = run_stt.save_results(HERE, "stt", data)
    s = data["summary"]
    print(f"merged {len(a.files)} runs: {s['n_jobs']} jobs, {s['n_errors']} errors -> {path}")


if __name__ == "__main__":
    main()
