# Gusan — the keyboard that does things for you

Say what you want, wherever you're typing. Gusan writes it, or an H Company computer-use agent goes and does it.

## The problem
On a phone, getting anything done means leaving the conversation: open a browser, log in, hunt through a site, copy the answer back, paste it. Every AI app today adds yet another app to switch to. The keyboard is the one surface that is already open in every app, so that's where Gusan lives.

## What it is
Gusan is an iPhone keyboard with a slim strip above the normal keys. Tap the mic and talk. Two modes:
- **Smart** — understands your selected text, the text around your cursor and (if you allow it) what's on your screen, then writes the reply, translation, summary or answer straight into the field at your cursor. One tap to Undo.
- **Agent** — hands the request to an H Company computer-use agent that operates a real browser. Progress lives in the Dynamic Island and lock screen as a Live Activity; the result comes back into the chat you were in, with Copy and Open.

## Why Computer Use is the core
Anything behind a login or a form is out of reach for a text model; Agent mode is the hands. H's agent navigates, reads, clicks and types; Gusan supplies context (selection, screen, calendar) and the user supplies intent in one sentence.

## User control
- Plan → **Confirm** → execute: nothing irreversible without Confirm. Stop at any time.
- Logins the agent never sees: Keychain + Face ID, released per use, typed by H's Vault outside the agent's context.
- Screen shared only while the screen icon is selected; every session visible, continuable, stoppable.

## Tech stack
- Computer use: H Company agents (planning + execution, Vault)
- Speech-to-text: Gladia
- Web search: Linkup
- LLM: Devin (Smart actions)
- Payments: Stripe
- Cloud: Supabase (Auth, Postgres, Edge Functions); Swift/SwiftUI app + custom keyboard, ActivityKit; Windows/macOS desktop companion

## Reliability
One cloud fronts Gladia, Smart actions, Linkup, Stripe and H, with server-side keys, per-user auth, a plan → confirm → execute state machine with audit log, and a Live Activity that survives the app being closed.

## Why it's new
AI keyboards rewrite text; computer-use agents live in a browser tab. Gusan puts a computer-use agent in the keyboard: host-app context in, cursor out, and logins without the agent knowing the password.
