"""The mouse wheel must never silently change a dropdown/spin-box value.

Scrolling a settings page with the cursor over a QComboBox used to rewrite the
setting under it (Qt's default wheel behaviour) - e.g. quietly switching the
Whisper model or spoken language. Two layers are covered here: the pure
ancestor-walk helper (always runs), and the real end-to-end filter behaviour
(runs when real Qt is importable; skipped under the suite-wide PySide6 stubs
installed by test_core, and runnable on its own).
"""
import unittest


def _real_qt():
    """True only with genuine PySide6 - the suite's stubs hand back a dummy
    class whose __module__ is the stubbing test module, not PySide6."""
    try:
        from PySide6.QtWidgets import QComboBox
        return (isinstance(QComboBox, type)
                and QComboBox.__module__.startswith("PySide6"))
    except Exception:
        return False


class TestEnclosingScrollArea(unittest.TestCase):
    """Pure ancestor walk - duck-typed, no Qt needed."""

    def test_walks_up_to_the_scroll_area(self):
        if not _real_qt():
            self.skipTest("needs real Qt for the isinstance check")
        from PySide6.QtWidgets import QScrollArea, QWidget, QComboBox
        from ui.wheelguard import _enclosing_scroll_area
        _ensure_app()
        area = QScrollArea()
        inner = QWidget(area)
        combo = QComboBox(inner)
        self.assertIs(_enclosing_scroll_area(combo), area)

    def test_returns_none_without_a_scroll_area(self):
        if not _real_qt():
            self.skipTest("needs real Qt for the isinstance check")
        from PySide6.QtWidgets import QWidget, QComboBox
        from ui.wheelguard import _enclosing_scroll_area
        _ensure_app()
        parent = QWidget()
        combo = QComboBox(parent)
        self.assertIsNone(_enclosing_scroll_area(combo))


def _ensure_app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@unittest.skipUnless(_real_qt(), "real PySide6 not importable (stubbed)")
class TestWheelGuardBehaviour(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QScrollArea, QWidget, QVBoxLayout, QComboBox
        import ui.wheelguard as wg

        self.app = _ensure_app()
        self.assertTrue(wg.install(self.app))          # (idempotent: the classes themselves are guarded)

        self.area = QScrollArea()
        inner = QWidget()
        lay = QVBoxLayout(inner)
        self.combo = QComboBox(inner)
        self.combo.addItems([f"item {i}" for i in range(10)])
        self.combo.setCurrentIndex(3)
        lay.addWidget(self.combo)
        inner.setFixedHeight(3000)          # taller than the viewport -> scrollable
        self.area.setWidget(inner)
        self.area.resize(300, 200)
        self.area.show()

    def tearDown(self):
        self.area.close()

    def _wheel(self, widget, dy=-120):
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        pos = QPointF(widget.rect().center())
        return QWheelEvent(pos, widget.mapToGlobal(pos.toPoint()).toPointF(),
                           QPoint(0, dy), QPoint(0, dy), Qt.NoButton,
                           Qt.NoModifier, Qt.NoScrollPhase, False)

    def test_wheel_does_not_change_combo_value(self):
        from PySide6.QtWidgets import QApplication
        before = self.combo.currentIndex()
        for _ in range(5):
            QApplication.sendEvent(self.combo, self._wheel(self.combo))
        self.assertEqual(self.combo.currentIndex(), before,
                         "scrolling over the dropdown changed its value")

    def test_wheel_scrolls_the_page_instead(self):
        from PySide6.QtWidgets import QApplication
        bar = self.area.verticalScrollBar()
        bar.setValue(0)
        QApplication.sendEvent(self.combo, self._wheel(self.combo))
        self.assertGreater(bar.value(), 0,
                           "the scroll gesture was swallowed instead of scrolling the page")

    def test_spinbox_value_is_guarded_too(self):
        from PySide6.QtWidgets import QApplication, QSpinBox
        spin = QSpinBox(self.area.widget())
        spin.setRange(0, 100)
        spin.setValue(10)
        QApplication.sendEvent(spin, self._wheel(spin))
        self.assertEqual(spin.value(), 10)

    def test_normal_widgets_are_untouched(self):
        # A plain scrollable widget keeps its own wheel handling (only the three value widgets are guarded).
        from PySide6.QtWidgets import QComboBox, QTextEdit
        import ui.wheelguard as wg
        self.assertIsNot(QTextEdit.wheelEvent, wg._leave_it_to_the_page)
        self.assertIs(QComboBox.wheelEvent, wg._leave_it_to_the_page)

    def test_subclasses_and_sliders_are_guarded_and_nothing_is_filtered_app_wide(self):
        """2026-11-16 speed audit: an application-wide filter put every event of every widget through Python."""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QComboBox, QSlider

        class Mine(QComboBox):
            pass
        mine = Mine(self.area.widget())
        mine.addItems(["a", "b", "c"])
        QApplication.sendEvent(mine, self._wheel(mine))
        self.assertEqual(mine.currentIndex(), 0)
        slider = QSlider(Qt.Horizontal, self.area.widget())
        slider.setValue(5)
        QApplication.sendEvent(slider, self._wheel(slider))
        self.assertEqual(slider.value(), 5)
        import ui.wheelguard as wg
        self.assertFalse(hasattr(wg, "_guard") and wg._guard is not None)

    def test_keyboard_still_changes_the_value(self):
        # Blocking the wheel must not block deliberate keyboard changes.
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent as _QEvent
        from PySide6.QtWidgets import QApplication
        self.combo.setFocus()
        before = self.combo.currentIndex()
        QApplication.sendEvent(self.combo, QKeyEvent(
            _QEvent.Type.KeyPress, Qt.Key_Down, Qt.NoModifier))
        self.assertEqual(self.combo.currentIndex(), before + 1)


if __name__ == "__main__":
    unittest.main()
