"""A whole Smart Actions take with text selected on screen (main.AppController._stop_impl with doubles, as
tests/test_smart_shortcut.py does): the selection and the spoken words go to the AI, and the result is pasted, or
shown and copied."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import main
import selection_reader as sr

try:
    from tests.test_smart_shortcut import _Recorder
except ImportError:
    from test_smart_shortcut import _Recorder

SMART = main.actions.ACTION_SMART_AUTO
SELECTION = main.actions.ACTION_SMART_SELECTION


class FakeJob:
    def __init__(self, text):
        self.sel = sr.Selection(text, "uia") if text else sr.Selection()
        self.asked = 0

    def result(self, timeout=None):
        self.asked += 1
        return self.sel


class TestSelectionTake(unittest.TestCase):
    def _run(self, selected="Bonjour à tous.", answer="<change>\nHello everyone.", engine="managed", token="tok",
             editable=None):
        conf = {"backend": "local", "whisper_model": "base", "save_history": True, "privacy_mode": False,
                "translate_target": "en", "analytics_enabled": False}
        if token:
            conf["_managed_token"] = token
        auth = SimpleNamespace(is_pro=True, is_authenticated=True, user_id="u1")
        job = FakeJob(selected)
        app = SimpleNamespace(cfg=conf, auth=auth, _oneshot_action=SMART, _take_selection=job, _job_seq=1, is_rec=True,
                              recorder=_Recorder(), overlay=mock.MagicMock(),
                              hints=[], pastes=[], saved=[], processed=[], cards=[], copied=[])
        app.overlay.call_soon = lambda fn, *a, **k: fn(*a, **k)
        app._show_model_wait = lambda: None
        app._note_local_wait = lambda *a, **k: None
        app.show_tray_hint = lambda title, text, on_click=None: app.hints.append((title, text))
        app._postprocess_transcript = lambda t: t
        app.is_pro = lambda: True
        app.sig_auth_changed = SimpleNamespace(emit=lambda: None)
        app._resolve_action_engine = lambda: (engine, conf)
        app._wait_keys_released = lambda s=0: None
        app._paste_via_clipboard = lambda text, **kw: app.pastes.append((text, kw)) or True
        app._nudge_update_after_dictation = lambda: None
        app.notify_history_changed = lambda: None
        app._run_on_gui = lambda fn, *a: fn(*a)

        def process(text, mode, **kw):
            app.processed.append((mode, text))
            return answer if mode == SELECTION else "## To-do\n\n- [ ] Call David"
        with mock.patch.object(main.actions, "process", process), \
                mock.patch.object(main.hist, "save_entry",
                                  lambda text, lang, backend, **extra: app.saved.append((text, extra))), \
                mock.patch.object(sr, "focus_editable", lambda: editable), \
                mock.patch.object(main.AppController, "_rich_copy", staticmethod(lambda t: app.copied.append(t) or t)), \
                mock.patch.object(main.AppController, "_show_answer_card",
                                  lambda self, text, kind="answer": app.cards.append((kind, text))):
            main.AppController._stop_impl(app, 1)
        app.job = job
        return app

    def test_the_selection_and_the_words_go_to_the_ai_and_the_change_is_pasted(self):
        app = self._run()
        mode, text = app.processed[0]
        self.assertEqual(mode, SELECTION)
        self.assertIn("<selected_text>\nBonjour à tous.\n</selected_text>", text)
        self.assertIn("<instruction>call David and book the room</instruction>", text)
        self.assertEqual(app.pastes, [("Hello everyone.", {"restore": False, "rich": True})])
        self.assertEqual(app.cards, [])

    def test_history_keeps_the_words_and_the_result_never_the_selection(self):
        app = self._run()
        text, extra = app.saved[0]
        self.assertEqual(text, "Hello everyone.")
        self.assertEqual(extra, {"action": SMART, "said": "call David and book the room", "on_selection": True})
        self.assertNotIn("Bonjour", repr(app.saved))

    def test_an_answer_is_shown_and_copied_not_pasted(self):
        app = self._run(answer="<answer>\nIt is a greeting to everyone.")
        self.assertEqual(app.pastes, [])
        self.assertEqual(app.cards, [("answer", "It is a greeting to everyone.")])
        self.assertEqual(app.copied, ["It is a greeting to everyone."])

    def test_with_nowhere_to_type_the_change_is_copied_and_shown(self):
        app = self._run(editable=False)
        self.assertEqual(app.pastes, [])
        self.assertEqual(app.cards, [("copied", "Hello everyone.")])

    def test_nothing_selected_is_a_smart_action_as_before(self):
        app = self._run(selected="")
        self.assertEqual([m for m, _t in app.processed], [SMART])
        self.assertEqual(app.pastes[0][1], {"rich": True})

    def test_no_ai_that_can_read_it_is_a_smart_action_as_before(self):
        app = self._run(engine="rule_based", token=None)
        self.assertEqual([m for m, _t in app.processed], [SMART])
        self.assertEqual(app.job.asked, 0)                      # it did not even wait for the selection


if __name__ == "__main__":
    unittest.main()
