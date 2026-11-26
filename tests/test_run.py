"""The run sub-action for pytest, run through its action.yml on fixture projects."""
import contextlib
import io
import json
import os
import platform
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from _action_harness import ROOT, run_action
from test_ratchet import cobertura, run, split
from test_split import make_repo

LINUX = platform.system() == 'Linux'
OS = {'Linux': 'Linux', 'Darwin': 'macOS'}.get(platform.system(), 'Windows')
CELL = f'{OS}-{sys.version_info[0]}.{sys.version_info[1]}'
LIB = b'def f(x):\n    if x:\n        return 1\n    return 2\n'
CHILDREN = b'''import os
import subprocess
import sys

sys.path.insert(0, 'src')
import lib


def test_lib():
    assert lib.f(1) == 1


def test_children():
    assert 'COVERAGE_PROCESS_START' not in os.environ and 'COV_CORE_SOURCE' not in os.environ
    assert os.environ['COVERAGE_FILE'].startswith(os.environ['RUNNER_TEMP'])
    # Started in src, the child still reads the repository's relative omit from the root.
    done = subprocess.run([sys.executable, 'child.py'], cwd='src', env=dict(os.environ),
                          capture_output=True, text=True, check=True)
    assert done.stdout == 'child 3\\n'
    subprocess.run(['node', 'web/tool.js'], check=True)
'''
UNIT = b'import unittest\n\n\nclass T(unittest.TestCase):\n    def test_one(self):\n        pass\n'
PAGE = b'var a = 1;\nvar b = 2;\n'
BROWSER = b'''import json
import os
import subprocess
import sys


def test_browser_and_shell():
    entry = {'url': 'http://localhost:8000/static/web/page.js',
             'functions': [{'ranges': [{'startOffset': 0, 'endOffset': %d, 'count': 1}]}]}
    with open(os.path.join(os.environ['TEST_RATCHET_V8_DIR'], 'page.json'), 'w') as dump:
        json.dump([entry], dump)
    armed = os.environ.get('TEST_RATCHET_SHELL_LOG_DIR')
    assert not armed or os.environ['BASH_ENV'].startswith(os.environ['TMPDIR'])
    script = 'import subprocess; subprocess.run(["bash", "scripts/s.sh"], check=True)'
    subprocess.run([sys.executable, '-c', script], check=True)
''' % len(PAGE)
SCRIPT = b'if [ "${1-}" = x ]; then\n  echo never\nfi\necho ran\n'
MP_LIB = (b'def by_default():\n    return 1\n\n\ndef by_spawn():\n    return 2\n\n\n'
          b'def by_fork():\n    return 3\n\n\ndef by_pool():\n    return 4\n')
MP_TEST = b'''import multiprocessing
import signal
import sys

sys.path.insert(0, 'src')
import mp


def test_multiprocessing_children():
    methods = multiprocessing.get_all_start_methods()
    for method, target in ((None, mp.by_default), ('spawn', mp.by_spawn), ('fork', mp.by_fork)):
        if method and method not in methods:
            continue
        context = multiprocessing.get_context(method)
        child = context.Process(target=target)
        child.start()
        child.join()
        assert child.exitcode == 0
    # A worker that close() and join() let end by itself saves its data; terminate() kills it,
    # and under a Python SIGTERM handler that kill can hang the pool.
    assert not callable(signal.getsignal(signal.SIGTERM))
    pool = multiprocessing.Pool(1)
    assert pool.apply(mp.by_pool) == 4
    pool.close()
    pool.join()
'''
CHDIR = (b'def test_cd(monkeypatch, tmp_path):\n    monkeypatch.chdir(tmp_path)\n'
         b'    import helper\n    assert helper.VALUE == 1\n')
SHARD_ONLY = b'''import subprocess
import sys


def test_a(value):
    # The plugin that keeps the shard to its files reaches no test child.
    child = subprocess.run([sys.executable, '-c', 'import test_ratchet_shard'], check=False)
    assert value == 1 and child.returncode == 1


def test_skipme():
    assert False
'''
NODE_CHILD = (b"import subprocess\n\n\ndef test_x():\n"
              b"    subprocess.run(['node', 'scripts/x.mjs'], check=True)\n")
FAILING = b'def test_good():\n    pass\n\n\ndef test_bad():\n    assert False\n'
EXPLAINED = b'function f() {\n  // explain\n  return 1;\n}\nf();\n'
VENDOR = b'if (a) {}\n/x/.test(a);\n'  # valid, but its slash after } is refused
EXPLAINED_DUMP = (b"import json\nimport os\n\n\ndef test_page():\n    entries = [{'url': url,"
                  b" 'functions': [{'ranges': [{'startOffset': 0, 'endOffset': size,"
                  b" 'count': 1}]}]}\n        for url, size in (('http://h/page.js', %d),"
                  b" ('http://h/vendor/v.js', %d))]\n    with open(os.path.join(os.environ["
                  b"'TEST_RATCHET_V8_DIR'], 'p.json'), 'w') as dump:\n        json.dump(entries,"
                  b" dump)\n" % (len(EXPLAINED), len(VENDOR)))
DATA_PAGE = b'<html>\n<script>\nconst DATA = {"n": 1};\nshow(DATA);\n</script>\n'
DATA_DUMP = (b"import json\nimport os\n\n\ndef test_page():\n    source = %a\n    entry = {'url':"
             b" 'http://h/page.html', 'source': source, 'functions': [{'ranges': [{'startOffset':"
             b" 0, 'endOffset': len(source), 'count': 1}]}]}\n    with open(os.path.join("
             b"os.environ['TEST_RATCHET_V8_DIR'], 'p.json'), 'w') as dump:\n"
             b"        json.dump([entry], dump)\n"
             % 'const DATA = {"n": 22};\nshow(DATA);\n')


def suite_names(junit):
    return sorted((case.get('file', ''), case.get('name', ''))
                  for case in ET.parse(junit).iter('testcase'))


class Fixtures(unittest.TestCase):
    """Fixture projects driven through the run action; each upload is kept by its name."""

    def setUp(self):
        self.scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.uploads = {}

    def project(self, files):
        return make_repo(Path(tempfile.mkdtemp(dir=self.scratch)), files)

    def upload(self, values):
        target = self.scratch / 'uploads' / values['name']
        shutil.copytree(values['path'], target)
        self.uploads[values['name']] = target

    def run_in(self, repo, interpreter='python3', tools=('node', 'bash'), **inputs):
        inputs.setdefault('runner', 'pytest')
        inputs.setdefault('tests', 'tests/test_*.py')
        result = run_action('run', inputs, repo, interpreter, tools=tools,
                            uses={'actions/upload-artifact': self.upload},
                            runner_env={'TMPDIR': str(self.scratch)})
        self.assertEqual(list(self.scratch.glob('test-ratchet-shell-*')), [])  # hook gone
        return result

    def artifact(self, result):
        self.assertIn(result.outputs['name'], self.uploads, result.stdout + result.stderr)
        return self.uploads[result.outputs['name']]


class RunTest(Fixtures):

    def test_the_tests_their_python_and_node_children_are_measured(self):
        if not shutil.which('node'):
            self.skipTest('needs node on PATH')
        repo = self.project({
            '.coveragerc': b'[run]\nomit =\n    src/omitted.py\n', 'src/lib.py': LIB,
            'src/child.py': b'import omitted\nprint("child", omitted.g())\n',
            'src/omitted.py': b'def g():\n    return 3\n',
            'web/tool.js': b'function used(x) {\n  return x + 1;\n}\nfunction unused() {\n'
                           b'  return 0;\n}\nused(1);\n',
            'tests/test_children.py': CHILDREN, 'tests/test_unit.py': UNIT})
        # The suite's own measurer, armed here when the suite runs unmeasured, must not reach it.
        armed = {'COVERAGE_PROCESS_START': str(ROOT / '.coveragerc'),
                 'COVERAGE_FILE': str(self.scratch / 'suite' / '.coverage')}
        with mock.patch.dict(os.environ, {**armed, **os.environ, 'COV_CORE_SOURCE': 'x'}):
            result = self.run_in(repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.outputs, {'name': f'test-ratchet-{CELL}-all', 'passed': 'true'})
        self.assertIn('workers', result.stdout)
        artifact = self.artifact(result)
        self.assertEqual(sorted(path.name for path in artifact.iterdir()),
                         ['coverage-javascript.xml', 'coverage-python.xml', 'junit.xml',
                          'manifest.json'])
        self.assertEqual(json.loads((artifact / 'manifest.json').read_text(encoding='utf-8')), {
            'schema_version': 1, 'runner': 'pytest', 'cell': CELL, 'shard': None,
            'files': ['tests/test_children.py', 'tests/test_unit.py'], 'passed': True})
        python = cobertura.parse(artifact / 'coverage-python.xml', '/elsewhere')
        self.assertEqual(sorted(python), ['src/child.py', 'src/lib.py', 'tests/test_children.py',
                                          'tests/test_unit.py'])
        self.assertEqual((python['src/lib.py'], python['src/child.py']),
                         ({1: 1, 2: 1, 3: 1, 4: 0}, {1: 1, 2: 1}))
        self.assertEqual(cobertura.parse(artifact / 'coverage-javascript.xml', '/elsewhere'),
                         {'web/tool.js': {1: 1, 2: 1, 3: 1, 4: 0, 5: 0, 6: 0, 7: 1}})
        self.assertEqual(suite_names(artifact / 'junit.xml'), [
            ('tests/test_children.py', 'test_children'), ('tests/test_children.py', 'test_lib'),
            ('tests/test_unit.py', 'test_one')])

    def test_browser_dumps_and_shell_scripts_a_child_runs_are_measured(self):
        repo = self.project({'tests/test_browser.py': BROWSER, 'web/page.js': PAGE,
                             'scripts/s.sh': SCRIPT, 'tests/test_mp.py': MP_TEST,
                             'src/mp.py': MP_LIB})
        result = self.run_in(repo, shell='scripts/*.sh\nnone/*.sh', args='-n 0', source='src')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Matched before the tests run and read after: the pattern matching nothing warns once.
        self.assertEqual([line for line in result.stdout.splitlines() if 'none/' in line],
                         ['::warning::input shell patterns matched no file git tracks:'
                          ' none/*.sh'] * LINUX)
        artifact = self.artifact(result)
        # The default start method (forkserver on Linux, spawn on macOS), spawn, and fork.
        self.assertEqual(cobertura.parse(artifact / 'coverage-python.xml', '/elsewhere'),
                         {'src/mp.py': {1: 1, 2: 1, 5: 1, 6: 1, 9: 1, 10: 1, 13: 1, 14: 1}})
        self.assertEqual(cobertura.parse(artifact / 'coverage-javascript.xml', '/elsewhere'),
                         {'web/page.js': {1: 1, 2: 1}})
        shell = artifact / 'coverage-shell.xml'
        if LINUX:
            self.assertEqual(cobertura.parse(shell, '/elsewhere'),
                             {'scripts/s.sh': {1: 1, 2: 0, 4: 1}})
        else:
            self.assertFalse(shell.exists())
            self.assertIn('shell coverage is Linux only', result.stdout)

    def test_javascript_keeps_only_the_tracked_files_it_matches_in_the_report(self):
        if not shutil.which('node'):
            self.skipTest('needs node on PATH')
        repo = self.project({'tests/test_x.py': NODE_CHILD, 'src/unloaded.mjs': b'export {};\n',
                             'scripts/x.mjs': b"import { f } from '../src/m.mjs';\nf();\n",
                             'src/m.mjs': b'export function f() {\n  return 1;\n}\n'})
        reports = {}
        for javascript in (' , , ', ' src/** , none/** '):
            result = self.run_in(repo, args='-n 0', javascript=javascript, name=str(len(reports)))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual([line for line in result.stdout.splitlines() if 'none/' in line],
                             ['::warning::input javascript patterns matched no file git tracks:'
                              ' none/**'] * ('none/' in javascript))
            reports[javascript] = cobertura.parse(
                self.artifact(result) / 'coverage-javascript.xml', '/elsewhere')
        # Unmatched, the script leaves; matched but never loaded, the module stays out.
        whole = reports[' , , ']
        self.assertEqual(sorted(whole), ['scripts/x.mjs', 'src/m.mjs'])
        self.assertEqual(reports[' src/** , none/** '], {'src/m.mjs': whole['src/m.mjs']})

    def test_javascript_code_only_leaves_out_the_lines_holding_only_comments(self):
        repo = self.project({'tests/test_page.py': EXPLAINED_DUMP, 'page.js': EXPLAINED,
                             'vendor/v.js': VENDOR})
        # The vendor script code-only refuses passes when javascript leaves it out of the report.
        for code_only, javascript, lines in (
                ('true', 'page.js', {'page.js': [1, 3, 4, 5]}),
                ('false', '', {'page.js': [1, 2, 3, 4, 5], 'vendor/v.js': [1, 2]}),
                ('', 'page.js', {'page.js': [1, 2, 3, 4, 5]})):
            with self.subTest(code_only=code_only):
                inputs = {'javascript-code-only': code_only} if code_only else {}
                result = self.run_in(repo, args='-n 0', name=code_only or 'unset',
                                     javascript=javascript, **inputs)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(cobertura.parse(
                    self.artifact(result) / 'coverage-javascript.xml', '/elsewhere'),
                                 {path: dict.fromkeys(hit, 1) for path, hit in lines.items()})

    def test_javascript_data_lines_place_a_page_built_over_other_data(self):
        repo = self.project({'tests/test_page.py': DATA_DUMP, 'page.html': DATA_PAGE})
        for data, lines in (('const DATA = .*;', {'page.html': {3: 1, 4: 1}}), ('', {})):
            with self.subTest(data=data):
                result = self.run_in(repo, args='-n 0', name=data and 'set' or 'unset',
                                     **{'javascript-data-lines': data})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = self.artifact(result) / 'coverage-javascript.xml'
                self.assertEqual(cobertura.parse(report, '/elsewhere') if lines else
                                 report.exists(), lines or False)
                self.assertEqual('http://h/page.html (page.html)' in result.stdout, not lines)

    def test_a_failing_test_uploads_a_complete_artifact_and_fails_the_action(self):
        repo = self.project({'tests/test_x.py': FAILING, 'src/lib.py': LIB, 'src/__init__.py': LIB,
                             'tools/t.py': b'X = 1\n', 'tools/__init__.py': LIB, 'src/x.py': b'('})
        result = self.run_in(repo, cell='mine', args='-n 2', source=' src , tools ')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.outputs, {'name': 'test-ratchet-mine-all', 'passed': 'false'})
        artifact = self.artifact(result)
        self.assertEqual(sorted(path.name for path in artifact.iterdir()),
                         ['coverage-python.xml', 'junit.xml', 'manifest.json'])
        manifest = json.loads((artifact / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual((manifest['cell'], manifest['passed']), ('mine', False))
        self.assertEqual(cobertura.parse(artifact / 'coverage-python.xml', '/elsewhere'),
                         {**dict.fromkeys(('src/__init__.py', 'src/lib.py', 'tools/__init__.py'),
                                          {1: 0, 2: 0, 3: 0, 4: 0}), 'tools/t.py': {1: 0}})
        failed = [case.get('name') for case in ET.parse(artifact / 'junit.xml').iter('testcase')
                  if case.find('failure') is not None]
        self.assertEqual(failed, ['test_bad'])
        self.assertIn('the tests failed', result.stdout)
        self.assertNotIn('dropped', result.stdout)

    def test_args_reach_pytest_after_the_actions_own_and_no_coverage_floor_applies(self):
        repo = self.project({'tests/test_x.py': FAILING,
                             '.coveragerc': b'[report]\nfail_under = 100\n'})
        result = self.run_in(repo, interpreter='python',
                             args='-n 0 -k "good or nothing" nosuch.py tests/test_x.py::test_bad')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('workers', result.stdout)
        # A path or node id of its own is no shard file: the whole shard file ran, and it says so.
        self.assertIn('::warning::input args names nosuch.py, tests/test_x.py::test_bad: ignored',
                      result.stdout)
        self.assertEqual(suite_names(self.artifact(result) / 'junit.xml'),
                         [('tests/test_x.py', 'test_good')])
        self.assertEqual(result.outputs['passed'], 'true')

    def test_a_runner_that_dies_before_its_reports_fails_with_the_manifest_alone(self):
        repo = self.project({'tests/test_x.py': FAILING,
                             'tests/conftest.py': b'import os\nos._exit(9)\n'})
        result = self.run_in(repo)
        self.assertEqual((result.returncode, result.outputs['passed']), (1, 'false'))
        self.assertEqual([path.name for path in self.artifact(result).iterdir()], ['manifest.json'])

    def test_only_the_shards_files_run_whatever_the_repository_adds(self):
        # A directory, a file and a node id in addopts; a conftest that would collect anything;
        # and a conftest beside them that fails to import.
        for index, added in enumerate(('extra', 'extra/test_extra.py',
                                       'extra/test_good.py::test_e')):
            with self.subTest(added=added):
                repo = self.project({
                    'pytest.ini': f'[pytest]\naddopts = {added} -k "not skipme"\n'.encode(),
                    'conftest.py': b'def pytest_ignore_collect(collection_path, config):\n'
                                   b'    return False\n',
                    'tests/conftest.py': b'import pytest\n\n\n@pytest.fixture\ndef value():\n'
                                         b'    return 1\n',
                    'tests/test_a.py': SHARD_ONLY, 'extra/conftest.py': b'raise ImportError\n',
                    'extra/test_extra.py': b'import nosuchmodule\n',
                    'extra/test_good.py': b'def test_e():\n    pass\n'})
                result = self.run_in(repo, args='-n 2', name=str(index))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(suite_names(self.artifact(result) / 'junit.xml'),
                                 [('tests/test_a.py', 'test_a')])
                self.assertNotIn('::warning::', result.stdout)

    def test_a_test_that_changes_directory_still_imports_the_repositorys_modules(self):
        repo = self.project({'helper.py': b'VALUE = 1\n', 'tests/test_cd.py': CHDIR})
        for args in ('-n 0 --cov-report=xml', '-n 2'):  # a report with no path
            with self.subTest(args=args):
                result = self.run_in(repo, args=args, name=args, source='nothing')  # no data
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_usage_error_or_no_test_fails_naming_what_pytest_meant(self):
        repo = self.project({'tests/test_x.py': FAILING})
        for args, said in (('--no-such-option', 'pytest exited 4, a usage or configuration'),
                           ('-n 0 -k nothing', 'pytest exited 5, no tests were collected')):
            with self.subTest(args=args):
                result = self.run_in(repo, args=args, name=args)
                self.assertEqual((result.returncode, result.outputs['passed']), (1, 'false'))
                self.assertIn(result.outputs['name'], self.uploads)
                self.assertIn(said, result.stdout)
                self.assertNotIn('the tests failed', result.stdout)

    def test_each_shard_runs_exactly_its_files_from_the_plan_split(self):
        big = b'def test_it():\n    pass\n' + b'#' * 400 + b'\n'
        repo = self.project({'tests/test_a.py': big, 'deep/tests/test_a.py': big,
                             'tests/test_b.py': b'def test_b():\n    pass\n'})
        tests = 'tests/test_*.py\ndeep/tests/test_*.py'
        seen = []
        for shard in ('1/2', '2/2'):
            result = self.run_in(repo, tests=tests, shard=shard, cell='c', args='-n 0')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            artifact = self.artifact(result)
            files = json.loads((artifact / 'manifest.json').read_text(encoding='utf-8'))['files']
            self.assertEqual(files, split.shard_files(repo, tests, None, 'c', shard))
            self.assertEqual(sorted({path for path, _ in suite_names(artifact / 'junit.xml')}),
                             files)
            seen.append(files)
        self.assertEqual(seen, [['deep/tests/test_a.py', 'tests/test_b.py'], ['tests/test_a.py']])

    def test_two_cells_with_one_shard_get_distinct_names(self):
        repo = self.project({'tests/test_x.py': UNIT})
        names = [self.run_in(repo, shard=' 1/1 ', args='-n 0', **inputs).outputs['name']
                 for inputs in ({'cell': 'linux-a'}, {'cell': 'linux b'},
                                {'cell': 'linux-a', 'name': 'my/own name'})]
        self.assertEqual(names, ['test-ratchet-linux-a-1_1', 'test-ratchet-linux_b-1_1',
                                 'test-ratchet-my_own_name'])
        self.assertEqual(sorted(self.uploads), sorted(names))

    def test_junit_cases_carry_their_test_files(self):
        repo = self.project({
            'tests/__init__.py': b'',
            'tests/_base.py': b'import unittest\n\n\nclass Base(unittest.TestCase):\n'
                              b'    def test_inherited(self):\n        pass\n',
            'tests/test_inherit.py': b'from tests._base import Base\n\n\nclass TestChild(Base):\n'
                                     b'    pass\n',
            'tests/test_param.py': b'import pytest\n\n\n@pytest.mark.parametrize("x", ["a/b::c"])\n'
                                   b'def test_p(x):\n    pass\n',
            'tests/test_broken.py': b'import nosuchmodule\n',
            'tests/v1.2/test_dot.py': b'def test_it():\n    pass\n'})
        result = self.run_in(repo, tests='tests/**/test_*.py', args='-n 2')
        self.assertEqual(result.outputs['passed'], 'false')
        self.assertEqual(suite_names(self.artifact(result) / 'junit.xml'), [
            ('tests/test_broken.py', 'tests.test_broken'),
            ('tests/test_inherit.py', 'test_inherited'),
            ('tests/test_inherit.py', 'test_inherited'),
            ('tests/test_param.py', 'test_p[a/b::c]'), ('tests/v1.2/test_dot.py', 'test_it')])
        self.assertNotIn('::warning::', result.stdout)

    def test_invalid_configuration_fails_naming_the_input(self):
        repo = self.project({'tests/test_x.py': b"def test_x():\n    open('ran', 'w').close()\n",
                             'bad.json': b'{"schema_version": 2}'})
        for inputs, named in (({'runner': 'nose'}, 'runner'), ({'shard': '3/2'}, 'shard'),
                              ({'tests': 'none/*.py'}, 'tests'), ({'cell': 'a\nb'}, 'cell'),
                              ({'args': '-k "open'}, 'args'), ({'times': 'bad.json'}, 'bad.json'),
                              ({'javascript': 'none/**'}, 'input javascript matched no file'),
                              ({'javascript-code-only': 'yes'},
                               "input javascript-code-only must be 'true' or 'false', got 'yes'"),
                              ({'javascript-data-lines': 'show('},
                               'input javascript-data-lines is not a regular expression'),
                              ({'javascript-all-files': 'true'}, 'input javascript-all-files needs'
                                                                 ' input javascript')):
            with self.subTest(inputs=inputs):
                result = self.run_in(repo, **inputs)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(named, result.stderr)
                self.assertEqual((result.outputs, self.uploads), ({'name': '', 'passed': ''}, {}))
                self.assertFalse((repo / 'ran').exists())  # no test ran
        self.assertEqual(self.run_in(repo, name='ran').returncode, 0)
        self.assertTrue((repo / 'ran').exists())

    def test_a_shell_input_matching_no_tracked_file_fails_before_the_tests_run(self):
        repo = self.project({'tests/test_x.py': b"def test_x():\n    open('ran', 'w').close()\n"})
        result = self.run_in(repo, shell='scripts/*.sh')
        if not LINUX:
            # Where shell is not measured its patterns are not matched: the tests run.
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((repo / 'ran').exists())
            return
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stderr.splitlines()[-1], 'test-ratchet run: input shell matched'
                         ' no file git tracks: scripts/*.sh')
        self.assertFalse((repo / 'ran').exists())
        self.assertEqual((result.outputs, self.uploads), ({'name': '', 'passed': ''}, {}))
        # The marker is live: without the input the test runs and leaves it.
        self.assertEqual(self.run_in(repo).returncode, 0)
        self.assertTrue((repo / 'ran').exists())

    def test_what_the_run_wrote_and_cannot_be_read_fails_it_and_the_rest_uploads(self):
        # A dump that sorts before the good one, then a JUnit report cut short under a failure.
        broken = BROWSER.replace(b"    script = ", b"    with open(os.path.join(os.environ["
                                 b"'TEST_RATCHET_V8_DIR'], 'bad%\\n.json'), 'w') as dump:\n"
                                 b"        dump.write('{')\n    script = ")
        repo = self.project({'tests/test_browser.py': broken, 'web/page.js': PAGE,
                             'scripts/s.sh': SCRIPT})
        result = self.run_in(repo, args='-n 0')
        self.assertEqual((result.returncode, result.outputs['passed']), (1, 'false'),
                         result.stdout + result.stderr)
        [error] = [line for line in result.stdout.splitlines() if line.startswith('::error::')]
        # A newline in the reason stays inside the one annotation.
        self.assertRegex(error, r'^::error::V8 coverage dump \S+bad%25%0A\.json cannot be read as'
                                r' JSON: ')
        artifact = self.artifact(result)
        self.assertEqual(sorted(path.name for path in artifact.iterdir()),
                         ['coverage-javascript.xml', 'coverage-python.xml', 'junit.xml',
                          'manifest.json'])
        self.assertFalse(json.loads((artifact / 'manifest.json').read_text(encoding='utf-8'))
                         ['passed'])
        self.assertEqual(cobertura.parse(artifact / 'coverage-javascript.xml', '/elsewhere'),
                         {'web/page.js': {1: 1, 2: 1}})
        cut = (b"def pytest_unconfigure(config):\n"
               b"    for path in (config.option.xmlpath, config.option.cov_report['xml']):\n"
               b"        open(path, 'w').write('<cut>')\n")
        repo = self.project({'tests/test_x.py': FAILING, 'conftest.py': cut})
        result = self.run_in(repo, args='-n 0', name='cut')
        self.assertEqual((result.returncode, result.outputs['passed']), (1, 'false'))
        [error] = [line for line in result.stdout.splitlines() if line.startswith('::error::')]
        self.assertRegex(error, r'^::error::the tests failed: their reports are in the uploaded'
                         r' artifact; coverage report \S+coverage\.xml is not valid XML: [^;]+;'
                         r' JUnit report \S+junit\.xml is not valid XML: ')
        self.assertEqual([path.name for path in self.artifact(result).iterdir()], ['manifest.json'])

    @unittest.skipUnless(LINUX, 'shell coverage is measured on Linux only')
    def test_a_script_shell_matched_that_a_test_deletes_fails_the_run_and_the_rest_uploads(self):
        gone = (b"import os\nimport subprocess\n\n\ndef test_x():\n"
                b"    subprocess.run(['bash', 'scripts/s.sh'], check=True)\n"
                b"    os.remove('scripts/s.sh')\n")
        repo = self.project({'tests/test_x.py': gone, 'scripts/s.sh': SCRIPT})
        result = self.run_in(repo, shell='scripts/*.sh')
        self.assertEqual((result.returncode, result.outputs['passed']), (1, 'false'),
                         result.stdout + result.stderr)
        [error] = [line for line in result.stdout.splitlines() if line.startswith('::error::')]
        self.assertRegex(error, r'^::error::input shell matches scripts/s\.sh, which cannot be'
                         r' read after the tests ran: ')
        self.assertEqual(sorted(path.name for path in self.artifact(result).iterdir()),
                         ['coverage-python.xml', 'junit.xml', 'manifest.json'])

    def test_outside_a_runner_step_is_a_configuration_error(self):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {'RUNNER_TEMP': ''}), contextlib.redirect_stderr(stderr):
            self.assertEqual(run.main(), 2)
        self.assertIn('RUNNER_TEMP', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
