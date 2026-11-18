"""JUnit reports: every test case given its repository-relative file, seconds per file."""
import contextlib
import io
import math
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from test_ratchet import ConfigError, junit

PROJECT = {
    'tests/__init__.py': b'',
    'tests/test_a.py': b'import pytest\n\ndef test_plain():\n    pass\n\nclass TestGroup:\n'
                       b'    def test_inner(self):\n        pass\n\n'
                       b'@pytest.mark.parametrize("n", [1, 2])\ndef test_param(n):\n'
                       b'    assert n\n\ndef test_fail():\n    assert False\n',
    'tests/unit/test_b.py': b'import unittest\n\nclass Case(unittest.TestCase):\n'
                            b'    def test_u(self):\n        self.assertTrue(True)\n',
    'deep/tests/test_a.py': b'def test_deep():\n    pass\n'}
EXPECTED = {('tests.test_a', 'test_plain'): 'tests/test_a.py',
            ('tests.test_a.TestGroup', 'test_inner'): 'tests/test_a.py',
            ('tests.test_a', 'test_param[1]'): 'tests/test_a.py',
            ('tests.test_a', 'test_param[2]'): 'tests/test_a.py',
            ('tests.test_a', 'test_fail'): 'tests/test_a.py',
            ('tests.unit.test_b.Case', 'test_u'): 'tests/unit/test_b.py',
            ('deep.tests.test_a', 'test_deep'): 'deep/tests/test_a.py'}
FILES = sorted(PROJECT)

# vitest 3.2.7 `--reporter=junit` output, verbatim but for the failure body: the classname
# and the suite name are the test file's path, and no case carries a file attribute.
VITEST_JUNIT = """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="4" failures="1" errors="0" time="0.016386741">
    <testsuite name="tests/a.test.ts" timestamp="2026-11-17T02:08:20.463Z" \
hostname="h" tests="3" failures="1" errors="0" skipped="0" time="0.013548785">
        <testcase classname="tests/a.test.ts" name="group &gt; picks" time="0.001786577">
        </testcase>
        <testcase classname="tests/a.test.ts" name="group &gt; inner &gt; nested" \
time="0.000169055">
        </testcase>
        <testcase classname="tests/a.test.ts" name="top" time="0.00967006">
            <failure message="expected 1 to be 2" type="AssertionError">trace</failure>
        </testcase>
    </testsuite>
    <testsuite name="deep/tests/a.test.ts" timestamp="2026-11-17T02:08:20.468Z" \
hostname="h" tests="1" failures="0" errors="0" skipped="0" time="0.002837956">
        <testcase classname="deep/tests/a.test.ts" name="deep" time="0.001163797">
        </testcase>
    </testsuite>
</testsuites>
"""


def cases(text):
    return [(case.get('classname'), case.get('name'), case.get('file'))
            for case in ET.fromstring(text).iter('testcase')]


class Scratch(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.out = io.StringIO()

    def write(self, text, name='junit.xml'):
        path = self.root / name
        path.write_bytes(text.encode('utf-8') if isinstance(text, str) else text)
        return path

    def rewrite(self, text, files):
        with contextlib.redirect_stdout(self.out):
            return junit.rewrite(self.write(text), files)


class RealPytestTest(Scratch):
    def pytest(self, family, cwd, *targets):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('COVERAGE_', 'PYTEST_'))}
        report = self.root / f'{family}-{len(targets)}.xml'
        subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                        '-o', f'junit_family={family}', f'--junitxml={report}', *targets],
                       cwd=cwd, env=env, capture_output=True, check=False)
        return report.read_text(encoding='utf-8')

    def project(self, files):
        project = self.root / 'project'
        for name, data in files.items():
            Path(project, name).parent.mkdir(parents=True, exist_ok=True)
            Path(project, name).write_bytes(data)
        return project

    def test_both_junit_families_get_every_case_its_file(self):
        project = self.project(PROJECT)
        for family in ('xunit1', 'xunit2'):
            with self.subTest(family=family):
                original = self.pytest(family, project, 'tests', 'deep')
                self.assertEqual(len(cases(original)), len(EXPECTED), original)
                rewritten = self.rewrite(original, FILES)
                self.assertEqual({(classname, name): file
                                  for classname, name, file in cases(rewritten)}, EXPECTED)
        self.assertEqual(self.out.getvalue(), '')

    def test_a_run_rooted_below_the_repository_maps_by_suffix(self):
        project = self.project(PROJECT)
        for family in ('xunit1', 'xunit2'):
            with self.subTest(family=family):
                original = self.pytest(family, project / 'tests', 'unit/test_b.py')
                self.assertEqual(cases(original)[0][0], 'unit.test_b.Case')
                self.assertEqual(cases(self.rewrite(original, FILES)),
                                 [('unit.test_b.Case', 'test_u', 'tests/unit/test_b.py')])

    def test_a_collection_error_case_gets_its_file(self):
        project = self.project({'tests/__init__.py': b'',
                                'tests/test_broken.py': b'import nonexistent_module_xyz\n'})
        for family in ('xunit1', 'xunit2'):
            with self.subTest(family=family):
                original = self.pytest(family, project, 'tests')
                self.assertEqual(cases(original)[0][:2], ('', 'tests.test_broken'))
                self.assertEqual(cases(self.rewrite(original, ['tests/test_broken.py'])),
                                 [('', 'tests.test_broken', 'tests/test_broken.py')])


class RewriteTest(Scratch):
    def test_real_vitest_cases_take_the_path_in_their_classname(self):
        rewritten = self.rewrite(VITEST_JUNIT, ['tests/a.test.ts', 'deep/tests/a.test.ts'])
        self.assertEqual([file for _, _, file in cases(rewritten)],
                         ['tests/a.test.ts'] * 3 + ['deep/tests/a.test.ts'])
        self.assertTrue(rewritten.startswith('<?xml version="1.0" encoding="utf-8"?>'))
        failure = ET.fromstring(rewritten).find('.//failure')
        assert failure is not None
        self.assertEqual((failure.get('message'), failure.text), ('expected 1 to be 2', 'trace'))
        self.assertEqual(self.out.getvalue(), '')

    def test_a_case_without_classname_falls_back_to_its_suite_name(self):
        suite = '<testsuite name="tests\\a.test.ts"><testcase name="t" time="1"/></testsuite>'
        for text in (f'<testsuites>{suite}</testsuites>', suite):
            with self.subTest(text=text):
                self.assertEqual(cases(self.rewrite(text, ['tests/a.test.ts'])),
                                 [(None, 't', 'tests/a.test.ts')])

    def test_a_bare_testsuite_root_is_rewritten(self):
        text = '<testsuite name="pytest"><testcase classname="pkg.test_m" name="t"/></testsuite>'
        self.assertEqual(cases(self.rewrite(text, ['src/pkg/test_m.py', 'test_m.py'])),
                         [('pkg.test_m', 't', 'src/pkg/test_m.py')])

    def test_more_matched_segments_beat_a_whole_shorter_module(self):
        text = '<testsuite><testcase classname="a.b.C" name="t"/></testsuite>'
        self.assertEqual(cases(self.rewrite(text, ['a.py', 'x/a/b.py'])),
                         [('a.b.C', 't', 'x/a/b.py')])

    def test_a_wrong_file_attribute_gives_way_to_the_classname(self):
        text = ('<testsuite><testcase classname="tests.test_a" name="t" file="../gone.py"/>'
                '</testsuite>')
        self.assertEqual(cases(self.rewrite(text, ['tests/test_a.py', 'gone.py/y.py'])),
                         [('tests.test_a', 't', 'tests/test_a.py')])

    def test_an_absolute_or_backslashed_file_attribute_maps_to_the_repository_path(self):
        for file in ('/home/u/repo/tests/test_a.py', 'D:\\a\\r\\tests\\test_a.py',
                     '.\\tests\\test_a.py'):
            with self.subTest(file=file):
                text = f'<testsuite><testcase classname="" name="t" file="{file}"/></testsuite>'
                self.assertEqual(
                    cases(self.rewrite(text, ['tests/test_a.py', 'deep/tests/test_a.py'])),
                    [('', 't', 'tests/test_a.py')])

    def test_unresolvable_cases_lose_their_file_and_are_warned_once(self):
        text = ('<testsuite name="pytest">'
                '<testcase classname="nowhere.mod" name="t1" file="../outside.py"/>'
                '<testcase classname="test_x" name="t2" file="test_x.py"/>'
                '<testcase classname="tests.test_a" name="t3"/></testsuite>')
        rewritten = self.rewrite(text, ['a/test_x.py', 'b/test_x.py', 'tests/test_a.py'])
        self.assertEqual(cases(rewritten), [('nowhere.mod', 't1', None), ('test_x', 't2', None),
                                            ('tests.test_a', 't3', 'tests/test_a.py')])
        lines = self.out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('::warning::'))
        self.assertIn('nowhere.mod', lines[0])
        self.assertIn('t2', lines[0])

    def test_invalid_reports_are_configuration_errors_naming_the_file(self):
        for text in (b'<testsuite', b'<coverage/>'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ConfigError, 'junit.xml'):
                    self.rewrite(text, [])


class SecondsPerFileTest(Scratch):
    def seconds(self, text):
        return junit.seconds_per_file(self.write(text))

    def test_times_add_up_per_file(self):
        text = ('<testsuites><testsuite>'
                '<testcase file="tests/a.py" time="0.5"/><testcase file="tests/a.py" time="0.25"/>'
                '<testcase file="tests/b.py" time="1"/><testcase file="tests\\c.py"/>'
                '<testcase name="no file" time="9"/></testsuite></testsuites>')
        self.assertEqual(self.seconds(text), {'tests/a.py': 0.75, 'tests/b.py': 1.0,
                                              'tests/c.py': 0.0})

    def test_the_rewritten_real_vitest_report(self):
        files = ['tests/a.test.ts', 'deep/tests/a.test.ts']
        seconds = junit.seconds_per_file(self.write(self.rewrite(VITEST_JUNIT, files), 'r.xml'))
        self.assertEqual(sorted(seconds), sorted(files))
        self.assertTrue(math.isclose(seconds['tests/a.test.ts'],
                                     0.001786577 + 0.000169055 + 0.00967006))
        self.assertTrue(math.isclose(seconds['deep/tests/a.test.ts'], 0.001163797))

    def test_an_invalid_time_is_a_configuration_error_naming_the_file_and_case(self):
        for time in ('nan', 'inf', '-1', 'abc', '1e400', ''):
            with self.subTest(time=time):
                text = f'<testsuite><testcase file="a.py" name="bad" time="{time}"/></testsuite>'
                with self.assertRaises(ConfigError) as caught:
                    self.seconds(text)
                self.assertIn('junit.xml', str(caught.exception))
                self.assertIn('bad', str(caught.exception))
