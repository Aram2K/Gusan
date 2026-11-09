"""Write results/report_<stamp>.md from the newest results of each kind.

    python report.py                         newest stt_*.json and interpreter_*.json
    python report.py --stt results/stt_20261108-120000.json --baseline-stt results/stt_20261101-090000.json

Sections: meeting transcription (accuracy and speed per engine / input / language mode,
per language, mixed-language lines, echo in speakers mode, failures), the interpreter (latency
percentiles per stage, translation quality, gender-matched voice, echo loops, drops / merges,
characters), ElevenLabs characters this month, what changed since the previous results file of
the same kind (a dry run is only compared with a dry run), and plain suggestions drawn from the
numbers.
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from bench_client import BENCH_DIR, MONTHLY_CHARS, CharLedger, _utf8_console

RESULTS_DIR = BENCH_DIR / "results"
KINDS = ("stt", "interpreter")
STAGES = ("stt_commit", "translate_first", "translate_total", "tts_first_audio", "mouth_to_ear")
HIGHER_BETTER = ("chrf", "adequacy", "fluency", "match", "accuracy", "f1", "recall", "precision", "detected_ok",
                 "bag")
LOWER_BETTER = ("wer", "cer", "error", "latency", "delay", "stt_commit", "translate", "tts", "mouth", "loop",
                "echo", "drop", "merge", "skip", "fail", "rtf", "_s", "_ms", "seconds", "behind", "lag")
QUALITY_WORDS = ("chrf", "judge", "adequacy", "fluency", "gender", "voice", "echo", "loop", "drop", "merge",
                 "skip", "chars", "error", "fail", "phrase", "wer", "cer", "accuracy", "match")
SPEC_MOUTH_TO_EAR = (1.2, 1.8)        # docs/redesign/interpreter-spec.md: ~1.2-1.8 s after a phrase ends


# -- files ----------------------------------------------------------------------------------

def list_results(results_dir, kind):
    """<kind>_<stamp>.json files, oldest first (the stamp sorts by time)."""
    return sorted(p for p in Path(results_dir).glob(f"{kind}_*.json") if not p.name.endswith(".tmp"))


def load(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _dry(data):
    return bool(((data or {}).get("config") or {}).get("dry_run"))


# Settings that make two runs of a kind comparable (the conversation subset always matters).
COMPARE_KEYS = {"stt": ("only", "variants", "vocabulary", "diarize"),
                "interpreter": ("only", "variant", "route", "captions_only", "tts_sample", "voice", "output",
                                "tts_model", "echo_path", "no_speak", "no_hear", "no_duck", "vad", "limit_seconds",
                                "judge_route")}


def setting_diffs(kind, a, b):
    """["variant 'clean' -> 'speakers'", ...]: the comparable settings that differ between runs."""
    ca, cb = (a or {}).get("config") or {}, (b or {}).get("config") or {}
    # a setting an older run did not record yet ("judge_route") counts as its empty default
    return [f"{k} {ca.get(k)!r} -> {cb.get(k)!r}" for k in COMPARE_KEYS.get(kind, ())
            if ca.get(k) != cb.get(k) and not (k not in ca and not cb.get(k)) and not (k not in cb and not ca.get(k))]


def pick(results_dir, kind, given=None, baseline=None, dry=None):
    """(path, data, previous path, previous data).

    The results: the given file, else the newest real run of that kind - or the newest dry run
    when there is no real one (dry=True: dry runs only, dry=False: real runs only), so a dry run
    made after a real one never hides the real numbers. The run to compare it with: the given
    baseline, else the newest older run of the same kind, dry-run flag and settings, else the
    newest older one with the same dry-run flag (report.py then names the settings that differ)."""
    files = list_results(results_dir, kind)
    seen = {}

    def get(p):
        if p not in seen:
            seen[p] = load(p)
        return seen[p]
    path = data = None
    if given:
        path, data = Path(given), get(Path(given))
    else:
        newest = {}
        for p in reversed(files):
            d = get(p)
            if d is None:
                continue
            newest.setdefault(_dry(d), (p, d))
            if (True if dry is True else False) in newest:
                break                                   # found what is wanted: nothing older matters
        choice = newest.get(dry) if dry is not None else (newest.get(False) or newest.get(True))
        if choice:
            path, data = choice
    if data is None:
        return None, None, None, None
    if baseline:
        return path, data, Path(baseline), load(baseline)
    fallback = None
    for p in reversed([p for p in files if p.name < path.name]):
        d = get(p)
        if d is None or _dry(d) != _dry(data):
            continue
        if not setting_diffs(kind, d, data):
            return path, data, p, d
        fallback = fallback or (p, d)
    return (path, data) + (fallback or (None, None))


def newer_dry(results_dir, kind, path):
    """The newest dry run that is newer than ``path`` (a real run), if any: the report says it skipped it."""
    for p in reversed(list_results(results_dir, kind)):
        if path is None or p.name <= Path(path).name:
            return None
        if _dry(load(p)):
            return p
    return None


# -- formatting -----------------------------------------------------------------------------

def pct(x):
    return "-" if x is None else f"{100 * x:.1f}%"


def secs(x):
    return "-" if x is None else f"{x:.2f}"


def num(x):
    if x is None:
        return "-"
    if isinstance(x, bool):
        return "yes" if x else "no"
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        if abs(x) >= 100:
            return f"{x:.0f}"
        return f"{x:.3f}".rstrip("0").rstrip(".") if x != int(x) else str(int(x))
    return str(x)


def _st(d, k="p50"):
    return (d or {}).get(k) if isinstance(d, dict) else None


def md_table(headers, rows, right=None):
    """Markdown table; right = column indexes to right-align (default: all but the first)."""
    if not rows:
        return ["(none)", ""]
    right = set(range(1, len(headers))) if right is None else set(right)
    cell = (lambda v: str(v).replace("|", "/").replace("\n", " "))
    out = ["| " + " | ".join(cell(h) for h in headers) + " |",
           "| " + " | ".join("---:" if i in right else "---" for i in range(len(headers))) + " |"]
    out += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    out.append("")
    return out


def _source(path, data):
    cfg = data.get("config") or {}
    bits = [f"`{Path(path).name}`", f"git {data.get('git') or '?'}"]
    if cfg.get("dry_run"):
        bits.append("**DRY RUN: fake engines, the numbers mean nothing**")
    if cfg.get("interrupted"):
        bits.append("interrupted (partial)")
    return "Source: " + ", ".join(bits)


# -- meeting transcription ------------------------------------------------------------------

def stt_section(path, data):
    s = data.get("summary") or {}
    cfg = data.get("config") or {}
    L = ["## Meeting transcription (batch speech-to-text)", "", _source(path, data), ""]
    L.append(f"{s.get('n_jobs', 0)} jobs over {len(data.get('conversations') or [])} conversations, "
             f"{s.get('n_errors', 0)} failed, {round((s.get('audio_s_total') or 0) / 60, 1)} min of audio scored. "
             f"Engines {', '.join(cfg.get('engines') or [])}; inputs {', '.join(cfg.get('inputs') or [])}; "
             f"modes {', '.join(cfg.get('modes') or [])}; variants {', '.join(cfg.get('variants') or [])}.")
    L += ["", "WER counts words (each Japanese / Chinese / Thai character as one), CER counts characters "
              "without spaces. *WER by turn* scores each turn on its own (by word times), so cross-talk written "
              "in another order is not counted twice. *fixed* = the track's language is sent (call: their "
              "language, mic: mine); *auto* = the engine detects it; *multi* = the track's languages with code "
              "switching (Gladia). Speed is the wall time of one batch request from this PC (upload included); "
              "RTF = the engine's seconds per second of audio.", ""]

    best = s.get("best") or []
    if best:
        L += ["### Best per input", ""]
        L += md_table(["variant / input", "most accurate", "WER", "CER", "fastest", "p50 s", "its WER"],
                      [[f"{b['variant']} / {b['input']}", b["most_accurate"], pct(b.get("wer")), pct(b.get("cer")),
                        b["fastest"], secs(b.get("fastest_p50_s")), pct(b.get("fastest_wer"))] for b in best],
                      right={2, 3, 5, 6})

    eng = s.get("by_engine") or []
    if eng:
        L += ["### Engines (clean audio, every input and mode)", ""]
        L += md_table(["engine", "WER", "CER", "p50 s", "p90 s", "RTF p50", "jobs", "failed"],
                      [[e["engine"], pct(e.get("wer")), pct(e.get("cer")), secs(_st(e.get("client_s"))),
                        secs(_st(e.get("client_s"), "p90")), num(_st(e.get("rtf"))), e.get("n"), e.get("n_errors")]
                       for e in sorted(eng, key=lambda e: (e.get("wer") is None, e.get("wer") or 0))])

    configs = s.get("configs") or []
    if configs:
        L += ["### Every configuration", ""]
        rows = []
        for c in sorted(configs, key=lambda c: (c["variant"], c["input"], c["mode"],
                                                 c.get("wer") if c.get("wer") is not None else 9)):
            rows.append([c["variant"], c["input"], c["mode"], c["engine"], pct(c.get("wer")), pct(c.get("cer")),
                         pct(c.get("wer_turns")), num(c.get("bag_f1")), num(c.get("echo_units")),
                         num(c.get("dropped_turns")),
                         secs(_st(c.get("client_s"))), secs(_st(c.get("client_s"), "p90")), num(_st(c.get("rtf"))),
                         pct(c.get("detected_ok")) if c.get("detected_ok") is not None else "-",
                         f"{c.get('n_ok', 0)}/{c.get('n', 0)}"])
        L += md_table(["variant", "input", "mode", "engine", "WER", "CER", "WER by turn", "bag F1", "echo words",
                       "dropped turns", "p50 s", "p90 s", "RTF", "lang detected", "ok"], rows, right=set(range(4, 15)))

    langs = s.get("by_lang") or []
    if langs:
        L += ["### Per language (call and mic tracks; WER, CER for ja / zh / th)", ""]
        for variant in sorted({r["variant"] for r in langs}):
            rows_v = [r for r in langs if r["variant"] == variant]
            cols = sorted({r["lang"] for r in rows_v})
            keyed = {(r["engine"], r["mode"], r["lang"]): r for r in rows_v}
            table = []
            for engine, mode in sorted({(r["engine"], r["mode"]) for r in rows_v}):
                table.append([f"{engine} ({mode})"] + [pct((keyed.get((engine, mode, lg)) or {}).get("primary"))
                                                       for lg in cols])
            L += [f"*{variant}*", ""]
            L += md_table(["engine (mode)"] + cols, table)
        best = best_by_lang(langs)
        if best:
            L += ["### Best engine per language (clean audio)", ""]
            L += md_table(["language", "most accurate", "rate", "runner-up", "rate", "worst", "rate", "turns"],
                          [[b["lang"], b["best"], pct(b["best_rate"]), b.get("second") or "-", pct(b.get("second_rate")),
                            b.get("worst") or "-", pct(b.get("worst_rate")), b["n_turns"]] for b in best],
                          right={2, 4, 6, 7})
    not_sent = s.get("fixed_not_sent") or []
    if not_sent:
        L += ["*fixed* sent no language for: " + ", ".join(f"{x['engine']} {x['language']} ({x['jobs']} jobs)"
                                                          for x in not_sent)
              + " - the engine does not take that language (or refused it), so those runs were really *auto*.", ""]
    retried = sum(c.get("retried") or 0 for c in configs)
    if retried:
        L += [f"{retried} job(s) needed a retry (429 / 5xx); their times are those of the try that answered.", ""]

    mixed = s.get("mixed") or []
    if any(m.get("mixed_turns") for m in mixed):
        L += ["### Mixed-language lines vs single-language lines", ""]
        L += md_table(["variant", "engine", "mode", "mixed WER", "single WER", "mixed CER", "single CER", "mixed turns"],
                      [[m["variant"], m["engine"], m["mode"], pct(m.get("mixed_wer")), pct(m.get("single_wer")),
                        pct(m.get("mixed_cer")), pct(m.get("single_cer")), m.get("mixed_turns")]
                       for m in sorted(mixed, key=lambda m: (m["variant"], m["engine"], m["mode"]))],
                      right={3, 4, 5, 6, 7})

    fails = [(c["id"], j) for c in data.get("conversations") or [] for j in c.get("jobs") or []
             if j.get("status") != "ok"]
    if fails:
        L += [f"### Failures ({len(fails)})", ""]
        for cid, j in fails[:20]:
            L.append(f"- {cid} {j.get('variant')}/{j.get('input')} {j.get('mode')} {j.get('engine')}: "
                     f"{(j.get('error') or '')[:200]}")
        if len(fails) > 20:
            L.append(f"- ... {len(fails) - 20} more")
        L.append("")
    return L


def best_by_lang(lang_rows, variant="clean"):
    """Per language: the engine (mode) with the lowest primary error rate (WER; CER for ja / zh /
    th), the runner-up and the worst, over the per-language rows of run_stt's summary."""
    out = []
    rows = [r for r in lang_rows if r.get("variant") == variant and r.get("primary") is not None]
    for lang in sorted({r["lang"] for r in rows}):
        rs = sorted((r for r in rows if r["lang"] == lang), key=lambda r: (r["primary"], r.get("cer") or 0))
        b = {"lang": lang, "best": f"{rs[0]['engine']} ({rs[0]['mode']})", "best_rate": rs[0]["primary"],
             "n_turns": rs[0].get("n_turns")}
        if len(rs) > 1:
            b.update(second=f"{rs[1]['engine']} ({rs[1]['mode']})", second_rate=rs[1]["primary"])
        if len(rs) > 2:
            b.update(worst=f"{rs[-1]['engine']} ({rs[-1]['mode']})", worst_rate=rs[-1]["primary"])
        out.append(b)
    return out


def stt_diff(cur, prev):
    def keyed(d):
        return {(c["engine"], c["variant"], c["input"], c["mode"]): c for c in (d.get("summary") or {}).get("configs") or []}
    a, b = keyed(prev), keyed(cur)
    rows = []
    for k in sorted(set(a) | set(b)):
        x, y = a.get(k) or {}, b.get(k) or {}
        dw = _delta(x.get("wer"), y.get("wer"))
        dc = _delta(x.get("cer"), y.get("cer"))
        ds = _delta(_st(x.get("client_s")), _st(y.get("client_s")))
        if all(d is None or abs(d) < 0.0005 for d in (dw, dc)) and (ds is None or abs(ds) < 0.05):
            continue
        rows.append([" / ".join(k[1:]) + f" {k[0]}", pct(x.get("wer")), pct(y.get("wer")), _arrow(dw, False, True),
                     pct(x.get("cer")), pct(y.get("cer")), _arrow(dc, False, True),
                     secs(_st(x.get("client_s"))), secs(_st(y.get("client_s"))), _arrow(ds, False)])
    return md_table(["variant / input / mode engine", "WER was", "WER now", "", "CER was", "CER now", "",
                     "p50 s was", "p50 s now", ""], rows)


# -- interpreter (schema written by run_interpreter.py; read generically) --------------------

def _is_stats(d):
    return isinstance(d, dict) and "p50" in d and ("p90" in d or "mean" in d)


def _label(x, i):
    if isinstance(x, dict):
        for k in ("name", "config", "label", "id", "key", "route", "stage"):
            if isinstance(x.get(k), str) and x.get(k):
                return x[k]
    return str(i)


def stats_dicts(obj, path=()):
    """[(path, stats)] for every {"p50", "p90"/"mean", ...} found inside obj."""
    out = []
    if _is_stats(obj):
        return [(path, obj)]
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += stats_dicts(v, path + (str(k),))
    elif isinstance(obj, list) and len(obj) <= 200 and all(isinstance(x, dict) for x in obj):
        for i, x in enumerate(obj):
            out += stats_dicts(x, path + (_label(x, i),))
    return out


def scalar_leaves(obj, path=()):
    """[(path, value)] for every number / string / bool outside the stats dicts."""
    if _is_stats(obj):
        return []
    if isinstance(obj, dict):
        out = []
        for k, v in obj.items():
            out += scalar_leaves(v, path + (str(k),))
        return out
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return [(path, obj)]
    if isinstance(obj, list):
        if len(obj) <= 12 and all(isinstance(x, (int, float, str)) for x in obj):
            return [(path, ", ".join(num(x) for x in obj))]
        if len(obj) <= 200 and all(isinstance(x, dict) for x in obj):
            out = []
            for i, x in enumerate(obj):
                out += scalar_leaves(x, path + (_label(x, i),))
            return out
    return []


def _p(path):
    return " / ".join(path) if path else "(top)"


def _stage_rank(path):
    low = [p.lower() for p in path]
    for i, st in enumerate(STAGES):
        if any(st in p for p in low):
            return i
    return len(STAGES)


def numeric_leaves(obj, path=()):
    """{path: number} of every number, stats dicts included (p50, p90, ...)."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(numeric_leaves(v, path + (str(k),)))
    elif isinstance(obj, list) and len(obj) <= 200 and all(isinstance(x, dict) for x in obj):
        for i, x in enumerate(obj):
            out.update(numeric_leaves(x, path + (_label(x, i),)))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out[path] = float(obj)
    return out


# run_interpreter.py's own layout: summary {hear: AGG, speak: AGG, by_pair, echo_loops, ...}.
# The contract's stage names, and where each one is in that layout (all from the end of speech
# except tts_first_audio, which is the voice's own time to its first audio).
DIRECTIONS = ("hear", "speak")
STAGE_SOURCES = (("stt_commit", "latency", "stt", "speech end -> committed text"),
                 ("translate_first", "latency", "first_delta", "speech end -> first translated words"),
                 ("translate_total", "latency", "final", "speech end -> whole translation"),
                 ("tts_first_audio", "stages", "ttfb", "voice asked -> its first audio"),
                 ("mouth_to_ear", "latency", "ear", "speech end -> the listener hears it"))
OTHER_LATENCY = (("turn_commit", "end of a turn -> its last phrase committed"),
                 ("turn_ear", "end of a turn -> its last phrase heard"),
                 ("first_caption", "speech start -> first caption"),
                 ("app", "the delay the app itself shows"))
STAGE_PARTS = (("translate_wait", "commit -> translation asked"), ("ttft", "the model's time to first words"),
               ("translate_rest", "first -> last translated words"), ("speak_wait", "translation done -> voice asked"),
               ("ttfb", "voice asked -> first audio"), ("play_wait", "first audio -> played"))
COUNT_KEYS = ("turns", "phrases", "real", "dropped_turns", "partial_turns", "merged_turns", "split_turns",
              "duplicates", "extra", "echo", "echo_mixed", "echo_suspect", "crosstalk", "crosstalk_mixed",
              "untranslated", "translate_errors")
SPEECH_END_KINDS = ("turn_end", "pause", "gate", "estimate")


def _dir_layout(s):
    return any(isinstance(s.get(d), dict) and isinstance(s[d].get("latency"), dict) for d in DIRECTIONS)


def _g(obj, *keys):
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _minutes(seconds):
    return None if not isinstance(seconds, (int, float)) else round(seconds / 60.0, 1)


def _pair(block, k1, k2):
    """"a / b" of two counts in a dict (None when there is no dict)."""
    return None if not isinstance(block, dict) else f"{block.get(k1) or 0} / {block.get(k2) or 0}"


def _ends(se):
    """How the phrases' speech ends were found: "turn end / pause / gate / estimate" counts."""
    if not isinstance(se, dict) or not se:
        return None
    return " / ".join(str(se.get(k) or 0) for k in SPEECH_END_KINDS)


def _dirs_section(s):
    """Tables for run_interpreter.py's layout."""
    dirs = [d for d in DIRECTIONS if isinstance(s.get(d), dict)]
    L = [f"{s.get('ok', '?')} of {s.get('conversations', '?')} conversations ran; directions: "
         + ", ".join(f"{d} ({'call -> you' if d == 'hear' else 'you -> the call'})" for d in dirs) + ".", ""]
    head = ["stage", "what"] + [f"{d} {q}" for d in dirs for q in ("p50", "p90", "p95")]
    rows = []
    for name, block, key, what in STAGE_SOURCES:
        rows.append([name, what] + [num(_g(s, d, block, key, q)) for d in dirs for q in ("p50", "p90", "p95")])
    L += ["### Latency per stage (s)", "",
          f"The spec expects ~{SPEC_MOUTH_TO_EAR[0]}-{SPEC_MOUTH_TO_EAR[1]} s from the end of a phrase to hearing "
          "it (mouth_to_ear).", ""]
    L += md_table(head, rows, right=set(range(2, len(head))))
    rows = [[k, what] + [num(_g(s, d, "latency", k, q)) for d in dirs for q in ("p50", "p90", "p95")]
            for k, what in OTHER_LATENCY if any(_g(s, d, "latency", k, "n") for d in dirs)]
    rows += [[k, what] + [num(_g(s, d, "stages", k, q)) for d in dirs for q in ("p50", "p90", "p95")]
             for k, what in STAGE_PARTS if any(_g(s, d, "stages", k, "n") for d in dirs)]
    if rows:
        L += ["### Where the time goes (s)", ""]
        L += md_table(head, rows, right=set(range(2, len(head))))

    def per_dir(label, fn, fmt=num):
        return [label] + [fmt(fn(s.get(d) or {})) for d in dirs]
    rows = [per_dir("STT WER (phrases vs the track)", lambda a: a.get("wer"), pct),
            per_dir("STT WER incl. stray phrases", lambda a: a.get("wer_all"), pct),
            per_dir("STT CER", lambda a: a.get("cer"), pct),
            per_dir("chrF++ end to end", lambda a: a.get("chrf")),
            per_dir("chrF++ matched turns", lambda a: a.get("chrf_matched")),
            per_dir("judge adequacy", lambda a: _g(a, "judge", "adequacy")),
            per_dir("judge fluency", lambda a: _g(a, "judge", "fluency")),
            per_dir("judge answers that could not be read", lambda a: _g(a, "judge", "unparsed") or None),
            per_dir("mixed-language turns: WER mean", lambda a: _g(a, "mixed", "wer_mean"), pct),
            per_dir("mixed-language turns: chrF mean", lambda a: _g(a, "mixed", "chrf_mean")),
            per_dir("single-language turns: WER mean", lambda a: _g(a, "single", "wer_mean"), pct),
            per_dir("single-language turns: chrF mean", lambda a: _g(a, "single", "chrf_mean")),
            per_dir("gender guessed right", lambda a: _g(a, "gender", "accuracy"), pct),
            per_dir("voice of the right gender", lambda a: _g(a, "gender", "voice_accuracy"), pct),
            per_dir("voice switches (expected)", lambda a: None if not a.get("gender") else
                    f"{_g(a, 'gender', 'switches')} ({_g(a, 'gender', 'expected_switches')})")]
    rows = [r for r in rows if any(v != "-" for v in r[1:])]
    L += ["### Accuracy and translation quality", ""]
    L += md_table(["metric"] + dirs, rows)
    rows = [per_dir("own speech muted before the STT (the listen gate)", lambda a: _g(a, "stt_input", "muted_ratio"),
                    pct),
            per_dir("speech on the track (s)", lambda a: _g(a, "stt_input", "speech_s")),
            per_dir("Gusan's own voice that reached this STT (s)", lambda a: _g(a, "stt_input", "gusan_reached_stt_s")),
            per_dir("audio fed to the live STT (min)", lambda a: _minutes(_g(a, "stt_input", "sent_s"))),
            per_dir("reconnects: STT / voice (fresh tokens after the start)", lambda a: _pair(a.get("reconnects"),
                                                                                              "stt", "voice")),
            per_dir("phrase ends from: turn end / pause / gate / estimate", lambda a: _ends(a.get("speech_end_by")))]
    rows = [r for r in rows if any(v != "-" for v in r[1:])]
    if rows:
        L += ["### What reached the speech-to-text", "",
              "Muted: the share of the track's own speech that reached its STT as silence (those words cannot be "
              "transcribed). The delays from the speech end use the phrases whose end is known (the turn's end, a "
              "pause in the voice, or where the gate muted it); a phrase cut mid-sentence (estimate) is left out "
              "of them.", ""]
        L += md_table(["what"] + dirs, rows)
    rows = [[k] + [num(_g(s, d, "counts", k)) for d in dirs] for k in COUNT_KEYS
            if any(_g(s, d, "counts", k) is not None for d in dirs)]
    if rows:
        L += ["### Phrases and turns", "",
              "dropped / partial turns: never or only partly translated; merged / split: several turns in one "
              "phrase or one turn in several; echo: Gusan transcribing its own voice; crosstalk: the call heard "
              "by the mic through the speakers.", ""]
        L += md_table(["count"] + dirs, rows)
    kinds = s.get("echo_loops_by_kind")
    whole = [("echo loops", s.get("echo_loops")),
             ("... Gusan's voice / mixed with mine / while only Gusan played",
              None if not isinstance(kinds, dict) else
              f"{kinds.get('echo') or 0} / {kinds.get('echo_mixed') or 0} / {kinds.get('echo_suspect') or 0}"),
             ("longest echo chain", s.get("echo_chain_max")),
             ("crosstalk phrases", s.get("crosstalk")), ("errors", s.get("errors")),
             ("reconnects: STT / voice", _pair(s.get("reconnects"), "stt", "voice")),
             ("audio fed to the live STT (min)", _minutes(s.get("stt_audio_s"))),
             ("characters spoken", s.get("chars_spoken")), ("characters if every phrase were spoken",
                                                            s.get("chars_would_speak")),
             ("characters charged to the budget (the ledger)", s.get("chars_charged")),
             ("conversations failed / skipped", _pair(s, "failed", "skipped")),
             ("voice sockets left open", _g(s, "leaks", "sockets_open_after_stop")),
             ("phrases spoken after stop", _g(s, "leaks", "utterances_after_stop")),
             ("simulated capture max lag (s)", _g(s, "sim", "capture_max_lag_s"))]
    L += ["### Whole run", ""]
    L += md_table(["what", "value"], [[k, num(v)] for k, v in whole if v is not None])
    pairs = s.get("by_pair") or {}
    if pairs:
        L += ["### Per language pair", ""]
        head = ["pair"] + [f"{d} {m}" for d in dirs for m in ("WER", "chrF", "ear p50", "dropped")]
        L += md_table(head, [[pair] + [v for d in dirs for v in
                                       (pct(_g(blk, d, "wer")), num(_g(blk, d, "chrf")),
                                        num(_g(blk, d, "latency", "ear", "p50")), num(_g(blk, d, "counts", "dropped_turns")))]
                             for pair, blk in sorted(pairs.items())])
    return L


def interp_failures(data):
    """The conversations of an interpreter run that failed or were skipped (a refused token, a stubbed
    function: the rest of the run is skipped), with why."""
    bad = [c for c in data.get("conversations") or [] if c.get("status") != "ok"]
    if not bad:
        return []
    L = [f"### Conversations that did not run ({len(bad)})", ""]
    for c in bad[:20]:
        L.append(f"- {c.get('id')}: {c.get('status') or 'error'} - {str(c.get('error') or '')[:200]}")
    if len(bad) > 20:
        L.append(f"- ... {len(bad) - 20} more")
    return L + [""]


def dirs_key_metrics(s):
    """{label: (value, higher_is_better)} of the numbers worth comparing between two runs."""
    out = {}
    for d in DIRECTIONS:
        a = s.get(d)
        if not isinstance(a, dict):
            continue
        for k in ("wer", "cer", "wer_all"):
            out[f"{d} STT {k}"] = (a.get(k), False)
        for k in ("chrf", "chrf_matched"):
            out[f"{d} {k}"] = (a.get(k), True)
        out[f"{d} judge adequacy"] = (_g(a, "judge", "adequacy"), True)
        for name, block, key, _w in STAGE_SOURCES:
            out[f"{d} {name} p50"] = (_g(a, block, key, "p50"), False)
            out[f"{d} {name} p90"] = (_g(a, block, key, "p90"), False)
        out[f"{d} gender accuracy"] = (_g(a, "gender", "accuracy"), True)
        out[f"{d} voice gender accuracy"] = (_g(a, "gender", "voice_accuracy"), True)
        out[f"{d} speech muted (gate)"] = (_g(a, "stt_input", "muted_ratio"), False)
        for k in ("dropped_turns", "merged_turns", "echo", "echo_mixed", "crosstalk", "translate_errors"):
            out[f"{d} {k}"] = (_g(a, "counts", k), False)
        for k in ("stt", "voice"):
            out[f"{d} reconnects {k}"] = (_g(a, "reconnects", k), False)
    for k in ("echo_loops", "crosstalk", "errors"):
        out[k] = (s.get(k), False)
    out["chars_spoken"] = (s.get("chars_spoken"), None)
    return out


def interp_section(path, data):
    s = data.get("summary") or {}
    cfg = data.get("config") or {}
    L = ["## Interpreter (speech to speech)", "", _source(path, data), ""]
    if _dir_layout(s):
        cfg_bits = [f"{k}: {num(v) if not isinstance(v, (list, dict)) else json.dumps(v, ensure_ascii=False)[:60]}"
                    for k, v in cfg.items() if k != "dry_run" and v not in (None, "", [], {})]
        if cfg_bits:
            L += ["Config: " + "; ".join(cfg_bits)[:900], ""]
        return L + _dirs_section(s) + interp_failures(data)
    convs = data.get("conversations") or []
    cfg_bits = [f"{k}: {num(v) if not isinstance(v, (list, dict)) else json.dumps(v, ensure_ascii=False)[:80]}"
                for k, v in cfg.items() if k not in ("dry_run",)]
    if cfg_bits:
        L += ["Config: " + "; ".join(cfg_bits), ""]
    L += [f"{len(convs)} conversations.", ""]

    st = stats_dicts(s)
    stage_rows = sorted([x for x in st if _stage_rank(x[0]) < len(STAGES)], key=lambda x: (_stage_rank(x[0]), x[0]))
    other_rows = [x for x in st if _stage_rank(x[0]) >= len(STAGES)]
    if stage_rows:
        L += ["### Latency per stage (s)", "",
              "stt_commit: speech end -> committed text; translate_first / translate_total: phrase -> first / "
              "last translated words; tts_first_audio: translation -> first voice audio; mouth_to_ear: speech "
              f"end -> the listener hears the translation (spec expects ~{SPEC_MOUTH_TO_EAR[0]}-"
              f"{SPEC_MOUTH_TO_EAR[1]} s).", ""]
        L += md_table(["stage", "n", "mean", "p50", "p90", "p95", "max"],
                      [[_p(p), num(d.get("n")), num(d.get("mean")), num(d.get("p50")), num(d.get("p90")),
                        num(d.get("p95")), num(d.get("max"))] for p, d in stage_rows])
    if other_rows:
        L += ["### Other distributions", ""]
        L += md_table(["what", "n", "mean", "p50", "p90", "p95"],
                      [[_p(p), num(d.get("n")), num(d.get("mean")), num(d.get("p50")), num(d.get("p90")),
                        num(d.get("p95"))] for p, d in other_rows[:60]])

    scal = scalar_leaves({k: v for k, v in s.items() if k not in ("by_config", "configs")})
    quality = [(p, v) for p, v in scal if any(w in " ".join(p).lower() for w in QUALITY_WORDS)]
    rest = [(p, v) for p, v in scal if (p, v) not in quality]
    if quality:
        L += ["### Translation quality and behaviour", "",
              "chrF++ is 0-100 against the corpus reference; the judge scores adequacy / fluency 0-100; gender "
              "match = the voice of the speaker's gender was chosen; echo loops = Gusan translated its own "
              "voice (speakers mode); drops / merges = phrases never spoken / spoken together.", ""]
        L += md_table(["metric", "value"], [[_p(p), num(v)] for p, v in quality[:80]])

    for key in ("by_config", "configs"):
        block = s.get(key)
        rows = list(block.items()) if isinstance(block, dict) else [(_label(x, i), x) for i, x in enumerate(block or [])]
        rows = [(name, x) for name, x in rows if isinstance(x, dict)]
        if not rows:
            continue
        cols = []
        for _name, x in rows:
            for k, v in x.items():
                if k in ("name", "config", "label", "id"):
                    continue
                c = (k, "p50") if _is_stats(v) else (k, None) if isinstance(v, (int, float, str, bool)) else None
                if c and c not in cols:
                    cols.append(c)
        cols.sort(key=lambda c: (_stage_rank((c[0],)), c[0]))
        cols = cols[:12]
        L += [f"### Per configuration ({key})", ""]
        L += md_table(["config"] + [f"{k} p50" if sub else k for k, sub in cols],
                      [[str(name)] + [num(_st(x.get(k)) if sub else x.get(k)) for k, sub in cols] for name, x in rows])
    if rest:
        L += ["### Other numbers", ""]
        L += md_table(["what", "value"], [[_p(p), num(v)] for p, v in rest[:60]])
    return L


def dirs_diff(cur, prev):
    a = dirs_key_metrics((prev or {}).get("summary") or {})
    b = dirs_key_metrics((cur or {}).get("summary") or {})
    rows = []
    for k in list(b) + [k for k in a if k not in b]:
        x, hb_a = a.get(k, (None, None))
        y, hb_b = b.get(k, (None, None))
        hb = hb_b if hb_b is not None else hb_a
        if x is None and y is None:
            continue
        d = _delta(x, y)
        if d is not None and abs(d) < 1e-9:
            continue
        fmt = pct if any(w in k for w in ("wer", "cer", "accuracy", "muted")) else num
        rows.append([k, fmt(x), fmt(y), _arrow(d, hb is True, hb is False)])
    return md_table(["metric", "was", "now", ""], rows)


def generic_diff(cur, prev):
    if _dir_layout((cur or {}).get("summary") or {}) and _dir_layout((prev or {}).get("summary") or {}):
        return dirs_diff(cur, prev)
    a = numeric_leaves((prev or {}).get("summary") or {})
    b = numeric_leaves((cur or {}).get("summary") or {})
    rows = []
    for k in sorted(set(a) | set(b), key=lambda k: (_stage_rank(k), k)):
        x, y = a.get(k), b.get(k)
        if x is not None and y is not None and abs(y - x) < 1e-9:
            continue
        if k and k[-1] in ("n", "min", "max"):
            continue
        low = " ".join(k).lower()
        higher = any(w in low for w in HIGHER_BETTER)
        lower = not higher and any(w in low for w in LOWER_BETTER + ("p50", "p90", "p95", "mean"))
        d = _delta(x, y)
        rows.append([_p(k), num(x), num(y), _arrow(d, higher, lower)])
    return md_table(["metric", "was", "now", ""], rows[:80])


def _delta(x, y):
    if x is None or y is None:
        return None
    return float(y) - float(x)


def _arrow(d, higher_better, lower_better=True):
    """'better' / 'worse' / '' for a change d."""
    if d is None or abs(d) < 1e-9:
        return ""
    if higher_better:
        return "better" if d > 0 else "worse"
    if lower_better:
        return "better" if d < 0 else "worse"
    return "changed"


# -- characters and suggestions -------------------------------------------------------------

def chars_section(ledger_path, interp, stt=None):
    L = ["## ElevenLabs characters", ""]
    led = CharLedger(ledger_path) if ledger_path else CharLedger()
    m = led.month()
    by = ", ".join(f"{k} {v}" for k, v in sorted((m.get("by") or {}).items()))
    L.append(f"This month ({datetime.now():%Y-%m}): **{m.get('total', 0)}** of about {MONTHLY_CHARS} used by the "
             f"bench ({m.get('calls', 0)} entries{': ' + by if by else ''}). \"unsure\" = tries that failed after "
             "the text may have been voiced (counted to be safe). `python bench_client.py usage` gives the "
             "account's own count.")
    if interp:
        found = [(p, v) for p, v in scalar_leaves(interp.get("summary") or {}) if "chars" in " ".join(p).lower()]
        found += [(("config",) + p, v) for p, v in scalar_leaves(interp.get("config") or {})
                  if "chars" in " ".join(p).lower()]
        if found:
            L.append("Interpreter run: " + "; ".join(f"{_p(p)} {num(v)}" for p, v in found[:6]) + ".")
    usage = ((stt or {}).get("config") or {}).get("elevenlabs_usage") or {}
    if usage.get("delta") is not None:
        L.append(f"Meeting transcription run: the account's count rose by {usage['delta']} during it (Scribe audio, "
                 f"and anything else using the account meanwhile); {num((usage.get('after') or {}).get('used'))} of "
                 f"{num((usage.get('after') or {}).get('limit'))} after it.")
    mins = ((stt or {}).get("config") or {}).get("audio_min_by_engine") or {}
    if mins:
        L.append("Audio sent for transcription: " + ", ".join(f"{e} {num(m)} min" for e, m in sorted(mins.items())) + ".")
    L.append("")
    return L


def suggestions(stt, interp):
    out = []
    s = (stt or {}).get("summary") or {}
    for b in s.get("best") or []:
        if b["variant"] == "clean":
            out.append(f"Meetings, {b['input']} track: {b['most_accurate']} is the most accurate "
                       f"(WER {pct(b.get('wer'))}); {b['fastest']} is the fastest ({secs(b.get('fastest_p50_s'))} s, "
                       f"WER {pct(b.get('fastest_wer'))}).")
    mixed = {(m["engine"], m["variant"], m["mode"]): m for m in s.get("mixed") or []}
    for engine in sorted({k[0] for k in mixed}):
        a, f = mixed.get((engine, "clean", "auto")), mixed.get((engine, "clean", "fixed"))
        if a and f and a.get("mixed_wer") is not None and f.get("mixed_wer") is not None:
            d_mixed = f["mixed_wer"] - a["mixed_wer"]
            d_single = (f.get("single_wer") or 0) - (a.get("single_wer") or 0)
            if abs(d_mixed) >= 0.01 or abs(d_single) >= 0.01:
                out.append(f"{engine}: sending the language changes WER by {100 * d_single:+.1f} pt on single-language "
                           f"lines and {100 * d_mixed:+.1f} pt on mixed-language lines (negative = better).")
    best_l = best_by_lang(s.get("by_lang") or [])
    if best_l:
        out.append("Most accurate per language (clean): " + "; ".join(
            f"{b['lang']} {b['best']} {pct(b['best_rate'])}" for b in best_l) + ".")
    echo = [c for c in s.get("configs") or [] if c["variant"] == "speakers" and c["input"] == "mic"
            and c.get("echo_units") is not None and c.get("ref_units")]
    if echo:
        rate = (lambda c: 100.0 * c["echo_units"] / c["ref_units"])
        lo, hi = min(echo, key=rate), max(echo, key=rate)
        out.append(f"Speakers mode, mic track: the call leaks in as {rate(lo):.0f}-{rate(hi):.0f} extra words per 100 "
                   f"words of mine ({lo['engine']} {lo['mode']} fewest). Echo removal (or transcribing the call only "
                   "once) is what fixes this.")
    isum = (interp or {}).get("summary") or {}
    if interp and _dir_layout(isum):
        for d in DIRECTIONS:
            ear = _g(isum, d, "latency", "ear")
            if ear and ear.get("p50") is not None:
                verdict = "within" if ear["p50"] <= SPEC_MOUTH_TO_EAR[1] else "slower than"
                parts = [f"{name} {num(_g(isum, d, block, key, 'p50'))}" for name, block, key, _w in STAGE_SOURCES[:4]]
                out.append(f"Interpreter {d}: mouth-to-ear p50 {num(ear['p50'])} s, p90 {num(ear.get('p90'))} s "
                           f"({verdict} the spec's ~{SPEC_MOUTH_TO_EAR[0]}-{SPEC_MOUTH_TO_EAR[1]} s); "
                           + ", ".join(parts) + ".")
            a = isum.get(d) or {}
            if a.get("chrf") is not None:
                out.append(f"Interpreter {d}: chrF++ {num(a.get('chrf'))} end to end ({num(a.get('chrf_matched'))} on "
                           f"matched turns), STT WER {pct(a.get('wer'))}"
                           + (f", voice gender right {pct(_g(a, 'gender', 'voice_accuracy'))}" if a.get("gender") else "")
                           + ".")
            muted = _g(a, "stt_input", "muted_ratio")
            if isinstance(muted, (int, float)) and muted >= 0.05:
                out.append(f"Interpreter {d}: {pct(muted)} of {'the call' if d == 'hear' else 'my'} speech reached the "
                           "STT as silence (muted by the listen gate): those words were never transcribed, so they "
                           "could not be translated.")
            rc = a.get("reconnects") or {}
            if (rc.get("stt") or 0) + (rc.get("voice") or 0):
                out.append(f"Interpreter {d}: {rc.get('stt') or 0} live STT and {rc.get('voice') or 0} voice "
                           "reconnect(s) during the run (each one a fresh single-use token).")
        if isum.get("echo_loops"):
            out.append(f"Interpreter: {isum['echo_loops']} echo loop(s) - Gusan translated its own voice "
                       "(speakers mode needs the own-voice filter or the listen gate).")
    elif interp:
        for p, d in stats_dicts(isum):
            if any("mouth_to_ear" in x.lower() for x in p) and d.get("p50") is not None:
                verdict = "within" if d["p50"] <= SPEC_MOUTH_TO_EAR[1] else "slower than"
                out.append(f"Interpreter {_p(p)}: p50 {num(d['p50'])} s, p90 {num(d.get('p90'))} s ({verdict} the "
                           f"spec's ~{SPEC_MOUTH_TO_EAR[0]}-{SPEC_MOUTH_TO_EAR[1]} s).")
                break
    if (stt and _dry(stt)) or (interp and _dry(interp)):
        out.append("A dry run is in this report: its numbers come from fakes and only show that the bench works.")
    return out


# -- CLI ------------------------------------------------------------------------------------

def build_report(results_dir=RESULTS_DIR, stt=None, interpreter=None, baseline_stt=None, baseline_interpreter=None,
                 ledger=None, dry=None):
    """-> (markdown text, {kind: path used}). dry: None = the newest real runs (dry runs only when
    there is no real one), True = the newest dry runs."""
    picked = {"stt": pick(results_dir, "stt", stt, baseline_stt, dry),
              "interpreter": pick(results_dir, "interpreter", interpreter, baseline_interpreter, dry)}
    L = [f"# Gusan bench report, {datetime.now():%Y-%m-%d %H:%M}", ""]
    used = {k: v[0] for k, v in picked.items() if v[0]}
    if not used:
        L += [f"No results in {results_dir} yet: run run_stt.py or run_interpreter.py first.", ""]
    skipped = [(k, newer_dry(results_dir, k, picked[k][0])) for k, given in (("stt", stt), ("interpreter", interpreter))
               if picked[k][0] and not given and not dry and not _dry(picked[k][1])]
    skipped = [(k, p) for k, p in skipped if p]
    if skipped:
        L += ["Newer dry runs are left out (real runs win; `python report.py --dry` reports them): "
              + ", ".join(f"`{p.name}`" for _k, p in skipped) + ".", ""]
    sp, sd, spp, spd = picked["stt"]
    ip, idata, ipp, ipd = picked["interpreter"]
    tips = suggestions(sd, idata)
    if tips:
        L += ["## In short", ""] + [f"- {t}" for t in tips] + [""]
    if sd:
        L += stt_section(sp, sd)
    if idata:
        L += interp_section(ip, idata)
    L += chars_section(ledger, idata, sd)
    if spd or ipd:
        L += ["## Changes since the previous results", ""]
        if spd:
            L += [f"### Meeting transcription: `{spp.name}` -> `{sp.name}`", ""] + _differs("stt", spd, sd)
            L += stt_diff(sd, spd)
        if ipd:
            L += [f"### Interpreter: `{ipp.name}` -> `{ip.name}`", ""] + _differs("interpreter", ipd, idata)
            L += generic_diff(idata, ipd)
    return "\n".join(L).rstrip() + "\n", used


def _differs(kind, prev, cur):
    d = setting_diffs(kind, prev, cur)
    return [f"Not the same settings ({'; '.join(d)}): read the changes with that in mind.", ""] if d else []


def main(argv=None):
    _utf8_console()
    ap = argparse.ArgumentParser(description="Write results/report_<stamp>.md from the newest results.")
    ap.add_argument("--results-dir", default=str(RESULTS_DIR))
    ap.add_argument("--stt", default="", help="an stt_*.json (default: the newest)")
    ap.add_argument("--interpreter", default="", help="an interpreter_*.json (default: the newest)")
    ap.add_argument("--baseline-stt", default="", help="compare with this stt_*.json instead of the previous one")
    ap.add_argument("--baseline-interpreter", default="")
    ap.add_argument("--ledger", default="", help="character ledger (default results/chars_ledger.json)")
    ap.add_argument("--dry", action="store_true",
                    help="report the newest dry runs (by default real runs win over newer dry runs)")
    ap.add_argument("--out", default="", help="where to write (default results/report_<stamp>.md)")
    args = ap.parse_args(argv)
    text, used = build_report(args.results_dir, args.stt or None, args.interpreter or None,
                              args.baseline_stt or None, args.baseline_interpreter or None, args.ledger or None,
                              dry=True if args.dry else None)
    out = Path(args.out) if args.out else None
    if out is None:
        rd = Path(args.results_dir)
        rd.mkdir(parents=True, exist_ok=True)
        while True:
            out = rd / f"report_{datetime.now():%Y%m%d-%H%M%S}.md"
            if not out.exists():
                break
            time.sleep(1.05)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"report: {out}" + (f" (from {', '.join(p.name for p in used.values())})" if used else " (no results yet)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
