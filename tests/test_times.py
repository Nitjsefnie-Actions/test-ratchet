"""The committed per-file times file: read strictly, written canonically."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from test_ratchet import times
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

    def test_keys_are_normalised_and_seconds_read_as_floats(self):
        data = {'schema_version': 1, 'extra': True, 'cells': {
            'linux 3.15': {'tests\\a.py': 1, './tests/b.py': 2.5, 'tests/c.py': 0},
            'empty': {}}}
        self.assertEqual(self.load_text(json.dumps(data)), {
            'linux 3.15': {'tests/a.py': 1.0, 'tests/b.py': 2.5, 'tests/c.py': 0.0},
            'empty': {}})

    def test_invalid_content_names_the_file_and_key(self):
        def doc(cells, version: object = 1):
            return json.dumps({'schema_version': version, 'cells': cells})
        for text, key in (
                ('not json', 'JSON'), (b'\xff\xfe', 'UTF-8'), ('[]', 'schema_version'),
                (json.dumps({'cells': {}}), 'schema_version'),
                (doc({}, 2), 'schema_version'), (doc({}, True), 'schema_version'),
                (doc({}, 1.0), 'schema_version'), (doc({}, '1'), 'schema_version'),
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
                (doc({'linux': {'a.py': 1, '.\\a.py': 2}}), '.\\a.py')):
            with self.subTest(text=text[:60]):
                with self.assertRaises(ConfigError) as caught:
                    self.load_text(text)
                self.assertIn(str(self.path), str(caught.exception))
                self.assertIn(key, str(caught.exception))

    def test_an_unreadable_path_is_a_configuration_error(self):
        with self.assertRaisesRegex(ConfigError, self.root.name):
            times.load(self.root.name)


class DumpTest(unittest.TestCase):
    def test_sorted_rounded_indented_with_a_trailing_newline(self):
        text = times.dump({'mac': {'b.py': 1.006, 'a.py': 2}, 'linux': {'c.py': 0.004}})
        self.assertEqual(text, json.dumps({'schema_version': 1, 'cells': {
            'linux': {'c.py': 0.0}, 'mac': {'a.py': 2, 'b.py': 1.01}}}, indent=2) + '\n')
        self.assertEqual(list(json.loads(text)), ['schema_version', 'cells'])

    def test_a_dumped_file_loads_back(self):
        cells = {'linux': {'tests/a.py': 1.25, 'tests/b.py': 0.0}}
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, 'times.json')
            path.write_text(times.dump(cells), encoding='utf-8')
            self.assertEqual(times.load(path), cells)


if __name__ == '__main__':
    unittest.main()
