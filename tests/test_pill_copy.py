"""The dictation pill's words (pill_copy): what a Smart Action is about to
write, what the pill says while it transcribes and writes, and its "done"
line. Pure logic: no Qt, no app."""
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("TRANSCRIBE_APP_DATA_DIR", tempfile.mkdtemp(prefix="gusan-pill-copy-"))
os.environ.setdefault("TRANSCRIBE_DISABLE_KEYRING", "1")
os.environ.setdefault("TRANSCRIBE_SKIP_MIGRATION", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pill_copy as pc  # noqa: E402
import engine_choice  # noqa: E402,F401  (in the app the overlay warms it when a take starts)


class TestIntent(unittest.TestCase):
    def test_each_ask_is_named(self):
        for text, want in (
                ("write an email to John that the Q3 report is ready", ("email", "")),
                ("email Sarah saying I'll be late", ("email", "")),
                ("make a todo list call mom buy groceries", ("list", "")),
                ("checklist for the trip passport tickets charger", ("list", "")),
                ("translate to Spanish I will meet you at five", ("translate", "Spanish")),
                ("summarize this we talked about the hiring plan", ("summary", "")),
                ("give me a recap of the call", ("summary", "")),
                ("clean this up um so basically we need to ship next week", ("polish", "")),
                ("polish this paragraph for me", ("polish", "")),
                ("meeting notes we decided to move the launch", ("notes", "")),
                ("draft a reply to Anna thanks for the invite", ("reply", "")),
                ("the meeting is at three pm in room b", (None, ""))):
            self.assertEqual(pc.intent(text), want, text)

    def test_the_ask_said_first_wins(self):
        # detect_action_mode's fixed order would call these emails.
        self.assertEqual(pc.intent("summarize this email from John about the budget")[0], "summary")
        self.assertEqual(pc.intent("remind me to email John and call the bank")[0], "list")
        self.assertEqual(pc.intent("translate to French: I'll email you the draft"), ("translate", "French"))
        self.assertEqual(pc.intent("write an email that answers his reply")[0], "email")

    def test_the_built_in_rules_follow_their_own_order(self):
        # No AI model: actions._run_smart_rule_based routes by
        # detect_action_mode, so the pill names what those rules write.
        self.assertEqual(pc.intent("summarize this email from John", rules=True)[0], "email")
        self.assertEqual(pc.intent("make a todo list call mom", rules=True)[0], "list")
        # The rules write an email for "message" / "draft" too.
        self.assertEqual(pc.intent("write a message to the team we ship today", rules=True)[0], "email")
        self.assertEqual(pc.intent("draft a tweet about the launch", rules=True)[0], "email")
        self.assertEqual(pc.intent("draft a reply to Anna", rules=True)[0], "reply")
        self.assertEqual(pc.intent("rewrite this in Spanish", rules=True)[0], "polish")

    def test_an_ai_writer_is_named_for_what_was_asked(self):
        # smart_prompt's email rule fires on "message", "draft", "compose";
        # an AI writer writes what was asked, so the pill doesn't say email.
        for text, want in (
                ("write a message to the team that we ship today", ("message", "")),
                ("send a message to Anna about lunch", ("message", "")),
                ("draft a tweet about our launch", (None, "")),
                ("compose a short poem about rain", (None, "")),
                ("draft an email to the client about the invoice", ("email", "")),
                ("respond to her and say thanks", ("reply", "")),
                ("rewrite this in Spanish we open at nine", ("translate", "Spanish")),
                ("write it again in French please", ("translate", "French")),
                ("polish this, I wrote it in English", ("polish", "")),
                ("how do you say good morning in Japanese", ("translate", "Japanese")),
                ("how do you say thank you", (None, "")),
                ("turn this into bullet points we need milk and eggs", ("list", "")),
                ("fix my grammar i has a apple", ("polish", "")),
                ("make this sound more professional hey whats up", ("polish", "")),
                ("write a tweet in English about the launch", (None, ""))):
            self.assertEqual(pc.intent(text), want, text)

    def test_translation_targets(self):
        for text, lang in (
                ("translate to spanish see you tomorrow", "Spanish"),
                ("translate this to portuguese the report is ready", "Portuguese"),
                ("please translate to ru hello there", "Russian"),
                ("translate to es hola", "Spanish"),
                ("can you translate this into Japanese please", "Japanese"),
                ("translation in German of the following", "German"),
                ("translate this", ""),
                ("translate to the board what we said", "")):
            self.assertEqual(pc.intent(text), ("translate", lang), text)

    def test_language_names_match_the_translate_targets(self):
        import actions
        for code, name in actions.TRANSLATE_TARGETS.items():
            self.assertEqual(pc.language_name(code), name, code)
        self.assertEqual(pc.language_name("xx"), "")
        self.assertEqual(pc.language_name(None), "")

    def test_nothing_to_go_on(self):
        for text in ("", "   ", None, 42, ["email"]):
            self.assertEqual(pc.intent(text), (None, ""), repr(text))

    def test_never_raises(self):
        with mock.patch("smart_prompt.detect_action_mode", side_effect=RuntimeError("boom")), \
                mock.patch("smart_prompt._DETECT_RULES", None):
            self.assertEqual(pc.intent("write an email"), (None, ""))

    def test_never_slow(self):
        text = ("so we talked about the budget and the hiring plan and then the launch " * 400
                + "summarize this")
        start = time.perf_counter()
        for _ in range(20):
            kind, _detail = pc.intent(text)
        self.assertEqual(kind, "summary")                              # said at the end: seen
        self.assertLess((time.perf_counter() - start) / 20, 0.005)    # a 25-minute take, under 5 ms
        filler = "so we talked about the budget and the plan " * 400
        self.assertEqual(pc.intent("translate to German " + filler), ("translate", "German"))
        self.assertEqual(pc.intent("write an email to Sam " + filler + " thanks")[0], "email")
        self.assertEqual(pc.intent(filler + " make this a list of the action items")[0], "list")
        self.assertEqual(pc.intent(filler + " summarize this " + filler)[0], None)   # buried: not a guess


class TestTranscribing(unittest.TestCase):
    def busy(self, **ctx):
        return pc.busy("transcribing", ctx=ctx)

    def test_says_the_language_else_the_model(self):
        self.assertEqual(self.busy(speech="Whisper Small", language="English"), ("Transcribing", "English"))
        self.assertEqual(self.busy(speech="Whisper Small"), ("Transcribing", "Whisper Small"))
        self.assertEqual(self.busy(speech="Gusan Cloud", speech_local=False), ("Transcribing", "Gusan Cloud"))
        self.assertEqual(self.busy(), ("Transcribing", ""))
        self.assertEqual(pc.busy("transcribing"), ("Transcribing", ""))

    def test_a_long_take_says_how_much_audio(self):
        self.assertEqual(self.busy(speech="Whisper Small", seconds=30), ("Transcribing", "Whisper Small"))
        self.assertEqual(self.busy(speech="Whisper Small", seconds=50), ("Transcribing", "1 min of audio"))
        self.assertEqual(self.busy(seconds=118, language="English"), ("Transcribing", "2 min of audio"))
        self.assertEqual(self.busy(seconds=118, elapsed=6), ("Transcribing", "2 min of audio"))

    def test_after_a_few_seconds_almost_there(self):
        self.assertEqual(self.busy(speech="Whisper Small", elapsed=3.9)[1], "Whisper Small")
        self.assertEqual(self.busy(speech="Whisper Small", elapsed=4.0), ("Transcribing", "Almost there"))
        self.assertEqual(self.busy(speech="Whisper Small", elapsed=11.9, seconds=10),
                         ("Transcribing", "Almost there"))

    def test_a_long_wait_on_this_computer(self):
        self.assertEqual(self.busy(speech_local=True, elapsed=12.5, seconds=70),
                         ("Long take", "Still transcribing"))
        self.assertEqual(self.busy(speech_local=True, elapsed=12.5), ("Long take", "Still transcribing"))
        # A short take that is slow isn't a "long take".
        self.assertEqual(self.busy(speech_local=True, elapsed=12.5, seconds=6),
                         ("Transcribing", "This computer is working on it"))
        # In the cloud: just honest.
        self.assertEqual(self.busy(speech_local=False, elapsed=13, seconds=70),
                         ("Transcribing", "Still working"))
        self.assertEqual(self.busy(local=False, elapsed=13), ("Transcribing", "Still working"))

    def test_a_smart_take_says_the_same(self):
        self.assertEqual(self.busy(smart=True, speech="Gusan Cloud", speech_local=False),
                         ("Transcribing", "Gusan Cloud"))

    def test_bad_numbers_are_ignored(self):
        self.assertEqual(self.busy(speech="Whisper Base", elapsed="soon", seconds=None),
                         ("Transcribing", "Whisper Base"))
        self.assertEqual(self.busy(speech="Whisper Base", elapsed=-5), ("Transcribing", "Whisper Base"))


class TestWriting(unittest.TestCase):
    CLOUD = {"writer": "Gusan Cloud", "writer_local": False}
    LOCAL = {"writer": "Qwen 1.5B", "writer_local": True}

    def test_each_kind_has_its_words(self):
        want = {"email": "Writing your email", "list": "Making your list", "translate": "Translating",
                "summary": "Summarizing", "polish": "Polishing your words", "notes": "Writing notes",
                "reply": "Writing a reply", "message": "Writing your message", None: "Thinking"}
        for kind, title in want.items():
            self.assertEqual(pc.busy("writing", kind, ctx=self.CLOUD), (title, "Gusan Cloud"), kind)
        self.assertEqual(set(want) - {None}, set(pc.KINDS))
        self.assertEqual(pc.busy("writing", "something-else", ctx=self.CLOUD), ("Thinking", "Gusan Cloud"))

    def test_translating_names_the_language(self):
        self.assertEqual(pc.busy("writing", "translate", "Spanish", self.CLOUD),
                         ("Translating to Spanish", "Gusan Cloud"))
        self.assertEqual(pc.busy("writing", "translate", "  ", self.CLOUD)[0], "Translating")
        # A detail only means something for a translation.
        self.assertEqual(pc.busy("writing", "email", "Spanish", self.CLOUD)[0], "Writing your email")

    def test_the_writer_line(self):
        self.assertEqual(pc.busy("writing", "email", ctx=self.LOCAL)[1], "Qwen 1.5B · on this computer")
        self.assertEqual(pc.busy("writing", "email", ctx={"writer": "Ministral 14B", "writer_local": False})[1],
                         "Ministral 14B")
        self.assertEqual(pc.busy("writing", "email", ctx={"writer": "", "writer_local": True})[1], "")
        self.assertEqual(pc.busy("writing", "email", ctx={"writer": "Qwen 3B", "where": "on this computer"})[1],
                         "Qwen 3B · on this computer")
        self.assertEqual(pc.busy("writing", "email")[1], "")

    def test_a_slow_writer(self):
        self.assertEqual(pc.busy("writing", "email", ctx=dict(self.CLOUD, elapsed=4.2)),
                         ("Writing your email", "Still writing"))
        self.assertEqual(pc.busy("writing", "translate", "Spanish", dict(self.CLOUD, elapsed=5)),
                         ("Translating to Spanish", "Still translating"))
        self.assertEqual(pc.busy("writing", None, ctx=dict(self.CLOUD, elapsed=5))[1], "Still thinking")
        self.assertEqual(pc.busy("writing", "summary", ctx=dict(self.CLOUD, elapsed=30))[1], "Still summarizing")
        # This computer, ten seconds in: say who is working.
        self.assertEqual(pc.busy("writing", "list", ctx=dict(self.LOCAL, elapsed=9.9))[1], "Still writing")
        self.assertEqual(pc.busy("writing", "list", ctx=dict(self.LOCAL, elapsed=10.0)),
                         ("Making your list", "This computer is working on it"))

    def test_copy_is_short_and_plain(self):
        texts = []
        for kind in (None,) + pc.KINDS:
            for elapsed in (0, 5, 11, 20):
                for ctx in (self.CLOUD, self.LOCAL):
                    texts += pc.busy("writing", kind, "Portuguese", dict(ctx, elapsed=elapsed))
            for pasted in (True, False):
                texts.append(pc.done(kind, pasted, smart=True))
        for elapsed in (0, 5, 13):
            for seconds in (5, 70):
                texts += pc.busy("transcribing", ctx={"elapsed": elapsed, "seconds": seconds,
                                                      "speech": "Whisper Small"})
        for text in filter(None, texts):
            self.assertNotIn("!", text)
            self.assertNotIn("...", text)
            self.assertLessEqual(len(text), 40, text)
            self.assertTrue(text[0].isupper() or text[0].isdigit(), text)       # sentence case
            self.assertFalse(re.search(r"\b(AI|LLM|model)\b", text), text)      # plain words


class TestDone(unittest.TestCase):
    def test_dictation(self):
        self.assertEqual(pc.done(None, True), "Pasted")
        self.assertEqual(pc.done(None, False, mac=False), "Copied · press Ctrl+V")
        self.assertEqual(pc.done(None, False, mac=True), "Copied · press Cmd+V")

    def test_smart_actions(self):
        want = {"email": "Email pasted", "list": "List pasted", "translate": "Translated · pasted",
                "summary": "Summary pasted", "polish": "Polished · pasted", "notes": "Notes pasted",
                "reply": "Reply pasted", "message": "Message pasted"}
        for kind, text in want.items():
            self.assertEqual(pc.done(kind, True), text)
        self.assertEqual(pc.done(None, True, smart=True), "Done · pasted")

    def test_copied_says_how_to_paste(self):
        self.assertEqual(pc.done("email", False, mac=False), "Email copied · press Ctrl+V")
        self.assertEqual(pc.done("translate", False, mac=True), "Translation copied · press Cmd+V")
        self.assertEqual(pc.done(None, False, smart=True, mac=False), "Copied · press Ctrl+V")
        for kind in pc.KINDS:
            self.assertTrue(pc.done(kind, False, mac=False).endswith(" copied · press Ctrl+V"), kind)

    def test_the_platform_picks_the_keys(self):
        with mock.patch.object(pc.sys, "platform", "darwin"):
            self.assertEqual(pc.paste_keys(), "Cmd+V")
        with mock.patch.object(pc.sys, "platform", "win32"):
            self.assertEqual(pc.paste_keys(), "Ctrl+V")

    def test_unknown_kind_is_plain(self):
        self.assertEqual(pc.done("poem", True), "Pasted")
        self.assertEqual(pc.done("poem", True, smart=True), "Done · pasted")


class TestContext(unittest.TestCase):
    def test_speech_info(self):
        self.assertEqual(pc.speech_info("local", "small"), {"speech": "Whisper Small", "speech_local": True})
        self.assertEqual(pc.speech_info(None, None), {"speech": "Whisper Base", "speech_local": True})
        self.assertEqual(pc.speech_info("managed"), {"speech": "Gusan Cloud", "speech_local": False})
        self.assertEqual(pc.speech_info("google", None, {"google_stt_model": "gemini-2.5-flash"}),
                         {"speech": "Gemini 2.5 Flash", "speech_local": False})
        self.assertEqual(pc.speech_info("mistral", None, {"mistral_stt_model": "nope"}),
                         {"speech": "Voxtral Mini", "speech_local": False})

    def test_speech_info_never_imports(self):
        # engine_choice not loaded yet (+ hardware: ~30 ms): the take never
        # waits for it - the names come from pill_copy, the same ones.
        import hardware
        with mock.patch.dict(sys.modules, {"engine_choice": None}):
            for mid, info in hardware.MODEL_INFO.items():
                self.assertEqual(pc.speech_info("local", mid), {"speech": info.real, "speech_local": True}, mid)
            self.assertEqual(pc.speech_info("managed"), {"speech": "Gusan Cloud", "speech_local": False})
            self.assertEqual(pc.speech_info("google", None, {}), {"speech": "Gemini", "speech_local": False})
            self.assertEqual(pc.speech_info("mistral", None, {}), {"speech": "Voxtral Mini", "speech_local": False})
            self.assertIsNone(sys.modules["engine_choice"])          # nothing was imported
        # A module still being imported (by warm()) counts as not loaded.
        half = mock.MagicMock()
        half.__spec__ = mock.MagicMock(_initializing=True)
        with mock.patch.dict(sys.modules, {"engine_choice": half}):
            self.assertEqual(pc.speech_info("local", "small"), {"speech": "Whisper Small", "speech_local": True})
            self.assertEqual(pc.task("make a todo list call mom", object()), ("list", "", {}))
        half.speech_model_name.assert_not_called()
        half.runs_on.assert_not_called()

    def test_warm_imports_engine_choice_once_off_the_caller_thread(self):
        threads = []

        class _Thread:
            def __init__(self, target=None, name=None, daemon=None):
                threads.append((target, name, daemon))

            def start(self):
                pass
        with mock.patch.object(pc, "_warming", False), mock.patch.object(pc.threading, "Thread", _Thread), \
                mock.patch.dict(sys.modules, {"engine_choice": None}):
            pc.warm()
            pc.warm()                                                   # once
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0][2])                                  # a daemon
        threads[0][0]()                                                 # the import itself works
        self.assertIsNotNone(pc._loaded("engine_choice"))
        with mock.patch.object(pc, "_warming", False), mock.patch.object(pc.threading, "Thread", _Thread):
            pc.warm()                                                   # loaded already: nothing to do
        self.assertEqual(len(threads), 1)

    def test_writer_info(self):
        import engine_choice as ec
        cloud = ec.runs_on({}, "smart_actions", pro=True, signed_in=True)
        self.assertEqual(pc.writer_info(cloud), {"writer": "Gusan Cloud", "writer_local": False})
        local = ec.runs_on({"action_model": "qwen_tiny"}, "smart_actions", downloaded=lambda m: True)
        self.assertEqual(pc.writer_info(local), {"writer": "Qwen 1.5B", "writer_local": True})
        own = ec.runs_on({"action_model": "api_mistral", "action_api_key": "k"}, "smart_actions")
        self.assertEqual(pc.writer_info(own), {"writer": "Ministral 14B", "writer_local": False})
        basic = ec.runs_on({}, "smart_actions")
        self.assertEqual(pc.writer_info(basic), {"writer": "", "writer_local": True})
        self.assertEqual(pc.writer_info(None), {})
        self.assertEqual(pc.writer_info({"kind": "none"}), {})

    def test_task(self):
        from types import SimpleNamespace
        app = SimpleNamespace(cfg={}, is_pro=lambda: True, auth=SimpleNamespace(is_authenticated=True))
        self.assertEqual(pc.task("translate to Spanish good morning", app),
                         ("translate", "Spanish", {"writer": "Gusan Cloud", "writer_local": False}))
        # The built-in rules write: the pill follows their routing.
        free = SimpleNamespace(cfg={}, is_pro=lambda: False, auth=None)
        self.assertEqual(pc.task("summarize this email from John", free),
                         ("email", "", {"writer": "", "writer_local": True}))

    def test_task_never_raises(self):
        with mock.patch("engine_choice.runs_on", side_effect=RuntimeError("boom")):
            self.assertEqual(pc.task("make a todo list call mom", object()), ("list", "", {}))
        self.assertEqual(pc.task(None, None)[:2], (None, ""))


if __name__ == "__main__":
    unittest.main()
