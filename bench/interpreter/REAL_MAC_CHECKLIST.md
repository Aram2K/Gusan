# Interpreter on a real Mac: what only real hardware can show

The Mac VM has no microphone or speakers and its audio is virtual (BlackHole), so these are untested.
Use the packaged app (the .dmg from CI, opened from Finder), an Apple Silicon Mac on macOS 14.2 or later,
`brew install --cask blackhole-2ch`, and a second device in the call.

1. **First permission prompt.** On a Mac that never ran Gusan, turn the Interpreter on.
   - macOS asks for the microphone, then for System Audio Recording. The text under the title names
     Gusan (the title says "TranscribeApp", the app's file name).
   - Allow it within 60 s: Hear starts without turning it on again.
   - Install the next build over it: with ad-hoc signing both prompts come back (they did on the VM).
2. **Real mic and speakers.** In Settings > Interpreter, Try it: say a sentence, hear the translation.
3. **A real Zoom call, then a real Meet call (Chrome or Safari).** In the call app pick "BlackHole 2ch"
   as the microphone. Have the other device play or speak the other side.
   - **Headphones first.** Check: Hear captions and voice follow the other side; they hear your
     translation (Speak); music or a notification from another app is not translated while the call
     app has sound; Gusan does not translate itself.
   - **Then the Mac's speakers.** Same checks. Watch for Gusan translating its own voice or the call's
     voice coming back through your mic (echo, cross-talk).
   - Record a meeting with "both sides of the call": both voices are in the transcript.
4. **Speak WER in speakers mode.** On speakers, read your 6 turns (the `turns`
   whose `speaker` is the file's `me`) of `corpus/conversations/c01_en_hy_standup.json` while the other device plays its other side. Copy
   what Gusan heard you say and score it against what you read (from `bench/interpreter`):
   `python -c "import metrics; print(metrics.wer(open('ref.txt').read(), open('hyp.txt').read(), 'en'))"`
   Do the same on headphones. The simulator says about 2% on headphones and 27-60% on speakers;
   a real number decides whether speakers mode needs echo cancelling before release.
