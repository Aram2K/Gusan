"""The decisions of the voice-print speaker engine (voice_prints.py): which stretches of the
call are embedded, how the voice prints are grouped into people, and what is folded away.

Pure Python on purpose: no model, no audio, no numpy needed, so every CI run checks all of it
(the numpy path of the similarities is compared with the plain one only where numpy is real).
"""
import itertools
import math
import random
import unittest

import voice_prints as vp


def them(a, b):
    return {"start": float(a), "end": float(b), "text": "x", "source": "them"}


def you(a, b):
    return {"start": float(a), "end": float(b), "text": "x", "source": "you"}


def blob(rng, centre, n, spread=0.12):
    """``n`` noisy unit-ish vectors around a direction in a 12-dimensional space."""
    out = []
    for _ in range(n):
        out.append([c + rng.gauss(0.0, spread) for c in centre])
    return out


def axis(k, dim=12):
    return [1.0 if i == k else 0.0 for i in range(dim)]


class TestUnits(unittest.TestCase):
    def test_pieces_of_about_five_seconds_stay_single(self):
        segs = [them(0, 5), them(5, 10.1), them(12, 17)]
        self.assertEqual(vp.units_for(segs), [(0.0, 5.0), (5.0, 10.1), (12.0, 17.0)])

    def test_the_users_own_segments_are_never_used(self):
        segs = [you(0, 6), them(7, 12), you(12, 20)]
        self.assertEqual(vp.units_for(segs), [(7.0, 12.0)])
        self.assertEqual(vp.units_for([you(0, 9)]), [])

    def test_a_segment_without_a_source_counts_as_the_call(self):
        self.assertEqual(vp.units_for([{"start": 1, "end": 6, "text": "a"}]), [(1.0, 6.0)])

    def test_short_neighbours_are_joined_up_to_four_seconds(self):
        # 1 s + 1.5 s + 1.5 s, each within 0.8 s of the last: one stretch of 5.3 s... the join stops
        # once the stretch has reached four seconds, so a fourth neighbour starts a new one.
        segs = [them(0, 1), them(1.5, 3), them(3.4, 4.9), them(5.2, 6.2)]
        units = vp.units_for(segs)
        self.assertEqual(units[0], (0.0, 4.9))
        self.assertEqual(len(units), 1 + (1 if (6.2 - 5.2) >= vp.TINY_SEC else 0))

    def test_neighbours_further_apart_than_the_gap_are_not_joined(self):
        segs = [them(0, 2), them(3, 5.5)]                    # 1 s apart > 0.8 s
        self.assertEqual(vp.units_for(segs), [(0.0, 2.0), (3.0, 5.5)])

    def test_a_long_segment_is_cut_into_equal_pieces(self):
        three = vp.units_for([them(10, 27)])                 # 17 s
        self.assertEqual(len(three), 3)
        self.assertAlmostEqual(three[0][0], 10.0)
        self.assertAlmostEqual(three[-1][1], 27.0)
        lengths = [e - s for s, e in three]
        self.assertTrue(all(abs(x - 17 / 3.0) < 1e-9 for x in lengths))
        two = vp.units_for([them(0, 9)])                     # 9 s
        self.assertEqual([round(e - s, 3) for s, e in two], [4.5, 4.5])
        self.assertEqual(vp.units_for([them(0, 8)]), [(0.0, 8.0)])      # exactly the longest stays one

    def test_every_piece_of_a_cut_segment_is_at_least_four_seconds(self):
        for length in (8.01, 9, 12.5, 16, 16.01, 23.9, 40, 61.3):
            pieces = vp.units_for([them(0, length)])
            self.assertTrue(all(e - s >= vp.MIN_UNIT_SEC - 1e-9 for s, e in pieces), length)
            self.assertTrue(all(e - s <= vp.MAX_UNIT_SEC + 1e-9 for s, e in pieces), length)
            self.assertAlmostEqual(sum(e - s for s, e in pieces), length)

    def test_a_lone_stretch_under_a_second_and_a_half_is_left_out(self):
        self.assertEqual(vp.units_for([them(0, 1.0)]), [])
        self.assertEqual(vp.units_for([them(0, 1.4), them(10, 15)]), [(10.0, 15.0)])
        self.assertEqual(vp.units_for([them(0, 1.5)]), [(0.0, 1.5)])     # exactly the smallest stays

    def test_segments_are_used_in_time_order_whatever_the_input_order(self):
        segs = [them(20, 25), them(0, 5), them(10, 15)]
        self.assertEqual(vp.units_for(segs), [(0.0, 5.0), (10.0, 15.0), (20.0, 25.0)])

    def test_odd_segments_are_skipped_not_fatal(self):
        segs = [them(0, 5), {"start": "x", "end": 3}, {"start": float("nan"), "end": 4}, {"start": 9, "end": 9},
                {"start": 7, "end": 3}, None, "text", {"start": float("inf"), "end": 20}, them(30, 36)]
        self.assertEqual(vp.units_for(segs), [(0.0, 5.0), (30.0, 36.0)])
        self.assertEqual(vp.units_for(None), [])
        self.assertEqual(vp.units_for([]), [])

    def test_a_negative_start_is_clamped_to_zero(self):
        self.assertEqual(vp.units_for([them(-2, 5)]), [(0.0, 5.0)])

    def test_never_more_than_the_cap_spread_over_the_whole_call(self):
        segs = [them(i * 6, i * 6 + 5) for i in range(400)]
        units = vp.units_for(segs, max_units=100)
        self.assertEqual(len(units), 100)
        self.assertEqual(units[0], (0.0, 5.0))
        self.assertGreater(units[-1][0], 2300)                # reaches the end of the call, not just its start
        self.assertEqual(units, sorted(units))
        self.assertEqual(len(vp.units_for(segs)), 400)        # under the default cap: all of them


class TestSimilarity(unittest.TestCase):
    def test_cosine_of_known_vectors(self):
        m = vp.cosine_matrix([[1, 0], [0, 1], [2, 0], [1, 1]])
        self.assertEqual(len(m), 4)
        self.assertAlmostEqual(m[0][1], 0.0)
        self.assertAlmostEqual(m[0][2], 1.0)
        self.assertAlmostEqual(m[0][3], math.sqrt(0.5))
        self.assertTrue(all(m[i][j] == m[j][i] for i in range(4) for j in range(4)))
        self.assertTrue(all(m[i][i] == 1.0 for i in range(4)))

    def test_empty_and_zero_vectors_do_not_divide_by_zero(self):
        self.assertEqual(vp.cosine_matrix([]), [])
        m = vp.cosine_matrix([[0, 0], [1, 0]])
        self.assertEqual(m[0][1], 0.0)
        self.assertEqual(m[0][0], 1.0)

    def test_the_numpy_path_and_the_plain_path_give_the_same_numbers(self):
        if vp._numpy() is None:
            self.skipTest("real numpy not importable (stubbed)")
        rng = random.Random(7)
        vectors = [[rng.gauss(0, 1) for _ in range(16)] for _ in range(25)]
        fast = vp.cosine_matrix(vectors)
        plain = vp._cosine_pure(vectors)
        for i in range(25):
            for j in range(25):
                self.assertAlmostEqual(fast[i][j], plain[i][j], places=9)

    def test_trouble_with_numpy_falls_back_to_plain_python(self):
        from unittest import mock
        import sys
        broken = mock.MagicMock()
        broken.asarray.side_effect = RuntimeError("boom")
        with mock.patch.object(vp, "_numpy", return_value=broken):
            m = vp.cosine_matrix([[1, 0], [1, 1]])
        self.assertAlmostEqual(m[0][1], math.sqrt(0.5))
        self.assertIn("voice_prints", sys.modules)


def brute_force(sim, threshold):
    """The textbook average linkage: always merge the most similar pair of clusters, until the best
    is under the threshold. O(n^3) - the reference the fast one is checked against."""
    clusters = [[i] for i in range(len(sim))]
    while len(clusters) > 1:
        best, pair = -2.0, None
        for a, b in itertools.combinations(range(len(clusters)), 2):
            s = sum(sim[i][j] for i in clusters[a] for j in clusters[b]) / float(len(clusters[a]) * len(clusters[b]))
            if s > best:
                best, pair = s, (a, b)
        if best < threshold:
            break
        a, b = pair
        clusters[a] = clusters[a] + clusters[b]
        del clusters[b]
    return {frozenset(c) for c in clusters}


def partition(labels):
    groups = {}
    for i, label in enumerate(labels):
        groups.setdefault(label, set()).add(i)
    return {frozenset(g) for g in groups.values()}


class TestAverageLinkage(unittest.TestCase):
    def test_it_equals_the_brute_force_search_on_random_points(self):
        for seed in range(12):
            rng = random.Random(seed)
            n = rng.randint(2, 30)
            vectors = [[rng.gauss(0, 1), rng.gauss(0, 1), rng.gauss(0, 1)] for _ in range(n)]
            sim = vp.cosine_matrix(vectors)
            for threshold in (-0.5, 0.0, 0.35, 0.7, 0.95):
                self.assertEqual(partition(vp.average_linkage(sim, threshold)), brute_force(sim, threshold),
                                 (seed, threshold))

    def test_it_equals_the_brute_force_search_on_the_shape_of_a_real_call(self):
        # one big voice and one small one, as in the call it was measured on
        rng = random.Random(3)
        vectors = blob(rng, axis(0), 40) + blob(rng, axis(1), 6)
        rng.shuffle(vectors)
        sim = vp.cosine_matrix(vectors)
        for threshold in (0.3, 0.35, 0.45):
            self.assertEqual(partition(vp.average_linkage(sim, threshold)), brute_force(sim, threshold))

    def test_ids_follow_first_appearance(self):
        sim = vp.cosine_matrix([[0, 1], [1, 0], [0, 1.1], [1, 0.1]])
        self.assertEqual(vp.average_linkage(sim, 0.5), [0, 1, 0, 1])

    def test_edges(self):
        self.assertEqual(vp.average_linkage([], 0.35), [])
        self.assertEqual(vp.average_linkage([[1.0]], 0.35), [0])
        self.assertEqual(vp.average_linkage(vp.cosine_matrix([[1, 0], [0, 1]]), 0.35), [0, 1])
        self.assertEqual(vp.average_linkage(vp.cosine_matrix([[1, 0], [1, 0.01]]), 0.35), [0, 0])

    def test_identical_vectors_with_ties_still_end(self):
        sim = vp.cosine_matrix([[1, 0]] * 9)
        self.assertEqual(vp.average_linkage(sim, 0.35), [0] * 9)

    def test_a_few_hundred_units_are_quick(self):
        import time
        rng = random.Random(5)
        vectors = blob(rng, axis(0, 32), 250, 0.1) + blob(rng, axis(1, 32), 50, 0.1)
        sim = vp.cosine_matrix(vectors)
        t0 = time.perf_counter()
        labels = vp.average_linkage(sim, 0.35)
        self.assertLess(time.perf_counter() - t0, 10.0)
        self.assertEqual(len(set(labels)), 2)


def secs(n, each=5.0):
    return [each] * n


class TestCluster(unittest.TestCase):
    def test_one_voice_is_one_cluster(self):
        rng = random.Random(1)
        vectors = blob(rng, axis(0), 30)
        ids = vp.cluster(vectors, secs(30))
        self.assertEqual(set(ids), {0})

    def test_two_voices_are_two_clusters_numbered_by_first_appearance(self):
        rng = random.Random(2)
        a, b = blob(rng, axis(0), 20), blob(rng, axis(1), 20)
        vectors = [v for pair in zip(b, a) for v in pair]          # the second voice speaks first
        ids = vp.cluster(vectors, secs(40))
        self.assertEqual(set(ids), {0, 1})
        self.assertEqual(ids[0], 0)                                # whoever comes first is 0
        self.assertEqual(ids[::2], [0] * 20)
        self.assertEqual(ids[1::2], [1] * 20)

    def test_a_stray_piece_is_folded_into_the_nearest_voice(self):
        rng = random.Random(3)
        vectors = blob(rng, axis(0), 20) + blob(rng, axis(1), 20) + [axis(5)]
        ids = vp.cluster(vectors, secs(41))
        self.assertEqual(len(set(ids)), 2)                         # not 3: the odd one out is not a person
        self.assertIn(ids[-1], (0, 1))

    def test_a_cluster_of_two_pieces_under_ten_seconds_is_folded_in(self):
        rng = random.Random(4)
        vectors = blob(rng, axis(0), 25) + blob(rng, axis(1), 2)
        durations = secs(25) + [4.0, 4.0]                          # 8 s: not enough to be a person
        self.assertEqual(set(vp.cluster(vectors, durations)), {0})
        durations = secs(25) + [5.0, 5.0]                          # 10 s: enough
        self.assertEqual(len(set(vp.cluster(vectors, durations))), 2)

    def test_a_single_piece_cluster_is_folded_in_even_when_long(self):
        rng = random.Random(4)
        vectors = blob(rng, axis(0), 25) + [axis(1)]
        ids = vp.cluster(vectors, secs(25) + [30.0])
        self.assertEqual(set(ids), {0})

    def test_when_every_cluster_is_small_the_call_is_one_voice(self):
        ids = vp.cluster([axis(0), axis(1), axis(2)], secs(3))
        self.assertEqual(ids, [0, 0, 0])
        self.assertEqual(vp.cluster([axis(0)], [5.0]), [0])

    def test_pieces_without_a_voice_print_stay_without_one(self):
        rng = random.Random(6)
        good = blob(rng, axis(0), 10)
        vectors = [None] + good[:5] + [None, None] + good[5:]
        ids = vp.cluster(vectors, secs(len(vectors)))
        self.assertEqual([i is None for i in ids], [v is None for v in vectors])
        self.assertEqual({i for i in ids if i is not None}, {0})
        self.assertEqual(vp.cluster([None, None], [5, 5]), [None, None])
        self.assertEqual(vp.cluster([], []), [])
        self.assertEqual(vp.cluster([[], axis(0)], [5, 5]), [None, 0])

    def test_never_more_voices_than_the_cap(self):
        rng = random.Random(8)
        vectors = []
        for k in range(12):
            vectors += blob(rng, axis(k), 4, 0.05)
        ids = vp.cluster(vectors, secs(len(vectors), 6.0))
        self.assertLessEqual(len(set(ids)), vp.MAX_SPEAKERS)
        self.assertEqual(len(set(ids)), vp.MAX_SPEAKERS)           # merged down to exactly the cap
        self.assertEqual(set(ids), set(range(vp.MAX_SPEAKERS)))
        self.assertEqual(len(set(vp.cluster(vectors, secs(len(vectors), 6.0), max_speakers=3))), 3)

    def test_the_threshold_decides_how_alike_two_voices_may_be(self):
        rng = random.Random(9)
        close = [0.7 if i == 1 else (1.0 if i == 0 else 0.0) for i in range(12)]   # cosine about 0.82 to axis 0
        vectors = blob(rng, axis(0), 15, 0.03) + blob(rng, close, 15, 0.03)
        self.assertEqual(len(set(vp.cluster(vectors, secs(30), threshold=0.5))), 1)
        self.assertEqual(len(set(vp.cluster(vectors, secs(30), threshold=0.95))), 2)

    def test_bad_durations_do_not_crash_it(self):
        rng = random.Random(10)
        vectors = blob(rng, axis(0), 6)
        ids = vp.cluster(vectors, [5, "x", None, 5.0, 5, 5])
        self.assertEqual(len(ids), 6)
        ids = vp.cluster(vectors, [])
        self.assertEqual(len(ids), 6)

    def test_the_default_threshold_is_the_measured_one(self):
        self.assertEqual(vp.THRESHOLD, 0.35)
        self.assertEqual((vp.MIN_UNIT_SEC, vp.MAX_UNIT_SEC, vp.JOIN_GAP_SEC, vp.TINY_SEC), (4.0, 8.0, 0.8, 1.5))
        self.assertEqual((vp.MIN_CLUSTER_UNITS, vp.MIN_CLUSTER_SEC, vp.MAX_SPEAKERS), (2, 10.0, 8))


class TestTurns(unittest.TestCase):
    def test_turns_map_back_with_the_offset_and_skip_units_without_a_voice(self):
        units = [(0.0, 5.0), (6.0, 11.0), (12.0, 17.0)]
        self.assertEqual(vp.turns_for(units, [0, None, 1], offset=100.0),
                         [(100.0, 105.0, 0), (112.0, 117.0, 1)])
        self.assertEqual(vp.turns_for(units, [0, 0, 0]), [(0.0, 5.0, 0), (6.0, 11.0, 0), (12.0, 17.0, 0)])
        self.assertEqual(vp.turns_for([], []), [])

    def test_the_shares_are_numbers_for_the_log(self):
        units = [(0, 5), (5, 10), (10, 15), (15, 20)]
        self.assertEqual(vp.shares(units, [0, 0, 0, 1]), [0.75, 0.25])
        self.assertEqual(vp.shares(units, [None] * 4), [])
        self.assertEqual(vp.shares([], []), [])


class TestEngineSwitch(unittest.TestCase):
    def test_voices_is_the_default_and_one_variable_brings_the_old_engine_back(self):
        self.assertEqual(vp.engine({}), "voices")
        self.assertEqual(vp.engine({"GUSAN_SPEAKER_ENGINE": ""}), "voices")
        self.assertEqual(vp.engine({"GUSAN_SPEAKER_ENGINE": "windows"}), "windows")
        self.assertEqual(vp.engine({"GUSAN_SPEAKER_ENGINE": " Windows "}), "windows")
        self.assertEqual(vp.engine({"GUSAN_SPEAKER_ENGINE": "anything else"}), "voices")


class TestWholeShape(unittest.TestCase):
    def test_a_call_like_the_measured_one_gives_one_big_voice_and_one_small(self):
        # 299 pieces of 5 s: 285 of one voice and 14 of another (the shares measured on a real call)
        rng = random.Random(11)
        main, other = blob(rng, axis(0, 24), 285, 0.15), blob(rng, axis(1, 24), 14, 0.15)
        vectors = main[:100] + other[:5] + main[100:200] + other[5:] + main[200:]
        segs = [them(i * 5.4, i * 5.4 + 5) for i in range(299)]
        units = vp.units_for(segs)
        self.assertEqual(len(units), 299)
        ids = vp.cluster(vectors, [e - s for s, e in units])
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(vp.shares(units, ids), [0.95, 0.05])
        turns = vp.turns_for(units, ids)
        self.assertEqual(len(turns), 299)


if __name__ == "__main__":
    unittest.main()
