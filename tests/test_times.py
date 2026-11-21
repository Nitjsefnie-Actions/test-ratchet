"""The committed per-file times file: read strictly, grown only by outliers, written canonically."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from test_ratchet import ratchet, times
from test_ratchet.split import ConfigError


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.path = Path(self.root.name, 'times.json')

    def tearDown(self):
        self.root.cleanup()

    def load_text(self, text):
        self.path.write_bytes(text.encode('utf-8') if isinstance(text, str) else text)
        return times.load(self.path)

    def test_no_path_is_no_records(self):
        for path in (None, ''):
            with self.subTest(path=path):
                self.assertEqual(times.load(path), {})

    def test_a_missing_file_is_no_records_logged_once(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(times.load(self.path), {})
        self.assertEqual(out.getvalue().count('\n'), 1)
        self.assertTrue(out.getvalue().startswith('::notice::'))
        self.assertIn(str(self.path), out.getvalue())

    def test_keys_are_normalised_and_a_version_1_number_reads_as_a_one_value_list(self):
        data = {'schema_version': 1, 'extra': True, 'cells': {
            'linux 3.15': {'tests\\a.py': 1, './tests/b.py': 2.5, 'tests/c.py': 0},
            'empty': {}}}
        self.assertEqual(self.load_text(json.dumps(data)), {
            'linux 3.15': {'tests/a.py': [1.0], 'tests/b.py': [2.5], 'tests/c.py': [0.0]},
            'empty': {}})

    def test_version_2_lists_are_read_in_order_up_to_ten_values(self):
        data = {'schema_version': 2, 'cells': {'linux': {
            '.\\a.py': [3, 1.5, 0], 'b.py': [4], 'c.py': list(range(10, 0, -1))}}}
        self.assertEqual(self.load_text(json.dumps(data)), {'linux': {
            'a.py': [3.0, 1.5, 0.0], 'b.py': [4.0], 'c.py': [float(n) for n in range(10, 0, -1)]}})

    def test_invalid_content_names_the_file_and_key(self):
        def doc(cells, version: object = 1):
            return json.dumps({'schema_version': version, 'cells': cells})
        for text, key in (
                ('not json', 'JSON'), (b'\xff\xfe', 'UTF-8'), ('[]', 'schema_version'),
                (json.dumps({'cells': {}}), 'schema_version'),
                (doc({}, 3), 'schema_version'), (doc({}, 0), 'schema_version'),
                (doc({}, True), 'schema_version'), (doc({}, 1.0), 'schema_version'),
                (doc({}, '1'), 'schema_version'),
                (json.dumps({'schema_version': 1}), 'cells'), (doc([]), 'cells'),
                (doc({'linux': []}), 'linux'),
                (doc({'linux': {'a.py': -1}}), 'a.py'),
                (doc({'linux': {'a.py': True}}), 'a.py'),
                (doc({'linux': {'a.py': '3'}}), 'a.py'),
                (doc({'linux': {'a.py': None}}), 'a.py'),
                ('{"schema_version": 1, "cells": {"linux": {"a.py": NaN}}}', 'a.py'),
                ('{"schema_version": 1, "cells": {"linux": {"a.py": Infinity}}}', 'a.py'),
                ('{"schema_version": 1, "cells": {"linux": {"a.py": 1e400}}}', 'a.py'),
                ('{"schema_version": 1, "cells": {"linux": {"a.py": 1' + '0' * 400 + '}}}',
                 'a.py'),
                (doc({'linux': {'a.py': 1, '.\\a.py': 2}}), '.\\a.py'),
                (doc({'linux': {'a.py': [1]}}), 'a.py'), (doc({'linux': {'a.py': 1}}, 2), 'a.py'),
                (doc({'linux': {'a.py': []}}, 2), 'a.py'),
                (doc({'linux': {'a.py': [1] * 11}}, 2), 'a.py'),
                (doc({'linux': {'a.py': [1, -1]}}, 2), 'a.py')):
            with self.subTest(text=text[:60]):
                with self.assertRaises(ConfigError) as caught:
                    self.load_text(text)
                self.assertIn(str(self.path), str(caught.exception))
                self.assertIn(key, str(caught.exception))

    def test_an_unreadable_path_is_a_configuration_error(self):
        with self.assertRaisesRegex(ConfigError, self.root.name):
            times.load(self.root.name)


class RecordedListTest(unittest.TestCase):
    def test_only_an_outlier_of_a_files_list_is_appended(self):
        # Against [8, 12] the band is 6 to 15: 25% of the min below it, of the max above it.
        for seconds, expected in ((5.9, [8, 12, 5.9]), (6.1, None), (15.1, [8, 12, 15.1]),
                                  (14.9, None)):
            with self.subTest(seconds=seconds):
                self.assertEqual(ratchet.times_moved({'a': [8, 12]}, {'a': seconds}),
                                 None if expected is None else {'a': expected})
        # Where 25% is under a second, the second decides, near-missed on both sides.
        for old, seconds, moved in (([2.0], 3.05, True), ([2.0], 2.95, False),
                                    ([2.0], 0.95, True), ([2.0], 1.05, False)):
            with self.subTest(old=old, seconds=seconds):
                self.assertEqual(ratchet.times_moved({'a': old}, {'a': seconds}),
                                 {'a': [*old, seconds]} if moved else None)

    def test_an_eleventh_value_drops_the_oldest(self):
        old = [float(index) for index in range(10, 20)]
        self.assertEqual(ratchet.times_moved({'a': old}, {'a': 30.0}), {'a': [*old[1:], 30.0]})
        self.assertEqual(ratchet.times_moved({'a': old[1:]}, {'a': 30.0}), {'a': [*old[1:], 30.0]})

    def test_an_added_or_removed_file_changes_the_cell_and_keeps_the_other_lists(self):
        old = {'a': [4.0, 5.0], 'b': [10.0]}
        for new, expected in (({'a': 4.9}, {'a': [4.0, 5.0]}),
                              ({'a': 4.9, 'b': 10.0, 'c': 0.5},
                               {'a': [4.0, 5.0], 'b': [10.0], 'c': [0.5]})):
            with self.subTest(new=new):
                self.assertEqual(ratchet.times_moved(old, new), expected)
        self.assertEqual(ratchet.times_moved(None, {}), {})
        self.assertIsNone(ratchet.times_moved({}, {}))


class DumpTest(unittest.TestCase):
    def test_sorted_rounded_indented_with_a_trailing_newline(self):
        text = times.dump({'mac': {'b.py': [1.006, 3], 'a.py': [2]}, 'linux': {'c.py': [0.004]}})
        self.assertEqual(text, json.dumps({'schema_version': 2, 'cells': {
            'linux': {'c.py': [0.0]}, 'mac': {'a.py': [2], 'b.py': [1.01, 3]}}}, indent=2) + '\n')
        self.assertEqual(list(json.loads(text)), ['schema_version', 'cells'])

    def test_a_dumped_file_loads_back(self):
        cells = {'linux': {'tests/a.py': [1.25, 0.5], 'tests/b.py': [0.0]}}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'times.json')
            path.write_text(times.dump(cells), encoding='utf-8')
            self.assertEqual(times.load(path), cells)


if __name__ == '__main__':
    unittest.main()
