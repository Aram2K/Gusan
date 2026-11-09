"""Choosing an avatar (ui/components/avatars.py, avatar_picker.py). The founder (2026-11-09): "allow us to choose an
avatar with edit over hover", "preselected avatars"; and the small avatar looked cut at the top and left."""

import pytest
from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from tests_qt.conftest import make_stub_app
from ui.components import avatars


def _spin(n=5):
    for _ in range(n):
        QApplication.processEvents()


@pytest.fixture(autouse=True)
def _fresh_choice(tmp_path, monkeypatch):
    import storage
    monkeypatch.setattr(storage, "path_for", lambda name: tmp_path / name)
    avatars.set_choice("")
    yield
    avatars.set_choice("")


@pytest.fixture
def account(qapp, monkeypatch):
    import ui.winfit
    from ui.shell import MainWindow
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)
    made = []

    def build(tier="pro"):
        app = make_stub_app(tier)
        saved = []
        app.set_setting = lambda key, value: saved.append((key, value)) or True
        win = MainWindow(app)
        win.resize(1280, 820)
        win.show()
        made.append(win)
        page = win.open("account")
        _spin()
        return win, page, saved
    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    _spin()


def test_the_choices_and_the_default():
    assert list(avatars.PRESETS)[:6] == ["blue", "violet", "teal", "coral", "pink", "slate"]     # the letter on a colour
    assert len(avatars.PRESETS) == 12
    assert avatars.normalize("") == avatars.normalize("nonsense") == "blue"
    assert avatars.normalize(" Sparkle ") == "sparkle" and avatars.normalize("photo") == "photo"
    avatars.set_choice("photo")
    assert avatars.choice() == "blue"                          # no photo saved yet: the default


def test_a_signed_in_avatar_shows_a_pencil_on_hover_and_opens_the_choices(account):
    win, page, saved = account("pro")
    from ui.components.avatar_picker import AvatarButton, EDIT_NAME
    av = page.avatar
    assert isinstance(av, AvatarButton) and av.accessibleName() == EDIT_NAME
    assert not av.editing_shown() and not page.avatar_picker.isVisible()
    av._hover = True
    assert av.editing_shown()
    QTest.mouseClick(av, Qt.LeftButton, pos=QPoint(22, 22))
    _spin()
    assert page.avatar_picker.isVisible()
    blue = page.avatar_picker.choices[0]
    assert blue.is_selected() and blue.accessibleName() == "Blue, chosen"
    sparkle = next(c for c in page.avatar_picker.choices if c.value == "sparkle")
    sparkle.click()
    assert saved == [("avatar", "sparkle")]
    assert avatars.choice() == "sparkle" and sparkle.is_selected() and not blue.is_selected()
    page.avatar_picker.done_button.click()
    assert not page.avatar_picker.isVisible()
    QTest.keyClick(av, Qt.Key_Space)                          # the keyboard opens it too
    assert page.avatar_picker.isVisible()


def test_a_guest_keeps_the_person_icon_and_has_nothing_to_choose(account):
    win, page, _saved = account("guest")
    from ui.components.avatar_picker import AvatarButton
    assert not isinstance(page.avatar, AvatarButton) and page.avatar.is_guest()
    assert page.avatar_picker is None


def test_your_own_photo_is_cut_to_a_square_and_offered_once_it_is_there(account, tmp_path):
    win, page, saved = account("pro")
    src = QImage(400, 200, QImage.Format_RGB32)
    src.fill(QColor("#ff0000"))
    path = tmp_path / "me.png"
    src.save(str(path))
    picker = page.avatar_picker
    page._toggle_avatar_picker(True)
    _spin()
    assert not picker.photo_choice.isVisible() and picker.upload_button.text() == "Upload a photo"
    assert avatars.save_photo(str(path))
    kept = QImage(avatars.photo_path())
    assert (kept.width(), kept.height()) == (avatars.PHOTO_SIZE, avatars.PHOTO_SIZE)
    page._choose_avatar(avatars.PHOTO)
    picker.refresh()
    _spin()
    assert avatars.choice() == "photo" and saved[-1] == ("avatar", "photo")
    assert picker.photo_choice.isVisible() and picker.photo_choice.is_selected()
    assert picker.upload_button.text() == "Change photo"
    assert not avatars.save_photo(str(tmp_path / "not-a-picture.txt"))


def test_every_avatar_follows_the_choice_and_the_window_reads_it_at_start(account):
    win, page, _saved = account("pro")
    win.app.cfg["avatar"] = "teal"
    win.on_setting_changed("avatar")
    assert avatars.choice() == "teal"


@pytest.mark.parametrize("ratio", [1.0, 1.25, 1.5])
def test_the_circle_is_never_cut_at_the_top_or_the_left(qapp, ratio):
    """Drawn half a pixel inside its square: at 125 and 150 percent the edge pixels are anti-aliased, not chopped."""
    size = 22
    img = QImage(int(size * ratio) + 2, int(size * ratio) + 2, QImage.Format_ARGB32)
    img.fill(0)
    p = QPainter(img)
    p.scale(ratio, ratio)
    avatars.paint(p, QRectF(0, 0, size, size), "A")
    p.end()
    mid = int(size * ratio / 2)
    # the topmost and leftmost painted pixels are lighter (partly covered) than the inside
    assert QColor.fromRgba(img.pixel(mid, 0)).alpha() < 255
    assert QColor.fromRgba(img.pixel(0, mid)).alpha() < 255
    assert QColor.fromRgba(img.pixel(mid, mid)).alpha() == 255


def test_the_dictation_link_says_where_it_goes(qapp):
    from ui.shell import MainWindow
    win = MainWindow(make_stub_app("pro"))
    try:
        page = win.open("dictation")
        assert page.more_options.text() == "Speech & AI settings"
    finally:
        win.deleteLater()
