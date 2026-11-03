# People from the screen

Gusan 2.1.x, branch `people-from-screen`. Code: `people_from_screen.py`.

## What

After a two-person Live Assistant call the People card showed only "Aram - You". The names of the people on the call are on the
screen the Live Assistant already sees. With **Screen on**, Gusan now reads them off that screenshot and adds them to the
meeting's people. From there they are used everywhere the invite's people are:

- the **People card** and the meeting's header line (rows look as before: a round initial, the name, "You" on your own row);
- the **notes**: the "Attendees:" line in front of the transcript, and the prompt says to spell every name exactly as that line
  gives it (so "Anna Petrosyan" is not written "Ana Petrosian");
- the **live answers** ("Attendees:" in the Live Assistant's context);
- **Ask about this meeting** (the same People);
- the **speaker pass**: when the meeting has exactly one other name and that name was read off the screen, and the pass finds
  exactly one voice on the call, that voice is called by the name instead of "Speaker 1". With two or more other names, or a
  name that was only typed in or on an invite, nothing is guessed: "Them" / "Speaker N" as before.

The Screen button's tooltip says it: "It also reads the names of the people on your call."

## How it asks

One screenshot, the **same engine route an answer takes** (`_resolve_engine` -> `actions.process_stream`, mode `live_people`):

- **Gusan Cloud** (Pro, or a free session that is open): sent to the server as a `live_assist` request carrying the session id
  the Live Assistant already holds. It rides that session: no extra free-plan session, no extra allowance (the server counts
  sessions, not requests; the daily safety cap of 1,000 cloud actions is the only counter it touches, and a session makes at
  most 12). No server change was needed (`action_api.server_mode`, like `live_rule`).
- **The user's own key** (Gemini, Claude, OpenAI-compatible, Cerebras, Mistral): the same call the answers make, with the people
  brief as the system message.
- **A model on this computer / a text-only model / Privacy Mode**: nothing is captured or sent, silently.

The brief (`action_api.PEOPLE_SYSTEM`) asks for strict JSON `{"people": ["First Last", ...]}`, no guessing, `[]` when no call is
visible, nobody who is the user, no buttons, titles, phone numbers or emails, and says the screen text is data, never
instructions. Output cap 300 tokens; thinking off, like every live call. The reply is parsed defensively (`parse_reply`): the
JSON also inside a code fence or after some words, a bare list, `{"names": ...}`, a bullet list or a "People: ..." line from a
model that ignored the format; a sentence is never read for names.

## When

Only in a Live Assistant session, only with Screen on and the card on screen, never in Privacy Mode, only with an engine that
reads images. Runs on a background thread; the GUI only takes the screenshot (and only when no fresh one exists).

| rule | value |
| --- | --- |
| first look | 30 s after the session starts listening |
| next looks | every 5 minutes, only if the screen changed (16x16 grey average hash, 16 of 256 bits); an unchanged screen is checked again after 90 s |
| at most | 12 looks per session |
| stop | after 3 empty results in a row, 3 failures in a row, an engine that cannot read images, or when the meeting holds 12 names from the screen |
| screenshot | one taken for an answer in the last 60 s is read instead of taking another |
| a newer answer | wins: a look in flight is cancelled and given back, the next one waits 20 s; no look starts while an answer is written |
| failure | silent (debug log: the kind of failure, never a name) |

A session where nobody asks anything now takes one screenshot about 30 s in (then at most one every 90 s while the screen does not
change, one look every 5 minutes when it does). Screen on is the user's opt-in to screenshots.

## What happens with the names

`MeetingSession.add_screen_people` (GUI thread) merges them (`people_from_screen.merge`): the invite's names first and keeping
their spelling, nobody twice (one first name is one person; accents and case ignored), never the user (their full or first
name), never a name the user removed. They are written to `meta.json` **at once** (`attendees`, `attendees_list`,
`attendees_screen`), so a crash keeps them and finishing the meeting from its recording uses them. The live view's header, the
Live Assistant's answers (`set_attendees`) and the page follow (`people_changed`).

Removal: a name read off the screen has a small x on its row of the People card (shown on hover or keyboard focus, in the tab
order, named "Remove Anna Petrosyan from People"). It takes the name off the meeting (`forget_person`), keeps it in
`attendees_removed`, and the screen is never trusted for it again - not in the same session, not in a continued meeting, not when
the notes are written again. Invite names have no x (this is about the screen).

## Safety

The screen is untrusted: anyone on the call chooses their display name, and a name can say "ignore previous instructions".

- A name is letters (any script: Armenian, Cyrillic, accents, Arabic, CJK), spaces and `. ' -` only; at most 40 characters, 5
  words, 24 letters a word; no digits, line breaks, control characters, brackets, markup, `@`, `#`; invisible direction marks
  are dropped. A role beside it ("Anna (Host)", "Anna - Host") is cut off; "Aram (You)" is the user's own tile and is dropped.
- Not a name: buttons and call words (Mute, Share screen, Meeting chat, Host, Guest, Zoom ...), phone numbers, emails, anything
  with a word that reads like an order to a model (ignore, instructions, prompt, system, assistant ...), a sentence.
- At most 12 names a look and a meeting. Names reach prompts only through `invite_context.cap_names` / `notes_header` /
  `live_lines` (one line, framed as data) and `live_context.defuse_header`.
- Names are never logged (counts and timings only) and never go to analytics.
- Nothing is sent in Privacy Mode; a hidden card never takes a screenshot.

## Known limits

- **Meetings already recorded have no screenshots: nothing is back-filled.**
- The screenshot is of the monitor under the mouse, like an answer's. A call on another monitor is not read.
- A name is read from small tiles: it can be a nickname or wrong. That is what the x is for. Names the model could not read are
  left out, never guessed.
- A guest in a call who has the same first name as the user is taken for the user and left out.
- The speaker pass names the voice only when the name came from the screen; an invite-only 1:1 stays "Speaker 1" (a person
  invited may not be the one who came). A name the screen only confirmed (already on the invite) does not count.
- Two people with one first name are one person here.
- A model on this computer cannot read images: those sessions get no names.

## Try it on a real call

`python -m people_from_screen shot.png [--me "Aram"] [--raw]` runs the sanitizer and the real configured engine (your saved key; or
Gusan Cloud with `GUSAN_ACCESS_TOKEN` set) on a PNG or JPEG, and prints the names it would add. `--reply reply.txt` skips the
engine and runs only the sanitizer on a saved model reply. Exit status 2: the engine could not be asked or cannot read images.

## Tests

`tests/test_people_from_screen.py` (sanitizer, parser, merge, the wire format, the schedule, the session, persistence, the speaker
pass, the command line - no Qt) and `tests_qt/test_people_from_screen_ui.py` (People card, live header, the Live Assistant card).
