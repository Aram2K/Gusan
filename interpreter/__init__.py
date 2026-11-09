"""Gusan Interpreter: live speech-to-speech translation in calls (no Qt in this package).

One direction is a chain of streaming steps (pipeline.DirectionPipeline):

    sound -> speech-to-text (eleven.ScribeRealtime) -> phrases (segmenter.PhraseCutter)
          -> translation (translate.translate_phrase) -> voice (eleven.DialogueVoice) -> a sink

The controller (controller.Interpreter) runs two of them at once, "hear" (the call -> your
headphones or speakers) and "speak" (your mic -> the Gusan Microphone, in your own voice), with
short-lived provider passes from the server (session.InterpreterSession). Keys never reach the PC.
See docs/redesign/interpreter-spec.md.
"""
