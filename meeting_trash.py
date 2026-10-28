"""The meetings' own trash: what makes "Delete" undoable for a few seconds, and safe.

Deleting a meeting from a list MOVES its folder to ``meetings/.trash/<token>/<folder name>`` (one
rename on the same drive, so it is instant whatever the recording's size) and the list says
"Deleted 3 meetings - Undo". The real deletion (``purge``) happens when that bar closes, when the
app quits, and on the next start for anything older than a minute (``purge_older_than``).
"Undo" (``restore``) is the same rename the other way: the folder comes back exactly as it was -
notes, transcript, recording, speaker data, meta.json - and the History lists (which are read from
those folders) show it again. "Delete everything" in Settings purges the trash too
(meeting_store.delete_all_meetings). The Notion page of a sent meeting is never touched.

Safety rules (every function that touches a folder goes through ``check_deletable``):
  * only a folder DIRECTLY inside the meetings library, never the library or the trash itself;
  * never a symlink or a Windows junction / mount point (they are not followed: a link that points
    out of the library - or into another meeting - is refused, not deleted through);
  * nothing is ever resolved to somewhere else first: the path the list gave is the path judged;
  * the trash folder itself is held to the same rule: if ``.trash`` is a link or junction (somebody
    pointed it elsewhere) nothing is moved into it, restored from it or emptied through it.
Why a failure is plain: ``TrashError.code`` is one of ``busy`` (a program has a file in it open -
Windows will not rename a folder then), ``denied``, ``missing`` (already gone), ``outside`` /
``link`` (refused by the rules above), ``exists`` (undo: the name is taken), ``failed``; ``plain()``
turns one into a sentence for the person.

Everything here blocks on the disk: call it on a worker thread (the lists do).
"""
import logging
import os
import re
import secrets
import shutil
import stat
import time
from collections import namedtuple
from pathlib import Path

logger = logging.getLogger("transcribe")

TRASH_NAME = ".trash"
UNDO_SECONDS = 10                 # how long the "Undo" bar stays
STALE_SECONDS = 60                # on start: anything older than this in the trash is purged
_TOKEN = re.compile(r"^\d+-[0-9a-f]{6}$")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

Trashed = namedtuple("Trashed", "token name original path at")


class TrashError(Exception):
    def __init__(self, code, detail=""):
        super().__init__("%s%s" % (code, ": " + detail if detail else ""))
        self.code = code
        self.detail = detail


def _library():
    import meeting_store
    return Path(meeting_store.meetings_dir())


def trash_root():
    return _library() / TRASH_NAME


def is_link(path):
    """A symlink - or, on Windows, a junction / mount point (a reparse point). Never followed."""
    try:
        st = os.lstat(str(path))
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def _same(a, b):
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def check_deletable(folder):
    """The folder as a Path when it is a real folder directly inside the meetings library; else
    TrashError (see the module notes). Nothing is changed."""
    if not folder:
        raise TrashError("missing")
    try:
        p = Path(os.path.abspath(str(folder)))
        name = p.name
        if not name or name.startswith("."):
            raise TrashError("outside")            # the library, the trash, a hidden folder
        if not _same(p.parent, _library()):
            raise TrashError("outside")
        if is_link(p):
            raise TrashError("link")
        if not os.path.lexists(str(p)):
            raise TrashError("missing")
        if not p.is_dir():
            raise TrashError("outside")
    except TrashError:
        raise
    except OSError as e:
        raise TrashError("failed", str(e))
    return p


def _classify(e):
    """An OSError from a rename / delete -> a TrashError code."""
    win = getattr(e, "winerror", None)
    if isinstance(e, FileExistsError) or win == 183:     # the name is taken (an undo that lost a race)
        return TrashError("exists", str(e))
    if win in (32, 33):                           # sharing / lock violation: a program has it open
        return TrashError("busy", str(e))
    if isinstance(e, PermissionError) or win == 5 or getattr(e, "errno", None) in (1, 13):
        return TrashError("denied", str(e))
    if isinstance(e, FileNotFoundError):
        return TrashError("missing", str(e))
    return TrashError("failed", str(e))


def plain(error, name="this meeting"):
    """One calm sentence for a TrashError (or its code)."""
    code = getattr(error, "code", error)
    name = (str(name or "this meeting")).strip() or "this meeting"
    if name != "this meeting":
        name = "“%s”" % name
    if code == "busy":
        return "%s is open in another program. Close it and try again." % name
    if code == "denied":
        return "Gusan isn't allowed to remove %s. It may be read-only or open in another program." % name
    if code in ("outside", "link"):
        return "%s isn't in Gusan's meetings folder, so it was left alone." % name
    if code == "exists":
        return "%s can't come back: a meeting with the same name is already there." % name
    if code == "missing":
        return "%s is already gone." % name
    return "Couldn't remove %s." % name


def _made(token, name, box):
    try:
        at = int(token.split("-", 1)[0]) / 1000.0
    except (ValueError, IndexError):
        at = 0.0
    return Trashed(token, name, str(_library() / name), str(Path(box) / name), at)


def move_to_trash(folder, now=None):
    """Move one meeting folder into the trash. Returns the Trashed record (``restore`` / ``purge``
    take it); raises TrashError."""
    p = check_deletable(folder)
    if is_link(trash_root()):                     # somebody pointed .trash elsewhere: nothing goes through it
        raise TrashError("link")
    token = "%d-%s" % (int((now if now is not None else time.time()) * 1000), secrets.token_hex(3))
    box = trash_root() / token
    try:
        box.mkdir(parents=True)
    except OSError as e:
        raise _classify(e)
    dest = box / p.name
    try:
        os.rename(str(p), str(dest))              # one rename: instant, whatever the recording's size
    except OSError as e:
        try:
            box.rmdir()
        except OSError:
            pass
        raise _classify(e)
    return _made(token, p.name, box)


def _box(trashed_or_token):
    token = getattr(trashed_or_token, "token", trashed_or_token)
    token = str(token or "")
    if not _TOKEN.match(token):
        raise TrashError("outside")
    if is_link(trash_root()):                     # a link in place of the trash: never restore from it or delete in it
        raise TrashError("link")
    return trash_root() / token


def restore(trashed):
    """Put a trashed meeting back where it was. Returns its folder (a Path); raises TrashError."""
    box = _box(trashed)
    name = str(getattr(trashed, "name", "") or "")
    if not name or name != os.path.basename(name) or name.startswith("."):
        raise TrashError("outside")
    src = box / name
    if is_link(src) or not src.is_dir():
        raise TrashError("missing")
    dest = _library() / name
    if os.path.lexists(str(dest)):
        raise TrashError("exists")
    try:
        os.rename(str(src), str(dest))
    except OSError as e:
        raise _classify(e)
    for empty in (box, trash_root()):             # the token's folder, then the trash itself when nothing is left
        try:
            empty.rmdir()
        except OSError:
            pass
    return dest


def _writable(func, path, _exc):
    """rmtree's error handler: a read-only file (Windows) is made writable and removed."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def _rmtree(path):
    """shutil.rmtree that also removes read-only files; it never follows a link or junction."""
    try:
        shutil.rmtree(str(path), onexc=_writable)             # Python 3.12+
    except TypeError:
        shutil.rmtree(str(path), onerror=_writable)           # 3.11


def purge(trashed_or_token):
    """Delete one trashed meeting for good. True when it is gone."""
    try:
        box = _box(trashed_or_token)
    except TrashError:
        return False
    if is_link(box):
        try:
            os.unlink(str(box))
        except OSError:
            return False
        return True
    try:
        if box.exists():
            _rmtree(box)
    except OSError:
        logger.debug("could not purge %s", box, exc_info=True)
    gone = not box.exists()
    if gone:
        try:
            trash_root().rmdir()                  # the trash folder itself goes when nothing is left in it
        except OSError:
            pass
    return gone


def _tokens():
    root = trash_root()
    if is_link(root):                             # (see _box) what a link points to is not ours to empty
        return []
    try:
        return sorted(p.name for p in root.iterdir() if p.name)
    except OSError:
        return []


def list_trash():
    """Everything in the trash as Trashed records (oldest first)."""
    out = []
    root = trash_root()
    for token in _tokens():
        box = root / token
        try:
            kids = [k for k in box.iterdir() if k.is_dir() and not is_link(k)]
        except OSError:
            continue
        if kids and _TOKEN.match(token):
            out.append(_made(token, kids[0].name, box))
    return out


def _purge_names(names, budget_s=None):
    """Purge the named entries of the trash folder; stops when ``budget_s`` has passed. Returns how
    many are gone."""
    root = trash_root()
    if is_link(root):
        return 0
    t0 = time.monotonic()
    gone = 0
    for name in names:
        if budget_s is not None and time.monotonic() - t0 > budget_s:
            break
        box = root / name
        if is_link(box):
            try:
                os.unlink(str(box))
                gone += 1
            except OSError:
                pass
            continue
        try:
            if box.is_dir():
                _rmtree(box)
            else:
                box.unlink()
        except OSError:
            logger.debug("could not purge %s", box, exc_info=True)
        if not box.exists():
            gone += 1
    try:
        root.rmdir()                              # an empty trash folder goes too
    except OSError:
        pass
    return gone


def purge_all(budget_s=None):
    """Empty the trash (Settings > Delete everything; the app quitting, with a time budget so a
    quit never waits for a huge recording - the next start finishes). Returns how many were purged."""
    return _purge_names(_tokens(), budget_s)


def purge_older_than(max_age_s=STALE_SECONDS, now=None):
    """On start: purge what a closed bar or a crash left behind (older than ``max_age_s``).
    An entry that isn't ours (no timestamp in its name) is judged by its folder's age."""
    now = now if now is not None else time.time()
    old = []
    root = trash_root()
    for name in _tokens():
        try:
            at = int(name.split("-", 1)[0]) / 1000.0 if _TOKEN.match(name) else (root / name).stat().st_mtime
        except (ValueError, OSError):
            at = 0.0
        if now - at >= max_age_s:
            old.append(name)
    return _purge_names(old)
