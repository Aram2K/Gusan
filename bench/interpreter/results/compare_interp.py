"""Side-by-side table of several interpreter runs (results/interpreter_*.json): per direction the
accuracy (WER, chrF++, the judge), the delays after the speaker stops (text committed, first translated
words, translation done, first audio heard: p50 / p90), gender match, echo and what went wrong.

    python results/compare_interp.py results/interpreter_A.json results/interpreter_B.json ...
    python results/compare_interp.py --all          # every real run in results/, oldest first
"""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def lat(s, key, q):
    v = ((s.get("latency") or {}).get(key) or {}).get(q)
    return "-" if v is None else "%.2f" % v


def row(path):
    d = json.load(open(path, encoding="utf-8"))
    c = d.get("config") or {}
    s = d.get("summary") or {}
    name = c.get("label") or os.path.basename(path)
    what = "%s %s vad%s %s%s" % (c.get("variant"), c.get("route"), c.get("vad"),
                                 "text" if c.get("captions_only") else "voice x%s" % c.get("tts_sample"),
                                 " only=" + ",".join(c.get("only") or []) if c.get("only") else "")
    out = []
    for direction in ("hear", "speak"):
        x = s.get(direction) or {}
        j = x.get("judge") or {}
        g = x.get("gender") or {}
        cnt = x.get("counts") or {}
        inp = x.get("stt_input") or {}
        out.append([name if direction == "hear" else "", what if direction == "hear" else "", direction,
                    "%.1f%%" % (100 * (x.get("wer") or 0)), "%.1f" % (x.get("chrf") or 0),
                    "%s/%s" % (j.get("adequacy", "-"), j.get("fluency", "-")),
                    lat(x, "stt", "p50"), lat(x, "first_delta", "p50"), lat(x, "final", "p50"),
                    lat(x, "final", "p90"), lat(x, "ear", "p50"), lat(x, "ear", "p90"),
                    "%.0f%%" % (100 * g["accuracy"]) if g.get("accuracy") is not None else "-",
                    str(cnt.get("dropped_turns", "-")), "%.0f%%" % (100 * (inp.get("muted_ratio") or 0)),
                    str(s.get("echo_loops", "-")) if direction == "hear" else "",
                    str(s.get("crosstalk", "-")) if direction == "hear" else "",
                    str(s.get("errors", "-")) if direction == "hear" else ""])
    return out


def main(argv):
    paths = argv
    if not paths or paths == ["--all"]:
        paths = sorted(glob.glob(os.path.join(HERE, "interpreter_*.json")))
        paths = [p for p in paths if not (json.load(open(p, encoding="utf-8")).get("config") or {}).get("dry_run")]
    head = ["run", "setup", "dir", "WER", "chrF", "judge a/f", "text p50", "1st words", "done p50", "done p90",
            "ear p50", "ear p90", "gender", "dropped", "muted", "loops", "xtalk", "errors"]
    rows = [head] + [r for p in paths for r in row(p)]
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(head))]
    for r in rows:
        print("  ".join(str(v).ljust(widths[i]) for i, v in enumerate(r)))
    print("\nDelays are seconds after the speaker stopped: text = speech-to-text committed the phrase, 1st words = "
          "first translated words, done = translation finished, ear = the translated voice starts playing.")


if __name__ == "__main__":
    main(sys.argv[1:])
