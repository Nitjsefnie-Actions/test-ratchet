"""The report driver: the gated cell's merged coverage against the floor at base-ref."""
import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _action_harness import ROOT
from test_collect import artifact, lines
from test_ratchet import ConfigError, report
from test_split import git, make_repo

MAIN = ROOT / 'report' / 'main.py'
# The oracle, by name: the suite's requirements install coverage, a lint environment may not.
display_covered = importlib.import_module('coverage.results').display_covered
THRESHOLDS = '.github/ci-thresholds.json'


def thresholds(**records):
    return json.dumps({'schema_version': 1, 'other': {'kept': [1]},
                       'coverage': {'go': {'any': 'shape'}, **records}}, indent=2).encode()


class PercentTest(unittest.TestCase):
    def test_it_is_what_coverage_report_precision_1_displays(self):
        for measurable in range(0, 301):
            for covered in range(0, measurable + 1):
                expected = display_covered(100.0 * covered / measurable if measurable else 100.0,
                                           1)
                self.assertEqual(report.percent(covered, measurable), expected)
        for measurable in (10000, 100000, 1000000):
            for covered in [*range(0, 30), *range(measurable - 30, measurable + 1)]:
                self.assertEqual(report.percent(covered, measurable),
                                 display_covered(100.0 * covered / measurable, 1))

    def test_near_0_and_near_100_clamp_and_halves_round_as_floats(self):
        # round() alone gives 0.0, 100.0 and 6.2 for the first three; decimal half-up 6.3.
        for covered, measurable, shown in ((4, 10000, '0.1'), (9996, 10000, '99.9'),
                                           (1, 16, '6.2'), (0, 5, '0.0'), (5, 5, '100.0'),
                                           (0, 0, '100.0'), (9849, 10000, '98.5'),
                                           (9844, 10000, '98.4')):
            with self.subTest(covered=covered, measurable=measurable):
                self.assertEqual(report.percent(covered, measurable), shown)


class ReportTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        (self.scratch / 'repo').mkdir()
        self.repo = make_repo(self.scratch / 'repo', {THRESHOLDS: thresholds(
            python={'floor': 85.0, 'measured': 86.5, 'cell': 'c'})})
        self.artifacts = self.scratch / 'artifacts'

    def report(self, **inputs):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('TEST_RATCHET_', 'GITHUB_'))}
        summary = self.scratch / 'summary.md'
        summary.write_text('', encoding='utf-8')
        env.update(GITHUB_STEP_SUMMARY=str(summary), TEST_RATCHET_ARTIFACTS=str(self.artifacts))
        env.update({f'TEST_RATCHET_{name.upper()}': value for name, value in inputs.items()})
        result = subprocess.run([sys.executable, '-I', str(MAIN)], cwd=self.repo, env=env,
                                capture_output=True, text=True, check=False)
        return result, summary.read_text(encoding='utf-8')

    def commit(self, **records):
        (self.repo / THRESHOLDS).write_bytes(thresholds(**records))
        git(self.repo, 'commit', '-q', '--allow-empty', '-am', 'thresholds')

    def test_a_total_at_or_above_the_floor_passes_and_is_summarised(self):
        artifact(self.artifacts, 'test-ratchet-c-1_2', shard='1/2', coverage={
            'python': lines(17, 20), 'shell': lines(1, 3, 'run.sh')})
        artifact(self.artifacts, 'test-ratchet-c-2_2', shard='2/2',
                 coverage={'python': lines(1, 1, 'src/b.py')})
        result, summary = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('| python | 18 / 21 | 85.7% | 85.0 | pass |', summary)
        self.assertIn('| shell | 1 / 3 | 33.3% | none | not gated: no record |', summary)
        self.assertIn('cell `c`', summary)
        self.assertIn('python 85.7% (18 / 21 lines), floor 85.0', result.stdout)

    def test_the_floor_gate_at_its_rounding_boundary_both_ways(self):
        # 98.49 shows as 98.5 and passes a 98.5 floor; 98.44 shows as 98.4 and fails it. The
        # clamps decide where round() would not: 99.96 is 99.9 and 0.04 is 0.1.
        for covered, measurable, floor, code in ((9849, 10000, 98.5, 0), (9844, 10000, 98.5, 1),
                                                 (9996, 10000, 100, 1), (4, 10000, 0.1, 0),
                                                 (1, 16, 6.3, 1), (1, 16, 6.2, 0)):
            with self.subTest(covered=covered, floor=floor):
                self.commit(python={'floor': floor, 'measured': 99.0})
                folder = artifact(self.artifacts, 'test-ratchet-c-all',
                                  coverage={'python': lines(covered, measurable)})
                result, summary = self.report()
                self.assertEqual(result.returncode, code, result.stderr)
                shown = report.percent(covered, measurable)
                self.assertIn(f'| python | {covered} / {measurable} | {shown}% | {floor} |',
                              summary)
                if code:
                    self.assertIn(f'python coverage {shown}% is below the floor {floor}',
                                  result.stderr)
                    self.assertIn('| fail |', summary)
                for item in folder.iterdir():
                    item.unlink()
                folder.rmdir()

    def test_a_pull_request_lowering_its_floor_is_gated_by_the_bases(self):
        base = git(self.repo, 'rev-parse', 'HEAD').stdout.decode().strip()
        self.commit(python={'floor': 50.0, 'measured': 51.5, 'cell': 'c'})
        artifact(self.artifacts, 'test-ratchet-c-all', coverage={'python': lines(80, 100)})
        result, summary = self.report(base_ref=base)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f'python coverage 80.0% is below the floor 85.0 (thresholds file'
                      f' {THRESHOLDS} at base-ref {base})', result.stderr)
        self.assertIn('| python | 80 / 100 | 80.0% | 85.0 | fail |', summary)
        result, summary = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('| 50.0 | pass |', summary)

    def test_failed_and_missing_shards_are_named_in_the_log_and_summary(self):
        artifact(self.artifacts, 'test-ratchet-c-1_3', shard='1/3',
                 coverage={'python': lines(9, 10)})
        artifact(self.artifacts, 'test-ratchet-c-3_3', shard='3/3', passed=False,
                 coverage={'python': lines(0, 50, 'src/b.py')})
        result, summary = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('| python | 9 / 10 | 90.0% |', summary)
        for named in ('test-ratchet-c-3_3: its tests failed',
                      'pytest shard 2/3 of cell c: no artifact'):
            self.assertIn(f'- {named}', summary)
            self.assertIn(f'::warning::left out of the merge: {named}', result.stdout)

    def test_nothing_merged_is_said_and_nothing_is_gated(self):
        artifact(self.artifacts, 'test-ratchet-c-all', passed=False,
                 coverage={'python': lines(0, 10)})
        result, summary = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No coverage was merged', summary)

    def test_without_a_thresholds_file_every_language_is_reported_not_gated(self):
        artifact(self.artifacts, 'test-ratchet-c-all', coverage={'python': lines(0, 10)})
        base = git(self.repo, 'rev-parse', 'HEAD').stdout.decode().strip()
        for inputs in ({'thresholds': 'absent.json'}, {'thresholds': 'absent.json',
                                                       'base_ref': base}):
            with self.subTest(inputs=inputs):
                result, summary = self.report(**inputs)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('absent.json', result.stdout)
                self.assertIn('| python | 0 / 10 | 0.0% | none | not gated: no record |',
                              summary)

    def test_the_records_cell_chooses_the_gated_cell_and_the_input_overrides_it(self):
        artifact(self.artifacts, 'test-ratchet-c-all', coverage={'python': lines(9, 10)})
        artifact(self.artifacts, 'test-ratchet-d-all', cell='d',
                 coverage={'python': lines(1, 10)})
        result, summary = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('| python | 9 / 10 |', summary)
        result, summary = self.report(cell='d')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('| python | 1 / 10 |', summary)

    def test_an_invalid_thresholds_file_or_base_ref_exits_2_naming_it(self):
        artifact(self.artifacts, 'test-ratchet-c-all', coverage={'python': lines(9, 10)})
        (self.repo / 'dir').mkdir()
        (self.repo / 'dir' / 'f').write_text('x', encoding='utf-8')
        git(self.repo, 'add', 'dir/f')
        git(self.repo, 'commit', '-q', '-m', 'dir')
        for data, inputs, named in (
                (b'{', {}, 'not JSON'), (b'[]', {}, 'schema_version'),
                (json.dumps({'schema_version': 1, 'coverage': []}).encode(), {}, 'coverage'),
                (thresholds(python={'floor': True}), {}, 'coverage.python.floor'),
                (thresholds(shell={'floor': 101}), {}, 'coverage.shell.floor'),
                (thresholds(javascript={'measured': 1}), {}, 'coverage.javascript.floor'),
                (thresholds(python=[]), {}, 'coverage.python.floor'),
                (thresholds(python={'floor': 1, 'cell': ''}), {}, 'coverage.python.cell'),
                (None, {'base_ref': 'nope'}, 'base-ref nope'),
                (None, {'base_ref': '--all'}, 'base-ref --all'),
                (None, {'base_ref': 'HEAD', 'thresholds': 'dir'},
                 'dir at base-ref HEAD is not a file'),
                (None, {'thresholds': 'dir'}, 'dir')):
            with self.subTest(data=data, inputs=inputs):
                if data is not None:
                    (self.repo / THRESHOLDS).write_bytes(data)
                result, summary = self.report(**inputs)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(named, result.stderr)
                self.assertEqual(summary, '')
                git(self.repo, 'checkout', '-q', '--', THRESHOLDS)

    def test_an_invalid_artifact_or_environment_exits_2_naming_it(self):
        artifact(self.artifacts, 'test-ratchet-c-all', runner='jest')
        result, _ = self.report()
        self.assertEqual(result.returncode, 2)
        self.assertIn('test-ratchet-c-all', result.stderr)
        result, _ = self.report(artifacts='')
        self.assertEqual(result.returncode, 2)
        self.assertIn('TEST_RATCHET_ARTIFACTS', result.stderr)

    def test_a_missing_git_repository_is_a_configuration_error(self):
        artifact(self.artifacts, 'test-ratchet-c-all')
        self.repo = self.scratch
        result, _ = self.report()
        self.assertEqual(result.returncode, 2)
        self.assertIn('repository', result.stderr)

    def test_records_hold_the_gated_languages_and_ignore_every_other_key(self):
        self.assertEqual(report.records(thresholds(python={'floor': 1, 'x': 2}), 'f'),
                         {'python': {'floor': 1, 'x': 2}})
        self.assertEqual(report.records(b'{"schema_version": 1}', 'f'), {})
        with self.assertRaisesRegex(ConfigError, 'f: key schema_version'):
            report.records(b'{"schema_version": 1.0}', 'f')


if __name__ == '__main__':
    unittest.main()
