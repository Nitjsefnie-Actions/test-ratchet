"""The plan sub-action, run through its action.yml `run:` block."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _action_harness import run_action
from test_ratchet import ConfigError, plan
from test_split import make_repo

TIMES = {'schema_version': 1, 'cells': {
    'linux': {'tests/test_a.py': 60, 'tests/test_b.py': 60, 'tests/test_c.py': 60, 'gone.py': 9},
    'mac': {'tests/test_a.py': 120, 'tests/test_b.py': 120, 'tests/test_c.py': 120}}}
FILES = {'tests/test_a.py': b'a' * 10, 'tests/test_b.py': b'b' * 20, 'tests/test_c.py': b'c' * 30,
         'tests/helper.txt': b'x', 'times.json': json.dumps(TIMES).encode()}


class PlanTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.repo = make_repo(scratch.name, FILES)
        Path(self.repo, 'tests/untracked.py').write_bytes(b'u')

    def plan(self, cwd=None, interpreter=None, **inputs):
        inputs.setdefault('tests', 'tests/test_*.py')
        inputs = {name.replace('_', '-'): value for name, value in inputs.items()}
        return run_action('plan', inputs, cwd or self.repo, interpreter)

    def fails(self, *named, **inputs):
        result = self.plan(**inputs)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.outputs, {'matrix': '', 'count': ''})
        for name in named:
            self.assertIn(name, result.stderr)
        return result

    def test_the_outputs_are_the_compact_matrix_and_the_count(self):
        result = self.plan(shards='2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.outputs, {'matrix': '{"shard":["1/2","2/2"]}', 'count': '2'})
        self.assertEqual(json.loads(result.outputs['matrix'])['shard'], ['1/2', '2/2'])

    def test_a_shard_count_above_the_file_count_is_capped_and_logged(self):
        result = self.plan(shards='10')
        self.assertEqual(result.outputs['count'], '3')
        self.assertEqual(result.outputs['matrix'], '{"shard":["1/3","2/3","3/3"]}')
        self.assertIn('capped at 3 by the matched file count; 10 were needed', result.stdout)

    def test_the_256_job_cap_is_logged_naming_the_need(self):
        files = {f'tests/test_{index:03}.py': b'x' for index in range(300)}
        with tempfile.TemporaryDirectory() as scratch:
            repo = make_repo(scratch, files)
            result = run_action('plan', {'tests': 'tests/*.py', 'shards': '1000'}, repo)
        self.assertEqual(result.outputs['count'], '256')
        self.assertEqual(len(json.loads(result.outputs['matrix'])['shard']), 256)
        self.assertIn("capped at 256 by GitHub's 256-job matrix limit; 1000 were needed",
                      result.stdout)

    def test_exactly_one_of_shards_and_max_minutes_is_required(self):
        for inputs in ({'shards': '2', 'max_minutes': '5'}, {}, {'shards': ' ', 'max_minutes': ''}):
            with self.subTest(inputs=inputs):
                self.fails('shards', 'max-minutes', **inputs)

    def test_a_shard_count_must_be_a_whole_number_of_at_least_one(self):
        for shards in ('0', '-1', '1.5', 'two', '2x', '1e1', '٣', '00'):
            with self.subTest(shards=shards):
                self.fails('shards', shards=shards)

    def test_max_minutes_must_be_a_positive_finite_number(self):
        for minutes in ('0', '-1', 'nan', 'inf', '1e999', 'ten', '1,5', '0.0'):
            with self.subTest(minutes=minutes):
                self.fails('max-minutes', max_minutes=minutes)

    def test_tests_is_required_and_a_pattern_matching_nothing_is_named(self):
        self.fails('tests', tests='  \n', shards='2')
        result = self.fails('nothing/*.py', tests='nothing/*.py', shards='2')
        self.assertIn('matched no file git tracks', result.stderr)

    def test_a_pattern_matching_nothing_beside_a_match_only_warns(self):
        result = self.plan(tests='tests/test_*.py\nnothing/*.py', shards='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('::warning::', result.stdout)
        self.assertIn('nothing/*.py', result.stdout)

    def test_max_minutes_defaults_to_every_cell_and_the_slower_one_decides(self):
        # linux: three 60 s files fit two shards of 2 minutes; mac: 120 s each needs three.
        self.assertEqual(self.plan(max_minutes='2', times='times.json', cell='linux')
                         .outputs['count'], '2')
        both = self.plan(max_minutes='2', times='times.json')
        self.assertEqual(both.outputs, {'matrix': '{"shard":["1/3","2/3","3/3"]}', 'count': '3'})
        self.assertEqual(self.plan(max_minutes='2', times='times.json', cell='linux\nmac\n')
                         .outputs['count'], '3')

    def test_max_minutes_names_a_file_alone_above_the_limit(self):
        result = self.plan(max_minutes='1.5', times='times.json', cell='mac')
        self.assertEqual(result.outputs['count'], '3')
        self.assertIn('tests/test_a.py alone is predicted at 120.0s in cell mac', result.stdout)

    def test_max_minutes_without_records_is_one_shard_and_says_so(self):
        for inputs in ({}, {'times': 'times.json', 'cell': 'windows'},
                       {'times': 'absent.json', 'cell': 'linux\nmac'}):
            with self.subTest(inputs=inputs):
                result = self.plan(max_minutes='1', **inputs)
                self.assertEqual(result.outputs['count'], '1')
                self.assertIn('nothing to measure', result.stdout)

    def test_each_named_cells_predicted_shard_totals_are_logged(self):
        result = self.plan(shards='2', times='times.json', cell='linux\nwindows')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cell linux: 1/2 120.0s (files: 2), 2/2 60.0s (files: 1)', result.stdout)
        # No record in this cell: blob sizes weigh the files, 30 / (10 + 20) bytes.
        self.assertIn('cell windows (blob size in bytes, no records): 1/2 30.0 (files: 1),'
                      ' 2/2 30.0 (files: 2)', result.stdout)

    def test_cells_default_to_every_cell_of_the_times_file(self):
        result = self.plan(shards='1', times='times.json')
        self.assertIn('cell linux: 1/1 180.0s (files: 3)', result.stdout)
        self.assertIn('cell mac: 1/1 360.0s (files: 3)', result.stdout)

    def test_a_missing_times_file_means_no_records(self):
        result = self.plan(shards='2', times='absent.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('absent.json does not exist', result.stdout)
        self.assertEqual(result.outputs['count'], '2')

    def test_an_invalid_times_file_names_the_file(self):
        Path(self.repo, 'bad.json').write_text('{"schema_version": 2}', encoding='utf-8')
        self.fails('bad.json', shards='2', times='bad.json')

    def test_a_relative_times_path_is_read_from_the_repository_root(self):
        result = self.plan(cwd=self.repo / 'tests', shards='2', times='times.json', cell='linux')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('cell linux: 1/2 120.0s', result.stdout)

    def test_outside_a_git_repository_is_a_configuration_error(self):
        with tempfile.TemporaryDirectory() as scratch:
            result = run_action('plan', {'tests': '*.py', 'shards': '1'}, Path(scratch))
        self.assertEqual(result.returncode, 2)
        self.assertIn('repository', result.stderr)

    def test_a_missing_git_is_a_configuration_error(self):
        with mock.patch.object(plan.subprocess, 'run', side_effect=FileNotFoundError('git')):
            with self.assertRaisesRegex(ConfigError, 'cannot run git'):
                plan.repository_root()

    def test_the_action_finds_its_interpreter_as_python3_or_python(self):
        for interpreter in ('python3', 'python'):
            with self.subTest(interpreter=interpreter):
                result = self.plan(interpreter=interpreter, shards='2')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.outputs['count'], '2')


if __name__ == '__main__':
    unittest.main()
