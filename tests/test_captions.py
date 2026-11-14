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

    def test_one_flow_lines_roll_up_one_at_a_time(self):
        """Founder 2026-11-14: "it's removing everything and writing everything back" - a new line for every sentence
        swapped blocks of lines; roll-up captions flow on."""
        lines, _ = cap.place([], "We looked at the plan for the new version today. Then".split(), 1)
        self.assertEqual(lines, [(1, "We looked at the plan for the new version today. Then".split())])
        self.assertTrue(cap.ends_sentence("you?") and cap.ends_sentence("այո։") and cap.ends_sentence('it."'))
        self.assertFalse(cap.ends_sentence("so,") or cap.ends_sentence("Mr"))
        self.assertTrue(cap.ends_clause("well,") and cap.ends_clause("now.") and not cap.ends_clause("so"))

    def test_the_sides(self):
        self.assertEqual(cap.side_of("You"), "me")
        for who in ("Them", "Speaker 2", "", None):
            self.assertEqual(cap.side_of(who), "them", who)
        self.assertTrue(cap.shows("both", "me") and cap.shows("both", "them"))
        self.assertTrue(cap.shows("them", "them") and not cap.shows("them", "me"))
        self.assertTrue(cap.shows("odd value", "me"))                                # unknown = both

    def test_the_sizes(self):
        # a slider in steps of 10 %; 100 = the size captions always had (founder 2026-11-14: "maybe a slider")
        self.assertEqual(cap.size_scale(cap.DEFAULT_SIZE), 1.0)
        self.assertEqual(cap.size_percent(133), 130)
        self.assertEqual((cap.size_percent(10), cap.size_percent(999)), (cap.SIZE_MIN, cap.SIZE_MAX))
        self.assertEqual(cap.size_percent("120"), 120)
        for bad in ("huge", None, True, float("nan")):
            self.assertEqual(cap.size_percent(bad), 100, bad)
        # what 2.1.7 saved still reads
        self.assertEqual([cap.size_percent(k) for k in ("s", "m", "l", "xl")], [80, 100, 130, 160])


class TestLane(unittest.TestCase):
    def test_lines_roll_up_with_ids_that_stay(self):
        clock = Clock()
        lane = cap.Lane(clock)
        n = (cap.MAX_CHARS + 1) // 5                          # "word " fills a line
        lane.final(["word"] * n)
        first = lane.view()
        for name in ("more", "next", "last"):
            lane.final([name] * n)
        view = _wait(lane, clock, 3 * cap.MAX_WAIT_S)
        self.assertEqual(len(view), cap.MAX_LINES)
        self.assertNotIn(first[0][0], [i for i, _ in view])  # the oldest rolled out at the top
        self.assertEqual(view[-1][1][0], "last")

    def test_the_sentence_being_said_follows_the_finished_ones_and_is_replaced_by_its_end(self):
        lane = cap.Lane(Clock())
        lane.final(["Hello."])
        lane.partial(["How", "are"])
        self.assertEqual(lane.view(), [(1, ["Hello.", "How", "are"])])
        lane.final(["How", "are", "you?"])
        self.assertEqual(lane.live, [])
        self.assertEqual(lane.view(), [(1, ["Hello.", "How", "are", "you?"])])

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

    def test_a_revised_live_word_keeps_its_place(self):
        lane = cap.Lane(Clock())
        lane.partial(["I", "think", "we"])
        lane.view()
        lane.partial(["I", "think"])                                     # the stream took a word back
        self.assertEqual(_words(lane.view()), ["I", "think"])
        lane.partial(["I", "think", "they", "said"])
        self.assertEqual(_words(lane.view()), ["I", "think", "they", "said"])


class TestTenSecondsALine(unittest.TestCase):
    """Founder 2026-11-14: "time sensitive ... it should disappear if there is no new sentence coming ... the limit
    should be 10 seconds for each sentence"."""

    def test_a_line_leaves_10_s_after_it_last_changed_even_with_newer_lines_on_screen(self):
        clock = Clock()
        lane = cap.Lane(clock)
        n = (cap.MAX_CHARS + 1) // 5                                    # (a full line types in, ~0.8 s)
        lane.queue(["aaaa"] * n)
        _wait(lane, clock, 1.0)                                          # "aaaa" complete at ~100.8
        clock.t += 5
        lane.queue(["bbbb"] * n)
        self.assertEqual([ws[0] for _i, ws in _wait(lane, clock, 1.0)], ["aaaa", "bbbb"])   # "bbbb" at ~106.8
        clock.t = 100.8 + cap.LINE_TTL_S + 0.3                           # "aaaa" 10 s old, "bbbb" 4 s
        self.assertEqual([ws[0] for _i, ws in lane.view()], ["bbbb"])
        clock.t += 7
        self.assertEqual(lane.view(), [])

    def test_the_sentence_being_said_does_not_expire_while_it_grows(self):
        clock = Clock()
        lane = cap.Lane(clock)
        for i in range(30):
            lane.partial(["w%d" % k for k in range(i + 1)][-8:])        # changes every 0.5 s for 15 s
            lane.view()
            clock.t += 0.5
        self.assertTrue(lane.view())


class TestTranslatedInPhrases(unittest.TestCase):
    """Founder 2026-11-14: "very slow ... I want it tack, tack, tack" - translated captions waited for the speaker
    to finish the sentence; now settled live words go to translation a phrase at a time."""

    def _feed(self):
        asked = []
        clock = Clock()
        cfg = {"captions_who": "both", "captions_language": "fr"}
        feed = cap.CaptionFeed(lambda: cfg, translate=lambda text, src, tgt, ctx: asked.append((text, list(ctx)))
                               or text.upper(), clock=clock, start_thread=False, spoken_language=lambda: "en")
        return feed, asked, clock

    def test_settled_words_go_as_a_phrase_before_the_sentence_ends(self):
        feed, asked, clock = self._feed()
        said = "so the plan for the new release is to ship it on tuesday".split()
        for k in range(2, len(said) + 1):
            feed.partial(" ".join(said[:k]), "Them")                 # the live stream, a word at a time
        feed.drain()
        self.assertTrue(asked, "a phrase went before the end of the sentence")
        self.assertEqual(asked[0][0].split(), said[:cap.PHRASE_WORDS])
        shown = " ".join(_text(_wait(feed, clock, 1.0), "them"))
        self.assertIn("SO THE PLAN", shown)                           # on screen while the sentence goes on
        feed.final(" ".join(said) + ".", "Them", "en")
        feed.drain()
        sent = " ".join(t for t, _c in asked).split()
        self.assertEqual([w.strip(".") for w in sent], said)          # every word once - none twice, none lost
        self.assertEqual(asked[1][1], [asked[0][0]])                  # the phrase before is the context

    def test_a_comma_ends_a_short_phrase(self):
        feed, asked, _clock = self._feed()
        for text in ("well", "well I think", "well I think so,", "well I think so, but"):
            feed.partial(text, "Them")
        feed.drain()
        self.assertEqual([t for t, _c in asked], ["well I think so,"])

    def test_words_still_changing_are_not_sent(self):
        feed, asked, _clock = self._feed()
        for text in ("one two three four five six", "one two three four five sticks seven"):
            feed.partial(text, "Them")
        feed.drain()
        self.assertEqual(asked, [])                                   # "six" became "sticks": only 5 settled
        feed.partial("one two three four five sticks seven eight", "Them")
        feed.drain()
        self.assertEqual(asked[0][0], "one two three four five sticks seven")   # the same twice: settled

    def test_a_sentence_with_no_phrase_goes_whole_at_its_end(self):
        feed, asked, _clock = self._feed()
        feed.partial("Hello", "Them")
        feed.final("Hello there.", "Them", "en")
        feed.drain()
        self.assertEqual([t for t, _c in asked], ["Hello there."])


class TestNeverLateOnPurpose(unittest.TestCase):
    """Founder 2026-11-14: "I hope it's not purposefully delaying it ... it should really be catching up with the
    words ... if you have the full sentence, just show the full sentence, but keep it" - a word is on screen the
    moment it is known; words wait only so a line is not pushed off before it could be read."""

    def test_a_finished_sentence_piece_or_translation_shows_whole_at_once(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.queue("one two three four five six seven".split())
        self.assertEqual(_words(lane.view()), "one two three four five six seven".split())
        feed, _cfg = _feed(clock=clock)
        feed.final("A long accurate piece of what was said.", "Them", "en", paced=True)
        self.assertEqual(_text(feed.view(), "them"), ["A long accurate piece of what was said."])
        feed.final("Right away.", "You")
        self.assertEqual(_text(feed.view(), "me"), ["Right away."])
        self.assertFalse(feed.pacing())

    def test_the_words_being_said_show_as_they_come(self):
        lane = cap.Lane(Clock())
        lane.partial("So the plan for today is".split())                 # the stream's first words come together
        self.assertEqual(_words(lane.view()), "So the plan for today is".split())

    def test_a_line_is_never_pushed_off_before_it_could_be_read(self):
        clock = Clock()
        lane = cap.Lane(clock)
        n = (cap.MAX_CHARS + 1) // 5                                     # 4-letter words: one full line
        for name in ("aaaa", "bbbb", "cccc"):                            # three full lines, just shown
            lane.queue([name] * n)
            _wait(lane, clock, 0.02)
        _wait(lane, clock, 3 * n / cap.TYPE_WPS + 0.1)                    # (typed in)
        done = clock.t
        lane.queue(["dddd"] * n)                                          # a fourth would push "aaaa" off unread
        _wait(lane, clock, 0.3)
        view = lane.view()
        self.assertEqual(view[0][1][0], "aaaa")
        self.assertEqual(lane.backlog(), n)
        clock.t = done + cap.MAX_WAIT_S + 0.01                            # it may wait no longer
        view = _wait(lane, clock, n / cap.TYPE_WPS + 0.3)
        self.assertEqual([ws[0] for _i, ws in view], ["bbbb", "cccc", "dddd"])
        self.assertEqual(lane.backlog(), 0)

    def test_a_word_never_waits_longer_than_MAX_WAIT_S(self):
        clock = Clock()
        lane = cap.Lane(clock)
        n = (cap.MAX_CHARS + 1) // 5
        lane.queue(["x%d" % i for i in range(3 * n)])
        lane.view()
        lane.queue(["late"])
        for _ in range(int(cap.MAX_WAIT_S / 0.05) + 2):                  # the lines keep changing...
            clock.t += 0.05
            lane.partial(["y"] * (clock.t > 0))
            lane.view()
        self.assertIn("late", _words(lane.view()))

    def test_the_pace_of_reading(self):
        self.assertEqual(cap.Lane.read_s(["a"]), cap.MIN_LINE_S)
        self.assertAlmostEqual(cap.Lane.read_s(["a"] * 12), 12 / cap.READ_WPS)

    def test_the_hold_counts_from_the_last_word_shown(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.queue("one two three".split())
        lane.view()
        clock.t += cap.HOLD_S - 0.3
        lane.partial(["and"])
        lane.view()
        clock.t += cap.HOLD_S - 0.3
        self.assertTrue(lane.view())                                      # the last word was shown later


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
        self.assertEqual(_text(feed.view(), "them"), ["Bonjour à"])                     # as they are said
        feed.final("Bonjour à tous.", "Them", "fr")
        self.assertEqual(_text(feed.view(), "them"), ["Bonjour à tous."])
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

    def test_a_translated_sentence_shows_whole_the_moment_it_is_translated(self):
        clock = Clock()
        feed, _cfg = _feed({"captions_language": "en"}, lambda *a: "one two three four five six seven eight", clock)
        feed.final("Մեկ երկու։", "Them", "hy")
        self.assertEqual(feed.view()["them"], [])                                 # (being translated)
        feed.drain()
        self.assertEqual(len(" ".join(_text(feed.view(), "them")).split()), 8)

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


class TestALineThatLeftStaysGone(unittest.TestCase):
    """Review 2026-11-14: as the lines of a long sentence reached their 10 s, earlier lines of it scrolled back in; a
    sentence the live stream never finished blinked back every 10 s."""

    def test_a_line_that_rolled_above_the_screen_never_comes_back(self):
        clock = Clock()
        lane = cap.Lane(clock)
        said = ["word%02d" % k for k in range(40)]                     # five lines of it
        for k in range(1, len(said) + 1):
            lane.partial(said[:k])
            lane.view()
            clock.t += 0.3
        lane.final(said)
        first = [i for i, _ws in lane.view()]
        self.assertEqual(len(first), cap.MAX_LINES)
        seen = [first]
        for _ in range(400):
            clock.t += 0.1
            ids = [i for i, _ws in lane.view()]
            if ids != seen[-1]:
                seen.append(ids)
        self.assertEqual(seen[-1], [])
        for ids in seen:                                               # always the lower lines of what was there
            self.assertEqual(ids, first[len(first) - len(ids):], seen)

    def test_lines_never_shown_do_not_take_the_place_of_one_that_expired(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.final(["word%02d" % k for k in range(40)])                # a finished sentence of five lines, at once
        first = [i for i, _ws in lane.view()]
        clock.t += cap.LINE_TTL_S + 0.5
        self.assertEqual(lane.view(), [], first)

    def test_a_sentence_nobody_finished_leaves_once_and_stays_gone(self):
        clock = Clock()
        feed, _cfg = _feed(clock=clock)
        feed.partial("so the next step is", "Them")                    # ... and the live stream stops
        flips, was = 0, bool(feed.view()["them"])
        self.assertTrue(was)
        for _ in range(400):                                           # 40 s of the caption window looking
            clock.t += 0.1
            feed.busy()
            now = bool(feed.view()["them"])
            flips += now != was
            was = now
        self.assertEqual((flips, was), (1, False))
        self.assertFalse(feed.busy())
        feed.partial("so the next step is", "Them")                    # the same words again: still gone
        self.assertEqual(feed.view()["them"], [])
        feed.partial("so the next step is to ship", "Them")            # it goes on: back, whole
        self.assertEqual(_text(feed.view(), "them"), ["so the next step is to ship"])


class TestWordsOnScreenStay(unittest.TestCase):
    """Review 2026-11-14: a finished sentence that did not fit wiped its words from the screen and typed them
    again; words being said could wait far longer than MAX_WAIT_S."""

    def _three_unread_lines(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.final(["aa"] * 60)                                        # three lines of 20 short words: 5 s to read
        self.assertEqual([len(ws) for _i, ws in lane.view()], [20, 20, 20])
        return lane, clock

    def test_the_words_being_said_never_wait_longer_than_MAX_WAIT_S(self):
        lane, clock = self._three_unread_lines()
        clock.t += 0.1
        lane.partial(["now", "this", "is", "new"])                     # a fourth line: the top one is not read yet
        self.assertNotIn("now", _words(_wait(lane, clock, cap.MAX_WAIT_S - 0.5)))
        self.assertIn("new", _words(_wait(lane, clock, 0.7)))          # (it needed 5 s to be read: not waited for)

    def test_a_finished_sentence_that_does_not_fit_keeps_its_words_on_screen(self):
        clock = Clock()
        lane = cap.Lane(clock)
        lane.final(["aa"] * 40)                                        # two unread lines
        lane.view()
        clock.t += 0.2
        live = ["bb"] * 15
        lane.partial(live)
        self.assertEqual(_words(lane.view())[-15:], live)              # on the third line, as it is said
        clock.t += 0.2
        lane.final(["bb"] * 15 + ["cc"] * 12)                          # its end needs a fourth line
        words = _words(lane.view())
        self.assertGreaterEqual(words.count("bb"), 15)                 # nothing wiped, nothing typed again
        self.assertEqual(words[:40], ["aa"] * 40)                      # ... and the unread line is still there
        self.assertEqual(_words(_wait(lane, clock, cap.MAX_WAIT_S + 1.0))[-12:], ["cc"] * 12)


class TestPhrasesOfOneSentence(unittest.TestCase):
    """Review 2026-11-14: the words a sentence already sent as phrases were counted, not matched, and the count
    outlived the sentence; its phrases and its end could land on different sides."""

    def _feed(self, heard="en"):
        asked = []
        clock = Clock()
        cfg = {"captions_who": "both", "captions_language": "fr"}
        feed = cap.CaptionFeed(lambda: cfg, translate=lambda text, src, tgt, ctx: asked.append(text) or text.upper(),
                               clock=clock, start_thread=False, spoken_language=lambda: heard)
        return feed, asked, clock, cfg

    @staticmethod
    def _say(feed, words, who="Them"):
        for k in range(1, len(words) + 1):
            feed.partial(" ".join(words[:k]), who)

    def test_the_next_sentence_keeps_its_first_words(self):
        feed, asked, clock, _cfg = self._feed()
        one = "nous allons maintenant parler du budget de cette annee".split()
        self._say(feed, one)                                           # heard as English so far: a phrase goes
        feed.final(" ".join(one) + ".", "Them", "fr")                  # ... but it was French, the caption language
        two = "so the next step is to send the contract tomorrow".split()
        self._say(feed, two)
        feed.final(" ".join(two) + ".", "Them", "en")
        feed.drain()
        sent = [w.strip(".") for w in " ".join(asked).split()]
        self.assertEqual(sent, one + two)                              # every word once: none lost, none twice
        shown = " ".join(_text(_wait(feed, clock, 3), "them"))
        self.assertEqual(shown.count("BUDGET"), 1)
        self.assertNotIn("budget", shown)                              # (not a second time as spoken)

    def test_a_revised_ending_is_matched_by_its_words(self):
        feed, asked, _clock, _cfg = self._feed()
        self._say(feed, "it costs twenty five dollars and that".split())
        feed.final("It costs 25 dollars and that is fine.", "Them", "en")
        feed.drain()
        self.assertEqual(asked, ["it costs twenty five dollars and", "that is fine."])
        self.assertEqual(cap.sent_upto("we are going to the the".split(), "We are going to the store.".split()), 5)
        self.assertEqual(cap.sent_upto("one two three".split(), "uno dos tres cuatro".split()), 3)   # by the count
        self.assertEqual(cap.sent_upto([], ["a"]), 0)

    def test_its_phrases_and_its_end_stay_on_one_side(self):
        feed, _asked, clock, _cfg = self._feed()
        said = "so the plan for the new release is to ship".split()
        self._say(feed, said, "You")                                   # loud on the microphone
        feed.final(" ".join(said) + " it.", "Speaker 1", "en")         # labelled a second voice (the call's side)
        feed.drain()
        view = _wait(feed, clock, 3)
        self.assertEqual(view["them"], [])
        self.assertEqual(" ".join(_text(view, "me")), (" ".join(said) + " it.").upper())

    def test_a_new_caption_language_starts_the_sentence_over(self):
        feed, asked, clock, cfg = self._feed()
        self._say(feed, "one two three four five six seven".split())   # a phrase is on its way ...
        item = feed._queue.popleft()
        cfg["captions_language"] = "de"
        feed.clear()                                                   # (the controller, on a language change)
        feed._translate_one(*item)                                     # ... and comes back after it
        self.assertEqual(feed.view()["them"], [])
        del asked[:]
        feed.final("Fresh start here.", "Them", "en")
        feed.drain()
        self.assertEqual(asked, ["Fresh start here."])                 # whole: nothing counted as already sent
        self.assertEqual(_text(_wait(feed, clock, 2), "them"), ["FRESH START HERE."])


class TestSeveralTranslationsAtOnce(unittest.TestCase):
    """Review 2026-11-14: one translation at a time (about 2 s each) could not keep up with a phrase every 2 s."""

    def test_they_overlap_and_still_show_in_the_order_said(self):
        import threading
        import time
        lock, running, most = threading.Lock(), [0], [0]

        def translate(text, *_a):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            time.sleep(0.4 if text.startswith("one") else 0.05)      # the first one is the slowest
            with lock:
                running[0] -= 1
            return text.upper()
        cfg = {"captions_who": "both", "captions_language": "fr"}
        feed = cap.CaptionFeed(lambda: cfg, translate=translate)
        try:
            self.assertEqual(len(feed._threads), cap.TRANSLATE_WORKERS)
            for text in ("one.", "two.", "three."):
                feed.final(text, "Them", "en")
            end = time.monotonic() + 5
            while feed._out < 3 and time.monotonic() < end:
                time.sleep(0.02)
            self.assertEqual(feed._out, 3)
            self.assertGreaterEqual(most[0], 2)
            end = time.monotonic() + 3
            while " ".join(_text(feed.view(), "them")) != "ONE. TWO. THREE." and time.monotonic() < end:
                time.sleep(0.02)
            self.assertEqual(" ".join(_text(feed.view(), "them")), "ONE. TWO. THREE.")
        finally:
            feed.close()
        for t in feed._threads:
            t.join(2)
            self.assertFalse(t.is_alive())

    def test_one_that_came_back_early_waits_for_the_ones_before_it(self):
        clock = Clock()
        feed, _cfg = _feed({"captions_language": "fr"}, lambda text, *a: text.upper(), clock)
        feed.final("one.", "Them", "en")
        feed.final("two.", "Them", "en")
        first, second = feed._queue.popleft(), feed._queue.popleft()
        feed._translate_one(*second)
        self.assertEqual(feed.view()["them"], [])
        self.assertTrue(feed.busy())                                   # (still on its way)
        feed._translate_one(*first)
        self.assertEqual(" ".join(_text(_wait(feed, clock, 1), "them")), "ONE. TWO.")


if __name__ == "__main__":
    unittest.main()
