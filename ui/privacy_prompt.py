"""The "Privacy mode is on" question, for a Pro person who picks Gusan Cloud.

Gusan Cloud sends the voice to our servers, which Privacy mode forbids. Instead
of a switch that does nothing, the page asks once:

    Privacy mode is on
    Gusan Cloud sends your voice to our servers. Turn off Privacy mode to use it?
    [Turn off Privacy mode]  [Keep it on]

    if privacy_prompt.confirm_turn_off_privacy(self, "Gusan Cloud"):
        # turn it off through the controller together with the choice:
        app.set_settings({"privacy_mode": False, ...the choice's changes})

The order a page checks in: Pro first (a Free person or a guest gets the Pro
window, whatever Privacy mode says), then Privacy mode. Tests replace
``confirm_turn_off_privacy`` (monkeypatch) instead of opening a window.
"""

from ui.components import mini_prompt

TITLE = "Privacy mode is on"
YES = "Turn off Privacy mode"
NO = "Keep it on"


def body_text(feature="Gusan Cloud"):
    return "%s sends your voice to our servers. Turn off Privacy mode to use it?" % feature


def confirm_turn_off_privacy(parent, feature="Gusan Cloud"):
    """Ask whether to turn Privacy mode off. True = turn it off (the caller does it)."""
    return mini_prompt.ask(parent, TITLE, body_text(feature), YES, NO, icon="incognito")


__all__ = ["NO", "TITLE", "YES", "body_text", "confirm_turn_off_privacy"]
