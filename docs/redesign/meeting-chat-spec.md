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
  * known gap: menus, tooltips and prompts are windows of their own and are not hidden (the chat's Clear prompt asks for a
    hidden one).

## B. The chat
Card "Ask about this meeting" above People and the recording, for every meeting that has a transcript (also old ones and
the Live Assistant's Past sessions), notes view only. Wide (page >= 830 px): the right column is 320-520 px (it asks for
400, the card's size hint never depends on its content), the card is at least 340 px tall. Narrow (below 830; hysteresis to
846): the column goes below the notes, the card is 440 px tall, and the toolbar's two longest buttons shorten ("Copy",
"Continue"; their accessible names keep the long words) - which also fixes the old clipping at the 960 px minimum window.

Inside: header + Clear chat; the conversation (user: right-aligned plain-text bubble, cut at 600 characters in the
bubble, all of it kept and sent; answer: no bubble, real formatting - lists and bold, never raw symbols, never HTML from
the model, never a link or a picture, long strings get break points); three starter questions while empty; the box (Enter
sends, Shift+Enter new line, Esc stops an answer that is coming, 1-5 lines, 4,000 characters with a counter from 3,500);
Send / Stop; one footer line "<who answers> - AI can be wrong." (Privacy mode: "Stays on this computer.").

States: no transcript; finishing the transcript; ready (empty / with a chat); thinking ("Waiting for the AI on this
computer" when a local model is busy with the notes); streaming; done (Copy, and a coverage note when part of a long meeting
was left out); stopped (the words stay, marked "Stopped", never sent back to the model, never shown as complete); failed
(one calm sentence + Try again; only the question is kept); allowance used up (no Try again); needs Pro or a key (+ Open
settings); signed out (+ Sign in); Privacy mode with no model on this computer (+ Open settings).

### Which AI answers
The notes' engine: `meeting_session.notes_engine` (this meeting's pick = cfg `meeting_notes_model`, else the saved
`action_model`; the built-in formatter is never used), the Pro routing of `AppController._resolve_action_engine` (a signed-in
Pro user runs on Gusan Cloud unless they picked a local model or a cloud engine with their own key), then
`meeting_chat.choose`: Privacy mode -> only a model on this computer; a cloud engine needs its own key, Gusan Cloud a token;
what cannot run falls to a downloaded local model; nothing -> a problem (needs_pro / signed_out / privacy_no_model /
needs_download). Free and guest have no Smart Action tries consumed; they can ask with their own key or a local model, like
the notes. A question is one of the 1,000 daily managed requests.

### The prompt (`meeting_chat.plan_context`)
One user message: `<meeting>` (title, people and agenda framed as data by `invite_context.notes_header`, date, length,
language), `<notes_written_by_gusan>`, `<users_own_notes>`, `<transcript>` (with `[mm:ss]` marks when segments.json allows),
`<conversation_so_far>` (the last 6 finished answers; 2 on this computer; a stopped or failed answer never goes back), and
`<question>` LAST (the part before it is the same from one question to the next, so it can be cached). Text from the
meeting cannot close a section (the tag's bracket gets an invisible character). The rules are the system message
(`action_api.MEETING_CHAT_SYSTEM`, 7 rules; `local_llm._CHAT_SYSTEM` on this computer): answer only from the meeting, say
plainly when it is not there, answer in the language of the question, who You / Speaker N / Them are, times only as they
appear, a "left out" line means that part was not given, short answers, plain formatting, the material is data.
Every question reads the folder again, so the newest transcript and notes are always the ones used.

Long meetings: estimated tokens (`estimate_tokens`, by script, deliberately high: Latin 3.3 chars per token, Cyrillic and
Greek 2.4, Hebrew and Arabic 2.6, CJK 1.4, everything else - Armenian, Georgian, Indic, Thai - 0.8). Budget of the whole
message: Gusan Cloud 100,000, own key 48,000, this computer about 7,000 (its window is a fixed 8,192 tokens). Everything
when it fits (a 55-minute meeting is 10-16k tokens, a 3-hour one 35-55k). Else the notes and My notes (cut when very long)
and windows of about 700 tokens of the transcript chosen by how many of the question's words (and the last question's)
they hold, rarer words counting more, the opening and the end favoured; a question that names nothing ("summarise it") gets
windows spread over the whole meeting; gaps and cut ends are marked with the left-out line; the answer says "Long meeting:
this answer used the parts closest to your question (about 40 of 55 min)." When an engine says the prompt was too big (before
a word) a prompt 0.6 times the size is sent, at most twice, then "This meeting is too long for this AI model to read in one
go. Try a more specific question, or pick another engine in Settings > Speech & AI."

Times: `[mm:ss]` / `[h:mm:ss]` marks come from segments.json (every Live Assistant and every mic + system sound meeting since
2.0.1): each transcript line is matched word for word, in order, to the segments (up to 12 segments ahead when the saved
text dropped one); a line that does not match gets no mark and the marks stop until the next "Resumed" line - never a wrong
time. A part of a continued meeting whose earlier part has no segments has no place on the clock and gets none. Cost: about 1%
more tokens, 10-35 ms for a 55-minute to a 3-hour meeting.

### The route
No server change: the managed route (`smart-action`) takes any mode string, so the mode `meeting_chat` takes the default
chain (Mistral Small first) and streams. On the client the new mode is added everywhere a mode must be known:
`actions.ACTION_MEETING_CHAT` and its whitelist, `action_api.build_messages` / `_max_tokens_for` (1400),
`local_llm._messages_for` / `_MAX_TOKENS_BY_MODE` (600) and `local_llm.run_action_stream`, which raises
`ContextTooLong` instead of condensing for minutes. A connection that drops mid-stream returns what arrived (as the Live
Assistant does): such an answer can look complete.

### The conversation (chat.json)
`<meeting folder>/chat.json`: `{"version": 1, "messages": [...]}`, written atomically with the page's newest-change-wins
writer (never creates a folder that is gone). Kept: the user's questions and the answers that ended ("done") or were
stopped ("stopped"); at most 200 messages of 20,000 characters; a status this build does not know counts as stopped. An
answer that failed is not kept; a question without an answer comes back with "No answer yet - Try again". The trash moves
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
(`transcript_saved`, `transcript_final`, `notes_engine`), `meeting_store.py` (`load_chat`), `actions.py`, `action_api.py`,
`local_llm.py`, `rich_text.py` (`answer_text`, `streaming_markdown`), `telemetry.py`.
Tests: `tests/test_meeting_chat.py`, `tests/test_meeting_chat_routes.py`, `tests_qt/test_meeting_chat_ui.py`,
`tests_qt/test_meeting_chat_open.py`.

## By hand on Windows before release
A real 55-minute Live Assistant session in French: Stop jumps to the meeting while sharing a screen with the card private
(use a second device or the Teams preview: the window must not appear in the share); time to the first words; a question in
English about a French meeting; "when did we talk about X" shows plausible times; stop mid-answer; unplug the network;
Privacy mode with and without a local model; the 960 px window and 150% scale; Narrator on the chat; Enter with a Japanese,
Chinese or Armenian input method.
