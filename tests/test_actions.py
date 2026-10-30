import unittest
from unittest.mock import patch

import action_api
import actions


class TestActions(unittest.TestCase):
    def test_transcribe_only_returns_text(self):
        self.assertEqual(
            actions.process("hello world", "transcribe_only"),
            "hello world",
        )

    def test_write_email_formats_draft(self):
        out = actions.process("please send the report today", "write_email")
        self.assertIn("Subject:", out)
        self.assertIn("Hi,", out)
        self.assertIn("Please send the report today.", out)

    def test_make_todo_list_formats_checkboxes(self):
        out = actions.process(
            "call Ani and then send the file. also review the notes",
            "make_todo_list",
        )
        self.assertIn("- [ ] Call Ani", out)
        self.assertIn("- [ ] Send the file", out)
        self.assertIn("- [ ] Review the notes", out)

    def test_unknown_action_falls_back_to_transcribe(self):
        self.assertEqual(
            actions.process("hello", "unknown"),
            "hello",
        )

    def test_qwen_missing_falls_back_for_email(self):
        with patch.object(actions.local_llm, "model_downloaded", return_value=False), \
             patch.object(actions.local_llm, "run_action") as run_action:
            out = actions.process(
                "send the report today",
                "write_email",
                model=actions.local_llm.QWEN_TINY_ID,
            )
        self.assertIn("Subject:", out)
        run_action.assert_not_called()

    def test_qwen_ready_handles_action(self):
        with patch.object(actions.local_llm, "model_downloaded", return_value=True), \
             patch.object(actions.local_llm, "run_action", return_value="LLM output") as run_action:
            out = actions.process(
                "send the report today",
                "write_email",
                model=actions.local_llm.QWEN_TINY_ID,
            )
        self.assertEqual(out, "LLM output")
        run_action.assert_called_once()

    def test_rule_based_formatter_does_not_call_llm(self):
        with patch.object(actions.local_llm, "model_downloaded", return_value=True), \
             patch.object(actions.local_llm, "run_action") as run_action:
            out = actions.process(
                "send the report today",
                "write_email",
                model=actions.RULE_BASED_ID,
            )
        self.assertIn("Subject:", out)
        run_action.assert_not_called()

    def test_smart_auto_email_uses_rule_formatter_when_rule_based(self):
        out = actions.process(
            "write an email to john that the report is ready tonight",
            actions.ACTION_SMART_AUTO,
            model=actions.RULE_BASED_ID,
        )
        self.assertIn("Subject:", out)
        self.assertIn("Hi,", out)
        self.assertNotIn("write an email", out.lower())

    def test_smart_auto_email_uses_llm_when_downloaded(self):
        with patch.object(actions.local_llm, "model_downloaded", return_value=True), \
             patch.object(actions.local_llm, "run_action", return_value="Subject: Hi\n\nBody\n\nBest,") as run_action:
            out = actions.process(
                "write an email to john that the report is ready",
                actions.ACTION_SMART_AUTO,
                model=actions.local_llm.QWEN_TINY_ID,
            )
        self.assertIn("Subject:", out)
        run_action.assert_called_once()

    def test_smart_auto_missing_local_model_raises_clear_error(self):
        # When the chosen local engine isn't downloaded, Smart mode must
        # surface a clear, actionable error instead of silently degrading to
        # rule-based output (which looked like "it just transcribed").
        with patch.object(actions.local_llm, "model_downloaded", return_value=False), \
             patch.object(actions.local_llm, "run_action") as run_action:
            with self.assertRaises(actions.ActionError) as ctx:
                actions.process(
                    "write an email to john that the report is ready",
                    actions.ACTION_SMART_AUTO,
                    model=actions.local_llm.QWEN_7B_ID,
                )
        self.assertIn("download", str(ctx.exception).lower())
        run_action.assert_not_called()

    def test_cloud_action_uses_configured_api(self):
        with patch.object(actions.action_api, "run_action", return_value="API output") as run_action:
            out = actions.process(
                "send the report today",
                "write_email",
                model=actions.API_OPENAI_ID,
                config={"action_api_key": "secret", "privacy_mode": False},
            )
        self.assertEqual(out, "API output")
        run_action.assert_called_once()

    def test_translate_can_use_qwen_when_argos_is_missing(self):
        with patch.object(actions.local_llm, "model_downloaded", return_value=True), \
             patch.object(actions.local_llm, "run_action", return_value="Bonjour") as run_action, \
             patch.object(actions, "_translate_local", side_effect=actions.ActionError("missing pack")):
            out = actions.process(
                "Hello",
                "translate",
                target_lang="fr",
                model="built_in",
            )
        self.assertEqual(out, "Bonjour")
        run_action.assert_called_once()

    def test_privacy_mode_forces_rule_based_actions(self):
        with patch.object(actions.action_api, "run_action") as run_action:
            out = actions.process(
                "send the report today",
                "write_email",
                model=actions.API_OPENAI_ID,
                config={"action_api_key": "secret", "privacy_mode": True},
            )
        self.assertIn("Subject:", out)
        run_action.assert_not_called()

    def test_managed_engine_calls_server_with_token(self):
        # Pro managed Smart Actions run through the server (the founder's keys);
        # the auth token is injected into config by the caller.
        with patch.object(actions.action_api, "run_managed_action", return_value="MANAGED") as run_managed:
            out = actions.process(
                "summarize the meeting we just had",
                actions.ACTION_SMART_AUTO,
                model=actions.API_MANAGED_ID,
                config={"_managed_token": "tok123"},
            )
        self.assertEqual(out, "MANAGED")
        run_managed.assert_called_once()

    def test_managed_engine_without_token_degrades_to_rule_based(self):
        # A lapsed/free user whose engine is still "managed" must not error - it
        # should quietly fall back to local rule-based output.
        with patch.object(actions.action_api, "run_managed_action") as run_managed:
            out = actions.process(
                "write an email to john that the report is ready tonight",
                actions.ACTION_SMART_AUTO,
                model=actions.API_MANAGED_ID,
                config={},
            )
        self.assertIn("Subject:", out)
        run_managed.assert_not_called()

    def test_managed_engine_blocked_by_privacy_mode(self):
        with patch.object(actions.action_api, "run_managed_action") as run_managed:
            out = actions.process(
                "send the report today",
                actions.ACTION_SMART_AUTO,
                model=actions.API_MANAGED_ID,
                config={"privacy_mode": True, "_managed_token": "tok"},
            )
        self.assertTrue(out)
        run_managed.assert_not_called()


class TestMeetingNotes(unittest.TestCase):
    SAMPLE = (
        "Hi everyone, thanks for joining the kickoff. We need to lock the "
        "API contract by Friday. I'll draft the spec and send it tonight. "
        "Aram should review it Monday morning. We also discussed the budget "
        "but did not decide anything yet. Open question: are we shipping the "
        "v2 endpoint with or without auth?"
    )

    def test_summarize_extractive_returns_a_short_summary(self):
        out = actions._summarize_extractive(self.SAMPLE, max_sentences=3)
        self.assertTrue(out)
        # Output is shorter than the original transcript and keeps the
        # first sentence + last sentence as anchors for context.
        self.assertLess(len(out), len(self.SAMPLE))
        self.assertTrue(out.startswith("Hi everyone"))
        # Sentence count in output should be at most max_sentences.
        sentence_count = sum(1 for ch in out if ch in ".!?")
        self.assertLessEqual(sentence_count, 3)
        self.assertGreaterEqual(sentence_count, 2)

    def test_action_items_heuristic_finds_obvious_todos(self):
        items = actions._extract_action_items_heuristic(self.SAMPLE)
        joined = " ".join(items).lower()
        self.assertIn("draft", joined)         # 'I'll draft the spec...'
        self.assertIn("review", joined)        # 'Aram should review...'

    def test_meeting_notes_rule_based_returns_structured_markdown(self):
        out = actions.process(self.SAMPLE, actions.ACTION_MEETING_NOTES,
                              model=actions.RULE_BASED_ID)
        self.assertIn("## Summary", out)
        self.assertIn("## Action items", out)
        self.assertIn("- [ ]", out)            # at least one checkbox item

    def test_meeting_notes_cloud_uses_api(self):
        # the notes are streamed: no whole-answer wait on a long meeting (see TestMeetingNotesStreaming)
        with patch.object(actions.action_api, "run_action_stream",
                          return_value="## Summary\nGreat meeting.") as call, \
                patch.object(actions.action_api, "run_action") as oneshot:
            out = actions.process(
                self.SAMPLE,
                actions.ACTION_MEETING_NOTES,
                model=actions.API_OPENAI_ID,
                config={"action_api_key": "secret", "privacy_mode": False},
            )
        self.assertIn("Great meeting", out)
        call.assert_called_once()
        oneshot.assert_not_called()

    def test_summarize_cloud_uses_api(self):
        with patch.object(actions.action_api, "run_action",
                          return_value="Two sentences.") as call:
            out = actions.process(
                self.SAMPLE,
                actions.ACTION_SUMMARIZE,
                model=actions.API_OPENAI_ID,
                config={"action_api_key": "secret", "privacy_mode": False},
            )
        self.assertEqual(out, "Two sentences.")
        call.assert_called_once()


def _stream_answer(text, cut=False, seen=None):
    """A stand-in for a streaming engine call: it returns ``text`` and says whether the model was cut."""
    def run(*args, **kwargs):
        if seen is not None:
            seen.append((args, kwargs))
        if cut and kwargs.get("info") is not None:
            kwargs["info"]["cut"] = True
        return text
    return run


LABELLED = ("You: Thanks for joining. I will send the budget by Friday.\n"
            "Them: Good, and we need to confirm the venue.\n"
            "Speaker 2: I'll check the venue on Monday. Let's decide on the date.\n")


class TestMeetingNotesStreaming(unittest.TestCase):
    """The notes are always streamed (a long meeting's notes outlast any whole-answer wait). A cut-off answer is trimmed, flagged
    and kept - unless it never got past the Summary."""
    MANAGED = {"_managed_token": "tok"}
    OWN = {"action_api_key": "k", "privacy_mode": False}

    def process(self, model, config, stream_name, **kw):
        with patch.object(actions.action_api, stream_name, **kw) as call, \
                patch.object(actions.action_api, "run_managed_action") as managed_once, \
                patch.object(actions.action_api, "run_action") as once:
            out = actions.process("You: hello", actions.ACTION_MEETING_NOTES, model=model, config=config)
        managed_once.assert_not_called()
        once.assert_not_called()
        return out, call

    def test_gusan_cloud_streams_the_notes(self):
        seen = []
        out, call = self.process(actions.API_MANAGED_ID, self.MANAGED, "run_managed_action_stream",
                                 side_effect=_stream_answer("## Summary\nOk.\n\n## Action items\n- [ ] x", seen=seen))
        self.assertEqual(out, "## Summary\nOk.\n\n## Action items\n- [ ] x")
        self.assertNotIsInstance(out, actions.action_api.CutShort)
        args, kwargs = seen[0]
        self.assertEqual(args[1:3], (actions.ACTION_MEETING_NOTES, "tok"))
        self.assertIsInstance(kwargs["info"], dict)

    def test_an_own_key_streams_the_notes(self):
        for model in (actions.API_OPENAI_ID, actions.API_ANTHROPIC_ID, actions.API_GEMINI_ID):
            config = dict(self.OWN, google_api_key="g")
            out, _call = self.process(model, config, "run_action_stream", side_effect=_stream_answer("## Summary\nOk."))
            self.assertEqual(out, "## Summary\nOk.", model)

    def test_the_streamed_pieces_are_not_shown_anywhere(self):
        self.assertIsNone(actions._drop_token("piece"))

    def test_other_actions_are_still_answered_in_one_piece(self):
        with patch.object(actions.action_api, "run_action", return_value="Two sentences.") as once, \
                patch.object(actions.action_api, "run_action_stream") as stream:
            out = actions.process("some text", actions.ACTION_SUMMARIZE, model=actions.API_OPENAI_ID, config=self.OWN)
        self.assertEqual(out, "Two sentences.")
        once.assert_called_once()
        stream.assert_not_called()

    CUT = ("## Summary\nAram led the call.\n\n## Action items\n- [ ] Send it (Owner: Aram, Due: Friday)\n"
           "- [ ] Half a sen")

    def test_a_cut_answer_is_trimmed_and_comes_back_flagged(self):
        for model, config, name in ((actions.API_MANAGED_ID, self.MANAGED, "run_managed_action_stream"),
                                    (actions.API_OPENAI_ID, self.OWN, "run_action_stream")):
            out, _call = self.process(model, config, name, side_effect=_stream_answer(self.CUT, cut=True))
            self.assertEqual(out, "## Summary\nAram led the call.\n\n## Action items\n"
                                  "- [ ] Send it (Owner: Aram, Due: Friday)")
            self.assertIsInstance(out, actions.action_api.CutShort)
            self.assertTrue(out.cut)

    def test_a_cut_answer_in_a_discussion_keeps_what_was_written(self):
        text = "## Summary\nA.\n\n## Discussion\n### One\n- It slips. A week.\n\n### Two\n- It cos"
        out, _call = self.process(actions.API_MANAGED_ID, self.MANAGED, "run_managed_action_stream",
                                  side_effect=_stream_answer(text, cut=True))
        self.assertEqual(out, "## Summary\nA.\n\n## Discussion\n### One\n- It slips. A week.")
        self.assertTrue(out.cut)

    def test_an_answer_cut_inside_the_summary_is_not_kept(self):
        with self.assertRaises(actions.ActionError) as cm:
            self.process(actions.API_MANAGED_ID, self.MANAGED, "run_managed_action_stream",
                         side_effect=_stream_answer("## Summary\nAram led the call and then", cut=True))
        self.assertEqual(str(cm.exception), actions.NOTES_CUT_MESSAGE)
        self.assertNotIn("token", actions.NOTES_CUT_MESSAGE.lower())

    def test_an_engine_error_is_an_action_error(self):
        err = actions.action_api.ActionAPIError("Daily fair-use limit reached")
        with self.assertRaises(actions.ActionError) as cm:
            self.process(actions.API_MANAGED_ID, self.MANAGED, "run_managed_action_stream", side_effect=err)
        self.assertIn("fair-use", str(cm.exception))

    def test_an_empty_answer_stays_empty(self):
        out, _call = self.process(actions.API_MANAGED_ID, self.MANAGED, "run_managed_action_stream",
                                  side_effect=_stream_answer("  "))
        self.assertEqual(out, "")

    def test_privacy_mode_never_streams_to_a_cloud(self):
        with patch.object(actions.action_api, "run_managed_action_stream") as m, \
                patch.object(actions.action_api, "run_action_stream") as s:
            out = actions.process(LABELLED, actions.ACTION_MEETING_NOTES, model=actions.API_MANAGED_ID,
                                  config={"_managed_token": "tok", "privacy_mode": True})
            out2 = actions.process(LABELLED, actions.ACTION_MEETING_NOTES, model=actions.API_OPENAI_ID,
                                   config={"action_api_key": "k", "privacy_mode": True})
        m.assert_not_called()
        s.assert_not_called()
        self.assertIn("## Summary", out)
        self.assertIn("## Summary", out2)


class TestRuleBasedNotesOnALabelledTranscript(unittest.TestCase):
    """The built-in formatter reads the talk, not the labels: no 'You:' or 'Them:' in its sentences. It stays short."""

    def notes(self, text):
        return actions.process(text, actions.ACTION_MEETING_NOTES, model=actions.RULE_BASED_ID)

    def test_no_label_reaches_the_summary_or_the_action_items(self):
        out = self.notes(LABELLED)
        for label in ("You:", "Them:", "Speaker 2:"):
            self.assertNotIn(label, out)
        self.assertIn("## Summary", out)
        self.assertIn("## Action items", out)
        self.assertIn("- [ ] I will send the budget by Friday", out)
        self.assertIn("- [ ] I'll check the venue on Monday", out)

    def test_it_is_short_and_has_no_discussion(self):
        long = "\n".join("You: Sentence number %d is about something. Them: Reply %d is about something else." % (i, i)
                         for i in range(400))
        out = self.notes(long)
        self.assertNotIn("## Discussion", out)
        self.assertLess(len(out.split()), 400)

    def test_a_line_without_its_full_stop_still_ends_its_sentence(self):
        out = self.notes("You: first line without stop\nThem: second line without stop\nYou: third")
        self.assertNotIn("You:", out)
        self.assertIn("first line without stop.", out)

    def test_a_transcript_without_labels_is_read_as_before(self):
        plain = "We need to lock the API contract by Friday. I'll draft the spec tonight. Nothing else."
        self.assertEqual(self.notes(plain), actions._meeting_notes_extractive(plain))
        self.assertIn("- [ ] We need to lock the API contract by Friday", self.notes(plain))

    def test_a_name_with_a_colon_is_not_a_label(self):
        out = self.notes("Budget: we need to send the budget by Friday. Anna: will check.")
        self.assertIn("Budget: we need to send the budget by Friday", out)


class TestGusanWords(unittest.TestCase):
    """Gusan 2.0: the engine errors users read name Gusan and point at the new
    Settings › Speech & AI page. Ids (modes, model ids) are unchanged."""
    SETTINGS = "Settings › Speech & AI"

    def _error(self, fn, *args, **kwargs):
        with self.assertRaises(actions.ActionError) as cm:
            fn(*args, **kwargs)
        return str(cm.exception)

    def test_managed_engine_is_gusan_cloud(self):
        self.assertEqual(actions.ACTION_MODELS[actions.API_MANAGED_ID]["label"], "Gusan Cloud (Pro)")
        self.assertEqual(actions.API_MANAGED_ID, "managed")
        self.assertEqual(actions.ACTION_LIVE_ASSIST, "live_assist")   # the server's mode

    def test_errors_point_at_speech_and_ai(self):
        self.assertIn("Gusan Pro", actions.NO_ENGINE_MESSAGE)
        self.assertIn(self.SETTINGS, actions.NO_ENGINE_MESSAGE)
        msgs = [
            self._error(actions.process, "hi there", actions.ACTION_SMART_AUTO,
                        model=actions.API_OPENAI_ID, config=None),
            self._error(actions.process_stream, "hi", actions.ACTION_LIVE_ASSIST, lambda d: None,
                        model=actions.API_MANAGED_ID, config={"privacy_mode": True}),
            self._error(actions.process_stream, "hi", actions.ACTION_LIVE_ASSIST, lambda d: None,
                        model=actions.API_MANAGED_ID, config={}),
            actions._live_assist_basic("User's question: what time is it?"),
        ]
        for msg in msgs:
            self.assertIn(self.SETTINGS, msg)
        self.assertIn("Gusan Cloud", msgs[2])

    def test_no_old_names_in_the_words(self):
        import inspect
        src = inspect.getsource(actions)
        for old in ("Transcribe Pro", "AI Actions", "Settings →", "Live Assistance"):
            self.assertNotIn(old, src, old)

    def test_live_answers_on_this_computer_say_what_to_download(self):
        # "Answers written by: This computer" (or Privacy Mode) with no local
        # model: signing in or an API key wouldn't help - name the download.
        for cfg in ({"live_assist_writer": "local"}, {"privacy_mode": True},
                    {"live_assist_writer": "local", "_managed_token": "T"}):
            msg = actions.no_live_engine_message(cfg)
            self.assertEqual(msg, "Download Qwen 1.5B in Settings › Speech & AI to answer on this computer.")
            self.assertNotIn("Sign in", msg)
            self.assertNotIn("API key", msg)
        for cfg in ({}, None, {"live_assist_writer": "cloud"}, {"live_assist_writer": ""}):
            self.assertEqual(actions.no_live_engine_message(cfg), actions.NO_ENGINE_MESSAGE)
        # It is the case live_engine answers None for.
        with patch.object(actions, "_first_downloaded_local_model", return_value=None):
            self.assertIsNone(actions.live_engine(actions.API_MANAGED_ID,
                                                  {"live_assist_writer": "local", "_managed_token": "T"}))


class TestGeminiRunsOnTheGoogleKey(unittest.TestCase):
    """The Gemini writer's key is the shared Google key (Settings' "Google
    Gemini"), never action_api_key - that holds the other writers' key."""

    def test_the_google_key_wins_and_an_openai_key_never_goes_to_google(self):
        both = {"google_api_key": " G-KEY ", "action_api_key": "sk-openai"}
        api = actions.cloud_api_config(actions.API_GEMINI_ID, both)
        self.assertEqual(api["action_api_key"], "G-KEY")
        self.assertEqual(api["action_api_provider"], action_api.PROVIDER_GEMINI)
        only_openai = {"action_api_key": "sk-openai", "google_api_key": ""}
        self.assertEqual(actions.cloud_api_config(actions.API_GEMINI_ID, only_openai)["action_api_key"], "")
        self.assertFalse(actions.engine_has_key(actions.API_GEMINI_ID, only_openai))   # "add its key"
        self.assertTrue(actions.engine_has_key(actions.API_GEMINI_ID, both))
        # The caller's config is never changed.
        self.assertEqual(both["action_api_key"], "sk-openai")

    def test_other_writers_keep_their_own_key(self):
        cfg = {"google_api_key": "G", "action_api_key": "sk-openai", "mistral_api_key": "M"}
        self.assertEqual(actions.cloud_api_config(actions.API_OPENAI_ID, cfg)["action_api_key"], "sk-openai")
        self.assertEqual(actions.cloud_api_config(actions.API_MISTRAL_ID, cfg)["action_api_key"], "sk-openai")
        self.assertEqual(actions.cloud_api_config(actions.API_MISTRAL_ID, {"mistral_api_key": "M"})
                         ["action_api_key"], "M")

    def test_a_gemini_call_sends_the_google_key(self):
        seen = {}

        def fake_run(text, mode, api_config, source_lang="auto", target_lang="en"):
            seen.update(api_config)
            return "ok"
        with patch.object(action_api, "run_action", fake_run):
            actions.process("hello there", actions.ACTION_SUMMARIZE, model=actions.API_GEMINI_ID,
                            config={"google_api_key": "G-KEY", "action_api_key": "sk-openai"})
        self.assertEqual(seen["action_api_key"], "G-KEY")


if __name__ == "__main__":
    unittest.main()
