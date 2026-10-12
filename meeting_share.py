"""Meeting notes as plain text to share, without Qt.

    build_transcript_with_markers(chunks)          "[speaker change]" after a long pause
    build_llm_input(transcript, title, attendees, user_notes)
    strip_markdown(md)                             headings, bold, code and checkboxes out
    format_share(fmt, summary, title)              "markdown" | "email" | "slack"

Lifted unchanged from the 1.x MeetingsWindow (retired in Gusan 2.0) so the
helpers and their tests stay in one Qt-free place.
"""
import re

SPEAKER_CHANGE_GAP = 1.4      # seconds of silence before a piece that mark a new speaker


def build_transcript_with_markers(chunks):
    """Join the live pieces; a piece after a long pause gets "[speaker change]"
    (never the first one)."""
    parts = []
    for i, chunk in enumerate(chunks or ()):
        text = (chunk.get("text") or "").strip()
        if not text:
            continue
        if i > 0 and (chunk.get("silence_before") or 0) >= SPEAKER_CHANGE_GAP:
            text = f"[speaker change] {text}"
        parts.append(text)
    return " ".join(parts)


def build_llm_input(transcript, title="", attendees="", user_notes=""):
    """The transcript with the meeting's title, people and the user's own
    notes in front - or the bare transcript when there are none."""
    if not title and not attendees and not user_notes:
        return transcript
    parts = []
    if title:
        parts.append(f"Meeting Title: {title}")
    if attendees:
        parts.append(f"Attendees: {attendees}")
    if user_notes:
        parts.append(f"User Notes:\n{user_notes}")
    parts.append(f"Transcript:\n{transcript}")
    return "\n\n".join(parts)


def strip_markdown(md):
    """Markdown notes as plain text: no # headings, ** or `, and "- " /
    "- [ ] " lines as "• " bullets."""
    lines = []
    for line in (md or "").splitlines():
        line = re.sub(r"^#+\s+", "", line)
        if line.strip().startswith("- [ ]") or line.strip().startswith("- "):
            line = re.sub(r"^\s*-\s*(\[\s*\])?\s*", "• ", line)
        line = line.replace("**", "")
        line = line.replace("`", "")
        lines.append(line)
    return "\n".join(lines)


def format_share(format_type, summary, title=""):
    """The notes for an email ("Subject: … - Notes", Hi, …, Best,), Slack
    (*bold* headings) or as they are (markdown, and anything unknown)."""
    summary = summary or ""
    title = title or "Meeting"
    if format_type == "email":
        return (
            f"Subject: {title} - Notes\n\n"
            "Hi,\n\n"
            f"{strip_markdown(summary)}\n\n"
            "Best,"
        )
    if format_type == "slack":
        slack = re.sub(r"^#+\s+(.*)$", r"*\1*", summary, flags=re.MULTILINE)
        slack = slack.replace("**", "*")
        return f"*{title} - Notes*\n\n{slack}"
    return summary
