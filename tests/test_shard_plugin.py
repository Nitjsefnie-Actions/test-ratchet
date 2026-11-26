"""The pytest plugin that keeps a run to its shard's files, driven through its hooks."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[1] / 'test_ratchet' / 'plugin'))
import test_ratchet_shard as plugin  # noqa: E402  pylint: disable=wrong-import-position

GIVEN = ['tests/test_a.py', 'extra', 'extra/test_x.py', 'tests/test_a.py::test_one',
         './tests/test_a.py', 'deep/tests/test_a.py']


class Config:
    def __init__(self, listing, args, typed=(), root=None):
        self.listing, self.args = listing, args
        self.invocation_params = SimpleNamespace(args=('-n', '0', *typed), dir=root)

    def getoption(self, name):
        if name != 'test_ratchet_shard':
            raise AssertionError(f'unmodelled option {name}')
        return self.listing


class ShardPluginTest(unittest.TestCase):
    def setUp(self):
        self.listing = Path(self.enterContext(tempfile.TemporaryDirectory()), 'files.txt')
        self.listing.write_text('tests/test_a.py\ntests/test_b.py\n', encoding='utf-8')

    def test_conftests_load_for_the_listed_files_alone(self):
        namespace = SimpleNamespace(test_ratchet_shard=str(self.listing), file_or_dir=list(GIVEN))
        plugin.pytest_load_initial_conftests(SimpleNamespace(known_args_namespace=namespace))
        self.assertEqual(namespace.file_or_dir, ['tests/test_a.py'])

    def test_collection_starts_from_the_listed_files_alone(self):
        config = Config(str(self.listing), list(GIVEN))
        plugin.pytest_configure(config)
        self.assertEqual(config.args, ['tests/test_a.py'])
        self.assertFalse(Path(f'{self.listing}.ignored').exists())

    def test_the_command_lines_own_cut_paths_are_recorded(self):
        config = Config(str(self.listing), list(GIVEN), ('tests/test_a.py', *GIVEN[2:4], '0'))
        plugin.pytest_configure(config)
        self.assertEqual(config.args, ['tests/test_a.py'])
        self.assertEqual(Path(f'{self.listing}.ignored').read_text(encoding='utf-8'),
                         'extra/test_x.py\ntests/test_a.py::test_one\n')

    def test_without_a_listing_nothing_is_cut(self):
        namespace = SimpleNamespace(test_ratchet_shard=None, file_or_dir=list(GIVEN))
        plugin.pytest_load_initial_conftests(SimpleNamespace(known_args_namespace=namespace))
        config = Config(None, list(GIVEN))
        plugin.pytest_configure(config)
        self.assertEqual((namespace.file_or_dir, config.args), (GIVEN, GIVEN))

    def test_children_read_relative_path_patterns_against_the_root(self):
        config = plugin.import_module('coverage.config').CoverageConfig()
        kept = ['*/a/*', '?b/c', '/x/*', 'n.py']  # wildcard, absolute and bare patterns
        config.run_omit, config.run_include = ['t/*', *kept, 'a\\b'], ['s/*']
        session = SimpleNamespace(config=Config(None, [], root='/r'))
        with mock.patch.dict(os.environ, clear=True):
            plugin.pytest_sessionstart(session)  # unmeasured: nothing to anchor
            os.environ['COVERAGE_PROCESS_CONFIG'] = config.serialize()
            plugin.pytest_sessionstart(session)
            new = config.deserialize(os.environ['COVERAGE_PROCESS_CONFIG'])
        self.assertEqual(new.run_omit + new.run_include, ['/r/t/*', *kept, '/r/a\\b', '/r/s/*'])


if __name__ == '__main__':
    unittest.main()
