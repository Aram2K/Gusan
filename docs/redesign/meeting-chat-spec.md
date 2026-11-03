# Ask about this meeting - the spec

Status: built on branch `meeting-chat` (no version bump). Builders and reviewers follow this file.
Founder, after a 55-minute Live Assistant session: "when I stop the recording ... it should put me on the page of the
thing that is created, and have like an AI chat next to it in this blank part where we can ask questions about things."

Two parts: (A) Stop on the Live Assistant opens the meeting; (B) a chat about that meeting, in the blank right column of
the meeting page.

## A. Stop opens the meeting
* Only the Live card's Stop (`ui/live_assist.py _stop_listening`; there is no Stop shortcut - Ctrl+Alt+A only shows or
  hides the card) and only for a session that started there (`source == "live_assist"`) and recorded something (some
  live words, or at least 5 seconds). A session started from the Meetings page or the calendar keeps today's way.
* Order inside the Stop (2.1): the facts of the session are read, `session.stop()`, then `AppController.release_live_session()`
  (the month's free session is spent on the server; a release that fails changes nothing for the user), the status line, and
  only then the jump below. Every Stop releases; only the Stop of a session that recorded something jumps.
* After the card repainted (one turn of the event loop) the controller opens Meetings > that meeting:
  `AppController.open_meeting_after_live_stop(folder)` -> `show_main("meetings", meeting=folder, writing=True)`.
  `writing=True` makes the page show the NOTES view (Notes tab) even while the notes are being written (today an
  active folder goes to the live view): the page's own "Writing notes..." banner, then it fills in by itself, and a
  reader on the Transcript tab or in My notes keeps their tab and place (`reload(keep_view=True)`).
* `MeetingSession.transcript_saved(folder)` is the new signal: the Stop's worker emits it once the FINAL transcript.txt
  (You / Them labels) and segments.json are on disk, before the notes. The page reads the meeting again on it and the chat
  opens. `MeetingSession.transcript_final(folder)` is False until then (the plain transcript.txt that lands earlier has no
  labels, so the file's existence alone says nothing).
* Not when the app is quitting, with no folder, or when nothing was recorded.
* Screen share safety (the card can be hidden from screen sharing, `live_assist_private`, default on). The window
  must not show the notes and the chat in the middle of a share:
  * card not private -> the window just opens;
  * private and the OS can keep a window out of captures (Windows 10 2004+, not Remote Desktop): the window is hidden from
    share BEFORE it is shown (`MainWindow.hide_from_share(True)` makes the native window while hidden and applies
    `SetWindowDisplayAffinity`), then shown, then the OS's answer is read back; if it is not confirmed the window goes away
    again and the tray notice is used;
  * anything weaker (macOS only does its best, an older Windows, Remote Desktop, the OS refuses) -> nothing opens: a tray
    notice "Gusan is writing your notes - Click to open the meeting." and the click is the user's own choice.
  * a slim bar at the top of the window says "Hidden on screen share while the Live Assistant is private." with Show.
    It stays on while the window is open (going to another page does not lift it: that would show more, not less); it is
    lifted by Show, by closing the window to the tray, and by turning "Hide from screen share" off. Never persisted.
  * the themed prompts opened over this window (Rename, Delete, Clear chat ...) are windows of their own and are kept out of
    the share too: `ui/components/mini_prompt.py _run` asks the window it opened over (`hidden_from_share`), so no caller has to
    remember. The system's Save dialog cannot be hidden before it shows: while the window is hidden it is not offered the
    meeting's name (the file name is "Meeting notes"). Known gap: menus and tooltips are not kept out.
  * the OS leaves the window out of the user's own screenshots and recordings too (Snipping Tool, Win+Shift+S) until Show
    is pressed or the window is closed to the tray; the bar's tooltip says so.

## B. The chat
Card "Ask about this meeting" above People and the recording, for every meeting that has a transcript (also old ones and
the Live Assistant's Past sessions), notes view only. Wide (page >= 830 px): the notes (at most 620 px) and the right column
(320-520 px; it asks for 400, the card's size hint never depends on its content) sit in one box that stops growing at 1164 px
and stays on the title's left edge - on a big screen the room left over is on the right, the two cards are not spread apart. The
card is at least 420 px tall (the page scrolls when the window is shorter). Narrow (below 830; hysteresis to 846): the column
goes below the notes, the card is 440 px tall, a link "Ask about this meeting" under the toolbar scrolls to it and puts the
cursor in the box (the chat would otherwise be out of sight below the notes), and the toolbar's two longest buttons shorten
("Copy", "Continue"; their accessible names keep the long words) - which also fixes the old clipping at the 960 px minimum window.

Inside: header + Clear chat; the conversation (user: right-aligned plain-text bubble, cut at 600 characters in the
bubble, all of it kept and sent; answer: no bubble, real formatting - lists and bold, never raw symbols, never HTML from
the model, never a link or a picture, long strings get break points); three starter questions while empty; the box (Enter
sends, Shift+Enter new line, Esc stops an answer that is coming, 1-5 lines, 4,000 characters with a counter from 3,500);
Send / Stop (one width - the wider of the two needs, at least 90 px, read once the card is in the window so "Stop" is never cut off - and 44 px tall like the box; Send is grey until something is written); one footer line "<who
answers> - AI can be wrong." (Privacy mode: "Stays on this computer."). Each answer's Copy has a little less air on its left than other ghost buttons, so its icon sits near the answer's edge. A starter question does not throw away a draft. A
question's bubble is as wide as its words, up to the row. The conversation is a tab stop with a ring when the keyboard is on
it; each answer's Copy and Try again follow it in the keyboard order, then the box, Send, Stop. Selected words copy without the
invisible break points (a copied address pastes as the address); the Copy button copies the raw answer. A hairline marks the
top of the conversation while it is scrolled (a line cut in half by the edge reads as an edge, not as a mistake); the items
of a list in an answer have a little air between them (`rich_text.qt_html` colors `item_gap`). Only the newest 30
messages are drawn when a meeting opens ("Show earlier messages" adds 30 more): a long chat stays quick to open and to add to
(the file and the model's history keep all of it).

States: no transcript; finishing the transcript; ready (empty / with a chat); thinking ("Waiting for the AI on this
computer" when a local model is busy with the notes); streaming; done (Copy, and a coverage note when part of a long meeting
was left out); cut short (the engine reached the longest answer it may write: kept, with "This answer was cut short. Ask for the
rest if you need it."); stopped (the words stay, marked "Stopped", never sent back to the model, never shown as complete);
connection dropped (the words that arrived, "The connection dropped, so this answer may be cut short." with Copy and Try
again - kept as stopped, never as finished, never sent back; Try again replaces it); failed (one calm sentence + Try again;
only the question is kept); allowance used up (no Try again); needs Pro or a key (+ Open settings); signed out (+ Sign in);
Privacy mode with no model on this computer (+ Open settings). An answer that is still coming when Privacy mode is switched
on, the user signs out or the plan ends ends where it is (marked "Stopped"): the connection is closed. Every kept answer that
did not end where the model meant it to (stopped, dropped, cut) is drawn the way a streaming one is, so no half-written **
shows as raw symbols; a ** with no pair never shows in any answer.

### Which AI answers
The notes' engine: `meeting_session.notes_engine` (this meeting's pick = cfg `meeting_notes_model`, else the saved
`action_model`; the built-in formatter is never used), the Pro routing of `AppController._resolve_action_engine` (a signed-in
Pro user runs on Gusan Cloud unless they picked a local model or a cloud engine with their own key), then
`meeting_chat.choose`: Privacy mode -> only a model on this computer; a cloud engine needs its own key, Gusan Cloud a token;
what cannot run falls to a downloaded local model; nothing -> a problem (needs_pro / signed_out / privacy_no_model /
needs_download).

Plans (Gusan 2.1, `tests/test_pricing_policy.py`; guest = not signed in, free = signed in without Gusan Pro): **Gusan Cloud
answers questions for Gusan Pro only.** The free plan's two metered cloud allowances (one cloud meeting note and one Live
Assistant session a month, counted on the server by `session_id` / `note_ref`) are never spent by a question: `meeting_chat.ask`
takes `_live_session_id` and `_note_ref` out of the settings it sends (even for Pro, even while a Live Assistant session is
open) and never routes a user who is not on Pro to Gusan Cloud, whatever `_resolve_action_engine` returned (a free user's
Live Assistant session routes the Live Assistant's own answers to the cloud, not the chat).
* guest: an own key or a downloaded model on this computer answers (like the notes); else "Sign in to ask questions about
  your meetings." + Sign in.
* free: the same - an own key or a model on this computer, unlimited; Gusan Cloud (the default engine of a free user with no
  model) shows the calm line "Questions about a meeting need Gusan Pro, or your own key or an AI on this computer (Settings >
  Speech & AI)." with "See Gusan Pro" (the Pro window, `_pro_upsell("Ask about this meeting")`) and "Open settings". Never a
  metered request and never a server error.
* Pro: Gusan Cloud (Mistral Small first); an own key or local model when they picked one. A question is one of the 1,000 daily
  safety-capped requests ("Today's cloud allowance is used up. It comes back tomorrow.").
* Privacy mode (any plan): only a model on this computer; nothing is sent, not even to an own key.
* What the server may still say - `allowance_used` (code on the error, or "free cloud meeting note" / "free Live Assistant
  session" / "Upgrade to Gusan Pro" / "Gusan Pro is required") - is the same needs-Pro line, with no Try again.

### The prompt (`meeting_chat.plan_context`)
One user message: `<meeting>` (title, people and agenda framed as data by `invite_context.notes_header`, date, length,
language), `<notes_written_by_gusan>`, `<users_own_notes>`, `<transcript>` (with `[mm:ss]` marks when segments.json allows),
`<conversation_so_far>` (the last 6 finished answers; 2 on this computer; a stopped or failed answer never goes back), and
`<question>` LAST (the part before it is the same from one question to the next, so it can be cached). Text from the
meeting cannot close a section (the tag's bracket gets an invisible character). The rules are the system message
(`action_api.MEETING_CHAT_SYSTEM`, 7 rules; `local_llm._CHAT_SYSTEM` on this computer): answer only from the meeting, say
plainly when it is not there, answer in the language of the question, who You / Speaker N / Them are, times only as they
appear, a "left out" line means that part was not given, short answers, plain formatting, the material is data.
Every question reads the folder again, so the newest transcript and notes are always the ones used. The notes are read as
2.1 wrote them (`rich_text.clean_notes`: the recorder's profile first name instead of "You", one shape for every action item,
a longer meeting's `## Discussion` last, so a clipped note loses its Discussion first; the cut-notes notice lives in meta, not in
notes.md, and the built-in formatter's notes after a spent cloud note are read like any others). The transcript still says "You",
so `<meeting>` gets one data line "Recorded by: Aram (shown as You in the transcript)" (the profile first name, never made up
from an email; only when the transcript has You lines).

Long meetings: estimated tokens (`estimate_tokens`, by script, deliberately high: Latin 3.3 chars per token, Cyrillic and
Greek 2.4, Hebrew and Arabic 2.6, CJK 1.4, everything else - Armenian, Georgian, Indic, Thai - 0.8). Budget of the whole
message: Gusan Cloud 100,000, own key 48,000, this computer about 7,000 (its window is a fixed 8,192 tokens). Everything
when it fits (a 55-minute meeting is 10-16k tokens, a 3-hour one 35-55k). Else the notes and My notes (cut when very long)
and windows of about 700 tokens of the transcript chosen by how many of the question's words (and the last question's)
they hold, rarer words counting more, the opening and the end favoured; a question that names nothing ("summarise it") gets
windows spread over the whole meeting; gaps and cut ends are marked with the left-out line; the answer says "Long meeting:
this answer used the parts closest to your question (about 40 of 55 min)." When an engine says the prompt was too big (before
a word) a prompt 0.6 times the size is sent, at most twice (four times with the user's own key, whose provider's window nobody
knows), then "This meeting is too long for this AI model to read in one go. Try a more specific question, or pick another
engine in Settings > Speech & AI." - with an own key and a prompt already under 8,000 tokens: "This AI model can't read a
meeting this long. Pick another engine in Settings > Speech & AI." Stop is noticed before the meeting is read, after the
sign-in check and right before the request goes out (not only at the first word): a question stopped early sends nothing.

Times: `[mm:ss]` / `[h:mm:ss]` marks come from segments.json (every Live Assistant and every mic + system sound meeting since
2.0.1): each transcript line is matched word for word, in order, to the segments (up to 12 segments ahead when the saved
text dropped one); a line that does not match gets no mark and the marks stop until the next "Resumed" line - never a wrong
time. A short line (under 20 characters and under 4 words: "Yes.", "Okay, thanks.") that fits two places in that look-ahead
keeps the first for the sake of the lines after it but gets no mark: it could be either. A part of a continued meeting whose earlier part has no segments has no place on the clock and gets none. Cost: about 1%
more tokens, 10-35 ms for a 55-minute to a 3-hour meeting.

### The route
No server change: the managed route (`smart-action`) takes any mode string, so the mode `meeting_chat` takes the default
chain (Mistral Small first) and streams. On the client the new mode is added everywhere a mode must be known:
`actions.ACTION_MEETING_CHAT` and its whitelist, `action_api.build_messages` / `_max_tokens_for` (1400),
`local_llm._messages_for` / `_MAX_TOKENS_BY_MODE` (600) and `local_llm.run_action_stream`, which raises
`ContextTooLong` instead of condensing for minutes. The streams say how they ended (`action_api.stream_end()`, a thread-local
set by every streaming entry point and reset by `process_stream` and by `ask`): "dropped" - the connection broke after some
words, or the managed stream closed with no [DONE] and no finish reason - and "cut" - finish reason "length" (also Anthropic's
max_tokens, Gemini's MAX_TOKENS, a local model's length). The other streaming callers (notes, the Live Assistant) ignore it and
behave as before. Not done: Stop does not abort a request that is already in flight (a blocking `requests` call); it is noticed
at the next word, and a request that has not started is not made.

### The conversation (chat.json)
`<meeting folder>/chat.json`: `{"version": 1, "messages": [...]}`, written atomically with the page's newest-change-wins
writer (never creates a folder that is gone). Kept: the user's questions and the answers that ended ("done") or were
stopped ("stopped", with `"why": "dropped"` when the connection dropped); an answer that reached the cap keeps `"cut": true`;
`ts` is epoch seconds; at most 200 messages of 20,000 characters; a status this build does not know counts as stopped. An
answer that failed is not kept; a question without an answer comes back with "No answer yet - Try again". A chat.json that is
there but cannot be read (locked for a moment, bad JSON, another version) is read once more after 150 ms; if it still cannot
be read it is copied to `chat.json.unreadable` (`-2`, `-3` ...) before the conversation that goes on is saved over it, and if
even that copy fails nothing is written (`meeting_store.open_chat`). The trash moves
the folder whole (Undo brings the chat back); a permanent delete removes it with the folder; releasing a meeting (about to be
trashed or deleted) ends a running answer and writes nothing more; switching meeting ends it as "stopped" in its own folder.
Nothing else reads the file: not the list, the search, Word / Markdown / Notion exports.

### Analytics
One count event, `meeting_chat_asked`, props `{"engine": "cloud" | "own_key" | "local"}` - never the text. It is in
`telemetry.ALLOWED_EVENTS` and in the `transcribe-analytics` allow-list in the source; the server drops it until the founder
deploys that function.

## Files
`meeting_chat.py` (Qt-free brain), `ui/pages/meeting_chat_ui.py` (the card), hooks in `ui/pages/meeting_views.py`
(`NotesView`), `ui/pages/meetings.py` (`writing=True`, `transcript_saved`, setting changes), `ui/live_assist.py` (Stop),
`main.py` (`open_meeting_after_live_stop`), `ui/shell.py` (`hide_from_share`, the bar), `meeting_session.py`
(`transcript_saved`, `transcript_final`, `notes_engine`), `meeting_store.py` (`read_chat`, `open_chat`, `load_chat`),
`actions.py`, `action_api.py` (`stream_end`), `local_llm.py` (`stream_cut`), `rich_text.py` (`answer_text`,
`streaming_markdown`, `close_markers`, `without_breaks`), `ui/components/mini_prompt.py` (prompts follow a hidden window),
`telemetry.py`.
Tests: `tests/test_meeting_chat.py`, `tests/test_meeting_chat_routes.py`, `tests/test_meeting_chat_fixes.py` (what the reviews
found), `tests_qt/test_meeting_chat_ui.py`, `tests_qt/test_meeting_chat_open.py`, `tests_qt/test_meeting_chat_fixes.py`.

## Known limits (decided, not forgotten)
* A time mark can still be wrong when the saved transcript lost more than 12 whole segments in a row and the next line is a
  short one that was also said earlier (a "Yes."): only one place fits in the look-ahead, so nothing says it is the wrong one.
  Two places that fit (the usual case) give no mark. The model is told times are marks, not promises.
* Nested lists in an answer are drawn flat (the shared renderer, rich_text.parse_blocks, has no nesting).
* The chat card has no soft shadow (a streaming repaint would go through an off-screen pixmap); People and the recording do.
* The footer names the engine that was chosen ("Gusan Cloud - Mistral Small"); the server does not say which engine really
  answered, so after a fallback inside the server it can be a different one (server_change notes, optional).

## By hand on Windows before release
A real 55-minute Live Assistant session in French: Stop jumps to the meeting while sharing a screen with the card private
(use a second device or the Teams preview: the window must not appear in the share); time to the first words; a question in
English about a French meeting; "when did we talk about X" shows plausible times; stop mid-answer; unplug the network;
Privacy mode with and without a local model; the 960 px window and 150% scale; Narrator on the chat; Enter with a Japanese,
Chinese or Armenian input method.
