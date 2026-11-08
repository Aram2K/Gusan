"""Who is speaking, live (live_speakers.py): one voice print per finished sentence, matched to the meeting's voices.
The speaker model is faked: a sentence's "audio" says whose voice it is, and its print is that voice's direction."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import live_speakers as ls  # noqa: E402

VOICES = {"aram": [1.0, 0.0, 0.0, 0.0], "anna": [0.0, 1.0, 0.0, 0.0], "marc": [0.0, 0.0, 1.0, 0.0],
          "lena": [0.0, 0.0, 0.0, 1.0]}


class Audio(list):
    """``seconds`` of a person's speech (only its length and owner matter to the fake model)."""

    def __init__(self, who, seconds):
        super().__init__([0.0] * int(16000 * seconds))
        self.who = who


def _print_of(samples):
    a, b, c, d = VOICES[samples.who]
    return [a + 0.05, b + 0.03, c + 0.02, d + 0.04]          # a little noise: never exactly the average


class TestLabels(unittest.TestCase):
    def test_a_call_you_then_each_person_on_the_call_gets_a_number(self):
        s = ls.LiveSpeakers(_print_of, two_sources=True)
        self.assertEqual(s.label(Audio("aram", 3), "mic"), "You")
        self.assertEqual(s.label(Audio("anna", 3), "call"), "Speaker 1")
        self.assertEqual(s.label(Audio("marc", 2.5), "call"), "Speaker 2")
        self.assertEqual(s.label(Audio("anna", 4), "call"), "Speaker 1")          # the same voice again
        self.assertEqual(s.label(Audio("aram", 2), "mic"), "You")

    def test_a_second_person_at_your_microphone_is_not_you(self):
        s = ls.LiveSpeakers(_print_of, two_sources=True)
        self.assertEqual(s.label(Audio("aram", 3), "mic"), "You")
        self.assertEqual(s.label(Audio("lena", 3), "mic"), "Speaker 1")         # the same mic, another voice

    def test_someone_on_the_call_is_never_you_even_with_a_similar_voice(self):
        s = ls.LiveSpeakers(_print_of, two_sources=True)
        self.assertEqual(s.label(Audio("aram", 3), "mic"), "You")
        self.assertEqual(s.label(Audio("aram", 3), "call"), "Speaker 1")         # the same print, the call's side
        self.assertEqual(s.label(Audio("anna", 3), ""), "Speaker 2")              # side unknown: any voice

    def test_one_source_numbers_everyone(self):
        s = ls.LiveSpeakers(_print_of, two_sources=False)
        self.assertEqual(s.label(Audio("aram", 3)), "Speaker 1")
        self.assertEqual(s.label(Audio("anna", 3)), "Speaker 2")

    def test_a_short_sentence_never_starts_a_voice(self):
        s = ls.LiveSpeakers(_print_of, two_sources=True)
        self.assertEqual(s.label(Audio("anna", 1.0), "call"), "")                  # nobody known yet
        self.assertEqual(s.label(Audio("anna", 3), "call"), "Speaker 1")
        self.assertEqual(s.label(Audio("marc", 1.0), "call"), "Speaker 1")         # too short for a new voice
        self.assertEqual(s.label(Audio("marc", 0.3), "call"), "Speaker 1")         # no print: the side's last

    def test_no_print_keeps_the_sides_last_label(self):
        calls = []

        def broken(samples):
            calls.append(1)
            raise RuntimeError("model")
        s = ls.LiveSpeakers(broken, two_sources=True)
        s._last["call"] = "Speaker 2"
        self.assertEqual(s.label(Audio("anna", 3), "call"), "Speaker 2")
        self.assertEqual(s.label(None, "call"), "Speaker 2")

    def test_at_most_eight_voices(self):
        n = 12
        vectors = {i: [1.0 if j == i else 0.0 for j in range(n)] for i in range(n)}
        s = ls.LiveSpeakers(lambda samples: vectors[samples.who])
        labels = [s.label(Audio(i, 3)) for i in range(n)]
        self.assertEqual(len(s.voices), ls.MAX_VOICES)
        self.assertEqual(len(set(labels)), ls.MAX_VOICES)

    def test_no_labeler_without_the_speaker_model(self):
        import diarization
        with mock.patch.object(diarization, "is_available", lambda: False):
            self.assertIsNone(ls.labeler(two_sources=True))


if __name__ == "__main__":
    unittest.main()
