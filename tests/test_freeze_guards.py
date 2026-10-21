"""Guards around the freeze fix (the speaker pass in its own process, the field
watchdog): the fix lives in meetings and in main()'s start-up only - plain
dictation (the same AudioRecorder class) is exactly as before, and the pieces
are wired the way the fix needs them. Source-level on purpose: nothing here
imports main.py (no Qt, no audio devices), so it runs in the Linux job too.
"""
import ast
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def _functions(path, owner, names):
    """{method name: ast node} of ``owner``'s methods in the file."""
    tree = ast.parse(_source(path))
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == owner:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name in names:
                    out[item.name] = item
    return out


def _function_sources(path, owner, names):
    src = _source(path)
    return {n: ast.get_source_segment(src, node) or "" for n, node in _functions(path, owner, names).items()}


def _identifiers(node):
    """Every name the code uses (variables, attributes, imports) - never its
    comments or docstrings."""
    found = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id.lower())
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr.lower())
        elif isinstance(sub, ast.alias):
            found.add(sub.name.lower())
        elif isinstance(sub, ast.ImportFrom) and sub.module:
            found.add(sub.module.lower())
    return found


FIX_WORDS = ("diariz", "stall_watchdog", "sherpa")


def test_dictation_never_touches_the_speaker_pass_or_the_watchdog():
    recorder = _functions("main.py", "AudioRecorder", {
        "start_recording", "_start_recording_impl", "_record", "_record_loop", "_drain_and_close_streams",
        "_transcribe_chunk", "request_abort", "stop_recording", "transcribe", "set_paused",
        "_run_local", "_run_local_once", "_run_local_with", "use_model", "load_model"})
    assert {"start_recording", "_record_loop", "stop_recording", "transcribe", "_run_local_with"} <= set(recorder)
    controller = _functions("main.py", "AppController", {
        "_on_hotkey", "_start", "_stop", "_stop_impl", "_cancel", "toggle_dictation", "force_stop_dictation",
        "_abort_everything", "_postprocess_transcript", "_paste_via_clipboard", "paste_text"})
    assert {"_on_hotkey", "_start", "_stop_impl"} <= set(controller)
    for where, methods in (("AudioRecorder", recorder), ("AppController", controller)):
        for name, node in methods.items():
            ids = _identifiers(node)
            for word in FIX_WORDS:
                hit = [i for i in ids if word in i]
                assert not hit, "%s.%s uses %s: dictation must not change" % (where, name, hit)


def test_the_speaker_pass_is_reached_only_through_diarization_diarize():
    # the one hand-over to the child process; nobody calls the worker directly
    for name in ("main.py", "meeting_session.py", "file_jobs.py"):
        src = _source(name)
        body = [ln for ln in src.splitlines() if "diarization_proc" in ln]
        for ln in body:
            assert ("import diarization_proc" in ln or "diarization_proc.worker_main" in ln
                    or "diarization_proc.cancel_all" in ln or "diarization_proc.sweep_stale" in ln
                    or ln.strip().startswith("#")), (name, ln)


def test_main_starts_the_watchdog_with_a_one_second_heartbeat_and_stops_it_at_exit():
    src = _source("main.py")
    start = src.index("def main():")
    body = src[start:]
    assert "stall_watchdog.start(logger)" in body
    assert "beat.setInterval(stall_watchdog.BEAT_MS)" in body
    assert "beat.timeout.connect(watchdog.beat)" in body
    assert "qapp.aboutToQuit.connect(watchdog.stop)" in body
    assert body.index("stall_watchdog.start(logger)") < body.index("sys.exit(qapp.exec())")
    import stall_watchdog
    assert stall_watchdog.BEAT_MS == 1000 and stall_watchdog.THRESHOLD_S == 5.0 and stall_watchdog.RELOG_S == 30.0


def test_start_up_clears_the_audio_copies_a_killed_speaker_pass_left_in_temp():
    src = _source("main.py")
    body = src[src.index("def main():"):]
    assert "diarization_proc.sweep_stale" in body
    assert 'kwargs={"max_age_s": 60.0}' in body
    # after the single-instance lock (no other Gusan's pass can be running), before the event loop
    assert body.index("acquire_single_instance_lock()") < body.index("diarization_proc.sweep_stale")
    assert body.index("diarization_proc.sweep_stale") < body.index("sys.exit(qapp.exec())")
    # and it is a plain thread: nothing on the start-up path waits for the disk
    assert "threading.Thread(target=diarization_proc.sweep_stale" in body


def test_quit_ends_a_running_speaker_pass_before_anything_else_shuts_down():
    quit_src = _function_sources("main.py", "AppController", {"quit_app"})["quit_app"]
    first = re.search(r"for stop in \(\s*lambda: ([^,]+),", quit_src)
    assert first and "diarization_proc.cancel_all()" in first.group(1)


def test_the_history_entry_is_written_by_the_worker_not_by_the_window_slot():
    src = _source("meeting_session.py")
    tree = ast.parse(src)
    segs = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            segs.setdefault(node.name, ast.get_source_segment(src, node) or "")
    assert "save_entry" not in segs["_on_processing_finished"]
    assert "save_entry" in segs["_save_history_entry"]
    done = segs["_summarize_and_finish"]
    assert done.index("self._save_history_entry(notes)") < done.index('self.proc_signals.finished.emit(notes, "")')


def test_the_watchdog_module_is_qt_free_and_plain_python():
    src = _source("stall_watchdog.py")
    assert "PySide6" not in src and "PyQt" not in src
    compile(src, "stall_watchdog.py", "exec", dont_inherit=True)
    for name in ("diarization_proc.py", "stall_watchdog.py"):
        assert "PySide6" not in _source(name)
