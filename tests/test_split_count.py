"""Predicted seconds, the greedy assignment and the shard count."""
import unittest

from test_ratchet import split


def cells_of(**cells):
    return {name: {f'{path}.py': seconds for path, seconds in records.items()}
            for name, records in cells.items()}


class PredictTest(unittest.TestCase):
    SIZES = {'a.py': 100, 'b.py': 200, 'c.py': 300, 'd.py': 1000, 'z.py': 0, 'gone.py': 10}

    def test_recorded_seconds_and_the_median_rate_for_unknown_files(self):
        # Rates over matched recorded nonzero files: 0.02, 0.005, 0.03 -> median 0.02.
        cells = cells_of(linux=dict(a=2, b=1, c=9, z=5, gone=100), mac=dict(d=1))
        files = ['a.py', 'b.py', 'c.py', 'd.py', 'z.py']
        self.assertEqual(split.predict(files, self.SIZES, cells, 'linux'),
                         {'a.py': 2.0, 'b.py': 1.0, 'c.py': 9.0, 'd.py': 20.0, 'z.py': 5.0})
        self.assertTrue(split.has_records(files, cells, 'linux'))

    def test_an_even_count_of_rates_takes_the_middle_mean(self):
        cells = cells_of(linux=dict(a=2, b=1))
        predictions = split.predict(['a.py', 'b.py', 'd.py'], self.SIZES, cells, 'linux')
        self.assertEqual(predictions['d.py'], 12.5)

    def test_without_a_matched_record_blob_size_is_the_weight(self):
        cells = cells_of(linux=dict(gone=100), mac=dict(a=7))
        files = ['a.py', 'b.py']
        for cell in ('linux', 'windows'):
            with self.subTest(cell=cell):
                self.assertEqual(split.predict(files, self.SIZES, cells, cell),
                                 {'a.py': 100.0, 'b.py': 200.0})
                self.assertFalse(split.has_records(files, cells, cell))

    def test_only_empty_recorded_files_predict_unknown_files_at_their_median(self):
        cells = cells_of(linux=dict(z=4))
        self.assertEqual(split.predict(['a.py', 'z.py'], self.SIZES, cells, 'linux'),
                         {'a.py': 4.0, 'z.py': 4.0})


class AssignTest(unittest.TestCase):
    def test_descending_greedy_onto_the_lowest_total(self):
        predictions = {'a': 5, 'b': 4, 'c': 3, 'd': 3, 'e': 1}
        self.assertEqual(split.assign(predictions, 2), [['a', 'd'], ['b', 'c', 'e']])

    def test_equal_predictions_are_taken_in_path_order(self):
        self.assertEqual(split.assign({'b': 2, 'a': 2}, 2), [['a'], ['b']])

    def test_equal_totals_go_to_the_lower_index(self):
        self.assertEqual(split.assign({'a': 3, 'b': 2, 'c': 1, 'd': 1}, 2),
                         [['a', 'd'], ['b', 'c']])
        self.assertEqual(split.assign({'only': 1}, 3), [['only'], [], []])

    def test_each_shard_is_sorted_by_path(self):
        self.assertEqual(split.assign({'z': 10, 'm': 1, 'a': 1}, 2), [['z'], ['a', 'm']])
        self.assertEqual(split.assign({'z': 10, 'm': 1, 'a': 1}, 1), [['a', 'm', 'z']])

    def test_every_file_lands_in_exactly_one_shard(self):
        predictions = {f'f{index}': float((index * 7) % 5) for index in range(23)}
        for count in (1, 2, 3, 5, 8, 23, 30):
            with self.subTest(count=count):
                shards = split.assign(predictions, count)
                self.assertEqual(len(shards), count)
                self.assertEqual(sorted(path for shard in shards for path in shard),
                                 sorted(predictions))


class CountForTest(unittest.TestCase):
    @staticmethod
    def files(count):
        return [f'f{index:03d}.py' for index in range(count)]

    def count(self, files, cells=None, names=(), **mode):
        return split.count_for(files, {path: 10 for path in files}, cells or {}, list(names),
                               **mode)

    def test_exactly_one_of_shards_and_max_minutes(self):
        for mode in ({}, {'shards': 2, 'max_minutes': 3}):
            with self.subTest(mode=mode):
                with self.assertRaises(split.ConfigError) as caught:
                    self.count(self.files(3), **mode)
                self.assertIn('shards', str(caught.exception))
                self.assertIn('max-minutes', str(caught.exception))

    def test_invalid_counts_and_limits(self):
        for mode, named in (({'shards': 0}, 'shards'), ({'max_minutes': 0}, 'max-minutes'),
                            ({'max_minutes': -1}, 'max-minutes'),
                            ({'max_minutes': float('nan')}, 'max-minutes'),
                            ({'max_minutes': float('inf')}, 'max-minutes')):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(split.ConfigError, named):
                    self.count(self.files(3), **mode)

    def test_shards_within_the_caps_is_taken_as_given(self):
        self.assertEqual(self.count(self.files(10), shards=3), (3, []))

    def test_the_file_count_cap(self):
        count, logs = self.count(self.files(4), shards=10)
        self.assertEqual(count, 4)
        self.assertEqual(len(logs), 1)
        self.assertIn('matched file count', logs[0])
        self.assertIn('10', logs[0])

    def test_the_256_job_cap(self):
        for shards in (300, 1000):
            with self.subTest(shards=shards):
                count, logs = self.count(self.files(300), shards=shards)
                self.assertEqual(count, 256)
                self.assertIn('256', logs[0])
                self.assertIn(str(shards), logs[0])

    def test_max_minutes_without_records_is_one_shard_naming_the_cells(self):
        cells = cells_of(linux=dict(gone=999), other=dict(f000=999))
        count, logs = self.count(self.files(5), cells, ('linux', 'mac'), max_minutes=1)
        self.assertEqual(count, 1)
        self.assertIn('linux', logs[0])
        self.assertIn('mac', logs[0])

    def test_max_minutes_is_the_fewest_shards_under_the_limit(self):
        cells = cells_of(linux=dict(a=60, b=60))
        self.assertEqual(self.count(['a.py', 'b.py'], cells, ['linux'], max_minutes=2), (1, []))
        self.assertEqual(self.count(['a.py', 'b.py'], cells, ['linux'], max_minutes=1.5)[0], 2)

    def test_max_minutes_follows_the_greedy_assignment_not_the_optimum(self):
        # 30-second limit: {a,b} and {c,d,e} would fit two shards; the greedy needs three.
        cells = cells_of(linux=dict(a=15, b=15, c=10, d=10, e=10))
        files = ['a.py', 'b.py', 'c.py', 'd.py', 'e.py']
        self.assertEqual(self.count(files, cells, ['linux'], max_minutes=0.5), (3, []))

    def test_one_file_over_the_limit_stands_alone_and_is_named(self):
        cells = cells_of(linux=dict(big=600, a=60, b=60, c=60))
        files = ['a.py', 'b.py', 'big.py', 'c.py']
        count, logs = self.count(files, cells, ['linux'], max_minutes=2)
        self.assertEqual(count, 3)
        self.assertEqual(len(logs), 1)
        self.assertIn('big.py', logs[0])
        predictions = split.predict(files, dict.fromkeys(files, 10), cells, 'linux')
        self.assertEqual(split.assign(predictions, 3)[0], ['big.py'])

    def test_across_cells_the_slower_one_decides(self):
        cells = cells_of(fast=dict(a=30, b=30, c=30, d=30), slow=dict(a=120, b=120, c=120, d=120))
        files = ['a.py', 'b.py', 'c.py', 'd.py']
        for names, expected in ((['fast'], 1), (['fast', 'slow'], 4), (['slow', 'fast'], 4),
                                (['fast', 'absent'], 1)):
            with self.subTest(names=names):
                self.assertEqual(self.count(files, cells, names, max_minutes=2)[0], expected)

    def test_every_measured_cell_must_fit_not_just_one(self):
        # The fast cell fits two shards at its lower bound; the slow one needs three.
        cells = cells_of(slow=dict(a=15, b=15, c=10, d=10, e=10),
                         fast=dict(a=1, b=1, c=1, d=1, e=1))
        files = ['a.py', 'b.py', 'c.py', 'd.py', 'e.py']
        self.assertEqual(self.count(files, cells, ['fast', 'slow'], max_minutes=0.5), (3, []))

    def test_max_minutes_beyond_256_shards_is_capped_naming_the_need(self):
        files = self.files(300)
        cells = {'linux': {path: 60 for path in files}}
        count, logs = self.count(files, cells, ['linux'], max_minutes=1)
        self.assertEqual(count, 256)
        self.assertIn('256', logs[-1])
        self.assertIn('300', logs[-1])


if __name__ == '__main__':
    unittest.main()
