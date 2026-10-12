"""Gusan 2.0 floating windows that live outside the main window.

    from ui.floating import MeetingReminder      # the "record this meeting?" toast

The tray menu is ui/tray_menu.py. The dictation bar (ui/overlay.py) and the
Live Assistant card (ui/live_assist.py) keep their own modules.
"""

from ui.floating.meeting_reminder import MeetingReminder

__all__ = ["MeetingReminder"]
