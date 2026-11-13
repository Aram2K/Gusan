"""Live captions (captions.py; founder 2026-11-11): the words kept for the caption window - two sides that never mix,
two lines a side that roll up, filler words out, translated a sentence at a time, whose words switchable mid-meeting."""
import unittest

import captions as cap


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def _feed(cfg=None, translate=None, clock=None):
    cfg = dict({"captions_who": "both", "captions_language": ""}, **(cfg or {}))
    feed = cap.CaptionFeed(lambda: cfg, translate=translate, clock=clock or Clock(), start_thread=False)
    return feed, cfg


def _text(view, side):
    return [" ".join(ws) for _i, ws in view[side]]


class TestWords(unittest.TestCase):
    def test_filler_words_are_left_out_like_dictations(self):
        self.assertEqual(cap.clean("So, um, today we'll, uh, look at it, you know."), "So, today we'll, look at it.")
        self.assertEqual(cap.clean("um, hello"), "hello")
        self.assertEqual(cap.clean("I like it", fillers=True), "I like it")          # "like" is a real word
        self.assertEqual(cap.clean("um hello", fillers=False), "um hello")

    def test_the_gap_mark_never_shows(self):
        self.assertEqual(cap.clean("[...]"), "")
        self.assertEqual(cap.clean("hello [...] there"), "hello there")

    def test_a_line_fills_while_it_fits_then_a_new_one(self):
        lines, nid = cap.place([], "one two three".split(), 1, max_chars=9)
        self.assertEqual(lines, [(1, ["one", "two"]), (2, ["three"])])
        self.assertEqual(nid, 3)
        lines, _ = cap.place([], ["supercalifragilistic", "a"], 1, max_chars=9)
        self.assertEqual(lines, [(1, ["supercalifragilistic"]), (2, ["a"])])        # a long word gets its own line

    def test_the_sides(self):
        self.assertEqual(cap.side_of("You"), "me")
        for who in ("Them", "Speaker 2", "", None):
            self.assertEqual(cap.side_of(who), "them", who)
        self.assertTrue(cap.shows("both", "me") and cap.shows("both", "them"))
        self.assertTrue(cap.shows("them", "them") and not cap.shows("them", "me"))
        self.assertTrue(cap.shows("odd value", "me"))                                # unknown = both


class TestLane(unittest.TestCase):
    def test_two_lines_at_most_rolling_up_with_ids_that_stay(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.final(("word " * 8).split())                    # 40 chars -> one line
        first = lane.view()
        lane.final(("more " * 8).split())
        lane.final(("last " * 8).split())
        view = lane.view()
        self.assertEqual(len(view), cap.MAX_LINES)
        self.assertNotIn(first[0][0], [i for i, _ in view])  # the oldest rolled out at the top
        self.assertEqual(view[-1][1][0], "last")

    def test_the_sentence_being_said_follows_the_finished_ones_and_is_replaced_by_its_end(self):
        lane = cap.Lane(Clock())
        lane.final(["Hello."])
        lane.partial(["How", "are"])
        self.assertEqual(lane.view(), [(1, ["Hello.", "How", "are"])])
        lane.final(["How", "are", "you?"])
        self.assertEqual(lane.view(), [(1, ["Hello.", "How", "are", "you?"])])
        self.assertEqual(lane.live, [])

    def test_it_fades_after_a_quiet_moment_and_starts_on_a_new_line(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.final(["One."])
        clock.t += cap.HOLD_S + 0.1
        self.assertEqual(lane.view(), [])
        lane.final(["Two."])
        self.assertEqual([ws for _i, ws in lane.view()], [["Two."]])


class TestPacing(unittest.TestCase):
    """Best accuracy's captions come from finished pieces (up to ~25 s of talk at once): let out at reading pace, so
    two lines never skip words, and faster when more is waiting, so they never fall far behind."""

    def test_a_piece_comes_out_word_by_word_at_reading_pace(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.queue("one two three four five six seven".split())
        self.assertEqual(lane.view(), [(1, ["one"])])                    # the first word at once
        clock.t += 1.0
        shown = lane.view()[-1][1]
        self.assertEqual(len(shown), 1 + int(1.0 * cap.PACE_WPS))        # then about 3.5 a second
        clock.t += 5.0
        self.assertEqual(" ".join(w for _i, ws in lane.view() for w in ws), "one two three four five six seven")

    def test_much_waiting_is_let_out_faster_and_nothing_expires_while_waiting(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.queue(["w%d" % i for i in range(60)])
        lane.view()
        clock.t += cap.CATCH_UP_S + 0.5
        lane.view()
        self.assertEqual(lane.pending, [])                               # all out within CATCH_UP_S
        self.assertTrue(lane.view())

    def test_pieces_from_the_feed_are_paced_live_sentences_are_not(self):
        clock = Clock()
        feed, _cfg = _feed(clock=clock)
        feed.final("A long accurate piece of what was said.", "Them", "en", paced=True)
        self.assertEqual(_text(feed.view(), "them"), ["A"])
        feed.final("Right away.", "You")
        self.assertEqual(_text(feed.view(), "me"), ["Right away."])


class TestFeed(unittest.TestCase):
    def test_both_sides_never_mix_and_the_switch_hides_one(self):
        feed, cfg = _feed()
        feed.final("Hello from the call.", "Them")
        feed.partial("And um me", "You")
        view = feed.view()
        self.assertEqual(_text(view, "them"), ["Hello from the call."])
        self.assertEqual(_text(view, "me"), ["And me"])
        cfg["captions_who"] = "them"                       # switched in the middle of the meeting
        self.assertEqual(feed.view()["me"], [])
        cfg["captions_who"] = "me"
        self.assertEqual(feed.view()["them"], [])
        self.assertEqual(_text(feed.view(), "me"), ["And me"])

    def test_translated_a_sentence_at_a_time_with_the_earlier_ones_as_context(self):
        asked = []

        def translate(text, source, target, context):
            asked.append((text, source, target, list(context)))
            return {"Բարեւ։": "Hello.", "Ինչպե՞ս ես։": "How are you?"}[text]
        feed, _cfg = _feed({"captions_language": "en"}, translate)
        feed.partial("Բարեւ", "Them")                      # a translation needs the whole sentence: not shown
        self.assertEqual(feed.view()["them"], [])
        feed.final("Բարեւ։", "Them", "hy")
        feed.final("Ինչպե՞ս ես։", "Them", "hy")
        self.assertEqual(feed.view()["them"], [])          # not translated yet
        feed.drain()
        clock = feed._clock
        clock.t += 5                                                      # translated sentences are paced too
        self.assertEqual(_text(feed.view(), "them"), ["Hello. How are you?"])
        self.assertEqual(asked[0], ("Բարեւ։", "hy", "en", []))
        self.assertEqual(asked[1][3], ["Բարեւ։"])          # context: the side's earlier sentence

    def test_captions_in_the_language_being_spoken_are_live_word_by_word(self):
        """Founder 2026-11-12: French captions of French speech were slow - they waited for whole sentences."""
        asked = []
        cfg = {"captions_who": "both", "captions_language": "fr"}
        feed = cap.CaptionFeed(lambda: cfg, translate=lambda *a: asked.append(a) or "x", clock=Clock(),
                               start_thread=False, spoken_language=lambda: "fr")
        feed.partial("Bonjour à", "Them")
        self.assertEqual(_text(feed.view(), "them"), ["Bonjour à"])         # the words being said, at once
        feed.final("Bonjour à tous.", "Them", "fr")
        self.assertEqual(_text(feed.view(), "them"), ["Bonjour à tous."])   # the sentence at once, not paced
        self.assertEqual(asked, [])

    def test_the_language_heard_decides_when_the_meeting_language_is_detected(self):
        cfg = {"captions_who": "both", "captions_language": "fr"}
        feed = cap.CaptionFeed(lambda: cfg, translate=lambda *a: "Bonjour.", clock=Clock(), start_thread=False,
                               spoken_language=lambda: "")
        feed.partial("Hello", "Them")
        self.assertEqual(feed.view()["them"], [])                           # not known yet: wait for the sentence
        feed.final("Bonjour tout le monde.", "Them", "fr")                  # heard in French
        feed.partial("Ça va", "Them")
        self.assertIn("Ça va", " ".join(_text(feed.view(), "them")))

    def test_a_translated_sentence_comes_out_quickly(self):
        clock = Clock()
        feed, _cfg = _feed({"captions_language": "en"}, lambda *a: "one two three four five six seven eight", clock)
        feed.final("Մեկ երկու։", "Them", "hy")
        feed.drain()
        feed.view()
        clock.t += 0.6
        self.assertEqual(len(" ".join(_text(feed.view(), "them")).split()), 8)   # ~12 words a second

    def test_privacy_mode_never_sends_a_sentence_to_translate(self):
        asked = []
        feed, cfg = _feed({"captions_language": "fr", "privacy_mode": True}, lambda *a: asked.append(a) or "x")
        feed.final("Hello there.", "Them", "en")
        feed.drain()
        self.assertEqual(asked, [])
        self.assertEqual(_text(feed.view(), "them"), ["Hello there."])            # as spoken

    def test_words_waiting_keep_their_pace(self):
        lane = cap.Lane(Clock())
        lane.queue(["a", "b", "c"])
        lane.queue(["d"], cap.TRANSLATED_WPS)
        self.assertEqual(lane.rate, cap.PACE_WPS)

    def test_a_sentence_already_in_that_language_is_not_sent(self):
        asked = []
        feed, _cfg = _feed({"captions_language": "en"}, lambda *a: asked.append(a) or "x")
        feed.final("Already English.", "Them", "en")
        self.assertEqual(asked, [])
        self.assertEqual(_text(feed.view(), "them"), ["Already English."])          # at once

    def test_a_failed_translation_shows_the_sentence_as_spoken(self):
        def boom(*_a):
            raise RuntimeError("down")
        feed, _cfg = _feed({"captions_language": "fr"}, boom)
        feed.final("Hello there.", "You", "en")
        feed.drain()
        feed._clock.t += 5
        self.assertEqual(_text(feed.view(), "me"), ["Hello there."])

    def test_behind_the_call_the_oldest_sentences_are_dropped(self):
        feed, _cfg = _feed({"captions_language": "fr"}, lambda text, *a: text.upper())
        for i in range(cap.TRANSLATE_BACKLOG + 2):
            feed.final("sentence %d" % i, "Them", "en-x")
        feed.drain()
        feed._clock.t += 10
        shown = " ".join(_text(feed.view(), "them"))
        self.assertNotIn("SENTENCE 0", shown)
        self.assertIn("SENTENCE %d" % (cap.TRANSLATE_BACKLOG + 1), shown)

    def test_a_translation_for_a_language_no_longer_picked_is_dropped(self):
        feed, cfg = _feed({"captions_language": "fr"}, lambda *a: "Bonjour.")
        feed.final("Hello.", "Them", "en")
        cfg["captions_language"] = "de"
        feed.drain()
        self.assertEqual(feed.view()["them"], [])

    def test_it_says_when_its_words_changed_and_stops_when_closed(self):
        seen = []
        feed, _cfg = _feed()
        feed.on_change = lambda: seen.append(1)
        feed.final("One.", "Them")
        self.assertEqual(seen, [1])
        feed.close()
        feed.final("Two.", "Them")
        self.assertEqual(seen, [1])


if __name__ == "__main__":
    unittest.main()


class TestOneSidePerSentence(unittest.TestCase):
    def test_a_sentence_never_shows_on_both_sides(self):
        """The partial's side comes from which source is loud, the finished sentence's from the speaker labels: a
        second voice at your microphone is "Speaker 1" (the call's side) - the words must not stay on yours too."""
        feed, _cfg = _feed()
        feed.partial("Hello every", "You")                       # loud on the mic
        feed.final("Hello everyone.", "Speaker 1", "en")         # labelled a second voice
        self.assertEqual(feed.view()["me"], [])
        self.assertEqual(_text(feed.view(), "them"), ["Hello everyone."])
        feed.partial("And", "Them")
        feed.partial("And now", "You")                           # the loud side flipped mid-sentence
        self.assertEqual(feed.view()["them"][-1][1][-1], "everyone.")   # only the finished words stay there

    def test_busy_only_while_something_is_shown_or_waiting(self):
        clock = Clock()
        feed, _cfg = _feed(clock=clock)
        self.assertFalse(feed.busy())
        feed.final("One.", "Them")
        self.assertTrue(feed.busy())
        clock.t += cap.HOLD_S + 0.1
        self.assertFalse(feed.busy())
        feed.final("A long piece of words here.", "Them", paced=True)
        self.assertTrue(feed.busy())
