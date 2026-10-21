"""The notes engines are told what the transcript's speaker labels mean: "You:" is
the person who recorded, "Speaker N:" / "Them:" are the others on the call."""
import unittest

import action_api


class TestNotesPromptsKnowTheLabels(unittest.TestCase):
    TRANSCRIPT = "You: I will send the budget.\nSpeaker 1: Good, by Friday.\nThem: Thanks."

    def _text(self, messages):
        return "\n".join(str(m.get("content")) for m in messages)

    def test_the_cloud_prompt_explains_you_and_the_others(self):
        body = self._text(action_api.build_messages(self.TRANSCRIPT, "meeting_notes"))
        self.assertIn("'You:'", body)
        self.assertIn("recorded the meeting", body)
        self.assertIn("'Speaker 1:'", body)
        self.assertIn("'Them:'", body)
        self.assertIn("never invent a name for a label", body)
        self.assertIn(self.TRANSCRIPT, body)               # the transcript itself is untouched

    def test_the_local_prompt_explains_them_too(self):
        try:
            import local_llm
        except Exception as e:                              # pragma: no cover - heavy import unavailable
            self.skipTest("local_llm can't be imported here: %s" % e)
        body = self._text(local_llm._messages_for("meeting_notes", self.TRANSCRIPT, "auto", "en"))
        self.assertIn("'You:'", body)
        self.assertIn("'Speaker N:'", body)
        self.assertIn("'Them:'", body)
        self.assertIn(self.TRANSCRIPT, body)

    def test_other_modes_are_unchanged(self):
        for mode in ("summarize", "make_todo_list", "write_email", "translate"):
            body = self._text(action_api.build_messages("hello", mode))
            self.assertNotIn("recorded the meeting", body, mode)


if __name__ == "__main__":
    unittest.main()
