"""Live captions (captions.py; founder 2026-11-11): the words kept for the caption window - two sides that never mix,
two lines a side that roll up, filler words out, translated a sentence at a time, whose words switchable mid-meeting.
Readable (founder 2026-11-13): every word written at reading pace, the start of a phrase never at once, a new sentence
on a new line, captions kept longer, a size to pick."""
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


def _words(view_lines):
    return [w for _i, ws in view_lines for w in ws]


def _wait(thing, clock, seconds, step=0.05):
    """Time passing with the caption window looking every ``step`` (as it does while words wait)."""
    end = clock.t + seconds
    while clock.t < end - 1e-9:
        clock.t = min(end, clock.t + step)
        thing.view()
    return thing.view()


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

    def test_a_new_sentence_starts_on_a_new_line_when_the_line_is_half_full(self):
        lines, _ = cap.place([], "We looked at the plan for the new version today. Then".split(), 1)
        self.assertEqual(lines, [(1, "We looked at the plan for the new version today.".split()), (2, ["Then"])])
        lines, _ = cap.place([], "Yes. I agree with that".split(), 1)          # a short "Yes." keeps the words beside it
        self.assertEqual(lines, [(1, "Yes. I agree with that".split())])
        self.assertTrue(cap.ends_sentence("you?") and cap.ends_sentence("այո։") and cap.ends_sentence('it."'))
        self.assertFalse(cap.ends_sentence("so,") or cap.ends_sentence("Mr"))

    def test_the_sides(self):
        self.assertEqual(cap.side_of("You"), "me")
        for who in ("Them", "Speaker 2", "", None):
            self.assertEqual(cap.side_of(who), "them", who)
        self.assertTrue(cap.shows("both", "me") and cap.shows("both", "them"))
        self.assertTrue(cap.shows("them", "them") and not cap.shows("them", "me"))
        self.assertTrue(cap.shows("odd value", "me"))                                # unknown = both

    def test_the_sizes(self):
        self.assertEqual([k for k, _n, _s in cap.SIZES], ["s", "m", "l", "xl"])
        self.assertEqual(cap.size_scale(cap.DEFAULT_SIZE), 1.0)
        self.assertGreater(cap.size_scale("xl"), cap.size_scale("l") > 1.0)
        self.assertEqual(cap.size_scale("huge"), 1.0)                               # unknown = Medium


class TestLane(unittest.TestCase):
    def test_two_lines_at_most_rolling_up_with_ids_that_stay(self):
        clock = Clock()
        lane = cap.Lane(clock)
        n = (cap.MAX_CHARS + 1) // 5                          # "word " fills a line
        lane.final(["word"] * n)
        first = _wait(lane, clock, n / 2.5)
        for name in ("more", "next", "last"):
            lane.final([name] * n)
        view = _wait(lane, clock, 3 * n / 2.5)
        self.assertEqual(len(view), cap.MAX_LINES)
        self.assertNotIn(first[0][0], [i for i, _ in view])  # the oldest rolled out at the top
        self.assertEqual(view[-1][1][0], "last")

    def test_the_sentence_being_said_follows_the_finished_ones_and_is_replaced_by_its_end(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.final(["Hello."])
        lane.partial(["How", "are"])
        self.assertEqual(_wait(lane, clock, 1), [(1, ["Hello.", "How", "are"])])
        lane.final(["How", "are", "you?"])
        self.assertEqual(lane.live, [])
        self.assertEqual(lane.view(), [(1, ["Hello.", "How", "are"])])      # the words on screen stay where they are
        self.assertEqual(_wait(lane, clock, 0.5), [(1, ["Hello.", "How", "are", "you?"])])

    def test_it_fades_after_a_quiet_moment_and_starts_on_a_new_line(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.final(["One."])
        self.assertTrue(lane.view())
        clock.t += cap.HOLD_S + 0.1
        self.assertEqual(lane.view(), [])
        lane.final(["Two."])
        self.assertEqual([ws for _i, ws in lane.view()], [["Two."]])

    def test_captions_stay_longer_than_before(self):
        self.assertGreaterEqual(cap.HOLD_S, 6.0)                 # founder 2026-11-13: "keep a bit longer"

    def test_the_hold_counts_from_the_last_word_written_not_from_when_it_came(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.queue("one two three four five six".split())
        _wait(lane, clock, 6 / cap.READ_WPS)                     # all written by now
        self.assertEqual(lane.backlog(), 0)
        _wait(lane, clock, cap.HOLD_S - 0.3, step=0.5)
        self.assertTrue(lane.view())


class TestPacing(unittest.TestCase):
    """Every word comes out at reading pace, one after another, in the order said (founder 2026-11-13: "people cannot
    read it ... the first sentence of every phrase comes like a burst")."""

    def test_a_piece_comes_out_word_by_word_at_reading_pace(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.queue("one two three four five six seven".split())
        self.assertEqual(lane.view(), [(1, ["one"])])                    # the first word at once
        shown = _words(_wait(lane, clock, 1.05))
        self.assertEqual(len(shown), 1 + int(1.05 * cap.READ_WPS))      # then three a second
        self.assertEqual(" ".join(_words(_wait(lane, clock, 3))), "one two three four five six seven")

    def test_the_start_of_a_phrase_never_lands_all_at_once(self):
        """The live stream's first words of a phrase often come together (four or five at once)."""
        clock = Clock()
        lane = cap.Lane(clock)
        lane.partial("So the plan for today is".split())
        self.assertEqual(_words(lane.view()), ["So"])
        counts = []
        for _ in range(20):
            counts.append(len(_words(_wait(lane, clock, 0.1, step=0.02))))
        self.assertTrue(all(b - a <= 1 for a, b in zip([1] + counts, counts)), counts)   # one word at a time
        self.assertEqual(_words(lane.view()), "So the plan for today is".split())

    def test_much_waiting_is_let_out_a_little_faster_never_in_a_heap(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.queue(["w%d" % i for i in range(60)])
        lane.view()
        out = len(_words(_wait(lane, clock, 4.0)))
        self.assertGreater(out, 1 + 4 * cap.READ_WPS)                    # faster than reading pace: it catches up
        self.assertLessEqual(out, 1 + 4 * cap.MAX_WPS + 1)              # but never faster than MAX_WPS
        while lane.backlog():                                            # nothing expires while words wait
            self.assertTrue(lane.view())
            _wait(lane, clock, 0.5)

    def test_the_pace(self):
        self.assertEqual(cap.Lane.rate(1), cap.READ_WPS)
        self.assertEqual(cap.Lane.rate(1000), cap.MAX_WPS)
        self.assertAlmostEqual(cap.Lane.rate(30), 30 / cap.CATCH_UP_S)
        # a finished piece (Best accuracy, ~25 s of talk) is written nearer reading pace: it has until the next one
        self.assertLess(cap.Lane.rate(70, cap.PIECE_CATCH_UP_S), cap.Lane.rate(70))
        self.assertGreaterEqual(cap.Lane.rate(70, cap.PIECE_CATCH_UP_S), 70 / 25.0)

    def test_after_a_busy_moment_the_waiting_words_do_not_come_in_a_heap(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.queue("one two three four five six seven eight".split())
        lane.view()
        clock.t += 3.0                                                   # the window could not look for 3 s
        self.assertLessEqual(len(_words(lane.view())), 3)
        self.assertEqual(len(_words(_wait(lane, clock, 3))), 8)

    def test_a_revised_live_word_keeps_its_place(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.partial(["I", "think", "we"])
        _wait(lane, clock, 1)
        lane.partial(["I", "think"])                                     # the stream took a word back
        self.assertEqual(_words(lane.view()), ["I", "think"])
        lane.partial(["I", "think", "they", "said"])
        self.assertEqual(_words(_wait(lane, clock, 1)), ["I", "think", "they", "said"])

    def test_pieces_and_live_sentences_are_both_paced(self):
        clock = Clock()
        feed, _cfg = _feed(clock=clock)
        feed.final("A long accurate piece of what was said.", "Them", "en", paced=True)
        self.assertEqual(_text(feed.view(), "them"), ["A"])
        feed.final("Right away.", "You")
        self.assertEqual(_text(feed.view(), "me"), ["Right"])
        self.assertEqual(_text(_wait(feed, clock, 0.4), "me"), ["Right away."])
        self.assertTrue(feed.pacing())                                   # the piece is still being written
        _wait(feed, clock, 4)
        self.assertFalse(feed.pacing())


class TestFeed(unittest.TestCase):
    def test_both_sides_never_mix_and_the_switch_hides_one(self):
        clock = Clock()
        feed, cfg = _feed(clock=clock)
        feed.final("Hello from the call.", "Them")
        feed.partial("And um me", "You")
        view = _wait(feed, clock, 2)
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
        clock = Clock()
        feed, _cfg = _feed({"captions_language": "en"}, translate, clock)
        feed.partial("Բարեւ", "Them")                      # a translation needs the whole sentence: not shown
        self.assertEqual(feed.view()["them"], [])
        feed.final("Բարեւ։", "Them", "hy")
        feed.final("Ինչպե՞ս ես։", "Them", "hy")
        self.assertEqual(feed.view()["them"], [])          # not translated yet
        feed.drain()
        self.assertEqual(_text(_wait(feed, clock, 3), "them"), ["Hello. How are you?"])
        self.assertEqual(asked[0], ("Բարեւ։", "hy", "en", []))
        self.assertEqual(asked[1][3], ["Բարեւ։"])          # context: the side's earlier sentence

    def test_captions_in_the_language_being_spoken_are_live_word_by_word(self):
        """Founder 2026-11-12: French captions of French speech were slow - they waited for whole sentences."""
        asked = []
        clock = Clock()
        cfg = {"captions_who": "both", "captions_language": "fr"}
        feed = cap.CaptionFeed(lambda: cfg, translate=lambda *a: asked.append(a) or "x", clock=clock,
                               start_thread=False, spoken_language=lambda: "fr")
        feed.partial("Bonjour à", "Them")
        self.assertEqual(_text(feed.view(), "them"), ["Bonjour"])                       # the first word at once
        self.assertEqual(_text(_wait(feed, clock, 0.4), "them"), ["Bonjour à"])         # then the next
        feed.final("Bonjour à tous.", "Them", "fr")
        self.assertEqual(_text(_wait(feed, clock, 0.4), "them"), ["Bonjour à tous."])
        self.assertEqual(asked, [])

    def test_the_language_heard_decides_when_the_meeting_language_is_detected(self):
        clock = Clock()
        cfg = {"captions_who": "both", "captions_language": "fr"}
        feed = cap.CaptionFeed(lambda: cfg, translate=lambda *a: "Bonjour.", clock=clock, start_thread=False,
                               spoken_language=lambda: "")
        feed.partial("Hello", "Them")
        self.assertEqual(feed.view()["them"], [])                           # not known yet: wait for the sentence
        feed.final("Bonjour tout le monde.", "Them", "fr")                  # heard in French
        feed.partial("Ça va", "Them")
        self.assertIn("Ça va", " ".join(_text(_wait(feed, clock, 3), "them")))

    def test_a_translated_sentence_is_written_at_reading_pace_too(self):
        clock = Clock()
        feed, _cfg = _feed({"captions_language": "en"}, lambda *a: "one two three four five six seven eight", clock)
        feed.final("Մեկ երկու։", "Them", "hy")
        feed.drain()
        self.assertEqual(len(" ".join(_text(feed.view(), "them")).split()), 1)
        self.assertEqual(len(" ".join(_text(_wait(feed, clock, 0.6), "them")).split()), 2)
        self.assertEqual(len(" ".join(_text(_wait(feed, clock, 2.0), "them")).split()), 8)

    def test_privacy_mode_never_sends_a_sentence_to_translate(self):
        asked = []
        clock = Clock()
        feed, cfg = _feed({"captions_language": "fr", "privacy_mode": True}, lambda *a: asked.append(a) or "x", clock)
        feed.final("Hello there.", "Them", "en")
        feed.drain()
        self.assertEqual(asked, [])
        self.assertEqual(_text(_wait(feed, clock, 1), "them"), ["Hello there."])            # as spoken

    def test_a_sentence_already_in_that_language_is_not_sent(self):
        asked = []
        clock = Clock()
        feed, _cfg = _feed({"captions_language": "en"}, lambda *a: asked.append(a) or "x", clock)
        feed.final("Already English.", "Them", "en")
        self.assertEqual(asked, [])
        self.assertEqual(_text(_wait(feed, clock, 0.4), "them"), ["Already English."])

    def test_a_failed_translation_shows_the_sentence_as_spoken(self):
        def boom(*_a):
            raise RuntimeError("down")
        clock = Clock()
        feed, _cfg = _feed({"captions_language": "fr"}, boom, clock)
        feed.final("Hello there.", "You", "en")
        feed.drain()
        self.assertEqual(_text(_wait(feed, clock, 2), "me"), ["Hello there."])

    def test_behind_the_call_the_oldest_sentences_are_dropped(self):
        clock = Clock()
        feed, _cfg = _feed({"captions_language": "fr"}, lambda text, *a: text.upper(), clock)
        for i in range(cap.TRANSLATE_BACKLOG + 2):
            feed.final("sentence %d" % i, "Them", "en-x")
        feed.drain()
        shown = " ".join(_text(_wait(feed, clock, 4), "them"))
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


class TestOneSidePerSentence(unittest.TestCase):
    def test_a_sentence_never_shows_on_both_sides(self):
        """The partial's side comes from which source is loud, the finished sentence's from the speaker labels: a
        second voice at your microphone is "Speaker 1" (the call's side) - the words must not stay on yours too."""
        clock = Clock()
        feed, _cfg = _feed(clock=clock)
        feed.partial("Hello every", "You")                       # loud on the mic
        feed.final("Hello everyone.", "Speaker 1", "en")         # labelled a second voice
        self.assertEqual(feed.view()["me"], [])
        self.assertEqual(_text(_wait(feed, clock, 0.5), "them"), ["Hello everyone."])
        feed.partial("And", "Them")
        feed.partial("And now", "You")                           # the loud side flipped mid-sentence
        self.assertEqual(feed.view()["them"][-1][1][-1], "everyone.")   # only the finished words stay there

    def test_busy_only_while_something_is_shown_or_waiting(self):
        clock = Clock()
        feed, _cfg = _feed(clock=clock)
        self.assertFalse(feed.busy())
        feed.final("One.", "Them")
        self.assertTrue(feed.busy())                             # (the window looks: "One." is written)
        clock.t += cap.HOLD_S + 0.1
        self.assertFalse(feed.busy())
        feed.final("A long piece of words here.", "Them", paced=True)
        self.assertTrue(feed.busy())


if __name__ == "__main__":
    unittest.main()
