"""The speed dots follow the computer (hardware.speed_here; the founder, 2026-11-21: a strong computer, or one with
a GPU, runs the big models fast or instantly). Accuracy never changes."""
import unittest

import hardware as h
import engine_choice


class TestSpeedHere(unittest.TestCase):
    def tearDown(self):
        h.clear_cache()

    def test_before_any_snapshot_the_table_speed(self):
        h.clear_cache()
        self.assertEqual({m: h.speed_here(m) for m in h.MODEL_INFO}, {m: i.speed for m, i in h.MODEL_INFO.items()})

    def test_a_gpu_makes_the_big_models_fast(self):
        p = {"gpu": True, "cores": 6, "ram_gb": 16}
        self.assertEqual(h.speed_here("large-v3-turbo", p), 5)
        self.assertEqual(h.speed_here("medium", p), 4)
        self.assertGreater(h.speed_here("large-v3", p), h.MODEL_INFO["large-v3"].speed)

    def test_a_strong_or_weak_processor_moves_the_bigger_models_one_dot(self):
        strong, weak, mid = {"cores": 12, "ram_gb": 16}, {"cores": 4, "ram_gb": 8}, {"cores": 6, "ram_gb": 16}
        for m, info in h.MODEL_INFO.items():
            self.assertEqual(h.speed_here(m, mid), info.speed, m)
            if info.speed <= 4:
                self.assertEqual(h.speed_here(m, strong), min(5, info.speed + 1), m)
                self.assertEqual(h.speed_here(m, weak), max(1, info.speed - 1), m)
            else:                                           # already the fastest: stays
                self.assertEqual(h.speed_here(m, strong), info.speed, m)
                self.assertEqual(h.speed_here(m, weak), info.speed, m)
        self.assertEqual(h.speed_here("medium", {"cores": 8, "ram_gb": 16, "arm_mac": True}), 3)

    def test_too_little_memory_is_one_dot(self):
        self.assertEqual(h.speed_here("large-v3", {"cores": 12, "ram_gb": 4, "gpu": True}), 1)

    def test_a_snapshot_without_the_gpu_probe_keeps_what_the_probe_found(self):
        def snap(state):
            return h.HardwareSnapshot(cpu_name="x", cores=6, ram_gb=16.0, ram_display_gb=16, gpu_name="",
                                      gpu_discrete=False, gpu_state=state, platform="win32")
        h.note_machine(snap("ready"))
        self.assertTrue(h.speed_profile()["gpu"])
        h.note_machine(snap(None))                          # a later quick snapshot (no GPU probe)
        self.assertTrue(h.speed_profile()["gpu"])
        self.assertEqual(h.speed_here("large-v3-turbo"), 5)

    def test_the_picker_rows_use_this_computers_speed_and_keep_accuracy(self):
        h.clear_cache()
        h.note_machine(h.HardwareSnapshot(cpu_name="x", cores=6, ram_gb=16.0, ram_display_gb=16, gpu_name="RTX",
                                          gpu_discrete=True, gpu_state="ready", platform="win32"))
        rows = {r["value"]: r for r in engine_choice._whisper_rows()}
        self.assertEqual(rows["large-v3-turbo"]["speed"], 5)
        self.assertEqual(rows["large-v3-turbo"]["accuracy"], h.MODEL_INFO["large-v3-turbo"].accuracy)


if __name__ == "__main__":
    unittest.main()
