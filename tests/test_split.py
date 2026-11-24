"""Patterns, git-tracked matching, shard parsing and the function `run` calls."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_ratchet import split, times

TRACKED = {name: 1 for name in (
    'a.py', 'tests/test_a.py', 'tests/unit/test_b.py', 'tests/unit/deep/test_c.py',
    'Tests/test_d.py', 'tests/a+b.py', 'tests/[x.py', 'x.y', 'x/y')}


def git(repo, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, HOME=str(repo))
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c',
                           'user.email=t@example.com', *args],
                          env=env, capture_output=True, check=True)


def make_repo(root, files):
    """A committed repository holding `files` (path -> bytes), written as given."""
    git(root, 'init', '-q')
    for name, data in files.items():
        path = Path(root, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    git(root, 'add', '--', *files)
    git(root, 'commit', '-q', '-m', 'fixture')
    return Path(root)


class ParsePatternsTest(unittest.TestCase):
    def test_lines_are_trimmed_and_normalised(self):
        for text, expected in (
                ('tests/**/*.py', ['tests/**/*.py']),
                ('  ./tests/a.py  \n\n  \n', ['tests/a.py']),
                ('tests\\unit\\*.py\r\nother/*.py\r\n', ['tests/unit/*.py', 'other/*.py']),
                ('.\\tests\\a.py', ['tests/a.py'])):
            with self.subTest(text=text):
                self.assertEqual(split.parse_patterns(text), expected)

    def test_no_pattern_is_a_configuration_error(self):
        for text in ('', '\n  \n'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(split.ConfigError, 'tests'):
                    split.parse_patterns(text)


class MatchTest(unittest.TestCase):
    def test_glob_semantics(self):
        for pattern, expected in (
                ('tests/**/*.py', ['tests/[x.py', 'tests/a+b.py', 'tests/test_a.py',
                                   'tests/unit/deep/test_c.py', 'tests/unit/test_b.py']),
                ('tests/**/test_*.py', ['tests/test_a.py', 'tests/unit/deep/test_c.py',
                                        'tests/unit/test_b.py']),
                ('**/test_c.py', ['tests/unit/deep/test_c.py']),
                ('tests/*.py', ['tests/[x.py', 'tests/a+b.py', 'tests/test_a.py']),
                ('tests/unit/**', ['tests/unit/deep/test_c.py', 'tests/unit/test_b.py']),
                ('*.py', ['a.py']),
                ('tests/test_?.py', ['tests/test_a.py']),
                ('tests/unit/test_[ab].py', ['tests/unit/test_b.py']),
                ('tests/unit/test_[!a].py', ['tests/unit/test_b.py']),
                ('Tests/*.py', ['Tests/test_d.py']),
                ('tests/a+b.py', ['tests/a+b.py']),
                ('tests/[x.py', ['tests/[x.py']),
                ('x[+-0]y', ['x.y']),
                ('x?y', ['x.y']),
                ('x*y', ['x.y'])):
            with self.subTest(pattern=pattern):
                self.assertEqual(split.match([pattern], TRACKED), expected)

    def test_every_pattern_is_the_whole_path_and_case_sensitive(self):
        for pattern in ('test_a.py', 'TESTS/test_a.py', 'tests', 'ests/test_a.py'):
            with self.subTest(pattern=pattern):
                with self.assertRaises(split.ConfigError):
                    split.match([pattern], TRACKED)

    def test_overlapping_patterns_give_one_sorted_list(self):
        self.assertEqual(split.match(['tests/unit/**', 'a.py', 'tests/unit/test_b.py'], TRACKED),
                         ['a.py', 'tests/unit/deep/test_c.py', 'tests/unit/test_b.py'])

    def test_zero_matches_fail_naming_every_pattern(self):
        with self.assertRaises(split.ConfigError) as caught:
            split.match(['nope/*.py', 'also/**'], TRACKED)
        self.assertIn('nope/*.py', str(caught.exception))
        self.assertIn('also/**', str(caught.exception))

    def test_a_pattern_matching_nothing_beside_matches_is_a_warning(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(split.match(['*.py', 'nope/*.py'], TRACKED), ['a.py'])
        self.assertTrue(out.getvalue().startswith('::warning::'), out.getvalue())
        self.assertIn('nope/*.py', out.getvalue())

    def test_an_invalid_pattern_is_a_configuration_error(self):
        with self.assertRaisesRegex(split.ConfigError, r'tests/\[z-a\]\.py'):
            split.match(['tests/[z-a].py'], TRACKED)


class TrackedFilesTest(unittest.TestCase):
    def test_only_committed_regular_blobs_with_their_sizes(self):
        with tempfile.TemporaryDirectory() as root:
            repo = make_repo(root, {'.gitignore': b'venv/\n', 'tests/test_a.py': b'abc\n',
                                    'tests/run.sh': b'#!/bin/sh\n'})
            git(repo, 'update-index', '--chmod=+x', 'tests/run.sh')
            Path(repo, 'venv/lib').mkdir(parents=True)
            Path(repo, 'venv/lib/test_venv.py').write_bytes(b'x\n')
            Path(repo, 'tests/test_untracked.py').write_bytes(b'x\n')
            git(repo, 'update-index', '--add', '--cacheinfo',
                '160000,' + git(repo, 'rev-parse', 'HEAD').stdout.decode().strip() + ',sub')
            git(repo, 'commit', '-q', '-m', 'executable bit and a submodule')
            if hasattr(os, 'symlink'):
                os.symlink('test_a.py', Path(repo, 'tests/test_link.py'))
                git(repo, 'add', 'tests/test_link.py')
                git(repo, 'commit', '-q', '-m', 'symlink')
            tracked = split.tracked_files(repo)
            self.assertEqual(tracked, {'.gitignore': 6, 'tests/test_a.py': 4, 'tests/run.sh': 10})
            self.assertEqual(split.match(['**/test_*.py'], tracked), ['tests/test_a.py'])

    def test_a_subdirectory_still_lists_the_whole_repository(self):
        with tempfile.TemporaryDirectory() as root:
            repo = make_repo(root, {'a.py': b'a\n', 'tests/test_a.py': b'abc\n'})
            expected = {'a.py': 2, 'tests/test_a.py': 4}
            self.assertEqual(split.tracked_files(repo), expected)
            self.assertEqual(split.tracked_files(Path(repo, 'tests')), expected)

    def test_a_directory_outside_git_is_a_configuration_error(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(split.ConfigError, 'git'):
                split.tracked_files(root)

    def test_a_missing_git_is_a_configuration_error(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {'PATH': root}):
                with self.assertRaisesRegex(split.ConfigError, 'git'):
                    split.tracked_files(root)


class ParseShardTest(unittest.TestCase):
    def test_valid_shards(self):
        for text, expected in (('1/1', (1, 1)), ('3/4', (3, 4)), ('256/256', (256, 256)),
                               (' 2/3\n', (2, 3))):
            with self.subTest(text=text):
                self.assertEqual(split.parse_shard(text), expected)

    def test_invalid_shards(self):
        for text in ('0/1', '2/1', '1/257', '1/0', '1', 'a/b', '', '1/2/3', '-1/2', '1 /2'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(split.ConfigError, 'shard'):
                    split.parse_shard(text)


# Line widths and counts vary, so CRLF bytes would reorder the files' sizes.
FILES = {f'tests/test_{index:02d}.py': (b'#' * width + b'\n') * lines for index, (width, lines)
         in enumerate(((60, 1), (1, 40), (30, 2), (2, 25), (10, 5), (3, 15), (40, 1), (1, 30),
                       (20, 2)))}


class ShardFilesTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.repo = make_repo(self.root.name, FILES)
        self.times = Path(self.root.name, 'times.json')

    def tearDown(self):
        self.root.cleanup()

    def shards(self, patterns, times_path, cell, count):
        return [split.shard_files(self.repo, patterns, times_path, cell, f'{index}/{count}')
                for index in range(1, count + 1)]

    def test_every_matched_file_lands_in_exactly_one_shard(self):
        for count in (1, 2, 3, 4, 9, 12):
            with self.subTest(count=count):
                shards = self.shards('tests/*.py', None, 'linux', count)
                self.assertEqual(sorted(path for shard in shards for path in shard), sorted(FILES))
                self.assertEqual(len(shards), count)

    def test_it_is_the_composition_plan_uses(self):
        self.times.write_text(json.dumps({'schema_version': 2, 'cells': {
            'linux': {'tests/test_00.py': [40], 'tests/test_05.py': [1, 3, 9]}}}))
        tracked = split.tracked_files(self.repo)
        files = split.match(['tests/*.py'], tracked)
        expected = split.assign(split.predict(files, tracked, times.load(self.times), 'linux'), 3)
        self.assertEqual(self.shards('tests/*.py', 'times.json', 'linux', 3), expected)

    def test_a_missing_times_file_weights_by_blob_size(self):
        tracked = split.tracked_files(self.repo)
        expected = split.assign({path: float(size) for path, size in tracked.items()}, 2)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.shards('tests/*.py', 'absent.json', 'linux', 2), expected)

    def test_windows_spelling_and_crlf_working_tree_give_the_same_assignment(self):
        self.times.write_text(json.dumps({'schema_version': 1, 'cells': {
            'win': {'tests/test_03.py': 9, 'tests/test_07.py': 1}}}))
        posix = self.shards('tests/*.py', 'times.json', 'win', 4)
        self.times.write_text(json.dumps({'schema_version': 1, 'cells': {
            'win': {'tests\\test_03.py': 9, '.\\tests\\test_07.py': 1}}}))
        for name, data in FILES.items():
            Path(self.repo, name).write_bytes(data.replace(b'\n', b'\r\n'))
        self.assertEqual(self.shards('.\\tests\\*.py\r\n', 'times.json', 'win', 4), posix)
        on_disk = {name: Path(self.repo, name).stat().st_size for name in FILES}
        cells = times.load(self.times)
        self.assertNotEqual(split.assign(split.predict(sorted(FILES), on_disk, cells, 'win'), 4),
                            posix)

    def test_an_invalid_shard_fails_before_git_is_read(self):
        with self.assertRaisesRegex(split.ConfigError, 'shard'):
            split.shard_files(Path(self.root.name, 'nowhere'), 'tests/*.py', None, 'x', '3/2')


if __name__ == '__main__':
    unittest.main()
