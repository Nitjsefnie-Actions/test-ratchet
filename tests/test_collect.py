"""Reading run's artifacts, choosing the gated cell and merging its coverage."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from test_ratchet import ConfigError, cobertura, collect


def lines(covered, total, name='src/a.py', first=1):
    return {name: {number: int(first <= number < first + covered)
                   for number in range(1, total + 1)}}


def artifact(root, name, cell: Any = 'c', shard: Any = '1/1', passed: Any = True,
             runner: Any = 'pytest', coverage=None, **manifest):
    folder = Path(root, name)
    folder.mkdir(parents=True, exist_ok=True)
    data = {'schema_version': 1, 'runner': runner, 'cell': cell, 'shard': shard,
            'files': ['tests/test_a.py'], 'passed': passed, **manifest}
    (folder / 'manifest.json').write_text(json.dumps(data), encoding='utf-8')
    for language, measured in (coverage or {}).items():
        (folder / f'coverage-{language}.xml').write_text(cobertura.dump(measured),
                                                         encoding='utf-8')
    return folder


class CollectTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name, 'artifacts')
        self.root.mkdir()
        self.repo = Path(scratch.name)

    def refused(self, *named):
        with self.assertRaises(collect.ArtifactError) as caught:
            collect.read(self.root)
        for name in named:
            self.assertIn(name, str(caught.exception))

    def test_each_subdirectory_is_one_artifact_read_from_its_manifest(self):
        artifact(self.root, 'test-ratchet-c-2_2', shard='2/2', passed=False, runner='vitest',
                 files=['b.test.ts', 'a.test.ts'])
        artifact(self.root, 'test-ratchet-c-1_2', shard='1/2')
        read = collect.read(self.root)
        self.assertEqual([item.name for item in read], ['test-ratchet-c-1_2', 'test-ratchet-c-2_2'])
        self.assertEqual(read[1], collect.Artifact('test-ratchet-c-2_2',
                                                   self.root / 'test-ratchet-c-2_2', 'vitest',
                                                   'c', (2, 2), ['b.test.ts', 'a.test.ts'], False))
        self.assertEqual(read[0].shard, (1, 2))

    def test_one_artifact_downloaded_alone_lies_in_the_root(self):
        # download-artifact extracts a pattern's single match into the path itself.
        artifact(self.root.parent, self.root.name, shard=None)
        (read,) = collect.read(self.root)
        self.assertEqual((read.path, read.cell, read.shard), (self.root, 'c', (1, 1)))
        self.assertIn('only artifact', read.name)

    def test_an_invalid_manifest_is_refused_naming_the_artifact(self):
        for manifest in ({'schema_version': 2}, {'schema_version': True}, {'runner': 'jest'},
                         {'cell': ''}, {'cell': 'a\nb'}, {'cell': 7}, {'shard': '3/2'},
                         {'shard': 2}, {'files': 'a.py'}, {'files': [1]}, {'passed': 'yes'},
                         {'passed': None}):
            with self.subTest(manifest=manifest):
                folder = artifact(self.root, 'test-ratchet-bad', **manifest)
                self.refused('test-ratchet-bad', next(iter(manifest)))
                for item in folder.iterdir():
                    item.unlink()
                folder.rmdir()

    def test_an_unreadable_or_missing_manifest_is_refused_naming_the_artifact(self):
        folder = Path(self.root, 'test-ratchet-x')
        folder.mkdir()
        self.refused('test-ratchet-x', 'manifest.json')
        for data in (b'{', b'[]', b'\xff'):
            with self.subTest(data=data):
                (folder / 'manifest.json').write_bytes(data)
                self.refused('test-ratchet-x', 'manifest.json')

    @unittest.skipIf(os.name == 'nt', 'symlinks need privileges on Windows')
    def test_symlinks_are_refused_whatever_they_point_at(self):
        outside = artifact(self.repo, 'outside')
        (self.root / 'test-ratchet-link').symlink_to(outside, target_is_directory=True)
        self.refused('test-ratchet-link', 'symlink')
        (self.root / 'test-ratchet-link').unlink()
        folder = Path(self.root, 'test-ratchet-file-link')
        folder.mkdir()
        (folder / 'manifest.json').symlink_to(outside / 'manifest.json')
        self.refused('test-ratchet-file-link', 'symlink')
        (folder / 'manifest.json').unlink()
        folder.rmdir()
        held = artifact(self.root, 'test-ratchet-c-all', coverage={'shell': lines(1, 1)})
        (held / 'coverage-python.xml').symlink_to(held / 'coverage-shell.xml')
        with self.assertRaisesRegex(collect.ArtifactError, 'test-ratchet-c-all.*symlink'):
            collect.merge(collect.read(self.root), 'c', self.repo)

    def test_a_file_beside_the_artifact_directories_is_refused(self):
        artifact(self.root, 'test-ratchet-c-all')
        (self.root / 'stray.txt').write_text('x', encoding='utf-8')
        self.refused('stray.txt', 'no manifest.json')

    def test_an_artifacts_directory_absent_or_a_file_is_refused(self):
        (self.root / 'file').write_text('x', encoding='utf-8')
        for path in (self.root / 'absent', self.root / 'file'):
            with self.subTest(path=path.name):
                with self.assertRaisesRegex(ConfigError, f'{path.name} is not a directory'):
                    collect.read(path)


class CellTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        for cell in ('linux', 'mac'):
            artifact(scratch.name, f'test-ratchet-{cell}-all', cell=cell)
        self.both = collect.read(Path(scratch.name))
        self.linux = self.both[:1]

    def test_the_input_wins_then_the_records_then_the_only_cell(self):
        records = {'python': {'floor': 1, 'cell': 'mac'}, 'shell': {'floor': 1}}
        for found, given, held, expected in (
                (self.both, 'linux', records, 'linux'),
                (self.both, 'linux', {'python': {'cell': 'mac'}, 'shell': {'cell': 'linux'}},
                 'linux'),
                (self.both, '', records, 'mac'),
                (self.both, '', {'python': {'floor': 1, 'cell': 'mac'}, 'shell': {'cell': 'mac'}},
                 'mac'),
                (self.linux, '', {'python': {'floor': 1}}, 'linux'),
                (self.linux, '', {}, 'linux')):
            with self.subTest(given=given, held=held):
                self.assertEqual(collect.gated_cell(found, given, held, 'file'), expected)

    def test_records_naming_different_cells_are_refused_naming_them(self):
        records = {'python': {'cell': 'linux'}, 'shell': {'cell': 'mac'}}
        with self.assertRaisesRegex(ConfigError, 'file:.*python linux, shell mac'):
            collect.gated_cell(self.both, '', records, 'file')

    def test_several_cells_with_neither_input_nor_record_are_refused_naming_them(self):
        with self.assertRaisesRegex(ConfigError, 'input cell.*linux, mac'):
            collect.gated_cell(self.both, '', {'python': {'floor': 1}}, 'file')

    def test_a_cell_without_an_artifact_or_of_two_lines_is_refused(self):
        for found, given, named in ((self.both, 'windows', 'windows.*linux, mac'),
                                    ([], '', 'no test-ratchet artifact'),
                                    (self.both, 'linux\nmac', 'one cell')):
            with self.subTest(given=given):
                with self.assertRaisesRegex(ConfigError, named):
                    collect.gated_cell(found, given, {}, 'file')


class MergeTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name, 'artifacts')
        self.repo = Path(scratch.name)

    def collected(self, cell='c'):
        found = collect.read(self.root)
        return collect.left_out(found, cell), collect.merge(found, cell, self.repo)

    def test_overlapping_shards_merge_a_line_covered_by_either(self):
        # A shard measures only the languages its tests reached: no shell report here.
        artifact(self.root, 'one', shard='1/2', coverage={'python': lines(2, 4)})
        artifact(self.root, 'two', shard='2/2', coverage={
            'python': {**lines(2, 4, first=2), **lines(1, 1, 'src/b.py')},
            'shell': lines(0, 3, 'run.sh')})
        artifact(self.root, 'other-cell', cell='d', coverage={'python': lines(4, 4)})
        left, merged = self.collected()
        self.assertEqual(left, [])
        self.assertEqual(merged, {'python': {'src/a.py': {1: 1, 2: 2, 3: 1, 4: 0},
                                             'src/b.py': {1: 1}},
                                  'shell': {'run.sh': {1: 0, 2: 0, 3: 0}}})

    def test_a_failed_and_a_missing_shard_are_left_out_and_named(self):
        artifact(self.root, 'test-ratchet-c-1_3', shard='1/3', coverage={'python': lines(1, 4)})
        artifact(self.root, 'test-ratchet-c-2_3', shard='2/3', passed=False,
                 coverage={'python': lines(4, 4), 'shell': lines(1, 1, 'run.sh')})
        artifact(self.root, 'test-ratchet-v-1_3', shard='1/3', runner='vitest')
        artifact(self.root, 'test-ratchet-d-all', cell='d', passed=False)
        left, merged = self.collected()
        self.assertEqual(left, ['test-ratchet-c-2_3: its tests failed',
                                'pytest shard 3/3 of cell c: no artifact',
                                'vitest shard 2/3 of cell c: no artifact',
                                'vitest shard 3/3 of cell c: no artifact'])
        self.assertEqual(merged, {'python': lines(1, 4)})

    def test_shards_of_one_runner_disagreeing_on_their_count_are_refused(self):
        # Another runner of the cell, or another cell, may use its own count.
        artifact(self.root, 'a', shard='1/2')
        artifact(self.root, 'b', shard='2/2')
        artifact(self.root, 'v', shard=None, runner='vitest')
        artifact(self.root, 'other', cell='d', shard='1/4')
        self.assertEqual(self.collected()[0], [])
        artifact(self.root, 'c', shard='2/3')
        with self.assertRaisesRegex(collect.ArtifactError,
                                    r'^the pytest artifacts of cell c disagree on the shard'
                                    r' count: a \(1/2\), b \(2/2\), c \(2/3\)$'):
            self.collected()

    def test_two_artifacts_of_one_shard_are_refused(self):
        artifact(self.root, 'a', shard=None)
        artifact(self.root, 'b', shard='1/1')
        with self.assertRaisesRegex(collect.ArtifactError, 'a and b.*pytest shard 1/1 of cell c'):
            self.collected()

    def test_a_coverage_path_that_is_no_file_or_no_cobertura_is_refused(self):
        folder = artifact(self.root, 'test-ratchet-c-all')
        (folder / 'coverage-shell.xml').mkdir()
        with self.assertRaisesRegex(collect.ArtifactError, 'c-all.*coverage-shell.xml'):
            self.collected()
        (folder / 'coverage-shell.xml').rmdir()
        (folder / 'coverage-shell.xml').write_text('<html/>', encoding='utf-8')
        with self.assertRaisesRegex(collect.ArtifactError, 'coverage-shell.xml.*not Cobertura'):
            self.collected()


if __name__ == '__main__':
    unittest.main()
