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
        with patch.object(actions.action_api, "run_action",
                          return_value="## Summary\nGreat meeting.") as call:
            out = actions.process(
                self.SAMPLE,
                actions.ACTION_MEETING_NOTES,
                model=actions.API_OPENAI_ID,
                config={"action_api_key": "secret", "privacy_mode": False},
            )
        self.assertIn("Great meeting", out)
        call.assert_called_once()

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
