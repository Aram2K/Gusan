"""Meeting transcription benchmark (batch speech-to-text, the engines the app's meetings use).

Every conversation x engine x input x language mode:

    engines   scribe_v2, voxtral, openai, gladia          (--engines)
    inputs    system (the call), mic (me), mix (both)    (--inputs)
    modes     fixed: the track's language (system -> their_lang, mic -> my_lang; the mix has
                     no single language, so it only runs auto)
              auto:  language None (the engine detects it)
              multi: "multi" + the languages spoken on that track (code switching; only Gladia
                     takes a list, so only Gladia runs it)
    variants  clean (default), noisy, speakers           (--variants)

Each job sends the WAV through the bench function's "stt" action, times it (client wall time,
server_ms, real-time factor) and scores it against the timeline (metrics.score_transcript):
WER / CER per track, per language, mixed-language lines, dropped turns and, for the mic track,
words of the call that leaked in (echo, speakers variant). Results:
results/stt_<YYYYmmdd-HHMMSS>.json (python report.py turns them into tables).

    python run_stt.py                                  everything, clean audio
    python run_stt.py --engines scribe_v2,gladia --only c01 --variants clean,speakers
    python run_stt.py --dry-run                        offline, fake engines (corpus/audio_dry)
"""
import argparse
import concurrent.futures as cf
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import benchaudio as ba
import generate
import metrics
from bench_client import (BENCH_DIR, STT_ENGINES, BenchError, CharBudget, FakeBench, TTSCache, is_fatal,
                          make_bench, _utf8_console)

INPUTS = ("system", "mic", "mix")
MODES = ("fixed", "auto", "multi")
DEFAULT_MODES = ("fixed", "auto", "multi")
MULTI_ENGINES = ("gladia",)          # the engines that take a list of languages (code switching)
RESULTS_DIR = BENCH_DIR / "results"


# -- shared helpers (run_interpreter.py may use them too) ----------------------------------

def git_sha():
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(BENCH_DIR), capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def new_stamp(results_dir, kind):
    """YYYYmmdd-HHMMSS not used yet by <kind>_<stamp>.json in results_dir, reserved at once (a
    <name>.reserved marker made exclusively): two runs started in the same second never share a file
    (2026-11-09: the OpenAI translate run overwrote the Mistral one). save_results removes the marker."""
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    while True:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = results_dir / f"{kind}_{stamp}.json"
        if not path.exists():
            try:
                with open(path.with_name(path.name + ".reserved"), "x", encoding="utf-8"):
                    pass
                return stamp
            except FileExistsError:
                pass
        time.sleep(1.05)


def save_results(results_dir, kind, data):
    """Write results/<kind>_<stamp>.json (data["stamp"] is filled in when missing). -> path"""
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = data.get("stamp") or new_stamp(results_dir, kind)
    data["stamp"] = stamp
    path = results_dir / f"{kind}_{stamp}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    try:
        path.with_name(path.name + ".reserved").unlink()
    except OSError:
        pass
    return path


def load_timelines(audio_dir, only=None):
    """[(folder, timeline)] for every <audio_dir>/<id>/timeline.json (ids or prefixes in only)."""
    out = []
    for path in sorted(Path(audio_dir).glob("*/timeline.json")):
        try:
            tl = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"  ! {path}: unreadable timeline", file=sys.stderr)
            continue
        if generate._matches(tl.get("id") or path.parent.name, only):
            out.append((path.parent, tl))
    return out


def ensure_dry_audio(corpus, audio_dir, only, log=print):
    """Dry runs make their fake audio on the fly for any conversation that has none yet."""
    have = {d.name for d, _ in load_timelines(audio_dir, only)}
    missing = [c for _p, c in generate.load_conversations(corpus, only) if c["id"] not in have]
    if not missing:
        return
    bench = FakeBench(voices=generate.load_voices(corpus))
    cache = TTSCache(Path(audio_dir).parent / "cache_dry")      # corpus/cache_dry next to corpus/audio_dry
    for conv in missing:
        try:
            generate.generate_conversation(conv, bench, cache=cache, budget=CharBudget(None), out_root=audio_dir,
                                           opts=generate.Options(dry_run=True), voices=None, log=lambda *_: None)
            log(f"  (made fake audio for {conv['id']})")
        except ValueError as e:
            log(f"  ! {conv['id']}: {e}")


def account_usage(bench):
    """The ElevenLabs account's {used, limit, left} via the bench function, or None when the key may
    not read it (the bench function's "usage" action; nothing is billed)."""
    try:
        u = bench.usage()
    except (BenchError, AttributeError, OSError):
        return None
    if not isinstance(u, dict):
        return None
    return {k: u.get(k) for k in ("used", "limit", "left")}


def fixed_language(tl, inp):
    return {"system": tl.get("their_lang"), "mic": tl.get("my_lang")}.get(inp)


def expected_turns(tl, inp):
    """(turns this input should contain, turns that would only be there as echo)."""
    turns = tl.get("turns") or []
    mic = [t for t in turns if t.get("track") == "mic"]
    system = [t for t in turns if t.get("track") != "mic"]
    if inp == "mic":
        return mic, system
    if inp == "system":
        return system, mic
    return turns, []


def track_languages(tl, inp):
    """Every language spoken on this input (their_lang / my_lang first), at most 8."""
    exp, _other = expected_turns(tl, inp)
    first = [fixed_language(tl, inp)] if inp != "mix" else [tl.get("their_lang"), tl.get("my_lang")]
    seen = []
    for lang in first + [x for t in exp for x in (t.get("langs") or [t.get("lang")])]:
        if lang and lang not in seen:
            seen.append(lang)
    return seen[:8]


def plan_jobs(tl, engines, inputs, modes, variants, vocabulary=True):
    jobs = []
    variant_files = tl.get("variants") or {}
    clean = variant_files.get("clean") or {}
    for variant in variants:
        files = variant_files.get(variant)
        if not files:
            continue
        for inp in inputs:
            name = files.get(inp)
            if not name:
                continue
            if variant != "clean" and name == clean.get(inp):
                continue                     # same file as clean (e.g. the call in speakers mode)
            exp, _other = expected_turns(tl, inp)
            if not exp:
                continue                     # nobody speaks on this track
            for mode in modes:
                langs = None
                if mode == "fixed":
                    lang = fixed_language(tl, inp)
                    if not lang:
                        continue             # the mix has no single language: auto covers it
                elif mode == "multi":
                    lang, langs = "multi", track_languages(tl, inp)
                    if len(langs) < 2:
                        continue             # one language only: fixed covers it
                else:
                    lang = None
                for engine in engines:
                    if mode == "multi" and engine not in MULTI_ENGINES:
                        continue             # the others treat "multi" as auto
                    job = {"conv": tl["id"], "engine": engine, "variant": variant, "input": inp,
                           "mode": mode, "language": lang, "file": name}
                    if langs:
                        job["languages"] = langs
                    if vocabulary and tl.get("glossary"):
                        job["vocabulary"] = list(tl["glossary"])[:100]
                    jobs.append(job)
    return jobs


# -- one job --------------------------------------------------------------------------------

class WavCache:
    def __init__(self):
        self._d = {}
        self._lock = threading.Lock()

    def get(self, path):
        with self._lock:
            if path not in self._d:
                data = Path(path).read_bytes()
                self._d[path] = (data, len(ba.wav_frames(data)) / 2.0 / ba.SR)
            return self._d[path]


def run_job(bench, job, folder, tl, wavs, stop=None):
    """One STT request, scored. ``stop`` (a threading.Event) is set on a wrong token / missing or
    stubbed function, and every job after that returns {"status": "skipped"} without a call."""
    if stop is not None and stop.is_set():
        return dict(job, status="skipped")
    data, audio_s = wavs.get(str(Path(folder) / job["file"]))
    rec = dict(job, audio_s=round(audio_s, 3))
    t0 = time.perf_counter()
    try:
        res = bench.stt(data, job["engine"], job["language"], languages=job.get("languages"),
                        vocabulary=job.get("vocabulary"), diarize=True if job.get("diarize") else None)
    except BenchError as e:
        rec.update(status="error", error=str(e)[:500], error_status=e.status, error_code=e.code,
                   client_s=round(time.perf_counter() - t0, 3))
        if is_fatal(e):
            rec["fatal"] = True
            if stop is not None:
                stop.set()
        return rec
    except (OSError, ValueError) as e:
        rec.update(status="error", error=str(e)[:500], client_s=round(time.perf_counter() - t0, 3))
        return rec
    try:
        return _score_job(rec, res, tl, job, t0, audio_s)
    except Exception as e:  # noqa: BLE001 - an odd answer must not stop the whole run
        rec.update(status="error", error=f"scoring failed: {type(e).__name__}: {e}"[:500],
                   text=str(res.get("text") or "")[:2000])
        return rec


def _score_job(rec, res, tl, job, t0, audio_s):
    client_s = (res.get("client_ms") / 1000.0) if res.get("client_ms") is not None else time.perf_counter() - t0
    server_s = (res.get("server_ms") / 1000.0) if isinstance(res.get("server_ms"), (int, float)) else None
    provider_s = (res.get("provider_ms") / 1000.0) if isinstance(res.get("provider_ms"), (int, float)) else None
    engine_s = provider_s if provider_s is not None else server_s
    exp, other = expected_turns(tl, job["input"])
    words = res.get("words") if isinstance(res.get("words"), list) else None
    sc = metrics.score_transcript(exp, res.get("text") or "", words, other_turns=other)
    ov = sc["overall"]
    rec.update(
        status="ok", client_s=round(client_s, 3), server_s=None if server_s is None else round(server_s, 3),
        provider_s=None if provider_s is None else round(provider_s, 3),
        rtf=None if engine_s is None or not audio_s else round(engine_s / audio_s, 4),
        language_sent=res.get("language_sent"), tries=res.get("tries") or 1,
        client_total_s=round(res["client_total_ms"] / 1000.0, 3) if res.get("client_total_ms") is not None else None,
        **{k: res[k] for k in ("covered", "retried_without_language", "vocabulary_dropped") if k in res},
        rtf_client=round(client_s / audio_s, 4) if audio_s else None,
        engine_model=res.get("engine_model"), detected_language=res.get("language"),
        words=len(words) if words is not None else None, method=sc["method"], text=res.get("text") or "",
        metrics={k: ov[k] for k in ("wer", "cer", "errors", "sub", "del", "ins", "ref_units", "hyp_units",
                                    "char_errors", "ref_chars", "echo_units", "echo_rate", "dropped_turns",
                                    "unassigned_units", "turn_errors", "wer_turns")},
        bag=ov["bag"], by_lang=sc["by_lang"], mixed=sc["mixed"], single=sc["single"], per_turn=sc["per_turn"])
    return rec


# -- summary --------------------------------------------------------------------------------

def _rate(err, n):
    return round(err / n, 4) if n else None


def _pool(jobs, field="metrics"):
    ref_u = sum(j[field]["ref_units"] for j in jobs)
    err = sum(j[field]["errors"] for j in jobs)
    ref_c = sum(j[field]["ref_chars"] for j in jobs)
    cerr = sum(j[field]["char_errors"] for j in jobs)
    terr = sum(j[field].get("turn_errors", j[field]["errors"]) for j in jobs)
    return {"wer": _rate(err, ref_u), "cer": _rate(cerr, ref_c), "wer_turns": _rate(terr, ref_u),
            "ref_units": ref_u, "ref_chars": ref_c}


def summarize(conversations):
    jobs = [j for c in conversations for j in c["jobs"]]
    ok = [j for j in jobs if j.get("status") == "ok"]
    groups = {}
    for j in jobs:
        groups.setdefault((j["engine"], j["variant"], j["input"], j["mode"]), []).append(j)
    configs = []
    for (engine, variant, inp, mode), js in sorted(groups.items()):
        good = [j for j in js if j.get("status") == "ok"]
        row = {"engine": engine, "variant": variant, "input": inp, "mode": mode, "n": len(js),
               "n_ok": len(good), "n_errors": len(js) - len(good)}
        if good:
            row.update(_pool(good))
            row["wer_mean"] = metrics.summary([j["metrics"]["wer"] for j in good])["mean"]
            row["bag_f1"] = metrics.summary([j["bag"]["f1"] for j in good])["mean"]
            row["echo_units"] = sum(j["metrics"]["echo_units"] for j in good)
            row["dropped_turns"] = sum(j["metrics"]["dropped_turns"] for j in good)
            row["client_s"] = metrics.summary([j["client_s"] for j in good])
            row["server_s"] = metrics.summary([j.get("server_s") for j in good])
            row["provider_s"] = metrics.summary([j.get("provider_s") for j in good])
            row["rtf"] = metrics.summary([j.get("rtf") for j in good])
            row["audio_s"] = round(sum(j["audio_s"] for j in good), 1)
            row["detected_ok"] = _detected_ok(good)
            row["retried"] = sum(1 for j in good if (j.get("tries") or 1) > 1)
            if mode == "fixed":
                row["lang_not_sent"] = sum(1 for j in good if not j.get("language_sent"))
        configs.append(row)

    # per language and mixed lines: the per-track inputs (system, mic), every turn once per job
    by_lang, mixed = {}, {}
    for j in ok:
        if j["input"] not in ("system", "mic"):
            continue
        for t in j["per_turn"]:
            by_lang.setdefault((j["engine"], j["variant"], j["mode"], t["lang"]), []).append(t)
            mixed.setdefault((j["engine"], j["variant"], j["mode"], "mixed" if t["mixed"] else "single"), []).append(t)
    lang_rows = []
    for (engine, variant, mode, lang), rows in sorted(by_lang.items()):
        agg = metrics.aggregate_turns(rows, lang)
        lang_rows.append({"engine": engine, "variant": variant, "mode": mode, "lang": lang, **agg})
    mixed_rows = []
    keys = sorted({k[:3] for k in mixed})
    for engine, variant, mode in keys:
        m = mixed.get((engine, variant, mode, "mixed")) or []
        s = mixed.get((engine, variant, mode, "single")) or []
        am, as_ = metrics.aggregate_turns(m) if m else None, metrics.aggregate_turns(s) if s else None
        mixed_rows.append({"engine": engine, "variant": variant, "mode": mode,
                           "mixed_turns": len(m), "mixed_wer": am and am["wer"], "mixed_cer": am and am["cer"],
                           "single_turns": len(s), "single_wer": as_ and as_["wer"], "single_cer": as_ and as_["cer"]})

    engines = []
    for engine in sorted({j["engine"] for j in jobs}):
        js = [j for j in jobs if j["engine"] == engine and j["variant"] == "clean"]
        good = [j for j in js if j.get("status") == "ok"]
        row = {"engine": engine, "n": len(js), "n_errors": len(js) - len(good)}
        if good:
            row.update(_pool(good))
            row["client_s"] = metrics.summary([j["client_s"] for j in good])
            row["rtf"] = metrics.summary([j.get("rtf") for j in good])
        engines.append(row)

    best = []
    for variant, inp in sorted({(c["variant"], c["input"]) for c in configs}):
        cands = [c for c in configs if c["variant"] == variant and c["input"] == inp and c.get("wer") is not None]
        if not cands:
            continue
        acc = min(cands, key=lambda c: (c["wer"], c.get("cer") or 0))
        fast = min(cands, key=lambda c: (c["client_s"]["p50"] if c.get("client_s", {}).get("p50") is not None else 1e9))
        best.append({"variant": variant, "input": inp,
                     "most_accurate": f"{acc['engine']} ({acc['mode']})", "wer": acc["wer"], "cer": acc.get("cer"),
                     "fastest": f"{fast['engine']} ({fast['mode']})", "fastest_p50_s": fast["client_s"]["p50"],
                     "fastest_wer": fast["wer"]})
    # "fixed" jobs whose language the engine was not sent (Voxtral lacks it, Scribe refused it): auto there
    not_sent = {}
    for j in ok:
        if j["mode"] == "fixed" and j.get("language") and not j.get("language_sent"):
            not_sent.setdefault((j["engine"], j["language"]), []).append(j)
    fixed_not_sent = [{"engine": e, "language": lg, "jobs": len(js)} for (e, lg), js in sorted(not_sent.items())]
    return {"n_jobs": len(jobs), "n_errors": len(jobs) - len(ok),
            "audio_s_total": round(sum(j["audio_s"] for j in ok), 1),
            "configs": configs, "by_engine": engines, "by_lang": lang_rows, "mixed": mixed_rows, "best": best,
            "fixed_not_sent": fixed_not_sent}


def _detected_ok(jobs):
    """Share of auto-mode single-track jobs whose detected language is the track's language."""
    hits = n = 0
    for j in jobs:
        if j["mode"] != "auto" or j["input"] == "mix" or not j.get("detected_language"):
            continue
        n += 1
        want = j.get("track_lang")
        hits += int(bool(want) and str(j["detected_language"]).lower().startswith(want))
    return round(hits / n, 3) if n else None


# -- CLI ------------------------------------------------------------------------------------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Meeting transcription (batch STT) benchmark.")
    ap.add_argument("--engines", default=",".join(STT_ENGINES))
    ap.add_argument("--inputs", default=",".join(INPUTS))
    ap.add_argument("--modes", default=",".join(DEFAULT_MODES),
                    help="fixed,auto,multi (multi runs only on engines that take a language list: gladia)")
    ap.add_argument("--no-vocabulary", action="store_true",
                    help="do not send the conversation's glossary as custom vocabulary (Gladia uses it)")
    ap.add_argument("--diarize", action="store_true", help="ask the engines for speaker labels too")
    ap.add_argument("--variants", default="clean", help="clean,noisy,speakers (default clean)")
    ap.add_argument("--only", default="", help="conversation ids or prefixes, comma-separated")
    ap.add_argument("--limit", type=int, default=0, help="run at most this many jobs (a quick check)")
    ap.add_argument("--workers", type=int, default=3, help="parallel STT calls")
    ap.add_argument("--dry-run", action="store_true", help="fake engines, offline")
    ap.add_argument("--corpus", default=str(generate.CORPUS_DIR))
    ap.add_argument("--audio-dir", default="", help="default corpus/audio (corpus/audio_dry for --dry-run)")
    ap.add_argument("--results-dir", default=str(RESULTS_DIR))
    ap.add_argument("--url", default="")
    ap.add_argument("--seed", type=int, default=0, help="fake engines only")
    ap.add_argument("--quiet", action="store_true")
    return ap.parse_args(argv)


def main(argv=None, bench=None):
    _utf8_console()
    args = parse_args(argv)
    split = (lambda s: [x.strip() for x in s.split(",") if x.strip()])
    engines, inputs, modes, variants = split(args.engines), split(args.inputs), split(args.modes), split(args.variants)
    only = split(args.only)
    for name, vals, allowed in (("input", inputs, INPUTS), ("mode", modes, MODES), ("variant", variants, generate.VARIANTS)):
        bad = [v for v in vals if v not in allowed]
        if bad:
            print(f"unknown {name}(s): {', '.join(bad)} (choose from {', '.join(allowed)})", file=sys.stderr)
            return 2
    unknown = [e for e in engines if e not in STT_ENGINES]
    if unknown:
        print(f"note: engine(s) {', '.join(unknown)} are not in {STT_ENGINES}; the bench function decides")
    audio_dir = Path(args.audio_dir) if args.audio_dir else generate.default_dirs(args.corpus, args.dry_run)[0]
    say = (lambda *a: None) if args.quiet else print

    if args.dry_run:
        ensure_dry_audio(args.corpus, audio_dir, only, log=say)
    timelines = load_timelines(audio_dir, only)
    if not timelines:
        print(f"no generated audio in {audio_dir}: run python generate.py{' --dry-run' if args.dry_run else ''} first")
        return 1
    if not args.dry_run:
        fake = [tl["id"] for _d, tl in timelines if tl.get("dry_run")]
        if fake:
            print(f"refusing: {', '.join(fake)} in {audio_dir} hold fake (dry-run) audio; regenerate them for real")
            return 1
    if bench is None:
        bench = FakeBench(seed=args.seed) if args.dry_run else make_bench(False, args.url or None)
    if hasattr(bench, "register_timeline"):
        for folder, tl in timelines:
            bench.register_timeline(tl, folder)

    jobs = []
    for folder, tl in timelines:
        for j in plan_jobs(tl, engines, inputs, modes, variants, vocabulary=not args.no_vocabulary):
            if args.diarize:
                j["diarize"] = True
            jobs.append((folder, tl, j))
    if args.limit:
        jobs = jobs[: args.limit]
    say(f"{'DRY RUN (fake engines) ' if args.dry_run else ''}{len(jobs)} STT jobs over {len(timelines)} "
        f"conversation(s): engines {','.join(engines)}; inputs {','.join(inputs)}; modes {','.join(modes)}; "
        f"variants {','.join(variants)}")
    minutes = {}
    for _folder, tl, j in jobs:
        minutes[j["engine"]] = minutes.get(j["engine"], 0.0) + float(tl.get("duration") or 0) / 60.0
    say("audio to send: " + ", ".join(f"{e} {m:.0f} min" for e, m in sorted(minutes.items()))
        + ("" if args.dry_run else " (each engine bills its own audio time; Scribe uses ElevenLabs credits)"))
    usage_before = None if args.dry_run else account_usage(bench)
    if usage_before:
        say(f"ElevenLabs account before: {usage_before.get('used')} of {usage_before.get('limit')} used")
    wavs = WavCache()
    records = {}
    lock = threading.Lock()
    done = [0]
    t_start = time.perf_counter()
    interrupted = fatal = False
    stop = threading.Event()
    futs = {}
    ex = cf.ThreadPoolExecutor(max_workers=max(1, args.workers))
    try:
        futs = {ex.submit(run_job, bench, j, folder, tl, wavs, stop): (k, tl) for k, (folder, tl, j) in enumerate(jobs)}
        for fut in cf.as_completed(futs):
            k, tl = futs[fut]
            if fut.cancelled():
                continue
            rec = fut.result()
            if rec.get("status") == "skipped":
                continue
            rec["track_lang"] = fixed_language(tl, rec["input"])
            with lock:
                records[k] = rec
                done[0] += 1
                if rec["status"] == "ok":
                    m = rec["metrics"]
                    say(f"[{done[0]}/{len(jobs)}] {rec['conv']} {rec['variant']}/{rec['input']} {rec['mode']}"
                        f"({rec['language'] or 'auto'}) {rec['engine']}: WER {100 * m['wer']:.1f}% "
                        f"CER {100 * m['cer']:.1f}%  {rec['client_s']:.1f} s")
                else:
                    say(f"[{done[0]}/{len(jobs)}] {rec['conv']} {rec['variant']}/{rec['input']} {rec['mode']} "
                        f"{rec['engine']}: ERROR {rec['error'][:160]}")
            if rec.get("fatal") and not fatal:
                fatal = True
                print(f"stopping: {rec['error'][:200]}")
                for f in futs:
                    f.cancel()
    except KeyboardInterrupt:
        interrupted = True
        print("interrupted: saving what finished")
        for f in futs:
            f.cancel()
    finally:
        ex.shutdown(wait=not interrupted, cancel_futures=True)

    usage = None
    if usage_before:
        after = account_usage(bench)
        usage = {"before": usage_before, "after": after}
        if after and isinstance(after.get("used"), (int, float)) and isinstance(usage_before.get("used"), (int, float)):
            usage["delta"] = after["used"] - usage_before["used"]
    convs = []
    for folder, tl in timelines:
        js = [records[k] for k in sorted(records) if records[k]["conv"] == tl["id"]]
        if not js:
            continue
        convs.append({"id": tl["id"], "title": tl.get("title") or "", "my_lang": tl.get("my_lang"),
                      "their_lang": tl.get("their_lang"), "tags": tl.get("tags") or [],
                      "duration": tl.get("duration"), "turns": len(tl.get("turns") or []),
                      "mixed_turns": sum(1 for t in tl.get("turns") or [] if len(set(t.get("langs") or [])) > 1),
                      "jobs": js})
    data = {"kind": "stt", "stamp": None, "git": git_sha(),
            "config": {"engines": engines, "inputs": inputs, "modes": modes, "variants": variants,
                       "dry_run": bool(args.dry_run), "only": only, "limit": args.limit, "workers": args.workers,
                       "vocabulary": not args.no_vocabulary, "diarize": bool(args.diarize),
                       "audio_dir": str(audio_dir), "url": "" if args.dry_run else (args.url or "default"),
                       "interrupted": interrupted, "stopped_on_error": fatal,
                       "wall_s": round(time.perf_counter() - t_start, 1),
                       "audio_min_by_engine": {e: round(m, 1) for e, m in sorted(minutes.items())},
                       "elevenlabs_usage": usage},
            "conversations": convs, "summary": summarize(convs)}
    path = save_results(args.results_dir, "stt", data)
    s = data["summary"]
    say(f"\n{s['n_jobs']} jobs, {s['n_errors']} errors, {s['audio_s_total']} s of audio scored")
    if usage and usage.get("delta") is not None:
        say(f"ElevenLabs account: {usage['delta']} more used during this run (Scribe, and anything else on the "
            f"account meanwhile); now {usage['after'].get('used')} of {usage['after'].get('limit')}")
    for b in s["best"]:
        say(f"  {b['variant']}/{b['input']}: most accurate {b['most_accurate']} WER {100 * b['wer']:.1f}%; "
            f"fastest {b['fastest']} {b['fastest_p50_s']:.2f} s (WER {100 * b['fastest_wer']:.1f}%)")
    print(f"results: {path}")
    return 0 if not s["n_errors"] else 3


if __name__ == "__main__":
    sys.exit(main())
