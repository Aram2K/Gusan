"""File transcription ("Audio to text"): the helpers the 1.x file tab used now
live in file_jobs (the Audio to text page and FileJobController run on them),
with no length limit for anyone.

The job itself is covered Qt-free in test_file_jobs; the classic tab
(ui/file_transcribe.py) retired with the 1.x Settings window.
"""
import unittest


class TestClassicTab(unittest.TestCase):
    def setUp(self):
        import file_jobs
        self.ft = file_jobs

    def test_any_length_for_everyone(self):
        # Transcription is local and unlimited: no cap, no refusal, no plan talk.
        for name in ("MAX_SEC", "duration_error"):
            self.assertFalse(hasattr(self.ft, name), name)

    def test_helpers_are_the_shared_ones(self):
        for name in ("AUDIO_EXTS", "probe_duration", "pick_auto_model", "_fmt_dur",
                     "_fmt_eta", "run_file_job", "refusal_message"):
            self.assertTrue(callable(getattr(self.ft, name)) or name == "AUDIO_EXTS", name)
        self.assertTrue(self.ft.AUDIO_EXTS)
        self.assertEqual(self.ft._fmt_dur(59), "59 s")
        self.assertEqual(self.ft._fmt_dur(3725), "1 h 02 min")

    def test_auto_model_is_a_real_catalog_model(self):
        from main import MODELS
        self.assertIn(self.ft.pick_auto_model(), MODELS)

    def test_catalog_names_match_main(self):
        from main import MODELS
        self.assertEqual(set(self.ft.WHISPER_MODELS), set(MODELS))


if __name__ == "__main__":
    unittest.main()
