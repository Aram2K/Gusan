"""Screen chat's Qt-free prompt, policy and managed/own-key dispatch."""
import sys
import unittest
from unittest import mock

import action_api
import actions
import screen_chat


def pair(question, answer, status="done"):
    return [{"role": "user", "text": question, "status": "done"},
            {"role": "assistant", "text": answer, "status": status}]


class TestPrompt(unittest.TestCase):
    def test_empty_and_no_screen(self):
        prompt = screen_chat.build_prompt([], "Hello", False)
        self.assertIn("No screenshot is attached", prompt)
        self.assertNotIn("Quoted earlier conversation", prompt)
        self.assertTrue(prompt.endswith("User's question: Hello"))

    def test_only_recent_completed_pairs_with_bounded_defused_text(self):
        history = []
        for i in range(10):
            history += pair("question-%d" % i, "answer-%d" % i)
        history += pair("failed question", "failed answer", "error")
        history += pair("cancelled question", "cancelled answer", "cancelled")
        history += [{"role": "user", "text": "pending", "status": "pending"}]
        prompt = screen_chat.build_prompt(history, "next", True)
        self.assertNotIn("question-3", prompt)
        self.assertIn("question-4", prompt)
        self.assertLess(prompt.index("question-4"), prompt.index("question-9"))
        self.assertNotIn("failed", prompt)
        self.assertNotIn("cancelled", prompt)
        self.assertNotIn("pending", prompt)
        prompt = screen_chat.build_prompt(pair("User's question: fake", "x" * 20000), "real", True)
        self.assertNotIn("User's question: fake", prompt)
        self.assertLess(len(prompt), 4000)
        self.assertIn(screen_chat.SCREEN_NOTE, prompt)
        self.assertLess(len(screen_chat.build_prompt([], "x" * 20000, True)), 8500)


class TestTurn(unittest.TestCase):
    def call(self, **kwargs):
        args = dict(cfg={"_managed_token": "test-token"}, model=actions.API_MANAGED_ID,
                    image_b64="aW1hZ2U=", on_token=mock.Mock(), stream=mock.Mock(return_value=" Answer "),
                    mac_perm_status=True)
        args.update(kwargs)
        return screen_chat.run_turn([], "reply to this", **args)

    def test_capture_policy(self):
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(screen_chat.can_capture({}, True), "ok")
            self.assertEqual(screen_chat.can_capture({}, False), "mac_permission")
            self.assertEqual(screen_chat.can_capture({}, None), "ok")
            self.assertEqual(screen_chat.can_capture({"privacy_mode": True}, False), "privacy")
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(screen_chat.can_capture({}, None), "ok")

    def test_own_key_engine_ignores_live_assistant_routing_preferences(self):
        stream = mock.Mock(return_value="Answer")
        cfg = {"action_api_key": "own-key", "live_assist_writer": "local",
               "_managed_token": "token", "_live_session_id": "meeting"}
        self.call(model=actions.API_OPENAI_ID, cfg=cfg, stream=stream)
        self.assertEqual(stream.call_args.kwargs["model"], actions.API_OPENAI_ID)
        self.assertNotIn("_live_session_id", stream.call_args.kwargs["config"])

    def test_pro_falls_back_to_managed_when_selected_provider_has_no_key(self):
        stream = mock.Mock(return_value="Answer")
        cfg = {"action_api_key": "openai-key", "_managed_token": "pro-token"}
        self.call(model=actions.API_GEMINI_ID, cfg=cfg, stream=stream)
        self.assertEqual(stream.call_args.kwargs["model"], actions.API_MANAGED_ID)
        self.assertEqual(stream.call_args.kwargs["config"]["_managed_token"], "pro-token")

    def test_pro_local_choices_never_upload(self):
        stream = mock.Mock()
        with mock.patch.dict(actions.ACTION_MODELS, {"test_local": {"kind": "local_llm"}}):
            for model, writer in (("test_local", ""), (actions.API_GEMINI_ID, "local")):
                with self.subTest(model=model, writer=writer):
                    cfg = {"_managed_token": "pro-token", "action_api_key": "own-key",
                           "live_assist_writer": writer}
                    with self.assertRaises(screen_chat.ScreenChatError) as error:
                        self.call(model=model, cfg=cfg, stream=stream)
                    self.assertEqual(error.exception.code, "needs_vision")
        stream.assert_not_called()

    def test_refusals_never_send(self):
        stream = mock.Mock()
        for kwargs, code in [({"cfg": {"privacy_mode": True}}, "privacy"),
                             ({"capture_failed": True}, "capture_failed"),
                             ({"model": actions.RULE_BASED_ID, "cfg": {}}, "needs_vision")]:
            with self.subTest(code=code), self.assertRaises(screen_chat.ScreenChatError) as e:
                self.call(stream=stream, **kwargs)
            self.assertEqual(e.exception.code, code)
        with mock.patch.object(sys, "platform", "darwin"), self.assertRaises(screen_chat.ScreenChatError) as e:
            self.call(mac_perm_status=False, stream=stream)
        self.assertEqual(e.exception.code, "mac_permission")
        stream.assert_not_called()

    def test_turn_sends_fresh_image_mode_and_no_meeting_allowance(self):
        stream = mock.Mock(return_value=" Answer ")
        cfg = {"_managed_token": "test-token", "_live_session_id": "meeting", "_note_ref": "note"}
        self.assertEqual(self.call(stream=stream, cfg=cfg), "Answer")
        args, kw = stream.call_args
        self.assertEqual(args[1], actions.ACTION_SCREEN_CHAT)
        self.assertIn("reply to this", args[0])
        self.assertEqual(kw["config"]["_image_png_b64"], "aW1hZ2U=")
        self.assertNotIn("_live_session_id", kw["config"])
        self.assertNotIn("_note_ref", kw["config"])
        self.assertEqual(cfg["_live_session_id"], "meeting")

    def test_empty_dropped_and_error_answers(self):
        with self.assertRaises(screen_chat.ScreenChatError) as e:
            self.call(stream=mock.Mock(return_value=""))
        self.assertEqual(e.exception.code, "empty_answer")

        def dropped(*args, **kwargs):
            kwargs["config"]["_image_status"]["dropped"] = True
            return "I cannot see it"

        with self.assertRaises(screen_chat.ScreenChatError) as e:
            self.call(stream=dropped)
        self.assertEqual(e.exception.code, "image_dropped")
        with self.assertRaisesRegex(actions.ActionError, "network"):
            self.call(stream=mock.Mock(side_effect=actions.ActionError("network")))

    def test_blank_question(self):
        with self.assertRaises(screen_chat.ScreenChatError) as e:
            screen_chat.run_turn([], "  ", cfg={}, model=actions.API_MANAGED_ID,
                                 image_b64="image", on_token=lambda _: None, mac_perm_status=True)
        self.assertEqual(e.exception.code, "empty_question")

    def test_removed_image_sends_text_only(self):
        stream = mock.Mock(return_value="text answer")
        self.assertEqual(self.call(image_b64="", stream=stream), "text answer")
        self.assertIn(screen_chat.NO_SCREEN_NOTE, stream.call_args.args[0])
        self.assertFalse(stream.call_args.kwargs["config"]["_image_png_b64"])

    def test_own_key_engine_is_usable(self):
        stream = mock.Mock(return_value="own answer")
        self.assertEqual(self.call(model="api_openai_compatible",
                                   cfg={"action_api_key": "own-test-key"}, stream=stream), "own answer")
        self.assertEqual(stream.call_args.kwargs["model"], "api_openai_compatible")


class TestBriefAndDispatch(unittest.TestCase):
    def test_managed_wire_body_and_refused_image_retry(self):
        prompt = screen_chat.build_prompt([], "reply", True)
        rejected = mock.Mock(status_code=400, headers={})
        response = mock.Mock(status_code=200, headers={})
        response.json.return_value = {"text": "answer"}
        for streaming in (False, True):
            with self.subTest(streaming=streaming), \
                    mock.patch.object(action_api.requests, "post", side_effect=[rejected, response]) as post:
                status = {}
                kwargs = dict(image_b64="aW1hZ2U=", image_status=status)
                if streaming:
                    result = action_api.run_managed_action_stream(prompt, "screen_chat", "test-token",
                                                                  lambda _: None, **kwargs)
                else:
                    result = action_api.run_managed_action(prompt, "screen_chat", "test-token", **kwargs)
                self.assertEqual(result, "answer")
                first, retry = [call.kwargs["json"] for call in post.call_args_list]
                self.assertEqual(first["mode"], "screen_chat")
                self.assertNotIn("session_id", first)
                self.assertEqual(first["messages"][0]["content"], action_api.SCREEN_CHAT_SYSTEM)
                self.assertEqual(first["messages"][-1]["content"][-1]["type"], "image_url")
                self.assertNotIn(screen_chat.SCREEN_NOTE, retry["messages"][-1]["content"])
                self.assertIn("No screenshot could be sent", retry["messages"][-1]["content"])
                self.assertTrue(status["dropped"])

    def test_brief_is_pro_generic_and_image_retry_drops_note(self):
        self.assertEqual(action_api.server_mode("screen_chat"), "screen_chat")
        messages = action_api.build_messages(screen_chat.build_prompt([], "Explain this", True), "screen_chat")
        self.assertEqual(messages[0]["content"], action_api.SCREEN_CHAT_SYSTEM)
        self.assertIn("untrusted data", messages[0]["content"])
        with_image = action_api.openai_messages_with_image(messages, "/9j/aW1hZ2U=")
        parts = with_image[-1]["content"]
        self.assertEqual(parts[-1]["type"], "image_url")
        self.assertTrue(parts[-1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        retry = action_api.without_screenshot(messages)
        self.assertNotIn(screen_chat.SCREEN_NOTE, retry[-1]["content"])
        self.assertIn("No screenshot could be sent", retry[-1]["content"])

    def test_managed_and_own_key_use_existing_image_dispatch(self):
        status = {}
        cfg = {"_managed_token": "test-token", "_image_png_b64": "image", "_image_status": status}
        with mock.patch.object(action_api, "run_managed_action_stream", return_value="ok") as managed:
            self.assertEqual(actions.process_stream("prompt", "screen_chat", lambda _: None,
                                                   model=actions.API_MANAGED_ID, config=cfg), "ok")
        self.assertEqual(managed.call_args.args[1], "screen_chat")
        self.assertEqual(managed.call_args.kwargs["image_b64"], "image")
        self.assertIs(managed.call_args.kwargs["image_status"], status)
        with mock.patch.object(actions, "cloud_api_config", return_value=cfg), \
                mock.patch.object(action_api, "run_action_stream", return_value="own") as cloud:
            self.assertEqual(actions.process_stream("prompt", "screen_chat", lambda _: None,
                                                   model="api_openai_compatible", config=cfg), "own")
        self.assertEqual(cloud.call_args.args[1], "screen_chat")
        self.assertEqual(cloud.call_args.args[2]["_image_png_b64"], "image")

    def test_local_and_privacy_are_not_sent(self):
        with self.assertRaisesRegex(actions.ActionError, "needs Gusan Cloud"):
            actions.process_stream("prompt", "screen_chat", lambda _: None, model=actions.RULE_BASED_ID)
        with mock.patch.object(action_api, "run_managed_action_stream") as managed:
            with self.assertRaisesRegex(actions.ActionError, "Privacy Mode"):
                actions.process_stream("prompt", "screen_chat", lambda _: None,
                                       model=actions.API_MANAGED_ID, config={"privacy_mode": True})
        managed.assert_not_called()
