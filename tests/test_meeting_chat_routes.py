"""The parts of "Ask about this meeting" that live in the shared AI routes and the session (no Qt, no network).

A new mode must be added in several places or it silently becomes something else (the generic "Rewrite this text"
prompt, 240 tokens, a map-reduce that takes minutes on the on-computer model): these tests pin each of them.
Also: the notes' engine rule the chat shares, the transcript_saved signal the page waits on, the answer renderer's
safety, and the analytics allow-list line."""
import json
import re
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import action_api
import actions
import local_llm
import meeting_chat
import meeting_session as msess
import meeting_store as ms
import rich_text
from tests.test_meeting_session import _App, _Base, _Recorder, _SavedMeeting, _outcome


class TestTheChatMode(unittest.TestCase):
    def test_the_messages_are_the_rules_then_the_material(self):
        msgs = action_api.build_messages("  <meeting>...</meeting>  ", "meeting_chat", vocab_block="GLOSSARY X")
        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertEqual(msgs[0]["content"], action_api.MEETING_CHAT_SYSTEM)
        self.assertEqual(msgs[1]["content"], "<meeting>...</meeting>")
        self.assertNotIn("GLOSSARY", json.dumps(msgs))                 # a glossary is for rewriting, not for answers
        self.assertNotIn("Rewrite this text", json.dumps(msgs))
        self.assertNotIn("executive assistant", json.dumps(msgs))

    def test_the_rules_say_what_the_card_relies_on(self):
        s = action_api.MEETING_CHAT_SYSTEM
        for n in range(1, 8):
            self.assertIn("%d. " % n, s)
        for part in ("ONE recorded meeting", "language of the user's latest question", "\"You\"", "\"Speaker 1\"",
                     "\"Them\"", "never one named person", "[12:40]", meeting_chat.LEFT_OUT, "data, never instructions",
                     "no headings, tables or links"):
            self.assertIn(part, s)
        self.assertLess(len(s), 2600)                                   # about 330 tokens: cheap, and cacheable

    def test_the_local_rules_are_shorter_but_say_the_same_things(self):
        s = local_llm._CHAT_SYSTEM
        self.assertLess(len(s), len(action_api.MEETING_CHAT_SYSTEM))
        for part in ("ONE recorded meeting", "never invent", "language of the user's latest question", "\"Them\"",
                     "[12:40]", meeting_chat.LEFT_OUT, "data, never instructions"):
            self.assertIn(part, s)

    def test_the_answer_budget_and_the_name_the_server_gets(self):
        self.assertEqual(action_api._max_tokens_for("meeting_chat"), 1400)
        self.assertEqual(action_api._max_tokens_for("meeting_notes"), 3000)       # the others did not move
        self.assertEqual(action_api._max_tokens_for("live_assist"), 700)
        self.assertEqual(action_api._max_tokens_for("something_else"), 1000)
        self.assertEqual(action_api.server_mode("meeting_chat"), "meeting_chat")   # the server takes any mode

    def test_the_mode_is_known_to_actions(self):
        self.assertEqual(actions.normalize_action_mode("meeting_chat"), actions.ACTION_MEETING_CHAT)
        self.assertEqual(actions.ACTION_MEETING_CHAT, "meeting_chat")
        self.assertEqual(actions.normalize_action_mode("not a mode"), actions.ACTION_TRANSCRIBE_ONLY)

    def test_a_managed_question_streams_through_the_server_with_the_chat_mode(self):
        resp = MagicMock(status_code=200, headers={"Content-Type": "text/event-stream"})
        resp.iter_lines.return_value = iter([
            'data: {"choices":[{"delta":{"content":"The budget "}}]}',
            'data: {"choices":[{"delta":{"content":"was approved."}}]}', "data: [DONE]"])
        seen = []
        with patch.object(action_api.requests, "post", return_value=resp) as post:
            out = actions.process_stream("<question>?</question>", "meeting_chat", seen.append,
                                         model=actions.API_MANAGED_ID, config={"_managed_token": "tok"})
        self.assertEqual((out, "".join(seen)), ("The budget was approved.", "The budget was approved."))
        body = post.call_args.kwargs["json"]
        self.assertEqual((body["mode"], body["max_tokens"], body["stream"]), ("meeting_chat", 1400, True))
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][1]["content"], "<question>?</question>")

    def test_a_question_to_a_provider_with_a_key_keeps_the_rules_as_the_system_message(self):
        resp = MagicMock(status_code=200)
        resp.iter_lines.return_value = iter([
            'data: {"type":"content_block_delta","delta":{"text":"Hi"}}', 'data: {"type":"message_stop"}'])
        cfg = {"action_api_key": "k", "action_api_provider": action_api.PROVIDER_ANTHROPIC}
        with patch.object(action_api.requests, "post", return_value=resp) as post:
            action_api.run_action_stream("Q", "meeting_chat", cfg, lambda d: None)
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["system"], action_api.MEETING_CHAT_SYSTEM)
        self.assertEqual(body["messages"], [{"role": "user", "content": "Q"}])
        self.assertEqual(body["max_tokens"], 1400)

    def test_no_glossary_is_looked_up_for_a_question(self):
        with patch.object(actions.vocabulary, "spelling_authority_block", side_effect=AssertionError("asked")), \
                patch.object(action_api, "run_managed_action_stream", return_value="ok") as run:
            actions.process_stream("q", "meeting_chat", lambda d: None, model=actions.API_MANAGED_ID,
                                   config={"_managed_token": "t"})
        self.assertEqual(run.call_args.kwargs["vocab_block"], "")

    def test_privacy_mode_refuses_the_cloud_for_a_question(self):
        with patch.object(action_api, "run_managed_action_stream") as run:
            with self.assertRaises(actions.ActionError):
                actions.process_stream("q", "meeting_chat", lambda d: None, model=actions.API_MANAGED_ID,
                                       config={"_managed_token": "t", "privacy_mode": True})
        run.assert_not_called()


class _FakeLlm:
    def __init__(self, tokens_per_char=0.3):
        self.chat_calls = []
        self.ratio = tokens_per_char

    def tokenize(self, data, add_bos=False, special=False):
        return [0] * int(len(data) * self.ratio)

    def create_chat_completion(self, **kw):
        self.chat_calls.append(kw)
        return iter([{"choices": [{"delta": {"content": "Hel"}}]}, {"choices": [{"delta": {"content": "lo."}}]}])


@contextmanager
def _fake_model(llm):
    with patch.object(local_llm, "_lease", side_effect=lambda mid: _Ctx(llm)), \
            patch.object(local_llm, "_infer_lock_for", side_effect=lambda mid: _Ctx(None)):
        yield


class _Ctx:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *a):
        return False


class TestTheChatOnThisComputer(unittest.TestCase):
    def test_a_short_answer_budget_and_its_own_rules(self):
        self.assertEqual(local_llm._MAX_TOKENS_BY_MODE["meeting_chat"], 600)
        msgs = local_llm._messages_for("meeting_chat", "THE MATERIAL", "auto", "en", vocab_block="GLOSSARY")
        self.assertEqual(msgs, [{"role": "system", "content": local_llm._CHAT_SYSTEM},
                                {"role": "user", "content": "THE MATERIAL"}])
        self.assertNotIn("Rewrite", json.dumps(msgs))

    def test_the_window_is_the_fixed_eight_thousand(self):
        self.assertEqual(local_llm._N_CTX, 8192)                      # meeting_chat.local_budget is built on it

    def test_a_question_that_fits_streams(self):
        llm = _FakeLlm()
        seen = []
        with _fake_model(llm):
            out = local_llm.run_action_stream("short question", "meeting_chat", seen.append, model_id="qwen_3b")
        self.assertEqual((out, seen), ("Hello.", ["Hel", "lo."]))
        self.assertEqual(llm.chat_calls[0]["max_tokens"], 600)

    def test_a_question_that_does_not_fit_is_refused_before_anything_is_generated(self):
        llm = _FakeLlm()
        with _fake_model(llm), patch.object(local_llm, "_run_action_locked",
                                           side_effect=AssertionError("map-reduce")) as slow:
            with self.assertRaises(local_llm.ContextTooLong):
                local_llm.run_action_stream("x" * 40000, "meeting_chat", lambda d: None, model_id="qwen_3b")
        slow.assert_not_called()
        self.assertEqual(llm.chat_calls, [])

    def test_other_modes_still_condense_long_input(self):
        llm = _FakeLlm()
        seen = []
        with _fake_model(llm), patch.object(local_llm, "_run_action_locked", return_value="short") as slow:
            out = local_llm.run_action_stream("x" * 40000, "summarize", seen.append, model_id="qwen_3b")
        self.assertEqual((out, seen), ("short", ["short"]))
        slow.assert_called_once()

    def test_actions_turns_it_into_its_own_error_the_chat_can_shrink_on(self):
        with patch.object(local_llm, "model_downloaded", return_value=True), \
                patch.object(local_llm, "run_action_stream", side_effect=local_llm.ContextTooLong("big")):
            with self.assertRaises(actions.ContextTooLong) as cm:
                actions.process_stream("q", "meeting_chat", lambda d: None, model="qwen_3b", config={})
        self.assertIsInstance(cm.exception, actions.ActionError)

    def test_a_model_that_is_not_downloaded_is_said_not_loaded(self):
        with patch.object(local_llm, "model_downloaded", return_value=False):
            with self.assertRaises(actions.ActionError) as cm:
                actions.process_stream("q", "meeting_chat", lambda d: None, model="qwen_3b", config={})
        self.assertIn("isn't downloaded", str(cm.exception))


def _old_live_engine(engine, config):
    """actions.live_engine as it was before usable_engine was taken out of it."""
    config = config or {}
    kind = actions.ACTION_MODELS.get(engine, {}).get("kind")
    if config.get("privacy_mode") or config.get("live_assist_writer") == "local":
        return engine if kind == "local_llm" else actions._first_downloaded_local_model()
    has_token = bool(config.get("_managed_token"))
    if has_token:
        return actions.API_MANAGED_ID
    if (engine == actions.RULE_BASED_ID or kind is None
            or (kind == "cloud" and not actions.engine_has_key(engine, config))
            or (kind == "managed" and not has_token)):
        return actions._first_downloaded_local_model()
    return engine


class TestWhichEngineCanRun(unittest.TestCase):
    ENGINES = ("rule_based", "managed", "api_openai_compatible", "api_gemini", "qwen_tiny", "qwen_3b", "nonsense", None)

    def test_live_engine_is_what_it_was(self):
        configs = [{}, {"_managed_token": "t"}, {"privacy_mode": True}, {"live_assist_writer": "local"},
                   {"action_api_key": "k"}, {"google_api_key": "g"}, {"action_api_key": "k", "_managed_token": "t"}]
        for downloaded in (set(), {"qwen_tiny"}, {"qwen_3b", "gemma_2b"}):
            with patch.object(local_llm, "model_downloaded", side_effect=lambda m: m in downloaded):
                for engine in self.ENGINES:
                    for cfg in configs:
                        self.assertEqual(actions.live_engine(engine, cfg), _old_live_engine(engine, cfg),
                                         (engine, cfg, downloaded))

    def test_usable_engine(self):
        have = lambda m: m == "qwen_3b"                                              # noqa: E731
        self.assertEqual(actions.usable_engine("managed", {"_managed_token": "t"}, have), "managed")
        self.assertEqual(actions.usable_engine("managed", {}, have), "qwen_3b")
        self.assertEqual(actions.usable_engine("managed", {}, lambda m: False), None)
        self.assertEqual(actions.usable_engine("rule_based", {"_managed_token": "t"}, have), "qwen_3b")
        self.assertEqual(actions.usable_engine("api_openai_compatible", {"action_api_key": "k"}, have),
                         "api_openai_compatible")
        self.assertEqual(actions.usable_engine("api_openai_compatible", {}, lambda m: False), None)
        self.assertEqual(actions.usable_engine("qwen_tiny", {}, lambda m: False), "qwen_tiny")   # taken as it is
        self.assertEqual(actions.usable_engine(None, None, have), "qwen_3b")

    def test_live_engine_still_asks_the_one_argument_less_helper_the_older_tests_replace(self):
        # tests (and callers) replace actions._first_downloaded_local_model with a function of no arguments
        with patch.object(actions, "_first_downloaded_local_model", lambda: "qwen_tiny"):
            self.assertEqual(actions.live_engine("rule_based", {}), "qwen_tiny")
            self.assertEqual(actions.live_engine("managed", {}), "qwen_tiny")
        with patch.object(actions, "_first_downloaded_local_model", lambda: None):
            self.assertIsNone(actions.live_engine("rule_based", {}))

    def test_the_allowance_sentence_is_one_sentence_in_one_place(self):
        self.assertEqual(actions.CLOUD_ALLOWANCE_USED, "Today's cloud allowance is used up. It comes back tomorrow.")
        self.assertNotIn("limit", actions.CLOUD_ALLOWANCE_USED.lower())


class TestTheNotesEngineTheChatShares(unittest.TestCase):
    def test_notes_engine_is_the_old_rule(self):
        for pro in (True, False):
            for downloaded in (set(), {"qwen_tiny"}):
                have = lambda m: m in downloaded                                     # noqa: E731
                with patch.object(local_llm, "model_downloaded", side_effect=have):
                    app = _App(pro=pro, action_model="rule_based")
                    for saved in ("rule_based", "managed", "qwen_3b", "api_gemini", "built_in", ""):
                        app.cfg["action_model"] = saved
                        for override in (None, "", "qwen_tiny", "managed"):
                            old = override or (msess.default_engine_id(app)
                                               if actions.normalize_action_model(saved) == actions.RULE_BASED_ID
                                               else None)
                            self.assertEqual(msess.effective_notes_engine(app, override), old, (pro, saved, override))
                            self.assertEqual(msess.notes_engine(app.cfg, pro, override, have), old)

    def test_default_engine(self):
        self.assertEqual(msess.default_engine(True), "managed")
        self.assertEqual(msess.default_engine(False, lambda m: m == "qwen_3b"), "qwen_3b")
        self.assertEqual(msess.default_engine(False, lambda m: False), "managed")
        self.assertEqual(msess.default_engine_id(None), "managed")

    def test_the_session_uses_the_same_function(self):
        app = _App(pro=True, action_model="rule_based")
        s = msess.MeetingEngine(app)
        self.assertEqual(s._effective_notes_engine(), msess.effective_notes_engine(app, None))
        s._notes_engine = "qwen_tiny"
        self.assertEqual(s._effective_notes_engine(), "qwen_tiny")


class TestTranscriptSaved(_Base):
    """The Stop's worker says when the FINAL transcript.txt (with You / Them) is on disk, before the notes."""

    def _run(self, app=None, notes="## Notes", write_ok=True, early=None):
        s = self.session(app or _App())
        heard = []
        s.transcript_saved.connect(lambda folder: heard.append((folder, (Path(folder) / "transcript.txt").read_text(
            encoding="utf-8") if (Path(folder) / "transcript.txt").exists() else None, s.transcript_final(folder))))
        s._build_attributed_transcript = early or (lambda: "You: Hello team.\nThem: We ship Friday.")
        s.start("Q4", "")
        s._facts = []
        orig = msess.actions.process

        def process(*a, **k):
            s._facts.append(("notes", s.transcript_final(str(s.meeting_dir))))
            return notes
        with patch.object(msess.actions, "process", side_effect=process):
            if write_ok:
                s.stop("")
            else:
                with patch.object(msess.meeting_store, "write_transcript", return_value=False):
                    s.stop("")
        del orig
        return s, heard

    def test_it_fires_once_after_the_final_transcript_is_written_and_before_the_notes(self):
        s, heard = self._run()
        folder = str(s.meeting_dir)
        self.assertEqual(len(heard), 1)
        self.assertEqual(heard[0][0], folder)
        self.assertEqual(heard[0][1], "You: Hello team.\nThem: We ship Friday.")        # the labelled one is on disk
        self.assertTrue(heard[0][2])                                                    # and it is final by then
        self.assertEqual(s._facts, [("notes", True)])
        self.assertTrue(s.transcript_final(folder))

    def test_until_then_the_plain_transcript_is_not_final(self):
        seen = []

        def slow_labels():
            folder = str(s.meeting_dir)
            seen.append((s.transcript_final(folder), (Path(folder) / "transcript.txt").exists(),
                         s.transcript_final(str(Path(folder).parent / "other"))))
            return ""
        s = self.session(_App())
        s._build_attributed_transcript = slow_labels
        s.start("Q4", "")
        with patch.object(msess.actions, "process", return_value="## Notes"):
            s.stop("")
        self.assertEqual(seen, [(False, True, True)])      # the plain file is there, the final one is not; other meetings are final
        self.assertTrue(s.transcript_final(str(s.meeting_dir)))

    def test_a_write_that_failed_says_nothing_and_never_leaves_the_chat_waiting(self):
        s, heard = self._run(write_ok=False)
        self.assertEqual(heard, [])
        self.assertTrue(s.transcript_final(str(s.meeting_dir)))        # the run ended: nothing waits

    def test_an_empty_meeting_never_leaves_it_waiting(self):
        app = _App()
        app.recorder.result = ("", "en")
        s = self.session(app)
        s._build_attributed_transcript = lambda: ""
        s.start("Q4", "")
        s.stop("")
        self.assertTrue(s.transcript_final(str(s.meeting_dir)))

    def test_the_qt_session_has_the_signal(self):
        names = [n for n, _t in msess._SIGNALS]
        self.assertIn("transcript_saved", names)
        if msess._QObject is not None:
            self.assertTrue(hasattr(msess.MeetingSession, "transcript_saved"))


class TestTranscriptSavedWhenAnInterruptedMeetingIsFinished(_SavedMeeting):
    def test_it_fires_after_the_rebuilt_transcript_is_written(self):
        d = self._folder()
        s = self.session(_App())
        self._build(s, _outcome(("You: rebuilt a\nSpeaker 1: rebuilt b",)))
        heard = _Recorder(s.transcript_saved)
        with patch.object(msess.actions, "process", return_value="## Notes"):
            self.assertTrue(s.retry_notes(None, folder=str(d)))
        self.assertEqual(heard.calls, [(str(d),)])
        self.assertTrue(s.transcript_final(str(d)))


class TestShowingAModelsAnswer(unittest.TestCase):
    def test_links_become_words_and_are_never_clickable(self):
        out = rich_text.answer_text("See [the plan](https://example.com/plan) and [https://a.co](https://a.co).")
        self.assertEqual(out.replace("\u200b", ""), "See the plan (https://example.com/plan) and https://a.co.")
        self.assertNotIn("](", out)
        html = rich_text.qt_html(out)
        self.assertNotIn("<a ", html)
        self.assertNotIn("href", html)

    def test_images_give_their_description_and_load_nothing(self):
        out = rich_text.answer_text("![a chart](http://evil.test/x.png) and ![](file:///C:/s.png)")
        self.assertEqual(out, "a chart and ")
        html = rich_text.qt_html(rich_text.answer_text("<img src='file:///C:/s.png'> <script>x</script> \\\\host\\share"))
        self.assertNotIn("<img", html)
        self.assertNotIn("<script", html)
        self.assertIn("&lt;img", html)

    def test_reference_style_links_go_too(self):
        out = rich_text.answer_text("Read [this][1].\n\n[1]: https://example.com\n")
        self.assertNotIn("[1]", out)
        self.assertNotIn("example.com", out)
        self.assertIn("Read this.", out)

    def test_a_table_becomes_lines(self):
        out = rich_text.answer_text("| Who | What |\n|---|---|\n| Aram | Send it |\n| Anna | Review |")
        self.assertEqual(out, "Who \u00b7 What\nAram \u00b7 Send it\nAnna \u00b7 Review")
        self.assertNotIn("|", out)
        self.assertEqual(rich_text.answer_text("a | b\n--- | ---\n1 | 2").split("\n")[1], "1 | 2")   # (a bare row stays)

    def test_bold_and_lists_survive_as_real_formatting(self):
        html = rich_text.qt_html(rich_text.answer_text("**Decisions**\n\n- one\n- two\n\n1. first\n2. second"))
        self.assertIn("<ul", html)
        self.assertIn("<ol", html)
        self.assertIn("<b>", html)
        self.assertNotIn("**", html)

    def test_a_half_written_marker_is_hidden_while_streaming_only(self):
        self.assertEqual(rich_text.answer_text("Say **this", streaming=True), "Say this")
        self.assertEqual(rich_text.answer_text("Run `pip", streaming=True), "Run pip")
        self.assertEqual(rich_text.answer_text("Say **this**", streaming=True), "Say **this**")
        self.assertEqual(rich_text.answer_text("Say **this"), "Say **this")
        self.assertEqual(rich_text.streaming_markdown("```python\nprint(1)"), "```python\nprint(1)\n```")
        self.assertEqual(rich_text.streaming_markdown(""), "")

    def test_a_long_string_without_spaces_gets_places_to_break(self):
        out = rich_text.answer_text("https://example.com/" + "a" * 120)
        self.assertIn("\u200b", out)
        self.assertEqual(out.replace("\u200b", ""), "https://example.com/" + "a" * 120)
        self.assertTrue(all(len(piece) <= 20 for piece in out.split("\u200b")))
        self.assertEqual(rich_text.answer_text("short words are left alone"), "short words are left alone")

    def test_the_break_points_never_split_a_bold_marker(self):
        text = "x" * 19 + "**" + "y" * 30 + "**"
        out = rich_text.answer_text(text)
        self.assertIn("**", out.replace("\u200b", ""))
        self.assertNotIn("*\u200b*", out)
        html = rich_text.qt_html(out)
        self.assertIn("<b>", html)

    def test_nothing_in_nothing_out(self):
        self.assertEqual(rich_text.answer_text(None), "")
        self.assertEqual(rich_text.answer_text(""), "")
        self.assertEqual(rich_text.answer_text("a\r\nb"), "a\nb")


class TestTheAnalyticsCount(unittest.TestCase):
    def test_the_server_list_has_the_event(self):
        src = (Path(__file__).resolve().parent.parent / "supabase" / "functions" / "transcribe-analytics" / "index.ts"
               ).read_text(encoding="utf-8")
        self.assertIn("'meeting_chat_asked'", src)
        self.assertEqual(len(re.findall(r"'meeting_chat_asked'", src)), 1)


if __name__ == "__main__":
    unittest.main()
