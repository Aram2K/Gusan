import unittest
from unittest.mock import MagicMock, patch

import action_api


class TestActionAPI(unittest.TestCase):
    def test_privacy_mode_blocks_cloud_actions(self):
        with self.assertRaises(action_api.ActionAPIError):
            action_api.run_action(
                "hello",
                "write_email",
                {"action_api_key": "secret", "privacy_mode": True},
            )

    def test_missing_key_blocks_cloud_actions(self):
        with self.assertRaises(action_api.ActionAPIError):
            action_api.run_action(
                "hello",
                "write_email",
                {"privacy_mode": False},
            )

    def test_openai_request_uses_auth_header_not_payload_key(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
        with patch.object(action_api.requests, "post", return_value=response) as post:
            out = action_api.run_action(
                "hello",
                "write_email",
                {
                    "privacy_mode": False,
                    "action_api_key": "secret",
                    "action_api_provider": action_api.PROVIDER_OPENAI,
                    "action_api_base_url": "https://api.example.com/v1",
                    "action_api_model": "demo",
                },
            )
        self.assertEqual(out, "ok")
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertNotIn("secret", str(kwargs["json"]))

    def test_managed_action_requires_token(self):
        with self.assertRaises(action_api.ActionAPIError):
            action_api.run_managed_action("hello", "smart_auto", "")

    def test_managed_action_posts_messages_with_bearer_token(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"text": "MANAGED OK"}
        with patch.object(action_api.requests, "post", return_value=response) as post:
            out = action_api.run_managed_action(
                "summarize this", "smart_auto", "tok123",
            )
        self.assertEqual(out, "MANAGED OK")
        args, kwargs = post.call_args
        # Goes to the smart-action edge function with the user's JWT, and the
        # built messages (never a raw API key) in the body.
        self.assertEqual(args[0], action_api.SMART_ACTION_URL)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok123")
        self.assertIn("messages", kwargs["json"])
        self.assertTrue(kwargs["json"]["messages"])
        # The mode picks the model on the server (Smart Actions -> DeepSeek).
        self.assertEqual(kwargs["json"]["mode"], "smart_auto")

    def test_managed_action_maps_403_to_pro_required(self):
        response = MagicMock(status_code=403)
        response.json.return_value = {"error": "pro_required"}
        with patch.object(action_api.requests, "post", return_value=response):
            with self.assertRaises(action_api.ActionAPIError) as ctx:
                action_api.run_managed_action("hi", "smart_auto", "tok")
        self.assertIn("Pro", str(ctx.exception))

    def test_managed_deepseek_answer_loses_its_thinking_and_never_pastes_nothing(self):
        def reply(text):
            response = MagicMock(status_code=200, headers={"X-Answer-Family": "deepseek"})
            response.json.return_value = {"text": text}
            return response
        with patch.object(action_api.requests, "post", return_value=reply("plan it\n</think>\nHello.")):
            self.assertEqual(action_api.run_managed_action("hi", "smart_auto", "tok"), "Hello.")
        # Only thinking came back: say so instead of pasting an empty string.
        with patch.object(action_api.requests, "post", return_value=reply("<think>still thinking")):
            with self.assertRaises(action_api.ActionAPIError) as ctx:
                action_api.run_managed_action("hi", "smart_auto", "tok")
        self.assertEqual(str(ctx.exception), action_api.THOUGHT_ONLY)


class TestAnthropicPayload(unittest.TestCase):
    """Regression: the Anthropic path used to send messages[1] as the user turn.
    For smart_auto that index is the FIRST FEW-SHOT EXAMPLE, so the user's real
    dictation never reached the model."""

    def _post(self, mode, text):
        response = MagicMock(status_code=200)
        response.json.return_value = {"content": [{"text": "OK"}]}
        cfg = {"action_api_key": "k", "action_api_provider": action_api.PROVIDER_ANTHROPIC}
        with patch.object(action_api.requests, "post", return_value=response) as post:
            action_api.run_action(text, mode, cfg)
        return post.call_args[1]["json"]

    def test_user_dictation_reaches_the_model(self):
        body = self._post("smart_auto", "translate to russian I will meet you at 5pm")
        last = body["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertIn("5pm", last["content"])

    def test_few_shot_turns_are_preserved(self):
        body = self._post("smart_auto", "hello world")
        roles = [m["role"] for m in body["messages"]]
        self.assertGreater(len(roles), 1, "few-shot turns should be forwarded")
        self.assertTrue(all(r in ("user", "assistant") for r in roles))
        self.assertNotIn("system", roles)   # system goes in its own field

    def test_simple_mode_still_works(self):
        body = self._post("summarize", "some long text to summarize")
        self.assertEqual(len(body["messages"]), 1)
        self.assertIn("some long text", body["messages"][0]["content"])
        self.assertTrue(body["system"])


class TestVocabularyInPrompts(unittest.TestCase):
    def test_vocab_block_reaches_system_message(self):
        msgs = action_api.build_messages("hi", "summarize", vocab_block="SPELLING: Aibuben")
        self.assertIn("Aibuben", msgs[0]["content"])

    def test_absent_by_default(self):
        msgs = action_api.build_messages("hi", "summarize")
        self.assertNotIn("SPELLING", msgs[0]["content"])

    def test_smart_auto_receives_it_too(self):
        msgs = action_api.build_messages("hi", "smart_auto", vocab_block="SPELLING: Aibuben")
        self.assertIn("Aibuben", msgs[0]["content"])


def _sse(*events):
    """Server-sent lines for the given JSON events, ending the way a server does."""
    import json
    return [("data: %s" % json.dumps(e)).encode("utf-8") for e in events] + [b"data: [DONE]"]


def _chunk(text="", finish=None):
    return {"choices": [{"delta": {"content": text} if text else {}, "finish_reason": finish}]}


class _Stream:
    """A streaming response: the given lines, then (optionally) the connection drops."""
    status_code = 200

    def __init__(self, lines, drop=False, kind="text/event-stream"):
        self.headers = {"Content-Type": kind}
        self.lines, self.drop, self.closed = list(lines), drop, False

    def iter_lines(self, *a, **k):
        for line in self.lines:
            yield line
        if self.drop:
            raise action_api.requests.ConnectionError("reset")

    def close(self):
        self.closed = True


class TestCaptionTranslation(unittest.TestCase):
    """Live captions translate a sentence at a time through Gusan Cloud's "interpret" mode (the Interpreter's)."""

    def test_one_sentence_in_the_translation_out(self):
        resp = _Stream(_sse(_chunk("Hello, "), _chunk("how are you?", "stop")))
        with patch.object(action_api.requests, "post", return_value=resp) as post:
            out = action_api.interpret_phrase("Բարեւ, ինչպե՞ս ես։", "en", "tok", "hy", ["earlier one", " "])
        self.assertEqual(out, "Hello, how are you?")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body, {"mode": "interpret", "text": "Բարեւ, ինչպե՞ս ես։", "target_lang": "en",
                                "source_lang": "hy", "context": ["earlier one"]})
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer tok"})
        self.assertTrue(resp.closed)

    def test_signed_out_and_empty(self):
        with self.assertRaises(action_api.ActionAPIError):
            action_api.interpret_phrase("hi", "fr", None)
        with patch.object(action_api.requests, "post") as post:
            self.assertEqual(action_api.interpret_phrase("   ", "fr", "tok"), "")
        post.assert_not_called()


NOTES_TEXT = "You: hello\nThem: hi there. " + "word " * 5000
OPENAI_CFG = {"action_api_provider": "openai_compatible", "action_api_key": "k"}
GEMINI_CFG = {"action_api_provider": "gemini", "action_api_key": "k"}
ANTHROPIC_CFG = {"action_api_provider": "anthropic", "action_api_key": "k"}


class TestNotesStreams(unittest.TestCase):
    """Meeting notes are streamed on every route. When the model stops at its limit, or the connection drops after some text,
    the caller's ``info`` dict says so (and the text that arrived is kept)."""

    def managed(self, lines, info=None, drop=False, text=NOTES_TEXT):
        resp = _Stream(lines, drop)
        with patch.object(action_api.requests, "post", return_value=resp) as post:
            out = action_api.run_managed_action_stream(text, "meeting_notes", "tok", lambda d: None, info=info)
        return out, post, resp

    def test_a_whole_answer_is_not_cut(self):
        info = {}
        out, _post, resp = self.managed(_sse(_chunk("## Summary\n"), _chunk("Done."), _chunk("", "stop")), info)
        self.assertEqual(out, "## Summary\nDone.")
        self.assertEqual(info, {})
        self.assertTrue(resp.closed)

    def test_the_model_stopping_at_its_limit_is_a_cut(self):
        info = {}
        out, _post, _r = self.managed(_sse(_chunk("## Summary\nPart"), _chunk("", "length")), info)
        self.assertEqual(out, "## Summary\nPart")
        self.assertTrue(info["cut"])

    def test_every_providers_word_for_stopping_early_is_a_cut(self):
        for reason in ("length", "model_length", "content_filter", "max_tokens", "MAX_TOKENS", "SAFETY"):
            info = {}
            action_api._note_finish(info, reason)
            self.assertTrue(info.get("cut"), reason)
        for reason in ("stop", "end_turn", "STOP", "", None, "tool_calls"):
            info = {}
            action_api._note_finish(info, reason)
            self.assertEqual(info, {}, reason)
        action_api._note_finish(None, "length")                         # no info dict: nothing to say, nothing breaks

    def test_a_connection_that_drops_after_some_text_keeps_it_and_is_a_cut(self):
        info = {}
        out, _post, _r = self.managed(_sse(_chunk("## Summary\nPart"))[:-1], info, drop=True)
        self.assertEqual(out, "## Summary\nPart")
        self.assertTrue(info["cut"])

    def test_a_connection_that_drops_before_any_text_is_an_error(self):
        with self.assertRaises(action_api.ActionAPIError):
            self.managed([], {}, drop=True)

    def test_info_is_optional(self):
        out, _post, _r = self.managed(_sse(_chunk("Text"), _chunk("", "length")), None)
        self.assertEqual(out, "Text")

    def test_the_request_streams_and_asks_for_what_the_meeting_needs(self):
        _out, post, _r = self.managed(_sse(_chunk("x"), _chunk("", "stop")))
        body = post.call_args.kwargs["json"]
        self.assertTrue(body["stream"])
        self.assertEqual(body["mode"], "meeting_notes")
        self.assertEqual(body["max_tokens"], 3600)                      # ~5,000 words
        self.assertEqual(body["messages"][1]["content"], action_api.build_messages(NOTES_TEXT, "meeting_notes")[1]["content"])
        self.assertEqual(post.call_args.kwargs["timeout"], (10, 90))        # idle time, not a whole-answer limit

    def test_a_short_meeting_asks_for_less_and_a_very_long_one_for_more(self):
        _o, post, _r = self.managed(_sse(_chunk("x")), text="You: hi\nThem: hello")
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 1200)
        _o, post, _r = self.managed(_sse(_chunk("x")), text="word " * 12000)
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 5000)

    def test_an_older_server_that_answers_in_one_json_piece_still_works(self):
        resp = MagicMock(status_code=200, headers={"Content-Type": "application/json"})
        resp.json.return_value = {"text": "## Summary\nWhole."}
        info = {}
        with patch.object(action_api.requests, "post", return_value=resp):
            out = action_api.run_managed_action_stream(NOTES_TEXT, "meeting_notes", "tok", lambda d: None, info=info)
        self.assertEqual(out, "## Summary\nWhole.")
        self.assertEqual(info, {})

    def own(self, cfg, lines, info, drop=False):
        resp = _Stream(lines, drop)
        with patch.object(action_api.requests, "post", return_value=resp) as post:
            out = action_api.run_action_stream(NOTES_TEXT, "meeting_notes", cfg, lambda d: None, info=info)
        return out, post

    def test_an_openai_compatible_key_streams_and_flags_a_cut(self):
        info = {}
        out, post = self.own(OPENAI_CFG, _sse(_chunk("## Summary\nA"), _chunk("", "length")), info)
        self.assertEqual(out, "## Summary\nA")
        self.assertTrue(info["cut"])
        body = post.call_args.kwargs["json"]
        self.assertTrue(body["stream"])
        self.assertEqual(body["max_tokens"], 3600)
        self.assertEqual(post.call_args.kwargs["timeout"], (10, 60))
        info = {}
        self.own(OPENAI_CFG, _sse(_chunk("A"), _chunk("", "stop")), info)
        self.assertEqual(info, {})

    def test_an_openai_compatible_server_that_ignores_stream_still_gives_the_notes(self):
        resp = MagicMock(status_code=200, headers={"Content-Type": "application/json"})
        resp.json.return_value = {"choices": [{"message": {"content": "## Summary\nWhole."}, "finish_reason": "length"}]}
        info, seen = {}, []
        with patch.object(action_api.requests, "post", return_value=resp):
            out = action_api.run_action_stream(NOTES_TEXT, "meeting_notes", OPENAI_CFG, seen.append, info=info)
        self.assertEqual((out, seen), ("## Summary\nWhole.", ["## Summary\nWhole."]))
        self.assertTrue(info["cut"])

    def test_an_openai_compatible_stream_that_drops_is_a_cut(self):
        info = {}
        out, _post = self.own(OPENAI_CFG, _sse(_chunk("## Summary\nA"))[:-1], info, drop=True)
        self.assertEqual(out, "## Summary\nA")
        self.assertTrue(info["cut"])

    def test_gemini_says_max_tokens_in_its_own_words(self):
        def gem(text, reason):
            ev = {"candidates": [{"content": {"parts": [{"text": text}]}}]}
            if reason:
                ev["candidates"][0]["finishReason"] = reason
            return ev
        info = {}
        out, _p = self.own(GEMINI_CFG, _sse(gem("## Summary\nA", None), gem("B", "MAX_TOKENS")), info)
        self.assertEqual(out, "## Summary\nAB")
        self.assertTrue(info["cut"])
        info = {}
        self.own(GEMINI_CFG, _sse(gem("A", None), gem("B", "STOP")), info)
        self.assertEqual(info, {})

    def test_anthropic_says_max_tokens_in_its_own_words(self):
        def delta(text):
            return {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}

        def stop(reason):
            return {"type": "message_delta", "delta": {"stop_reason": reason}}
        info = {}
        out, post = self.own(ANTHROPIC_CFG, _sse(delta("## Summary\nA"), stop("max_tokens"), {"type": "message_stop"}), info)
        self.assertEqual(out, "## Summary\nA")
        self.assertTrue(info["cut"])
        self.assertEqual(post.call_args.kwargs["json"]["max_tokens"], 3600)
        info = {}
        self.own(ANTHROPIC_CFG, _sse(delta("A"), stop("end_turn"), {"type": "message_stop"}), info)
        self.assertEqual(info, {})

    def test_a_stream_that_ends_without_saying_the_model_is_done_is_a_cut(self):
        """No finish reason and no [DONE]: a proxy or a server closed the connection. The last words may be half a word."""
        half = _sse(_chunk("## Summary\nA"), _chunk("B half wo"))[:-1]
        info = {}
        out, _p, _r = self.managed(half, info)
        self.assertEqual(out, "## Summary\nAB half wo")
        self.assertTrue(info["cut"])
        info = {}
        out, _p = self.own(OPENAI_CFG, half, info)
        self.assertEqual(out, "## Summary\nAB half wo")
        self.assertTrue(info["cut"])
        ev = {"candidates": [{"content": {"parts": [{"text": "## Summary\nA half wo"}]}}]}
        info = {}
        out, _p = self.own(GEMINI_CFG, _sse(ev)[:-1], info)
        self.assertTrue(info["cut"])
        delta = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "## Summary\nA half wo"}}
        info = {}
        out, _p = self.own(ANTHROPIC_CFG, _sse(delta)[:-1], info)
        self.assertEqual(out, "## Summary\nA half wo")
        self.assertTrue(info["cut"])

    def test_a_stream_that_says_it_is_done_in_any_way_is_not_a_cut(self):
        # [DONE] alone, a finish reason alone, and Anthropic's message_stop alone are each a clean end
        info = {}
        self.managed(_sse(_chunk("A")), info)
        self.assertEqual(info, {})
        info = {}
        self.managed(_sse(_chunk("A"), _chunk("", "stop"))[:-1], info)
        self.assertEqual(info, {})
        info = {}
        self.own(OPENAI_CFG, _sse(_chunk("A")), info)
        self.assertEqual(info, {})
        delta = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "A"}}
        info = {}
        self.own(ANTHROPIC_CFG, _sse(delta, {"type": "message_stop"})[:-1], info)
        self.assertEqual(info, {})
        ev = {"candidates": [{"content": {"parts": [{"text": "A"}]}, "finishReason": "STOP"}]}
        info = {}
        self.own(GEMINI_CFG, _sse(ev)[:-1], info)
        self.assertEqual(info, {})

    def test_an_error_in_the_middle_of_a_stream_keeps_the_text_as_a_cut(self):
        err = {"error": {"message": "overloaded"}}
        info = {}
        out, _p, _r = self.managed(_sse(_chunk("## Summary\nA"), err, _chunk("never read")), info)
        self.assertEqual(out, "## Summary\nA")
        self.assertTrue(info["cut"])
        info = {}
        out, _p = self.own(OPENAI_CFG, _sse(_chunk("## Summary\nA"), err, _chunk("never read")), info)
        self.assertEqual(out, "## Summary\nA")
        self.assertTrue(info["cut"])
        info = {}
        out, _p = self.own(GEMINI_CFG, _sse({"candidates": [{"content": {"parts": [{"text": "## Summary\nA"}]}}], }, err), info)
        self.assertEqual(out, "## Summary\nA")
        self.assertTrue(info["cut"])
        delta = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "## Summary\nA"}}
        info = {}
        out, _p = self.own(ANTHROPIC_CFG, _sse(delta, {"type": "error", "error": {"type": "overloaded_error",
                                                                              "message": "Overloaded"}}), info)
        self.assertEqual(out, "## Summary\nA")
        self.assertTrue(info["cut"])

    def test_an_error_before_any_text_is_an_error_to_say(self):
        err = {"error": {"message": "overloaded"}}
        with self.assertRaises(action_api.ActionAPIError) as cm:
            self.managed(_sse(err), {})
        self.assertIn("overloaded", str(cm.exception))
        with self.assertRaises(action_api.ActionAPIError) as cm:
            self.own(OPENAI_CFG, _sse(err), {})
        self.assertIn("overloaded", str(cm.exception))
        with self.assertRaises(action_api.ActionAPIError):
            self.own(GEMINI_CFG, _sse(err), {})
        with self.assertRaises(action_api.ActionAPIError) as cm:
            self.own(ANTHROPIC_CFG, _sse({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}), {})
        self.assertIn("Overloaded", str(cm.exception))

    def test_a_live_answer_ignores_all_this_and_reads_as_before(self):
        seen = []
        # no finish reason, no [DONE]: the live answer is what arrived
        resp = _Stream(_sse(_chunk("Short."))[:-1])
        with patch.object(action_api.requests, "post", return_value=resp):
            out = action_api.run_action_stream("hello", "live_assist", OPENAI_CFG, seen.append)
        self.assertEqual((out, seen), ("Short.", ["Short."]))
        # an error object in the middle is skipped, not raised
        resp = _Stream(_sse(_chunk("Short."), {"error": "x"}, _chunk(" More.")))
        with patch.object(action_api.requests, "post", return_value=resp):
            out = action_api.run_action_stream("hello", "live_assist", OPENAI_CFG, seen.append)
        self.assertEqual(out, "Short. More.")

    def test_only_the_notes_take_a_json_body_for_a_stream(self):
        """A live answer whose stream is labelled application/json is still read as a stream (the one-piece JSON fallback is
        for the notes only)."""
        seen = []
        resp = _Stream(_sse(_chunk("Live"), _chunk(" answer.", "stop")), kind="application/json")
        with patch.object(action_api.requests, "post", return_value=resp):
            out = action_api.run_action_stream("hello", "live_assist", OPENAI_CFG, seen.append)
        self.assertEqual((out, seen), ("Live answer.", ["Live", " answer."]))

    def test_a_live_answer_without_info_is_as_before(self):
        resp = _Stream(_sse(_chunk("Short."), _chunk("", "length")))
        seen = []
        with patch.object(action_api.requests, "post", return_value=resp):
            out = action_api.run_action_stream("hello", "live_assist", OPENAI_CFG, seen.append)
        self.assertEqual((out, seen), ("Short.", ["Short."]))

    def test_cut_short_is_a_str_that_says_so(self):
        cut = action_api.CutShort("## Summary\nA")
        self.assertIsInstance(cut, str)
        self.assertEqual(cut, "## Summary\nA")
        self.assertTrue(cut.cut)
        self.assertFalse(getattr("plain", "cut", False))


if __name__ == "__main__":
    unittest.main()


class TestStreamedSmartAction(unittest.TestCase):
    def test_the_answer_is_streamed_when_the_pill_waits_for_its_first_words(self):
        import actions
        seen = []
        cfg = {"_managed_token": "tok", "_on_writing": lambda: seen.append("writing")}

        def stream(text, mode, token, on_token, **kw):
            on_token("Hel")
            on_token("lo")
            return "Hello"
        with patch.object(actions.action_api, "run_managed_action_stream", stream), \
                patch.object(actions.action_api, "run_managed_action", side_effect=AssertionError("not streamed")):
            self.assertEqual(actions._managed_answer("hi", "smart_auto", "tok", config=cfg), "Hello")
        self.assertEqual(seen, ["writing"])                                  # once, at the first words
        with patch.object(actions.action_api, "run_managed_action", return_value="Plain") as plain:
            self.assertEqual(actions._managed_answer("hi", "smart_auto", "tok", config={}), "Plain")
        plain.assert_called_once()
