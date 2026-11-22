"""Who is who in a saved meeting (Gusan 2.1.11): one stable id per voice, and the name it goes by.

The founder (2026-11-21): "proper diarization with speaker names when identified, or Speaker 1, 2, 3 if not ... I want
to see that in the transcript and in People, and I want to edit a speaker's name."

A meeting's transcript.txt says who spoke at the start of each line ("You: ...", "Speaker 2: ...", "Anna: ..."). Behind
every such label is an id:

  * "you"  - the microphone, the user ("You");
  * "them" - the call, its people not told apart (yet) ("Them");
  * "s1", "s2", ... - Speaker 1, 2, ...: the voices a speaker pass told apart, numbered in the order they first spoke
    (the call's voices, or a microphone-only meeting's); "s?" - "Speaker ?" (speech no voice could be put on).

meta.json keeps the names: ``speaker_names`` {id: name} and ``speaker_name_by`` {id: where the name came from}:

  * "user"     - typed, or picked from the attendees, on the People card. No pass ever overwrites it.
  * "screen"   - the one other person on the call, read off the call's screen (meeting_speakers' one-name rule: exactly
                 one other person in the meeting and exactly one voice found). Shown as automatic, editable.
  * "calendar" - kept for a name an invite gives; no step sets it yet (names are never invented).
  * "auto"     - a name an older transcript already carried, found when it was read (see "Reading").

An id without a name shows its canonical label. transcript.txt is written FROM the ids and the names: every speaker step
makes its blocks with the canonical labels (meeting_speakers: ``Outcome.canonical``) and they are relabelled with the
meeting's names before they are written (``settle``); a rename relabels the lines of one id and writes transcript.txt
again in one atomic step, then the names (``rename``). Both run under LOCK, and so does the end of the background pass
(meeting_speakers._finish_pass), which finds its block by the ids - the stored canonical block rendered with the names
as they are now - never by text the person may have renamed meanwhile.

Reading (``from_text``): a line's label is looked up among the labels this meeting knows - its names first, then the
canonical ones - so ANY name is a label, whatever its case or script; only then, in a transcript that has labels at
all, by rich_text's pattern: an older transcript's names that were never stored ("Anna Petrosyan" from the one-name
rule before 2.1.11) become "auto" names of new ids, numbered after the ones in use. That is the lazy migration of old
meetings: done on every read, and stored with the first name somebody sets. Qt-free; the readers never raise.
"""
import logging
import re
import threading

import meeting_store
import rich_text

logger = logging.getLogger("transcribe")

YOU, THEM, UNKNOWN = "you", "them", "s?"
BY_USER, BY_SCREEN, BY_CALENDAR, BY_AUTO = "user", "screen", "calendar", "auto"
BYS = (BY_USER, BY_SCREEN, BY_CALENDAR, BY_AUTO)
NAMES_KEY, BY_KEY = "speaker_names", "speaker_name_by"
# A person set a name since the notes were written: the page offers "Update notes with these names" (the notes writer
# clears it when it has written them again).
CHANGED_KEY = "speaker_names_changed"
MAX_NAME = 40
LOCK = threading.RLock()        # transcript.txt + the names: a rename, settle and the end of a speaker pass

_SID_RE = re.compile(r"^s([1-9]\d{0,2})$")
_CANON_RE = re.compile(r"^Speaker ([1-9]\d{0,2})$")
_CANON_LINE_RE = re.compile(r"^(You|Them|Speaker\s+(?:[1-9]\d{0,2}|\?)):(?:\s|$)")

# Why a rename was refused (Rename.code) and what the page says.
R_NO_TRANSCRIPT, R_UNKNOWN, R_TAKEN, R_RESERVED, R_WRITE, R_BUSY = (
    "no_transcript", "unknown", "taken", "reserved", "write", "busy")
_SAY = {
    R_NO_TRANSCRIPT: "This meeting has no saved transcript to rename speakers in.",
    R_UNKNOWN: "That speaker isn't in the transcript any more.",
    R_WRITE: "Couldn't save the new name. Try again.",
    R_BUSY: "Wait until the notes are written, then rename.",
}


def canonical(sid):
    """The label an id has without a name: "You", "Them", "Speaker 2", "Speaker ?" ("" for no id)."""
    sid = str(sid or "")
    if sid == YOU:
        return "You"
    if sid == THEM:
        return "Them"
    if sid == UNKNOWN:
        return "Speaker ?"
    m = _SID_RE.match(sid)
    return "Speaker %d" % int(m.group(1)) if m else ""


def id_of_canonical(label):
    """The id a canonical label stands for ("Speaker 2" -> "s2"); "" for a name."""
    s = " ".join(str(label or "").split())
    if s in ("You", "Them", "Speaker ?"):
        return {"You": YOU, "Them": THEM, "Speaker ?": UNKNOWN}[s]
    m = _CANON_RE.match(s)
    return "s%d" % int(m.group(1)) if m else ""


def is_sid(value):
    return isinstance(value, str) and (value in (YOU, THEM, UNKNOWN) or bool(_SID_RE.match(value)))


def number(sid):
    """2 for "s2"; 0 for "you", "them", "s?"."""
    m = _SID_RE.match(str(sid or ""))
    return int(m.group(1)) if m else 0


def clean_name(text):
    """A name as a label can carry it: one line, no colon (it ends the label), no marker dashes around it, at most
    MAX_NAME characters. "" when nothing is left."""
    s = "".join(" " if (ch.isspace() or not ch.isprintable()) else ch for ch in str(text or ""))
    s = " ".join(s.replace(":", " ").split()).strip("—–-")
    return " ".join(s[:MAX_NAME].split())


def stored(meta):
    """{id: (name, by)} as meta.json keeps them - only clean names of real ids (a name without its source counts as the
    person's own: never overwritten)."""
    meta = meta if isinstance(meta, dict) else {}
    names, bys = meta.get(NAMES_KEY), meta.get(BY_KEY)
    bys = bys if isinstance(bys, dict) else {}
    out = {}
    if isinstance(names, dict):
        for sid, name in names.items():
            name = clean_name(name) if isinstance(name, str) else ""
            if is_sid(sid) and name:
                by = bys.get(sid)
                out[sid] = (name, by if by in BYS else BY_USER)
    return out


def current_labels(meta):
    """{id: the label transcript.txt carries for it} for the ids that have a stored name."""
    return {sid: name for sid, (name, _by) in stored(meta).items()}


label_of = rich_text.known_label      # ("Anna Lee" of "Anna Lee: Hi": the longest known label a line starts with)


class Speaker:
    """One voice of a meeting as the People card shows it."""

    __slots__ = ("sid", "label", "name", "by", "words", "share")

    def __init__(self, sid, label, name, by, words, share):
        self.sid, self.label, self.name, self.by, self.words, self.share = sid, label, name, by, words, share

    @property
    def canonical(self):
        return canonical(self.sid)

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


class SpeakerMap:
    """The ids of one transcript and their labels. ``names``: {id: (name, by)} (``stored``); ``labelled``: the meeting
    is known to carry labels (a pass reached its end, or names are stored) - then a line that starts with a name-shaped
    label nobody stored is an older transcript's name (``derived``) even without a "You:" line next to it.

    ``parse(text)`` reads a transcript: ``order`` (ids in the order they first speak), ``words`` per id, ``derived``
    {id: name} and ``raw_labels`` (every label as it is written, for the views). ``split(line)`` and ``relabel(text,
    {id: label})`` then work line by line with exactly what parse knew."""

    def __init__(self, names=None, labelled=False, canonical_only=False):
        self.names = dict(names or {})
        self.labelled = bool(labelled or self.names)
        self.canonical_only = bool(canonical_only)
        self.order, self.words, self.derived, self.raw_labels = [], {}, {}, []
        self._known = {}

    # ── labels ──
    def label(self, sid):
        got = self.names.get(sid)
        if got:
            return got[0]
        return self.derived.get(sid) or canonical(sid)

    def name(self, sid):
        got = self.names.get(sid)
        return got[0] if got else self.derived.get(sid, "")

    def by(self, sid):
        got = self.names.get(sid)
        if got:
            return got[1]
        return BY_AUTO if sid in self.derived else ""

    def _rebuild(self):
        known = {}
        if not self.canonical_only:
            for sid, (name, _by) in self.names.items():
                known.setdefault(name, sid)
            for sid, name in self.derived.items():
                known.setdefault(name, sid)
        self._known = known

    def split(self, line):
        """(the label as written, its id, the words) of one line; None when the line has no label this map knows."""
        s = (line or "").strip()
        if not s:
            return None
        lab = label_of(s, self._known) if self._known else ""
        if lab:
            return lab, self._known[lab], s[len(lab) + 1:].strip()
        m = _CANON_LINE_RE.match(s)
        if m:
            raw = m.group(1)
            return raw, id_of_canonical(raw), s[len(raw) + 1:].strip()
        return None

    def _legacy(self, line, next_number):
        """A name-shaped label (rich_text's pattern) an older transcript carries: (label, a new id, words) - the id
        numbered ``next_number[0]`` (then one more) - or None."""
        m = rich_text.match_label((line or "").strip())
        if not m:
            return None
        raw = m.group(1)
        if id_of_canonical(raw) or clean_name(raw) != raw:
            return None
        sid = "s%d" % next_number[0]
        next_number[0] += 1
        self.derived[sid] = raw
        self._rebuild()
        return raw, sid, m.group(2).strip()

    def parse(self, text):
        """Read ``text`` (see the class). Returns self."""
        self.order, self.words, self.derived, self.raw_labels = [], {}, {}, []
        self._rebuild()
        lines = (text or "").splitlines()
        rows = [self.split(line) for line in lines]
        labelled = self.labelled or any(rows)
        if labelled and not self.canonical_only and not all(rows[i] or rich_text.is_marker(lines[i])
                                                            or not lines[i].strip() for i in range(len(lines))):
            used = [number(sid) for sid in self.names] + [number(r[1]) for r in rows if r]
            nxt = [max(used + [0]) + 1]
            for i, line in enumerate(lines):
                if rows[i] is None and line.strip() and not rich_text.is_marker(line):
                    rows[i] = self.split(line) or self._legacy(line, nxt)
        for row in rows:
            if not row:
                continue
            raw, sid, body = row
            if sid not in self.words:
                self.order.append(sid)
                self.words[sid] = 0
            self.words[sid] += len(body.split())
            if raw not in self.raw_labels:
                self.raw_labels.append(raw)
        return self

    def relabel(self, text, new):
        """``text`` with the lines of every id in ``new`` ({id: label}) under that label (any label the id had: its
        name, its canonical one). Nothing else changes: the words, blank lines, markers, line ends."""
        if not new:
            return text or ""
        out = []
        for line in (text or "").split("\n"):
            got = self.split(line)
            if got and got[1] in new and new[got[1]]:
                body = line.lstrip()
                lead = line[:len(line) - len(body)]
                line = lead + new[got[1]] + body[len(got[0]):]
            out.append(line)
        return "\n".join(out)

    # ── what the page shows ──
    def speakers(self):
        """[Speaker] - "you" first, then everybody in the order they first spoke - with each one's share of the words."""
        total = float(sum(self.words.values())) or 1.0
        ids = ([YOU] if YOU in self.words else []) + [sid for sid in self.order if sid != YOU]
        return [Speaker(sid, self.label(sid), self.name(sid), self.by(sid), self.words.get(sid, 0),
                        self.words.get(sid, 0) / total) for sid in ids]

    def shown(self, me=""):
        """{label as written: label as the page shows it}: "You" is the user's first name with "(You)" while nobody
        renamed it; everything else as it is."""
        out = {lab: lab for lab in self.raw_labels}
        first = (str(me or "").split() or [""])[0]
        if first and YOU in self.words and not self.name(YOU):
            for lab in self.raw_labels:
                if lab == canonical(YOU):
                    out[lab] = "%s (You)" % first
        return out


def from_text(text, meta=None):
    """The SpeakerMap of a transcript and its meeting's meta (parsed). Never raises: a map without speakers instead."""
    meta = meta if isinstance(meta, dict) else {}
    try:
        labelled = bool(meta.get("speakers_at")) or bool(meta.get(NAMES_KEY))
        return SpeakerMap(stored(meta), labelled=labelled).parse(text)
    except Exception:
        logger.warning("The speakers of a transcript could not be read", exc_info=True)
        return SpeakerMap()


def load(folder):
    """The SpeakerMap of a saved meeting (transcript.txt, else its live pieces, and meta.json)."""
    return from_text(meeting_store.load_transcript(folder), meeting_store.load_meta(folder))


def ids_in(texts):
    """The ids the canonical labels of ``texts`` stand for, in the order they first speak."""
    m = SpeakerMap(canonical_only=True)
    out = []
    for text in texts or ():
        for sid in m.parse(text).order:
            if sid not in out:
                out.append(sid)
    return out


def relabel_canonical(text, labels):
    """A block a speaker step made (canonical labels only) with ``labels`` ({id: label}) put in."""
    return SpeakerMap(canonical_only=True).parse(text).relabel(text, labels)


def decide(meta, sids, auto=None, one_voice=False):
    """The names a meeting has once a speaker step produced the ids ``sids`` - {id: (name, by)} - from the names it
    already has (meta) and the step's own ``auto`` names ({id: name}: the one-name rule's, "screen").

      * a name the person set ("user") is kept, whatever the step found;
      * an id the step names gets that name; an automatic name of an id the step produced and did not name is dropped
        (the step is newer than whatever named it before: two voices found are never named on a guess);
      * ``one_voice`` (the step told exactly one person apart on the call): a name the person gave "Them" moves to
        Speaker 1 - that one voice IS "Them";
      * two speakers never share a name: an automatic name somebody else already has is not used."""
    have = stored(meta)
    out = dict(have)
    auto = {sid: clean_name(name) for sid, name in (auto or {}).items() if is_sid(sid) and clean_name(name)}
    sids = list(sids or ())
    for sid in sids:
        old = have.get(sid)
        if old and old[1] == BY_USER:
            continue
        if auto.get(sid):
            out[sid] = (auto[sid], BY_SCREEN)
        elif old:
            out.pop(sid, None)
    them = have.get(THEM)
    if one_voice and them and them[1] == BY_USER and "s1" in sids and THEM not in sids:
        cur = out.get("s1")
        if not cur or cur[1] != BY_USER:
            out["s1"] = them
            out.pop(THEM, None)
    seen = set()
    for sid in sorted(out, key=lambda s: (out[s][1] != BY_USER, number(s), s)):
        key = out[sid][0].casefold()
        if key in seen or id_of_canonical(out[sid][0]) not in ("", sid):
            if out[sid][1] != BY_USER:
                out.pop(sid)
            continue
        seen.add(key)
    return out


def _names_fields(names):
    """meta fields for {id: (name, by)} (None removes a key that would be empty)."""
    return {NAMES_KEY: {sid: n for sid, (n, _b) in names.items()} or None,
            BY_KEY: {sid: b for sid, (_n, b) in names.items()} or None}


def settle(folder, canon_texts, auto=None, one_voice=False):
    """Blocks a speaker step just made (``canon_texts``: canonical labels only), ready to be written: relabelled with the
    names the meeting has (``decide``), which are written to meta.json first (a meeting continued, renamed, or named
    by the one-name rule keeps its names). ``folder`` None: only ``auto`` applies. Never raises: the blocks as they
    came, when something fails."""
    texts = [str(t or "") for t in (canon_texts or ())]
    try:
        with LOCK:
            meta = meeting_store.load_meta(folder) if folder else {}
            names = decide(meta, ids_in(texts), auto, one_voice)
            if folder and names != stored(meta):
                if meeting_store.edit_meta(folder, lambda _m: _names_fields(names)) is None:
                    logger.warning("The speakers' names of %s could not be kept", folder)
            labels = {sid: n for sid, (n, _b) in names.items()}
            return [relabel_canonical(t, labels) for t in texts]
    except Exception:
        logger.warning("The speakers' names could not be put in", exc_info=True)
        return texts


class Rename:
    """What a rename came to: ``ok``; ``code`` and ``message`` (a plain sentence) when refused; ``text`` the transcript
    and ``meta`` the meeting's meta as they are now; ``label`` the speaker's label now."""

    def __init__(self, ok, code="", message="", text="", meta=None, label=""):
        self.ok, self.code, self.message = bool(ok), code, message
        self.text, self.meta, self.label = text, (meta if isinstance(meta, dict) else {}), label

    @classmethod
    def refused(cls, code, message=""):
        return cls(False, code, message or _SAY.get(code, "Couldn't rename this speaker."))


def rename(folder, sid, name, by=BY_USER):
    """Call ``sid`` of the meeting in ``folder`` ``name`` ("" = back to its own label: "Speaker 2", "You", "Them").
    transcript.txt is written again with every line of that speaker under the new label (one atomic write), then the
    name is kept in meta.json - with the names an older transcript carried (now stored), and ``CHANGED_KEY`` for the
    page's "Update notes with these names". Refused for a name another speaker already has (any case) or that is
    another speaker's own label ("Speaker 3", "You"). Returns a Rename; never raises."""
    try:
        with LOCK:
            if not meeting_store.has_transcript_file(folder):
                return Rename.refused(R_NO_TRANSCRIPT)
            text = meeting_store.load_transcript(folder)
            meta = meeting_store.load_meta(folder)
            m = from_text(text, meta)
            if sid not in m.words:
                return Rename.refused(R_UNKNOWN)
            name = clean_name(name)
            if name and id_of_canonical(name) == sid:
                name = ""                                   # its own label: no name
            if name:
                other = id_of_canonical(name)
                if other:
                    return Rename.refused(R_RESERVED, "“%s” is another speaker's label. Pick another name." % name)
                key = name.casefold()
                taken = [o for o in m.words if o != sid and m.label(o).casefold() == key]
                if taken:
                    return Rename.refused(R_TAKEN, "Another speaker is already called “%s”." % m.label(taken[0]))
            label = name or canonical(sid)
            new_text = m.relabel(text, {sid: label})
            if new_text != text and not meeting_store.write_transcript(folder, new_text):
                return Rename.refused(R_WRITE)
            derived = dict(m.derived)

            def change(cur):
                names = stored(cur)
                for d_sid, d_name in derived.items():          # an older transcript's names: stored from now on
                    if d_sid != sid and d_sid not in names:
                        names[d_sid] = (d_name, BY_AUTO)
                if name:
                    for o in [o for o, (n, _b) in names.items() if o != sid and n.casefold() == name.casefold()]:
                        names.pop(o)                            # (a speaker no longer in the transcript)
                    names[sid] = (name, by)
                else:
                    names.pop(sid, None)
                fields = _names_fields(names)
                if new_text != text and by == BY_USER:
                    fields[CHANGED_KEY] = True
                return fields
            merged = meeting_store.edit_meta(folder, change)
            if merged is None:
                logger.warning("A speaker's new name could not be kept in meta.json of %s", folder)
                merged = meta
            logger.info("Speaker renamed in %s: %s (%s)", getattr(folder, "name", folder), sid,
                        "named" if name else "back to its own label")
            return Rename(True, text=new_text, meta=merged, label=label)
    except Exception:
        logger.warning("Renaming a speaker failed", exc_info=True)
        return Rename.refused(R_WRITE)


__all__ = ["BY_AUTO", "BY_CALENDAR", "BY_SCREEN", "BY_USER", "CHANGED_KEY", "LOCK", "Rename", "Speaker", "SpeakerMap",
           "THEM", "UNKNOWN", "YOU", "canonical", "clean_name", "current_labels", "decide", "from_text", "id_of_canonical",
           "ids_in", "is_sid", "label_of", "load", "relabel_canonical", "rename", "settle", "stored"]
