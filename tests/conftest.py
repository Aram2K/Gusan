"""Shared set-up for the plain test suite."""
import os

# A test run must never read the selection of whatever app is in front, or send it copy keys
# (selection_reader.enabled).
os.environ.setdefault("TRANSCRIBE_NO_SELECTION", "1")
os.environ.setdefault("TRANSCRIBE_NO_WIN_HOTKEYS", "1")      # never take real system-wide shortcuts (win_hotkeys)
