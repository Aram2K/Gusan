"""The Live Assistant card shows an answer as Markdown. Qt's text widgets fetch an
image named in Markdown from the local disk - and on Windows a \\\\host\\share path
is a network connection - so a document (or anything the model read) that makes the
model write one must not get it loaded. Two layers, both tested here: the answer is
cleaned first (live_context.safe_answer_markdown, called by render_markdown), and
the answer box refuses every resource anyway (_AnswerEdit)."""
import re

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor, QImage, QTextDocument
from PySide6.QtWidgets import QTextEdit


@pytest.fixture
def local_picture(tmp_path):
    path = tmp_path / "secret.png"
    img = QImage(40, 40, QImage.Format_RGB32)
    img.fill(QColor("red"))
    assert img.save(str(path))
    return QUrl.fromLocalFile(str(path)).toString()


def _paint(qapp, edit):
    edit.resize(420, 300)
    edit.show()
    qapp.processEvents()
    edit.grab()
    qapp.processEvents()


def _resolved(edit, url):
    res = edit.document().resource(QTextDocument.ImageResource, QUrl(url))
    return res is not None and not (hasattr(res, "isNull") and res.isNull())


def test_stock_qt_does_load_a_local_image_named_in_markdown(qapp, local_picture):
    """The reason for the two layers: a plain QTextEdit really reads the file."""
    edit = QTextEdit()
    edit.document().setMarkdown("hi ![x](%s) there" % local_picture)
    _paint(qapp, edit)
    assert _resolved(edit, local_picture)
    edit.close()


def test_the_answer_box_never_loads_a_resource(qapp, local_picture):
    from ui.live_assist import _AnswerEdit
    edit = _AnswerEdit()
    edit.document().setMarkdown("hi ![x](%s) there" % local_picture)    # even uncleaned
    _paint(qapp, edit)
    assert not _resolved(edit, local_picture)
    edit.close()


@pytest.mark.parametrize("md", [
    "![a](%(url)s)",
    "![a][r]\n\n[r]: %(url)s",
    "![r]\n\n[r]: %(url)s",
    "![" + "x" * 300 + "](%(url)s)",
    "![a\nb](%(url)s)",
    "\\![r]\n\n[r]: %(url)s",
    "![a](//attacker.invalid/share/a.png)",
    "![a](\\\\attacker.invalid\\share\\a.png)",
    "<img src=\"%(url)s\">",
])
def test_render_markdown_shows_no_image_for_any_form(qapp, local_picture, md):
    from ui.live_assist import _AnswerEdit, render_markdown
    asked = []

    class Spy(_AnswerEdit):
        def loadResource(self, type_, name):
            asked.append(name.toString())
            return super().loadResource(type_, name)
    edit = Spy()
    render_markdown(edit, "Answer: " + md % {"url": local_picture})
    _paint(qapp, edit)
    rendering_asked = list(asked)               # before the probe below asks for it itself
    assert not _resolved(edit, local_picture)
    assert "<img" not in edit.toHtml()
    assert rendering_asked == [], rendering_asked
    assert "Answer:" in edit.toPlainText()
    edit.close()


def test_the_card_uses_that_answer_box(qapp):
    import inspect
    from ui import live_assist
    src = inspect.getsource(live_assist.LiveAssistOverlay)
    assert re.search(r"self\.txt_suggestion = _AnswerEdit\(", src)


def test_a_normal_answer_still_renders_with_formatting(qapp):
    from ui.live_assist import _AnswerEdit, render_markdown
    edit = _AnswerEdit()
    render_markdown(edit, "**Say this:** the deadline is Oct 31.\n\n- first\n- second\n\n```py\nx = [1, 2][0]\n```")
    html = edit.toHtml()
    assert "font-weight" in html and "first" in html and "x = [1, 2][0]" in edit.toPlainText()
    edit.close()
