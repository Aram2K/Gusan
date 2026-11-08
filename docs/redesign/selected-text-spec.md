# Smart Actions on the selected text

Status: agreed with the founder 2026-11-07, building on branch `selected-text`.

## What the person does

1. Select some text anywhere (an email, a web page, a PDF, a chat).
2. Press the Smart Actions shortcut and say what to do: "translate it to French", "fix the grammar",
   "make it shorter", "what does this mean?", "reply to this".
3. Gusan works on the selected text. The selection does not have to stay selected while it works.

Only the Smart Actions shortcut does this. Dictation (Alt+R) always pastes the exact words.
With nothing selected, Smart Actions work exactly as today.

## Where the result goes

| What was asked | Result |
| --- | --- |
| Change the text (translate, fix, rewrite, shorten, format) | Pasted where the cursor is. If the text is still selected in a box you can type in, it is replaced. |
| A question about the text ("what does this mean?", "is this polite?") | Shown in a small card, and copied. Nothing is pasted. |
| The text cannot be changed (a web page, a PDF) and nothing can be typed where the cursor is | Copied, and the pill says "Copied". |

The model says which of the two it wrote (a change, or an answer). The app never guesses from the words.

## Reading the selection

Read the moment the shortcut is pressed, while the person speaks (no wait added after they stop).

- **Windows:** ask the focused app for its selection through Windows UI Automation (no keys sent, the
  clipboard untouched). Works in Word, Outlook, browsers, Notepad and most apps.
  If the app does not answer, send Ctrl+C, read the clipboard, and put the person's clipboard back.
  Never Ctrl+C into a terminal (there it stops the running program): no copy keys for console windows.
- **Mac:** Cmd+C, read the clipboard, put the person's clipboard back (Cmd+C is safe in every app,
  Terminal included). Needs Accessibility, which Gusan already asks for to paste.
- A password box is never read.
- At most 20,000 characters (longer selections are cut, and the pill says so).

## What the AI gets

The selected text as material, and the spoken words as the instruction:
"Selected text: ... / The user said: translate it to French". The selection is data, never instructions.
Privacy mode keeps it on this computer like everything else.

## Images (later)

Gusan Cloud's models read images (the Live Assistant already sends screenshots), and so do OpenAI, Claude
and Gemini own keys. The on-computer model does not. A second step can send a selected or copied image
("what is in this picture", "type out this table"); text comes first.

## Tests

- Reading: UI Automation answer, the copy fallback, nothing selected, a password box, a terminal, the
  clipboard restored.
- The prompt: selection + instruction, the change / answer marker.
- Routing: change -> paste, answer -> card + copy, Privacy mode, a long selection cut.
- On the founder's MacBook: Notes, Safari, Mail, Terminal.
