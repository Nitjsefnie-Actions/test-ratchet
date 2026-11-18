"""The pytest plugin that keeps a run to its shard's files, driven through its hooks."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.append(str(Path(__file__).resolve().parents[1] / 'test_ratchet' / 'plugin'))
import test_ratchet_shard as plugin  # noqa: E402  pylint: disable=wrong-import-position

GIVEN = ['tests/test_a.py', 'extra', 'extra/test_x.py', 'tests/test_a.py::test_one',
         './tests/test_a.py', 'deep/tests/test_a.py']


class Parser:
    def __init__(self):
        self.options = []

    def addoption(self, name, **kwargs):
        self.options.append((name, kwargs))


class Config:
    def __init__(self, listing, args):
        self.listing = listing
        self.args = args

    def getoption(self, name):
        if name != 'test_ratchet_shard':
            raise AssertionError(f'unmodelled option {name}')
        return self.listing


class ShardPluginTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.listing = Path(scratch.name, 'files.txt')
        self.listing.write_text('tests/test_a.py\ntests/test_b.py\n', encoding='utf-8')

    def test_the_option_is_registered(self):
        parser = Parser()
        plugin.pytest_addoption(parser)
        self.assertEqual([name for name, _ in parser.options], ['--test-ratchet-shard'])

    def test_conftests_load_for_the_listed_files_alone(self):
        namespace = SimpleNamespace(test_ratchet_shard=str(self.listing), file_or_dir=list(GIVEN))
        plugin.pytest_load_initial_conftests(SimpleNamespace(known_args_namespace=namespace))
        self.assertEqual(namespace.file_or_dir, ['tests/test_a.py'])

    def test_collection_starts_from_the_listed_files_alone(self):
        config = Config(str(self.listing), list(GIVEN))
        plugin.pytest_configure(config)
        self.assertEqual(config.args, ['tests/test_a.py'])

    def test_without_a_listing_nothing_is_cut(self):
        namespace = SimpleNamespace(test_ratchet_shard=None, file_or_dir=list(GIVEN))
        plugin.pytest_load_initial_conftests(SimpleNamespace(known_args_namespace=namespace))
        config = Config(None, list(GIVEN))
        plugin.pytest_configure(config)
        self.assertEqual((namespace.file_or_dir, config.args), (GIVEN, GIVEN))


if __name__ == '__main__':
    unittest.main()
