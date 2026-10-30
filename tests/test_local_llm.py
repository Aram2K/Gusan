import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_llm


class TestLocalLLM(unittest.TestCase):
    def test_legacy_model_ids_normalize_to_real_models(self):
        self.assertEqual(local_llm.normalize_model_id("aibuben_tiny"), local_llm.QWEN_TINY_ID)
        self.assertEqual(local_llm.normalize_model_id("aibuben_balanced"), local_llm.QWEN_3B_ID)
        self.assertEqual(local_llm.normalize_model_id("aibuben_gpu"), local_llm.QWEN_7B_ID)

    def test_catalog_has_three_real_qwen_models(self):
        self.assertIn(local_llm.QWEN_TINY_ID, local_llm.MODEL_CATALOG)
        self.assertIn(local_llm.QWEN_3B_ID, local_llm.MODEL_CATALOG)
        self.assertIn(local_llm.QWEN_7B_ID, local_llm.MODEL_CATALOG)

    def test_remove_model_deletes_folder_and_partial_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_dir = root / "action_models" / local_llm.QWEN_TINY_ID
            model_dir.mkdir(parents=True)
            (model_dir / local_llm.MODEL_CATALOG[local_llm.QWEN_TINY_ID]["filename"]).write_bytes(b"x")
            (model_dir / f"{local_llm.MODEL_CATALOG[local_llm.QWEN_TINY_ID]['filename']}.part").write_bytes(b"partial")
            with patch.object(local_llm.storage, "path_for", side_effect=lambda name: root / name):
                self.assertTrue(local_llm.remove_model(local_llm.QWEN_TINY_ID))
                self.assertFalse(model_dir.exists())


if __name__ == "__main__":
    unittest.main()


class _FakeTokLLM:
    """Tokenizes ~1 token per whitespace word; never generates."""
    def tokenize(self, b, add_bos=False, special=False):
        return list(range(max(1, len(b.split()))))


class TestLongInputHandling(unittest.TestCase):
    def test_translate_output_budget_scales_with_input(self):
        # Regression: a flat 240-token cap silently truncated translations
        # (llama-cpp stops at max_tokens with no error). Every chunk's output
        # budget must be at least as large as its input.
        calls = []
        def fake_chat(llm, messages, max_tokens):
            calls.append((sum(len((m.get("content") or "").split())
                              for m in messages), max_tokens))
            return "T " * 40
        with patch.object(local_llm, "_chat", fake_chat):
            local_llm._run_action_locked(
                _FakeTokLLM(), "word " * 6000, "translate", "en", "ru", "")
        self.assertGreater(len(calls), 1, "long translate must chunk")
        for in_toks, mt in calls:
            self.assertGreaterEqual(mt, in_toks)

    def test_short_translate_single_call_still_scaled(self):
        calls = []
        with patch.object(local_llm, "_chat",
                          lambda llm, m, max_tokens: calls.append(max_tokens) or "ok"):
            local_llm._run_action_locked(
                _FakeTokLLM(), "word " * 500, "translate", "en", "ru", "")
        self.assertEqual(len(calls), 1)
        self.assertGreaterEqual(calls[0], 1000)

    def test_split_by_tokens_loses_nothing(self):
        llm = _FakeTokLLM()
        text = "\n".join(f"Speaker {i%3+1}: " + "x " * 40 for i in range(50))
        chunks = local_llm._split_by_tokens(llm, text, 200)
        self.assertEqual(" ".join(text.split()), " ".join("\n".join(chunks).split()))

    def test_infer_lock_shared_across_legacy_aliases(self):
        lock = local_llm._infer_lock_for("qwen_tiny")
        self.assertIs(lock, local_llm._infer_lock_for("aibuben_tiny"))


class TestLocalNotesLength(unittest.TestCase):
    """The notes' length is planned from the meeting as it was, even when the on-computer model has to condense it first."""

    def run_notes(self, text, condensed="the condensed talk"):
        seen = []
        with patch.object(local_llm, "_chat", lambda llm, messages, max_tokens: seen.append((messages, max_tokens)) or "ok"), \
                patch.object(local_llm, "_condense_to_fit", lambda llm, t, target: condensed):
            local_llm._run_action_locked(_FakeTokLLM(), text, "meeting_notes", "auto", "en", "")
        return seen

    def test_a_long_meeting_is_planned_long_though_the_model_reads_the_condensed_text(self):
        (messages, max_tokens), = self.run_notes("word " * 12000)
        user = messages[-1]["content"]
        self.assertIn("at most 8 topics", user)
        self.assertTrue(user.endswith("User text:\nthe condensed talk"))
        self.assertEqual(max_tokens, 2500)               # the on-computer budget is unchanged

    def test_a_meeting_that_fits_is_planned_from_itself(self):
        (messages, _mt), = self.run_notes("word " * 1000)
        self.assertIn("at most 4 topics", messages[-1]["content"])
        (messages, _mt), = self.run_notes("You: hello there. Them: hi.")
        self.assertNotIn("## Discussion", messages[-1]["content"])

    def test_the_condensing_asks_to_keep_the_topics_in_order(self):
        self.assertIn("topics in the order", local_llm._CONDENSE_INSTRUCTION)
        self.assertIn("owner name, number, date", local_llm._CONDENSE_INSTRUCTION)

    def test_the_other_modes_do_not_plan(self):
        for mode in ("summarize", "write_email", "make_todo_list", "live_recap"):
            body = local_llm._messages_for(mode, "word " * 1000, "auto", "en", notes_words=12000)[-1]["content"]
            self.assertNotIn("Discussion", body, mode)
