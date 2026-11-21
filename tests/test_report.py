"""The report driver: the gated cell's merged coverage against the floor at base-ref."""
import importlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from fractions import Fraction
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

    def test_above_precision_1_the_exact_value_is_truncated(self):
        for precision in range(2, 7):
            for measurable in [*range(0, 121), 10000, 100000, 1000000]:
                for covered in {*range(0, min(measurable, 120) + 1),
                                *range(max(measurable - 30, 0), measurable + 1)}:
                    exact = Fraction(100 * covered, measurable) if measurable else 100
                    whole, part = divmod(math.floor(exact * 10 ** precision), 10 ** precision)
                    self.assertEqual(report.percent(covered, measurable, precision),
                                     f'{whole}.{part:0{precision}d}',
                                     (precision, covered, measurable))

    def test_a_truncated_figure_is_unclamped_and_never_exceeds_the_coverage(self):
        # 93.86 - 1e-30 has more nines than a Decimal context or a float holds: rounded
        # first, it would truncate to 93.86.
        for covered, measurable, precision, shown in (
                (1, 100000, 2, '0.00'), (99999, 100000, 2, '99.99'), (0, 0, 2, '100.00'),
                (5, 5, 3, '100.000'), (0, 5, 2, '0.00'), (23464, 25000, 2, '93.85'),
                (9386 * 10 ** 28 - 1, 10 ** 32, 2, '93.85'),
                (9999999 * 10 ** 25 - 1, 10 ** 32, 6, '99.999989'),
                (23463, 25000, 6, '93.852000')):
            with self.subTest(covered=covered, measurable=measurable, precision=precision):
                self.assertEqual(report.percent(covered, measurable, precision), shown)

    def test_above_precision_1_the_gate_compares_the_exact_coverage(self):
        # 93.856 shows 93.85 at 2 and passes a 93.853 floor; at 1 the rounded figure decides.
        for covered, measurable, floor, precision, below in (
                (23464, 25000, 93.86, 2, True), (23465, 25000, 93.86, 2, False),
                (23464, 25000, 93.856, 2, False), (23464, 25000, 93.8561, 2, True),
                (23464, 25000, 93.853, 2, False), (0, 0, 100, 2, False), (0, 0, 100.0, 6, False),
                (0, 5, 0, 2, False), (23463, 25000, 93.86, 1, False),
                (23458, 25000, 93.81, 1, True)):
            with self.subTest(covered=covered, floor=floor, precision=precision):
                total = report.Total('python', covered, measurable,
                                     report.percent(covered, measurable, precision), floor,
                                     precision)
                self.assertEqual(total.below, below)
        # With at most `precision` decimals in the floor, the shown figure agrees with the gate.
        for covered in range(0, 401):
            for hundredths in range(0, 10001, 7):
                total = report.Total('python', covered, 400, report.percent(covered, 400, 2),
                                     hundredths / 100, 2)
                self.assertEqual(total.below, Decimal(total.shown) * 100 < hundredths,
                                 (covered, hundredths))


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
        self.assertIn(f'Floors from the thresholds file {THRESHOLDS}.', summary)

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

    def test_input_precision_sets_the_decimals_shown_and_gated(self):
        # One decimal passes 93.852 against 93.86 and fails 93.832 against 93.81; above it the
        # exact value is gated, so 93.856 fails 93.86 and passes 93.853 though it shows 93.85.
        for covered, floor, precision, shown, code in (
                (23463, 93.86, '', '93.9', 0), (23463, 93.86, '1', '93.9', 0),
                (23463, 93.86, '2', '93.85', 1), (23463, 93.86, '6', '93.852000', 1),
                (23458, 93.81, '', '93.8', 1), (23458, 93.81, '2', '93.83', 0),
                (23464, 93.86, '2', '93.85', 1), (23465, 93.86, '2', '93.86', 0),
                (23464, 93.853, '2', '93.85', 0), (23464, 93.8561, '3', '93.856', 1)):
            with self.subTest(covered=covered, floor=floor, precision=precision):
                self.commit(python={'floor': floor, 'measured': 99.0})
                artifact(self.artifacts, 'test-ratchet-c-all',
                         coverage={'python': lines(covered, 25000)})
                result, summary = self.report(precision=precision)
                self.assertEqual(result.returncode, code, result.stderr)
                verdict = 'fail' if code else 'pass'
                self.assertIn(f'| python | {covered} / 25000 | {shown}% | {floor} | {verdict} |',
                              summary.splitlines())
                self.assertIn(f'test-ratchet report: python {shown}% ({covered} / 25000 lines),'
                              f' floor {floor}', result.stdout.splitlines())
                self.assertEqual(f'python coverage {shown}% is below the floor {floor}'
                                 in result.stderr, bool(code))

    def test_no_measurable_lines_count_as_100_at_every_precision(self):
        self.commit(python={'floor': 100, 'measured': 100})
        artifact(self.artifacts, 'test-ratchet-c-all', coverage={'python': {'src/a.py': {}}})
        for precision, shown in (('1', '100.0'), ('2', '100.00'), ('6', '100.000000')):
            with self.subTest(precision=precision):
                result, summary = self.report(precision=precision)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'| python | 0 / 0 | {shown}% | 100 | pass |', summary)

    def test_a_pull_request_lowering_its_floor_is_gated_by_the_bases(self):
        base = git(self.repo, 'rev-parse', 'HEAD').stdout.decode().strip()
        self.commit(python={'floor': 50.0, 'measured': 51.5, 'cell': 'c'})
        artifact(self.artifacts, 'test-ratchet-c-all', coverage={'python': lines(80, 100)})
        result, summary = self.report(base_ref=base)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(f'python coverage 80.0% is below the floor 85.0 (thresholds file'
                      f' {THRESHOLDS} at base-ref {base})', result.stderr)
        self.assertIn('| python | 80 / 100 | 80.0% | 85.0 | fail |', summary)
        for blank in ('', ' '):
            result, summary = self.report(base_ref=blank)
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
            self.assertIn(f'- `{named}`', summary)
            self.assertIn(f'::warning::left out of the merge: {named}', result.stdout)

    def test_nothing_merged_is_said_and_nothing_is_gated(self):
        self.commit()
        artifact(self.artifacts, 'test-ratchet-c-all', passed=False,
                 coverage={'python': lines(0, 10)})
        result, summary = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No coverage was merged', summary)

    def test_a_recorded_language_nothing_measured_fails_the_gate(self):
        # Its report lost (a passed artifact without it, or only a failed one): never a pass.
        for passed, coverage in ((True, {'shell': lines(1, 3, 'run.sh')}), (True, {}),
                                 (False, {'python': lines(9, 10)})):
            with self.subTest(passed=passed, coverage=coverage):
                folder = artifact(self.artifacts, 'test-ratchet-c-all', passed=passed,
                                  coverage=coverage)
                result, summary = self.report()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('test-ratchet report: python has a recorded floor 85.0 but'
                              f' nothing measured it (thresholds file {THRESHOLDS})',
                              result.stderr.splitlines())
                self.assertIn('| python | nothing measured | none | 85.0 | fail: recorded floor'
                              ' but nothing measured |', summary.splitlines())
                self.assertNotIn('No coverage was merged', summary)
                for item in folder.iterdir():
                    item.unlink()
                folder.rmdir()

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
                self.assertNotIn('Floors from', summary)

    def test_the_records_cell_chooses_the_gated_cell_and_the_input_overrides_it(self):
        artifact(self.artifacts, 'test-ratchet-c-all', coverage={'python': lines(9, 10)})
        artifact(self.artifacts, 'test-ratchet-d-all', cell='d',
                 coverage={'python': lines(1, 10)})
        result, summary = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('| python | 9 / 10 |', summary)
        self.assertNotIn('::notice::', result.stdout)
        result, summary = self.report(cell=' d ')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('| python | 1 / 10 |', summary)
        self.assertIn('::notice::input cell d overrides the cell the records name: python (c)',
                      result.stdout)

    def test_an_invalid_thresholds_file_or_base_ref_exits_2_naming_it(self):
        artifact(self.artifacts, 'test-ratchet-c-all', coverage={'python': lines(9, 10)})
        (self.repo / 'dir').mkdir()
        (self.repo / 'dir' / 'f').write_text('x', encoding='utf-8')
        git(self.repo, 'add', 'dir/f')
        git(self.repo, 'commit', '-q', '-m', 'dir')
        tree = git(self.repo, 'rev-parse', 'HEAD^{tree}').stdout.decode().strip()
        for data, inputs, named in (
                (b'{', {}, 'not JSON'), (b'[]', {}, 'schema_version'),
                (json.dumps({'schema_version': 1, 'coverage': []}).encode(), {}, 'coverage'),
                (thresholds(python={'floor': True}), {}, 'coverage.python.floor'),
                (thresholds(shell={'floor': 101}), {}, 'coverage.shell.floor'),
                (thresholds(javascript={'measured': 1}), {}, 'coverage.javascript.floor'),
                (thresholds(python=[]), {}, 'coverage.python.floor'),
                (thresholds(python={'floor': 1, 'cell': ''}), {}, 'coverage.python.cell'),
                (thresholds(python={'floor': 1, 'cell': 'a\tb'}), {}, 'coverage.python.cell'),
                (b'{"schema_version": 1, "gap": -1}', {},
                 f'thresholds file {THRESHOLDS}: key gap must be a non-negative number, got -1'),
                (b'{"schema_version": 1, "hysteresis": "x"}', {}, 'key hysteresis must be'),
                (None, {'base_ref': tree}, f'base-ref {tree}'),
                (None, {'base_ref': 'nope'}, 'base-ref nope'),
                (None, {'base_ref': '--all'}, 'base-ref --all'),
                (None, {'base_ref': 'HEAD', 'thresholds': 'dir'},
                 'dir at base-ref HEAD is not a file'),
                (None, {'thresholds': 'dir'}, 'dir'),
                (None, {'hysteresis': 'abc'}, "input hysteresis must be a non-negative number,"
                                              " got 'abc'"), (None, {'gap': '-1'}, 'input gap'),
                *((None, {'precision': text}, 'input precision must be a whole number from 1 to'
                   f' 6, got {text!r}')
                  for text in ('0', '7', '1.5', '+2', '-1', 'abc', '\u0662'))):
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
        result, summary = self.report()
        self.assertEqual(result.returncode, 2)
        self.assertIn('test-ratchet-c-all', result.stderr)
        self.assertIn('refused the artifacts\n\n`artifact test-ratchet-c-all: manifest', summary)
        result, summary = self.report(artifacts='')
        self.assertEqual(result.returncode, 2)
        self.assertIn('TEST_RATCHET_ARTIFACTS', result.stderr)
        self.assertEqual(summary, '')

    def test_a_missing_git_repository_is_a_configuration_error(self):
        artifact(self.artifacts, 'test-ratchet-c-all')
        self.repo = self.scratch
        result, _ = self.report()
        self.assertEqual(result.returncode, 2)
        self.assertIn('repository', result.stderr)

    def test_the_gap_and_hysteresis_keys_are_numbers_of_at_least_0(self):
        for name in ('hysteresis', 'gap'):
            for text, held in (('0', {name: Decimal(0)}), ('0.0', {name: Decimal('0.0')}),
                               ('2.5', {name: Decimal('2.5')}), ('1e308', {name: Decimal('1e308')}),
                               ('1' + '0' * 30, {name: Decimal(10) ** 30})):
                with self.subTest(name=name, text=text):
                    found = report.thresholds(f'{{"schema_version": 1, "{name}": {text}}}'.encode(),
                                              'f')
                    self.assertEqual(found, ({}, held))
            for text, shown in (('-1', '-1'), ('-0.5', '-0.5'), ('true', 'True'),
                                ('false', 'False'), ('"1"', "'1'"), ('null', 'None'),
                                ('[]', '[]'), ('{}', '{}'), ('NaN', 'nan'), ('Infinity', 'inf'),
                                ('-Infinity', '-inf')):
                with self.subTest(name=name, text=text):
                    with self.assertRaisesRegex(ConfigError, f'^f: key {name} must be a'
                                                f' non-negative number, got {re.escape(shown)}$'):
                        report.thresholds(f'{{"schema_version": 1, "{name}": {text}}}'.encode(),
                                          'f')
        self.assertEqual(report.thresholds(thresholds(python={'floor': 1}), 'f'),
                         ({'python': {'floor': 1}}, {}))

    def test_records_hold_the_gated_languages_and_ignore_every_other_key(self):
        self.assertEqual(report.records(thresholds(python={'floor': 1, 'x': 2}), 'f'),
                         {'python': {'floor': 1, 'x': 2}})
        self.assertEqual(report.records(b'{"schema_version": 1}', 'f'), {})
        with self.assertRaisesRegex(ConfigError, 'f: key schema_version'):
            report.records(b'{"schema_version": 1.0}', 'f')


if __name__ == '__main__':
    unittest.main()
