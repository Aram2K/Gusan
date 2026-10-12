"""Gusan 2.0 pages, one module per sidebar route.

To add a page, create ``ui/pages/<route>.py`` (route = home, dictation,
smart_actions, meetings, live_assistant, audio_to_text, dictionary, settings
or account) with:

    def create(main_app):            # -> QWidget, built the first time it opens
        return MyPage(main_app)

The shell (ui/shell.py) finds it on its own - no other file changes. A route
without its module shows the error card. Optional methods on the widget:

    on_open(**opts)            every time the route is opened (e.g. section="speech")
    on_setting_changed(key)    after AppController.set_setting changed ``key``
    refresh_account()          after sign-in / sign-out / plan changes
    wants_scroll = False       the page scrolls itself (default: the shell wraps it)

If create() raises, the route shows "This page couldn't open." with Try
again and Copy details instead of breaking the window.
"""
