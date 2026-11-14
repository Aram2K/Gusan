#!/usr/bin/env python3
"""Writes the meeting-notes prompt the Android app sends, from the desktop's notes_prompt.py.

    python3 mobile/android/scripts/export_notes_prompt.py          # write the files
    python3 mobile/android/scripts/export_notes_prompt.py --check  # fail when they are out of date (CI)

Two files come out:
  core/src/main/resources/gusan/notes_prompt.json   the instruction with {{SUMMARY}} {{DISCUSSION}} {{LENGTH}}
                                                    {{LANGUAGE_RULE}} in place of the parts that depend on the meeting,
                                                    and the title instruction
  core/src/test/resources/gusan/notes_parity.json   the prompts for a few transcripts, as the desktop writes them;
                                                    NotesPromptTest checks the Kotlin port gives the same text
"""
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, ROOT)

import meeting_titles  # noqa: E402
import notes_prompt  # noqa: E402


def template():
    plan = notes_prompt.Plan("x", 0, "{{SUMMARY}}", "", 0, 0, 0)
    saved = notes_prompt._discussion, notes_prompt._length, notes_prompt._language_rule
    notes_prompt._discussion = lambda p: "{{DISCUSSION}}"
    notes_prompt._length = lambda p: "{{LENGTH}}"
    notes_prompt._language_rule = lambda language: "{{LANGUAGE_RULE}}"
    try:
        return notes_prompt.cloud_instruction(plan, "")
    finally:
        notes_prompt._discussion, notes_prompt._length, notes_prompt._language_rule = saved


def samples():
    word = "budget review with the design team on Friday about pricing "
    texts = {  # name: (a piece of text, how many times it is repeated)
        "tiny": ("We agreed to ship on Friday.", 1),
        "short": (word, 40),
        "medium": (word, 150),
        "long": (word, 500),
        "very_long": (word, 1500),
        "armenian": ("Մենք պայմանավորվեցինք ուրբաթ օրը ուղարկել նոր տարբերակը։ ", 120),
        "chinese": ("我们同意在星期五发布新版本。", 200),
    }
    out = []
    for name, (unit, count) in texts.items():
        text = unit * count
        for language in ("", "English"):
            plan = notes_prompt.plan_for(text)
            content = notes_prompt.cloud_user_content(text, language=language)
            out.append({
                "name": name, "unit": unit, "count": count, "language": language,
                "tier": plan.tier, "words": plan.words, "lo": plan.lo, "hi": plan.hi, "max_tokens": plan.max_tokens,
                # The whole prompt would be megabytes: its start (up to the transcript), its end and a hash say the same.
                "head": content[:content.index("\n\nText:\n")],
                "tail": content[content.rindex("\n\n") + 2:],
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            })
    return out


def main():
    files = {
        os.path.join(HERE, "..", "core", "src", "main", "resources", "gusan", "notes_prompt.json"): {
            "instruction": template(),
            "title_instruction": meeting_titles.TITLE_INSTRUCTION,
            "title_input_chars": meeting_titles.TITLE_INPUT_CHARS,
        },
        os.path.join(HERE, "..", "core", "src", "test", "resources", "gusan", "notes_parity.json"): samples(),
    }
    stale = []
    for path, value in files.items():
        text = json.dumps(value, ensure_ascii=False, indent=1) + "\n"
        path = os.path.normpath(path)
        if "--check" in sys.argv:
            if not os.path.exists(path) or open(path, encoding="utf-8").read() != text:
                stale.append(os.path.relpath(path, ROOT))
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
    if stale:
        sys.exit("Out of date (run mobile/android/scripts/export_notes_prompt.py): " + ", ".join(stale))


if __name__ == "__main__":
    main()
