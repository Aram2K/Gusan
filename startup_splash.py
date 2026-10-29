"""The loading screen for the seconds before Gusan's window answers.

The app needs several seconds to start (imports, the controller, then the first
build of the window - the log shows the window thread stuck for 6-20 s on a slow
laptop) and nothing on screen said it was opening. A small animated card
(splash_window.py) covers that time.

Rules this file keeps - read them before changing anything:

* A separate process. A card animated by the app's own window thread freezes
  exactly when it is needed (the thread is stuck for seconds at a time, and
  Windows ghosts a window that does not answer as "Not responding"), and an
  in-process card could show nothing during the import seconds. The card runs as
  "TranscribeApp.exe --splash-worker" (the same program; main.py dispatches the
  flag before anything heavy is imported). It is created on a thread
  (CreateProcess takes ~50 ms), so the app does not wait for it. It has no
  priority class of its own on purpose: a below-normal card starves when the CPU
  is busy, which is exactly when it is needed.
* This module is Qt-free and imports only the standard library, so main.py can
  load it first thing (and tests can run it without Qt). The Qt part is
  splash_window.py, imported only by the card process.
* Never in the way: no taskbar button, no focus, mouse clicks fall through it, and
  after splash_window.TOPMOST_S it no longer sits above the app's own dialogs.
* Quiet launches get none. The start-at-login launch (--background) stays
  silent, tests and source runs get none.
* One Gusan, from the first line of main.py. Every launch (the quiet one too)
  claims the single-instance port (claim_or_hand_over) before anything is
  imported, and main() later uses that very socket as its lock. A launch that
  finds the port taken (a second double-click, a click on the shortcut while the
  app sits in the tray, a click while the first copy is still importing) hands
  its request over and exits at once: no card, no imports. The request waits in
  the claimed socket's backlog until the first copy starts listening, and that
  copy opens its own window. A copy that is quitting lets go of the port when
  its quit begins (main.AppController.quit_app), so a relaunch right after
  "Quit" starts a new app instead of handing over to a copy that is ending.
* Never outlives the app. The card ends by itself when (1) the app says the
  window is up (one byte on its input pipe), (2) the pipe closes, (3) the app's
  process is gone - checked on every animation tick, so a crash, "End task" or a
  window closed by Windows removes the card within a frame - or (4) after
  HARD_TIMEOUT_S, the last guard for an app that stays alive and never says it
  is ready. The app is not asked for a heartbeat: its window thread stalls for
  many seconds while starting, and that is the very time the card is for.
* A failure to start the card means no card: nothing here may raise into the app.
"""
import logging
import os
import socket
import sys
import threading
import time

WORKER_FLAG = "--splash-worker"

# main.SINGLE_INSTANCE_PORT, main.IPC_ACTIONS and the reply bytes, as main.py has
# them (a test keeps them equal; they never change: a 1.9.x copy may be on the
# other end).
SINGLE_INSTANCE_PORT = 47823
IPC_ACTIONS = ("show_settings", "show_onboarding", "show_meeting", "live_prompter")
HANDSHAKE = b"transcribe-ok"

CLOSE = b"c"                  # the app -> the card, over its input pipe: "the window is up"
HARD_TIMEOUT_S = 60.0         # the longest the card ever stays up
SHOW_DELAY_S = 0.25           # the card does not show before the launch is this old (its own start, 1.4-2.4 s,
                              # always passes it; a card that is closed at once is kept from flashing by
                              # splash_window.MIN_SHOWN_S)
FADE_S = 0.16                 # the card's way out
PLATFORMS = ("win32",)        # macOS: a second copy of the bundle would add a second Dock icon

# CreateProcess flags: no console window. No priority flag, on purpose: a below-normal
# card starves when the CPU is busy, which is when it is needed (the card then takes the
# app's own class, so a deliberately low-priority launch is respected).
_CREATE_NO_WINDOW = 0x08000000

RUN, FADE, QUIT = "run", "fade", "quit"


# ── Should there be a card? ───────────────────────────────────────────────────
def enabled(env=None, frozen=None, platform=None):
    """A card is possible at all: a packaged build on a supported system, or a
    source run that asks for it (GUSAN_SPLASH=1). GUSAN_SPLASH=0 turns it off."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if platform not in PLATFORMS or "PYTEST_CURRENT_TEST" in env:
        return False
    flag = str(env.get("GUSAN_SPLASH", "")).strip()
    if flag == "0":
        return False
    return frozen or flag == "1"


def launch_wants_splash(argv, **kw):
    """A launch that opens the app (not the quiet start-at-login one, not a
    helper process) in a build that has the card."""
    if "--background" in argv or argv[1:2] == [WORKER_FLAG] or argv[1:2] == ["--diarize-worker"]:
        return False
    return enabled(**kw)


# ── A second launch ───────────────────────────────────────────────────────────
def second_launch_action(argv):
    """What a launch that finds Gusan running asks it to do: main.launch_action,
    word for word (a test compares them)."""
    for arg in argv[1:]:
        if arg in IPC_ACTIONS:
            return arg
    if "--background" in argv:
        return None
    return "show_settings"


def deliver(action, port=SINGLE_INSTANCE_PORT, timeout=0.6):
    """Hand ``action`` to the Gusan that owns the port, as signal_running_instance
    does. True once the request is sent. The reply is not required: a copy that has
    claimed the port but is still importing cannot answer yet, and its backlog keeps
    the request until it can. False when nobody listens (the connection or the send
    failed): the owner is gone or is not a Gusan."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(action.encode("utf-8"))
            try:
                s.recv(len(HANDSHAKE))
            except OSError:
                pass
            return True
    except OSError:
        return False


_claim = None                 # the single-instance socket this launch holds, until main() takes it over


def claim_or_hand_over(argv, port=None):
    """First thing in main.py, for every launch (--background too): take the
    single-instance port before anything is imported.

    True: this launch owns the port (main() keeps the socket as its lock:
    claimed_socket), or the claim could not be tried (main() then binds the old
    way). False: another Gusan owns it and the request is handed over (a launch
    that asks for nothing, --background, hands over nothing): the caller exits at
    once - no card, no imports. A port whose owner does not listen falls through
    to True: the old path (the imports, then main()'s own bind) arbitrates.

    The owner may still be importing: the request then waits in the listen backlog
    until its IPC thread starts. An owner that is quitting has let go of the port
    already (AppController.quit_app)."""
    global _claim
    try:
        port = SINGLE_INSTANCE_PORT if port is None else port
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            s.listen(5)                               # the backlog holds a request until main() starts the IPC thread
        except OSError:
            s.close()
            action = second_launch_action(argv)
            if action is None:
                return False
            return not deliver(action, port)
        except BaseException:
            s.close()
            raise
        _claim = s
        return True
    except Exception:
        return True


def claimed_socket():
    """The socket claim_or_hand_over() took, or None (main()'s lock)."""
    return _claim


# ── The app's side: start the card, dismiss it ────────────────────────────────
_active = None                # the running Card
_starter = None               # the thread that is starting it
_generation = 0               # counts dismiss() / kill() calls
_lock = threading.Lock()
_problem = None               # why the card could not start, for the log (see _report)


class Card:
    """The running card process, seen from the app."""

    def __init__(self, proc):
        self.proc = proc

    def _close_pipe(self, send):
        pipe = getattr(self.proc, "stdin", None)
        if pipe is None:
            return
        try:
            if send:
                pipe.write(CLOSE)
                pipe.flush()
        except (OSError, ValueError):
            pass
        try:
            pipe.close()
        except (OSError, ValueError):
            pass

    def close(self):
        """The window is up: the card fades out and ends."""
        self._close_pipe(True)

    def kill(self):
        """This launch lost the single-instance race: no card, at once."""
        self._close_pipe(False)
        try:
            self.proc.kill()
        except Exception:
            pass


def _command(executable=None, frozen=None):
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    executable = sys.executable if executable is None else executable
    if frozen:
        return [executable, WORKER_FLAG]
    return [executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"), WORKER_FLAG]


def _spawn(popen=None, now=time.time, getpid=os.getpid, platform=None, generation=None, **kw):
    """Start the card process. Nothing raises: a card that cannot start is no card.
    A dismiss() or kill() that came while the process was being created ends the
    new card at once (``generation`` is the count when this start began)."""
    global _active, _problem
    with _lock:
        if _active is not None:
            return _active
    try:
        import subprocess
        platform = sys.platform if platform is None else platform
        args = dict(stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    close_fds=True)
        if platform == "win32":
            args["creationflags"] = _CREATE_NO_WINDOW
        cmd = _command(**kw) + [str(getpid()), repr(now())]
        card = Card((popen or subprocess.Popen)(cmd, **args))
    except Exception as exc:
        _problem = "could not start: %r" % (exc,)     # logged later (dismiss/kill): logging is not set up yet
        return None
    with _lock:
        late = _active is not None or (generation is not None and generation != _generation)
        if not late:
            _active = card
    if late:
        card.kill()
        return None
    return card


def _begin(**spawn):
    """Start the card on a thread of its own: CreateProcess takes ~50 ms, which
    must not sit on the app's start-up path (or in a tray click)."""
    global _starter
    with _lock:
        if _active is not None or (_starter is not None and _starter.is_alive()):
            return False
        _starter = threading.Thread(target=_spawn, kwargs=dict(spawn, generation=_generation),
                                    name="gusan-splash-start", daemon=True)
        starter = _starter
    starter.start()
    return True


def _settle(timeout=5.0):
    """Wait for a card that is still being started (for tests)."""
    starter = _starter
    if starter is not None and starter is not threading.current_thread():
        starter.join(timeout)


def start(argv, gate=None, **spawn):
    """The launch's own card, started first thing in main.py (before the heavy
    imports). True when a card is being started, False when this launch gets
    none. ``gate`` and ``spawn`` are for tests (the keyword arguments of
    enabled() and _spawn())."""
    try:
        if not launch_wants_splash(argv, **(gate or {})):
            return False
        return _begin(**spawn)
    except Exception:
        return False


def start_for_window(gate=None, **spawn):
    """The window of a running app is about to be built for the first time (a
    click on the tray icon after a quiet start, the end of the first-run
    wizard): the same several seconds, so the same card. A card already up
    (the launch's own) is kept."""
    try:
        if _active is None and enabled(**(gate or {})):
            return _begin(**spawn)
    except Exception:
        pass
    return False


def _report(card):
    """One line in the log when the card did not work (nothing else would ever say:
    its output goes nowhere): it could not be started, or it ended on its own with
    an error. Called before the card is closed, so poll() only sees a card that
    died by itself (one that is still fading reads None). Never raises."""
    global _problem
    try:
        log = logging.getLogger("transcribe.splash")      # the root has its level and the file handler by now
        if _problem:
            problem, _problem = _problem, None
            log.info("Loading card: %s", problem)
        if card is not None:
            try:
                code = card.proc.poll()
            except Exception:
                code = None
            if isinstance(code, int) and code != 0:
                log.info("Loading card ended early with exit code %s", code)
    except Exception:
        pass


def dismiss():
    """A Gusan window is on screen: the card fades out. Cheap and safe to call
    from anywhere, any number of times - it never waits for anything."""
    global _active, _generation
    with _lock:
        card, _active = _active, None
        _generation += 1
    _report(card)
    if card is not None:
        try:
            card.close()
        except Exception:
            pass


def kill():
    """This launch lost the single-instance race and is about to exit: the card
    goes now, without its fade."""
    global _active, _generation
    with _lock:
        card, _active = _active, None
        _generation += 1
    _report(card)
    if card is not None:
        try:
            card.kill()
        except Exception:
            pass


# ── The card process's side ───────────────────────────────────────────────────
class ParentWatch:
    """Is the app process still there? Windows: a SYNCHRONIZE handle opened once,
    polled with a zero wait (no thread, no timer of its own). Elsewhere: the
    parent's pid. Without a pid, or when the system call cannot be made, the app
    counts as alive - the closed pipe and HARD_TIMEOUT_S still end the card."""

    def __init__(self, pid, platform=None):
        self.pid = pid if pid and pid > 0 else None
        self.platform = sys.platform if platform is None else platform
        self._gone = False
        self._handle = None
        self._k32 = None
        if self.pid and self.platform == "win32":
            try:
                import ctypes
                k32 = ctypes.WinDLL("kernel32")
                k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
                k32.OpenProcess.restype = ctypes.c_void_p
                k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                k32.WaitForSingleObject.restype = ctypes.c_uint32
                k32.CloseHandle.argtypes = [ctypes.c_void_p]
                handle = k32.OpenProcess(0x00100000, 0, self.pid)        # SYNCHRONIZE
            except Exception:
                return
            self._k32 = k32
            if handle:
                self._handle = handle
            else:
                self._gone = True              # it cannot be opened: the app is not there

    def alive(self):
        if self._gone:
            return False
        if self.pid is None:
            return True
        if self.platform == "win32":
            if self._handle is None:
                return True
            try:
                if self._k32.WaitForSingleObject(self._handle, 0) != 0:   # not WAIT_OBJECT_0: still running
                    return True
            except Exception:
                return True
            self._gone = True
            try:
                self._k32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None
            return False
        try:
            if os.getppid() != self.pid:       # re-parented: the app ended
                self._gone = True
                return False
            os.kill(self.pid, 0)
        except ProcessLookupError:
            self._gone = True
            return False
        except Exception:
            return True
        return True


class Lifecycle:
    """What the card does next: RUN, FADE (the app is ready, or the last guard
    ran out) or QUIT (the app is gone: end at once). Pure logic - the clock, the
    app watch and the input pipe's reader are all handed in."""

    def __init__(self, watch=None, clock=time.monotonic, hard_timeout=HARD_TIMEOUT_S):
        self.watch = watch
        self.clock = clock
        self.hard_timeout = hard_timeout
        self.started = clock()
        self.close_requested = False       # set by the pipe reader: the app sent CLOSE
        self.pipe_closed = False           # set by the pipe reader: end of input

    def poll(self):
        if self.watch is not None and not self.watch.alive():
            return QUIT
        if self.close_requested or self.pipe_closed:
            return FADE
        if self.clock() - self.started >= self.hard_timeout:
            return FADE
        return RUN


def watch_pipe(life, fd=0, read=os.read):
    """Read the input pipe on a daemon thread: CLOSE means "the window is up",
    end of input means the app is gone. A pipe that cannot be read (the card was
    started by hand) just ends the thread: the other guards still hold."""
    def run():
        try:
            while True:
                data = read(fd, 16)
                if not data:
                    life.pipe_closed = True
                    return
                if CLOSE in data:
                    life.close_requested = True
                    return
        except Exception:
            return
    t = threading.Thread(target=run, name="gusan-splash-pipe", daemon=True)
    t.start()
    return t


def animations_enabled(platform=None):
    """False when the user switched off Windows' "Animation effects" (Settings >
    Accessibility > Visual effects) - the card then stands still, as the dictation
    pill does (ui/glass.animations_enabled). True elsewhere or when it cannot be read."""
    if (sys.platform if platform is None else platform) != "win32":
        return True
    try:
        import ctypes
        value = ctypes.c_int(1)
        if ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(value), 0):   # SPI_GETCLIENTAREAANIMATION
            return bool(value.value)
    except Exception:
        pass
    return True


def _number(args, index, cast, default):
    try:
        return cast(args[index])
    except (IndexError, TypeError, ValueError):
        return default


def worker_main(args, ui=None, hard_timeout=HARD_TIMEOUT_S, platform=None):
    """The card process: ``args`` = [app pid, the app's launch time]. ``ui`` runs
    the window until the lifecycle says stop (default: splash_window.run)."""
    pid = _number(args, 0, int, 0)
    launched = _number(args, 1, float, time.time())
    life = Lifecycle(ParentWatch(pid, platform), hard_timeout=hard_timeout)
    watch_pipe(life)
    try:
        if ui is None:
            if (sys.platform if platform is None else platform) == "win32":
                try:
                    # A crashed card ends at once: no "stopped working" box, no frozen window, for
                    # what is only a loading card. SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
                    import ctypes
                    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
                except Exception:
                    pass
            from splash_window import run as ui
        return ui(life, show_at=launched + SHOW_DELAY_S, motion=animations_enabled(platform)) or 0
    except Exception:
        return 1
