"""How often live speech-to-text writes words nobody said: per turn, the transcript's extra words beyond
the script (insertions) and repeated-phrase loops, by language and direction.

    python results/hallucinations.py results/interpreter_A.json [results/interpreter_B.json ...]
"""
import collections
import json
import re
import sys

WORD = re.compile(r"\w+", re.UNICODE)


def words(text):
    return [w.lower() for w in WORD.findall(text or "")]


def loop_len(ws, n=3):
    """The longest run of a repeated n-gram (0 = none): "a b c a b c" -> 1 repeat."""
    best = 0
    for size in range(2, 6):
        for i in range(0, max(0, len(ws) - 2 * size + 1)):
            if ws[i:i + size] == ws[i + size:i + 2 * size]:
                best = max(best, size)
    return best


def main(paths):
    by = collections.defaultdict(lambda: {"turns": 0, "ref_w": 0, "hyp_w": 0, "inflated": 0, "loops": 0,
                                         "examples": []})
    for p in paths:
        d = json.load(open(p, encoding="utf-8"))
        for c in d["conversations"]:
            for t in c.get("turns") or []:
                if t.get("dropped"):
                    continue
                ref, hyp = words(t.get("text")), words(t.get("hyp"))
                if not ref:
                    continue
                key = (t.get("lang"), t.get("direction"))
                b = by[key]
                b["turns"] += 1
                b["ref_w"] += len(ref)
                b["hyp_w"] += len(hyp)
                extra = len(hyp) - len(ref)
                inflated = extra >= 3 and len(hyp) >= 1.4 * len(ref)
                lp = loop_len(hyp)
                if inflated:
                    b["inflated"] += 1
                if lp:
                    b["loops"] += 1
                if (inflated or lp) and len(b["examples"]) < 3:
                    b["examples"].append((c["id"], (t.get("text") or "")[:90], (t.get("hyp") or "")[:140]))
    print("lang dir    turns  words(script->heard)  inflated  loops")
    for (lang, direction), b in sorted(by.items(), key=lambda kv: (-kv[1]["inflated"] - kv[1]["loops"], kv[0])):
        print("%-4s %-6s %5d  %6d -> %-6d       %3d       %3d" % (lang, direction, b["turns"], b["ref_w"], b["hyp_w"],
                                                              b["inflated"], b["loops"]))
    print()
    for (lang, direction), b in sorted(by.items()):
        for ex in b["examples"]:
            print("[%s %s] %s\n   SAID:  %s\n   HEARD: %s" % (lang, direction, ex[0], ex[1], ex[2]))


if __name__ == "__main__":
    main(sys.argv[1:])
