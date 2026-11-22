"""The People card's speakers on a saved meeting (Gusan 2.1.11).

The founder: "I want proper diarization with speaker names when identified, or Speaker 1, 2, 3 if not; when I stop and
notes are generated I want to see that in the transcript and in People, and I want to edit a speaker's name."

Kept out of meeting_views.py (like meeting_speakers_ui.py) - NotesView._render_people calls ``add_speakers``:

  * a "Speakers" part at the top of the People card, built from the voices the transcript tells apart
    (speaker_map.py): "You" first - the user's first name with "(You)" -, then Speaker 1, 2 ... or their names, each
    with how much of the talk was theirs ("38%") and a pencil. A name Gusan put in by itself (the one other person,
    read off the call's screen) says so under it: it may be wrong, and it is editable like any other;
  * the pencil: with attendees (the invite's, the screen's) a small menu - their names, then "Type a name" - else the
    name turns into a field at once. Enter saves, Esc cancels, an empty field is "Speaker N" again; leaving a changed
    field saves it too;
  * a rename (MeetingSession.rename_speaker, off the window's thread) writes transcript.txt again with every line of
    that voice under the new name and keeps the name; the page then shows it at once - the Transcript tab, People and
    the chat (which reads the folder for every question);
  * "Update notes with these names" under the speakers once a name was set since the notes were written (meta
    ``speaker_names_changed``): Rewrite notes on the renamed transcript - the old notes stay until the new ones are
    written.

The attendees stay listed below under "Attendees", without the people a speaker already is (the user, or a name picked
for a voice). Colours only from theme tokens (the page's sheet: ``mtone``)."""
import logging

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QSizePolicy, QVBoxLayout, QWidget

import speaker_map
from ui import theme
from ui.components import Button, IconButton, icons

logger = logging.getLogger("transcribe")

SECTION_SPEAKERS, SECTION_ATTENDEES = "Speakers", "Attendees"     # (said only when the card has both parts)
TYPE_A_NAME = "Type a name"
OFFER_TEXT = "Update notes with these names"
HINT = {speaker_map.BY_SCREEN: "Named from the call's screen", speaker_map.BY_AUTO: "Named automatically",
        speaker_map.BY_CALENDAR: "Named from the invite"}
FAILED = "Couldn't rename this speaker."


def _views():
    from ui.pages import meeting_views          # (it imports this module: looked up when used)
    return meeting_views


def speaker_map_of(data):
    """The SpeakerMap of a meeting the notes view holds (read_meeting worked it out; worked out here otherwise)."""
    data = data if isinstance(data, dict) else {}
    smap = data.get("speaker_map")
    if not isinstance(smap, speaker_map.SpeakerMap):
        smap = speaker_map.from_text(data.get("transcript") or "", data.get("meta") or {})
        data["speaker_map"] = smap
    return smap


def share_text(sp):
    """"38%" - at least "1%" for somebody who said anything."""
    pct = int(round(float(sp.share or 0.0) * 100))
    return "%d%%" % (max(1, pct) if sp.words else 0)


def first_name(me):
    return (str(me or "").split() or [""])[0]


def section_title(text, parent=None):
    lb = _views().tlabel(text, 12, 600, "muted", parent=parent)
    lb.setAccessibleName(text)
    return lb


def is_a_speaker(name, info):
    """An attendee a speaker already is (the user, or the name picked for a voice): not listed twice."""
    from people_from_screen import first_key
    return bool(info) and first_key(name) in (info.get("keys") or set())


class SpeakerRow(QWidget):
    """One voice of the People card's Speakers: the face, the name ("Aram (You)", "Speaker 2", "Anna"), where an
    automatic name came from, the share of the talk and the pencil. ``renamed(id, name)`` when a name was picked or
    typed ("" = back to its own label)."""

    renamed = Signal(str, str)

    def __init__(self, sp, me="", picks=(), parent=None):
        super().__init__(parent)
        mv = _views()
        self.sid, self.speaker = sp.sid, sp
        self.picks = [p for p in picks if str(p).strip()]
        self.menu = None
        self._editing = False
        first = first_name(me)
        self.is_you = sp.sid == speaker_map.YOU and not sp.name
        self.shown = first if (self.is_you and first) else sp.label
        r = QHBoxLayout(self)
        r.setContentsMargins(0, 0, 0, 0)
        r.setSpacing(10)
        n = speaker_map.number(sp.sid)
        key = self.shown if (sp.name or self.is_you or not n) else str(n)
        face = mv.Faces([key], 28, me=first if self.is_you else "")
        face.setAccessibleName(self.shown)
        r.addWidget(face, 0, Qt.AlignVCenter)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(6)
        self.who = mv.ElidedLabel(self.shown)               # a name typed or read off a screen: plain text
        self.who.setProperty("mtone", "text")
        self.who.setFont(theme.font(13.5, 600))
        self.who.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        line.addWidget(self.who, 0, Qt.AlignVCenter)        # (it ends in "…" when the row is too narrow)
        self.you_tag = None
        if self.is_you and first:
            self.you_tag = mv.tlabel("(You)", 13, 400, "muted")
            line.addWidget(self.you_tag, 0, Qt.AlignVCenter)
        line.addStretch(1)
        self.edit = QLineEdit(self)
        self.edit.setMaxLength(speaker_map.MAX_NAME)
        self.edit.setPlaceholderText(sp.canonical)
        self.edit.setAccessibleName("Name for %s" % self.shown)
        self.edit.setFont(theme.font(13.5))
        self.edit.setFixedHeight(28)
        self.edit.hide()
        self.edit.returnPressed.connect(self.commit)
        self.edit.installEventFilter(self)
        line.addWidget(self.edit, 100, Qt.AlignVCenter)     # (while typing it takes the row: the stretch gives way)
        col.addLayout(line)
        hint = HINT.get(sp.by, "")
        self.hint = mv.tlabel(hint, 12, 400, "muted") if hint else None
        if self.hint is not None:
            self.hint.setToolTip(theme.plain_tip("Gusan put this name in by itself. Click the pencil to change it."))
            col.addWidget(self.hint)
        r.addLayout(col, 1)
        self.share = mv.tlabel(share_text(sp), 12.5, 600, "text2", tabular=True)
        self.share.setToolTip(theme.plain_tip("Share of the words spoken in this meeting"))
        r.addWidget(self.share, 0, Qt.AlignVCenter)
        self.pencil = IconButton("pen", "Rename %s" % self.shown, size=26, icon_size=15, color="text2", parent=self)
        self.pencil.setToolTip(theme.plain_tip("Rename %s" % self.shown))      # (a name is text, never markup)
        self.pencil.clicked.connect(self.pencil_clicked)
        r.addWidget(self.pencil, 0, Qt.AlignVCenter)
        self.setAccessibleName("%s%s, %s of the talk" % (self.shown, " (you)" if self.is_you else "", share_text(sp)))

    # ── the pencil ──
    def pencil_clicked(self):
        """With attendees to pick from: the small menu (their names, then "Type a name"); else the field."""
        if not self.picks:
            self.start_edit()
            return
        mv = _views()
        if self.menu is not None:
            self.menu.deleteLater()                       # (the last one's)
        self.menu = mv.make_menu(self)
        for name in self.picks:
            act = self.menu.addAction(name)
            act.setData(name)
        self.menu.addSeparator()
        typed = self.menu.addAction(icons.icon("pen", 16, "muted"), TYPE_A_NAME)
        typed.setData("")
        self.menu.triggered.connect(lambda act: self.pick(act.data()))
        self.menu.popup(self.pencil.mapToGlobal(QPoint(0, self.pencil.height() + 2)))

    def pick(self, name):
        """A name chosen in the menu ("" = "Type a name": the field)."""
        if not str(name or "").strip():
            self.start_edit()
            return
        self._say(str(name))

    # ── the field ──
    @property
    def editing(self):
        return self._editing

    def start_edit(self):
        self._editing = True
        self.who.hide()
        if self.you_tag is not None:
            self.you_tag.hide()
        self.edit.setText(self.speaker.name or "")
        self.edit.show()
        self.edit.setFocus(Qt.OtherFocusReason)
        self.edit.selectAll()

    def _end_edit(self):
        self._editing = False
        self.edit.hide()
        self.who.show()
        if self.you_tag is not None:
            self.you_tag.show()

    def commit(self):
        """Enter: the typed name - "" puts the speaker's own label back ("Speaker 2")."""
        if not self._editing:
            return
        text = speaker_map.clean_name(self.edit.text())
        self._end_edit()
        if text != (self.speaker.name or ""):
            self._say(text)

    def cancel(self):
        """Esc: nothing changes."""
        if self._editing:
            self._end_edit()

    def _say(self, name):
        if name:
            self.who.setText(name)                      # (shown at once; the page draws it again when it is saved)
        self.renamed.emit(self.sid, name)

    def eventFilter(self, obj, ev):                     # noqa: N802 (Qt name)
        if obj is self.edit and self._editing:
            if ev.type() == QEvent.KeyPress and ev.key() == Qt.Key_Escape:
                self.cancel()
                return True
            if ev.type() == QEvent.FocusOut:
                # Leaving a changed field keeps what was typed; leaving it empty or as it was changes nothing.
                text = speaker_map.clean_name(self.edit.text())
                if text and text != (self.speaker.name or ""):
                    self.commit()
                else:
                    self.cancel()
        return super().eventFilter(obj, ev)


def _picks(view, smap, me):
    """The names the pencil's menu offers: the meeting's people (the invite's, the screen's) - not the user, not a name
    a speaker already has."""
    from people_from_screen import first_key, is_me
    used = {first_key(sp.label) for sp in smap.speakers() if sp.name}
    out = []
    for name in (getattr(view, "data", None) or {}).get("people") or []:
        name = speaker_map.clean_name(name)
        if name and not is_me(name, me) and first_key(name) not in used and name not in out:
            out.append(name)
    return out


def add_speakers(view, layout):
    """The Speakers part of the People card (see the module notes), added to ``layout``. Returns what the attendees
    below need to know: {"any": speakers shown, "you": the user is one of them, "keys": the first names a speaker
    already has} - {} when the transcript tells nobody apart (no labels: the card is as it always was)."""
    data = getattr(view, "data", None) or {}
    smap = speaker_map_of(data)
    speakers = smap.speakers()
    view.speaker_rows = []
    if not speakers:
        return {}
    from people_from_screen import first_key
    me = view.page.me()
    first = first_name(me)
    picks = _picks(view, smap, me)
    busy = bool(view.page.is_active_folder(view.folder))
    keys = set()
    for sp in speakers:
        row = SpeakerRow(sp, me=me, picks=picks, parent=view.people_card)
        row.renamed.connect(lambda sid, name, v=view: rename(v, sid, name))
        row.pencil.setEnabled(not busy)                  # (the meeting's transcript is being written right now)
        layout.addWidget(row)
        view.speaker_rows.append(row)
        if sp.name:
            keys.add(first_key(sp.name))
        elif sp.sid == speaker_map.YOU and first:
            keys.add(first_key(first))
    view.speaker_offer = None
    meta = data.get("meta") or {}
    if meta.get(speaker_map.CHANGED_KEY) and (data.get("notes") or "").strip():
        offer = Button(OFFER_TEXT, "link", "s", icon="refresh", parent=view.people_card)
        offer.setAccessibleName(OFFER_TEXT)
        s = view.page.session()
        offer.setEnabled(not busy and not bool(s is not None and getattr(s, "is_busy", False)))
        offer.clicked.connect(lambda _=False, v=view: update_notes(v))
        layout.addWidget(offer, 0, Qt.AlignLeft)
        view.speaker_offer = offer
    return {"any": True, "you": any(sp.sid == speaker_map.YOU for sp in speakers), "keys": keys}


def rename(view, sid, name):
    """A speaker's new name from its row: saved off the window's thread (MeetingSession.rename_speaker, else
    speaker_map.rename), then the meeting is drawn again from what was written - the Transcript tab, People, the
    offer to update the notes. A name that can't be used is said in one line."""
    folder = view.folder
    if not folder:
        return
    s = view.page.session()
    fn = getattr(s, "rename_speaker", None) if s is not None else None

    def work():
        return fn(folder, sid, name) if callable(fn) else speaker_map.rename(folder, sid, name)

    def done(res, err):
        if not view._is_folder(folder):
            return                                       # another meeting is open now
        if err is not None or res is None:
            logger.warning("Renaming a speaker failed: %s", err)
            view.flash(FAILED, "red")
            view.render_keeping_view()
            return
        if not res.ok:
            view.flash(res.message or FAILED, "amber", ms=6000)
            view.render_keeping_view()
            return
        data = view.data if isinstance(view.data, dict) else {}
        data["transcript"] = res.text
        data["meta"] = res.meta
        data["speaker_map"] = speaker_map.from_text(res.text, res.meta)
        view.render_keeping_view()
    view._bg.run(work, done)


def update_notes(view):
    """"Update notes with these names": Rewrite notes on the renamed transcript (the notes on the page stay until the
    new ones are written)."""
    try:
        view.rewrite_notes()
    except Exception:
        logger.warning("Updating the notes with the speakers' names failed", exc_info=True)
