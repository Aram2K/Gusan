"""Shared set-up for the plain test suite."""
import os

# A test run must never read the selection of whatever app is in front, or send it copy keys
# (selection_reader.enabled).
os.environ.setdefault("TRANSCRIBE_NO_SELECTION", "1")
