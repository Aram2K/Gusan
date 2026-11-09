"""The "What's new" window (ui/whats_new_window.py) and when the app opens it (main.AppController._whats_new_check)."""

from types import SimpleNamespace
from unittest import mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QLabel

import whats_new


def _spin(n=5):
    for _ in range(n):
        QApplication.processEvents()


@pytest.fixture
def made(qapp, monkeypatch):
    import ui.winfit
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)
    items = []
    yield items
    for w in items:
        w.close()
        w.deleteLater()
    _spin()


def _window(made, previous="2.1.3", current="2.1.4", animate=False):
    from ui.whats_new_window import WhatsNewWindow
    w = WhatsNewWindow(whats_new.sections(previous, current), current=current, previous=previous,
                       release_url="https://example.invalid/releases/tag/v" + current, animate=animate)
    made.append(w)
    w.show()
    _spin()
    return w


def test_each_change_has_its_kind_title_and_line(made):
    w = _window(made)
    assert isinstance(w, QDialog) and not w.isModal()
    assert w.title_label.text() == "What's new"
    assert w.sub_label.text() == "Gusan 2.1.4 · updated from 2.1.3"
    notes = dict(whats_new.NOTES)["2.1.4"]
    assert [(i.kind, i.title) for i in w.items] == [(k, t) for k, t, _l in notes]
    words = {i.chip.text() for i in w.items}
    assert words == {"NEW", "BETTER", "FIXED"}
    assert w.items[0].accessibleName().startswith("New: Choose your avatar.")
    assert not [lb for lb in w.findChildren(QLabel) if "Gusan 2.1." in lb.text() and lb is not w.sub_label]  # one release: no labels


def test_skipped_releases_are_listed_newest_first_with_their_names(made):
    w = _window(made, previous="2.1.1")
    labels = [lb.text() for lb in w.findChildren(QLabel) if lb.text().startswith("Gusan 2.1.") and lb is not w.sub_label]
    assert labels == ["Gusan 2.1.4", "Gusan 2.1.3", "Gusan 2.1.2"]
    assert w.scroll.height() <= 470                              # a long list scrolls inside the window


def test_one_release_fits_without_a_scroll_bar(made):
    w = _window(made)
    assert not w.scroll.verticalScrollBar().isVisible()


def test_the_rows_rise_into_place_and_got_it_closes(made):
    w = _window(made, animate=True)
    assert w.rows and w.rows[-1].fx.opacity() < 1.0              # the last row is still on its way
    w.finish_animation()
    assert all(r.fx.opacity() == 1.0 and r.get_lift() == 0.0 for r in w.rows)
    assert w.ok_button.isDefault()
    w.ok_button.click()
    assert not w.isVisible()


def test_see_all_changes_opens_the_release_page(made):
    w = _window(made)
    with mock.patch("ui.whats_new_window.QDesktopServices.openUrl") as opened:
        w.all_button.click()
    assert opened.call_args[0][0].toString() == "https://example.invalid/releases/tag/v2.1.4"


def test_the_app_shows_it_once_after_an_update_and_never_on_a_fresh_install(qapp):
    import main
    timers = []
    app = SimpleNamespace(cfg={"onboarding_done": True, "whats_new_seen": "2.1.2"}, saves=0)
    app.schedule_config_save = lambda: setattr(app, "saves", app.saves + 1)
    shown = []
    app.show_whats_new = lambda previous, sections, activate=True: shown.append((previous, [v for v, _ in sections],
                                                                                 activate))
    with mock.patch.object(main, "APP_VERSION", "2.1.3"), \
            mock.patch.object(main, "QTimer", SimpleNamespace(singleShot=lambda ms, fn: timers.append((ms, fn)))):
        assert main.AppController._whats_new_check(app) is True
        assert app.cfg["whats_new_seen"] == "2.1.3" and app.saves == 1
        assert [ms for ms, _fn in timers] == [2500]
        timers[0][1]()
        assert shown == [("2.1.2", ["2.1.3"], False)]            # on top, the keyboard stays where it was
        assert main.AppController._whats_new_check(app) is False  # never twice
        fresh = SimpleNamespace(cfg={"onboarding_done": False}, saves=0)
        fresh.schedule_config_save = lambda: None
        assert main.AppController._whats_new_check(fresh) is False
        assert fresh.cfg["whats_new_seen"] == "2.1.3"            # a fresh install starts counting from here
