"""The default-branch ratchet: raise-only records, the times file and the records commit."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from _action_harness import ROOT
from _github_double import GitHub, answer
from test_collect import artifact, lines
from test_ratchet import ConfigError, ratchet
from test_split import git, make_repo

MAIN = ROOT / 'report' / 'main.py'
THRESHOLDS = '.github/ci-thresholds.json'
TIMES = 'ci/times.json'
AGENT = shutil.which('ssh-agent')
GIT = shutil.which('git')
DOCUMENT = {'schema_version': 1, 'other': {'kept': [1]},
            'coverage': {'go': {'any': 'shape'},
                         'python': {'floor': 85.0, 'measured': 86.5, 'note': 'kept', 'cell': 'c'},
                         'rust': {'any': 'shape'}},
            'last': True}
RECORDED_TIMES = {'schema_version': 1, 'cells': {
    'c': {'tests/test_a.py': 4.0, 'tests/test_b.py': 10.0}, 'gone': {'x.py': 1.0}}}


def dumped(document):
    return (json.dumps(document, indent=2) + '\n').encode()


def junit(folder, seconds):
    cases = ''.join(f'<testcase file="{name}" name="t{index}" time="{value}"/>'
                    for index, (name, value) in enumerate(seconds))
    (folder / 'junit.xml').write_text(f'<testsuite>{cases}</testsuite>', encoding='utf-8')


class RecordRuleTest(unittest.TestCase):
    def test_a_rise_beyond_hysteresis_moves_measured_and_floor_never_lower(self):
        record = {'floor': 85.0, 'measured': 86.5, 'cell': 'c'}
        for shown, hysteresis, gap, expected in (
                ('88.0', '1.5', '1.5', None),
                ('88.1', '1.5', '1.5', {'floor': 86.6, 'measured': 88.1, 'cell': 'c'}),
                ('86.4', '0', '1.5', None),
                ('86.6', '0', '1.5', {'floor': 85.1, 'measured': 86.6, 'cell': 'c'}),
                ('90.0', '1.5', '6', {'floor': 85.0, 'measured': 90.0, 'cell': 'c'}),
                ('90.0', '1.5', '0.25', {'floor': 89.7, 'measured': 90.0, 'cell': 'c'})):
            with self.subTest(shown=shown, hysteresis=hysteresis, gap=gap):
                self.assertEqual(ratchet.moved(record, shown, 'c', Decimal(hysteresis),
                                               Decimal(gap)), expected)
        # 86.6 is inexact in binary: read as a float, 88.1 - 86.6 would exceed 1.5.
        self.assertIsNone(ratchet.moved({'floor': 1, 'measured': 86.6}, '88.1', 'c',
                                        Decimal('1.5'), Decimal('1.5')))

    def test_a_raised_floor_rounds_down_to_the_precision(self):
        record = {'floor': 85.0, 'measured': 86.5, 'cell': 'c'}
        for old, shown, gap, precision, expected in (
                (record, '88.17', '1.5', 2, {'floor': 86.67, 'measured': 88.17, 'cell': 'c'}),
                (record, '88.2', '1.5', 1, {'floor': 86.7, 'measured': 88.2, 'cell': 'c'}),
                # 87.915 rounds down, never half up; at one decimal it is 87.9.
                (record, '88.17', '0.255', 2, {'floor': 87.91, 'measured': 88.17, 'cell': 'c'}),
                (record, '88.17', '0.255', 1, {'floor': 87.9, 'measured': 88.17, 'cell': 'c'}),
                (record, '88.123456', '0.0000005', 6,
                 {'floor': 88.123455, 'measured': 88.123456, 'cell': 'c'}),
                # A kept floor at or above the new one stays; one step below it is raised.
                ({**record, 'floor': 86.67}, '88.17', '1.5', 2,
                 {'floor': 86.67, 'measured': 88.17, 'cell': 'c'}),
                ({**record, 'floor': 86.66}, '88.17', '1.5', 2,
                 {'floor': 86.67, 'measured': 88.17, 'cell': 'c'}),
                ({**record, 'floor': 87}, '88.17', '6', 2,
                 {'floor': 87, 'measured': 88.17, 'cell': 'c'}),
                (None, '0.50', '1.5', 2, {'floor': 0.0, 'measured': 0.5, 'cell': 'c'}),
                (None, '50.00', '1e30', 2, {'floor': 0.0, 'measured': 50.0, 'cell': 'c'})):
            with self.subTest(old=old, shown=shown, gap=gap, precision=precision):
                self.assertEqual(ratchet.moved(old, shown, 'c', Decimal('1.5'), Decimal(gap),
                                               'coverage.python', precision), expected)

    def test_a_new_record_carries_its_cell_and_a_moved_record_its_new_cell(self):
        self.assertEqual(ratchet.moved(None, '1.0', 'd', Decimal('1.5'), Decimal('1.5')),
                         {'floor': 0.0, 'measured': 1.0, 'cell': 'd'})
        self.assertEqual(ratchet.moved({'measured': 50, 'floor': 40}, '60.0', 'd',
                                       Decimal('1.5'), Decimal('1.5')),
                         {'measured': 60.0, 'floor': 58.5})
        self.assertEqual(ratchet.moved({'floor': 1, 'measured': 1, 'cell': 'c'}, '60.0', 'd',
                                       Decimal(0), Decimal(0)),
                         {'floor': 60.0, 'measured': 60.0, 'cell': 'd'})

    def test_a_recorded_measured_that_is_no_percentage_is_refused(self):
        for measured in (None, True, '80', float('nan'), float('inf'), -1, 101):
            with self.subTest(measured=measured):
                with self.assertRaisesRegex(ConfigError, 'coverage.python.measured'):
                    ratchet.moved({'floor': 1, 'measured': measured}, '50.0', 'c',
                                  Decimal(0), Decimal(0), 'coverage.python')

    def test_times_move_on_a_key_change_or_beyond_both_tolerances(self):
        old = {'a': 4.0, 'b': 10.0}
        for new, expected in (({'a': 4.9, 'b': 12.4}, False), ({'a': 5.1, 'b': 10.0}, True),
                              ({'a': 4.0, 'b': 12.6}, True), ({'a': 0.5, 'b': 10.0}, True),
                              ({'a': 4.0}, True), ({**old, 'c': 0.0}, True)):
            with self.subTest(new=new):
                self.assertEqual(ratchet.times_moved(old, new), expected)
        self.assertTrue(ratchet.times_moved(None, {}))
        self.assertFalse(ratchet.times_moved({'a': 0.2}, {'a': 1.1}))
        # Where 25% is under a second, the second decides, near-missed on both sides.
        self.assertTrue(ratchet.times_moved({'a': 2.0}, {'a': 3.05}))
        self.assertFalse(ratchet.times_moved({'a': 2.0}, {'a': 2.95}))


class RatchetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if AGENT is None:
            raise unittest.SkipTest('the push needs ssh-agent')
        cls.keys = tempfile.TemporaryDirectory()
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'test', '-f',
                        str(Path(cls.keys.name, 'key'))], check=True)
        cls.key = Path(cls.keys.name, 'key').read_text(encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        (self.scratch / 'checkout').mkdir()
        self.repo = make_repo(self.scratch / 'checkout', {
            THRESHOLDS: dumped(DOCUMENT), TIMES: dumped(RECORDED_TIMES)})
        self.remote = self.scratch / 'remote.git'
        git(self.scratch, 'init', '-q', '--bare', str(self.remote))
        self.head = self.rev(self.repo, 'HEAD')
        git(self.repo, 'push', '-q', str(self.remote), 'HEAD:refs/heads/main')
        self.artifacts = self.scratch / 'artifacts'
        self.event = self.scratch / 'event.json'
        self.event.write_text(json.dumps({'repository': {'default_branch': 'main'},
                                          'pull_request': {'number': 1,
                                                           'head': {'sha': 'f' * 40}}}),
                              encoding='utf-8')
        bin_dir = self.scratch / 'bin'
        bin_dir.mkdir()
        self.agents = self.scratch / 'agents.log'
        wrapper = bin_dir / 'ssh-agent'
        wrapper.write_bytes(f'#!/bin/sh\necho $$ >> "{self.agents}"\nexec "{AGENT}" "$@"\n'
                            .encode())
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        # Every git call is logged with whether it saw a secret; a race file moves main once,
        # just before the push is built.
        self.calls, self.race = self.scratch / 'git.log', self.scratch / 'race'
        (bin_dir / 'git').write_bytes((
            '#!/bin/sh\necho "${TEST_RATCHET_PUSH_KEY:+key}${TEST_RATCHET_GITHUB_TOKEN:+token}call"'
            f' >> "{self.calls}"\n'
            f'if [ -f "{self.race}" ] && [ "$3 $4" = "rev-parse FETCH_HEAD" ]; then\n'
            f'  "{GIT}" -C "{self.repo}" push -q "{self.remote}" "$(cat "{self.race}"):'
            f'refs/heads/main" && rm "{self.race}"\nfi\nexec "{GIT}" "$@"\n').encode())
        (bin_dir / 'git').chmod(0o755)
        self.path = f'{bin_dir}{os.pathsep}{os.environ["PATH"]}'

    @staticmethod
    def rev(repo, name):
        return git(repo, 'rev-parse', name).stdout.decode().strip()

    def shards(self, covered=90, cell='c', count=2, javascript=None):
        for index in range(1, count + 1):
            measured = {'python': lines(covered, 100) if index == 1 else {}}
            if javascript and index == 1:
                measured['javascript'] = javascript
            folder = artifact(self.artifacts, f'test-ratchet-{cell}-{index}_{count}', cell=cell,
                              shard=f'{index}/{count}', files=[f'tests/test_{"ab"[index - 1]}.py'],
                              coverage=measured)
            junit(folder, [(f'tests/test_{"ab"[index - 1]}.py', 2.5), ('tests/test_a.py', 3.0)])

    def report(self, **inputs):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('TEST_RATCHET_', 'GITHUB_', 'GIT_'))}
        summary = self.scratch / 'summary.md'
        summary.write_text('', encoding='utf-8')
        env.update(no_proxy='*', GITHUB_STEP_SUMMARY=str(summary), GITHUB_EVENT_NAME='push',
                   GITHUB_REF='refs/heads/main', GITHUB_EVENT_PATH=str(self.event),
                   GITHUB_SHA=self.head, GITHUB_REPOSITORY='owner/repo', PATH=self.path,
                   GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, HOME=str(self.scratch),
                   TEST_RATCHET_ARTIFACTS=str(self.artifacts), TEST_RATCHET_TIMES=TIMES,
                   TEST_RATCHET_PUSH_REMOTE=str(self.remote), TEST_RATCHET_PUSH_KEY=self.key)
        env.update({key if key.startswith('GITHUB_') else f'TEST_RATCHET_{key.upper()}': value
                    for key, value in inputs.items()})
        result = subprocess.run([sys.executable, '-I', str(MAIN)], cwd=self.repo, env=env,
                                capture_output=True, text=True, check=False)
        if self.calls.exists():
            self.assertNotRegex(self.calls.read_text(encoding='utf-8'), 'key|token')
        return result, summary.read_text(encoding='utf-8')

    def remote_file(self, name, rev='main'):
        return git(self.remote, 'show', f'{rev}:{name}').stdout

    def assert_untouched(self, result):
        self.assertEqual(self.rev(self.remote, 'main'), self.head, result.stdout + result.stderr)
        self.assertEqual((self.repo / THRESHOLDS).read_bytes(), dumped(DOCUMENT))
        self.assertEqual((self.repo / TIMES).read_bytes(), dumped(RECORDED_TIMES))

    def test_a_raise_commits_both_files_on_github_sha_with_other_keys_kept(self):
        # Only the first shard's tests reached JavaScript: the other contributes no report.
        self.shards(javascript=lines(10, 10, 'web/a.js'))
        result, summary = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('call', self.calls.read_text(encoding='utf-8'))
        self.assertNotIn('left out', (result.stdout + summary).lower())
        tip = self.rev(self.remote, 'main')
        shown = git(self.remote, 'show', '-s', '--format=%P%n%s%n%an <%ae>%n%cn <%ce>', tip)
        self.assertEqual(shown.stdout.decode().splitlines(), [
            self.head, 'chore(ratchet): update the test-ratchet records',
            *[f'{ratchet.BOT_NAME} <{ratchet.BOT_EMAIL}>'] * 2])
        changed = git(self.remote, 'diff-tree', '--name-only', '-r', self.head, tip)
        self.assertEqual(changed.stdout.decode().split(), [THRESHOLDS, TIMES])
        expected = json.loads(json.dumps(DOCUMENT))
        expected['coverage']['python'] = {'floor': 88.5, 'measured': 90.0, 'note': 'kept',
                                          'cell': 'c'}
        expected['coverage']['javascript'] = {'floor': 98.5, 'measured': 100.0, 'cell': 'c'}
        self.assertEqual(self.remote_file(THRESHOLDS), dumped(expected))
        self.assertEqual(json.loads(self.remote_file(TIMES)), {'schema_version': 1, 'cells': {
            'c': {'tests/test_a.py': 5.5, 'tests/test_b.py': 2.5}, 'gone': {'x.py': 1.0}}})
        self.assertIn('python: measured 86.5 -> 90.0, floor 85.0 -> 88.5', summary)
        self.assertEqual((self.repo / THRESHOLDS).read_bytes(), dumped(DOCUMENT))
        [pid] = self.agents.read_text(encoding='utf-8').split()
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid), 0)
        self.assertNotIn(self.key.strip().splitlines()[1], result.stdout + result.stderr)

    def test_a_language_with_no_record_gets_one_and_the_times_file_is_created(self):
        artifact(self.artifacts, 'test-ratchet-c-all', coverage={
            'python': lines(86, 100), 'shell': lines(3, 4, 'run.sh')})
        junit(self.artifacts / 'test-ratchet-c-all', [('tests/test_a.py', 1.25)])
        result, _ = self.report(times='new/times.json', hysteresis='0', gap='0.5')
        self.assertEqual(result.returncode, 0, result.stderr)
        coverage = json.loads(self.remote_file(THRESHOLDS))['coverage']
        self.assertEqual(list(coverage), ['go', 'python', 'rust', 'shell'])
        self.assertEqual(coverage['shell'], {'floor': 74.5, 'measured': 75.0, 'cell': 'c'})
        self.assertEqual(coverage['python'], DOCUMENT['coverage']['python'])
        self.assertEqual(json.loads(self.remote_file('new/times.json')), {
            'schema_version': 1, 'cells': {'c': {'tests/test_a.py': 1.25}}})
        self.assertEqual(self.remote_file(TIMES), dumped(RECORDED_TIMES))

    def test_input_precision_sets_the_decimals_a_raise_records(self):
        # 88.179 is recorded truncated, as it is shown, never rounded to 88.18.
        artifact(self.artifacts, 'test-ratchet-c-all', coverage={'python': lines(88179, 100000)})
        junit(self.artifacts / 'test-ratchet-c-all', [('tests/test_a.py', 4.0)])
        result, summary = self.report(precision='2', times='')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.remote_file(THRESHOLDS))['coverage']['python'],
                         {'floor': 86.67, 'measured': 88.17, 'note': 'kept', 'cell': 'c'})
        self.assertIn('python: measured 86.5 -> 88.17, floor 85.0 -> 86.67', summary)

    def test_a_gap_or_hysteresis_key_overrides_the_input_of_its_name(self):
        # The figure is 90.0 against measured 86.5; the inputs differ from every key.
        for keys, inputs, expected in (
                ({'gap': 3}, {}, {'floor': 87.0, 'measured': 90.0}),
                ({}, {'gap': '2.5'}, {'floor': 87.5, 'measured': 90.0}),
                ({'gap': 3}, {'gap': '0.5'}, {'floor': 87.0, 'measured': 90.0}),
                ({'hysteresis': 5}, {}, None), ({}, {'hysteresis': '4'}, None),
                ({'hysteresis': 0, 'gap': 3}, {'hysteresis': '4', 'gap': '0.5'},
                 {'floor': 87.0, 'measured': 90.0})):
            with self.subTest(keys=keys, inputs=inputs):
                git(self.repo, 'checkout', '-q', self.head)
                (self.repo / THRESHOLDS).write_bytes(dumped({**DOCUMENT, **keys}))
                git(self.repo, 'commit', '-q', '--allow-empty', '-am', 'keys')
                sha = self.rev(self.repo, 'HEAD')
                git(self.repo, 'push', '-q', '-f', str(self.remote), 'HEAD:refs/heads/main')
                shutil.rmtree(self.artifacts, ignore_errors=True)
                self.shards()
                result, _ = self.report(GITHUB_SHA=sha, times='', **inputs)
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(self.remote_file(THRESHOLDS))['coverage']['python']
                self.assertEqual(record, {**DOCUMENT['coverage']['python'], **(expected or {})})

    def test_nothing_moves_within_hysteresis_and_tolerance(self):
        self.shards(covered=88)
        for folder in self.artifacts.iterdir():
            junit(folder, [('tests/test_a.py', 4.9), ('tests/test_b.py', 12.4)])
        result, summary = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('no record moved', result.stdout)
        self.assert_untouched(result)
        self.assertNotIn('->', summary)

    def test_no_record_moves_on_a_red_run(self):
        cases = (('a shard of another cell failed', {}, 'd', False),
                 ('a shard is missing', {}, None, True),
                 ('the gate failed', {'thresholds': 'strict.json'}, None, True))
        (self.repo / 'strict.json').write_bytes(dumped({'schema_version': 1, 'coverage': {
            'python': {'floor': 99.0, 'measured': 99.0}}}))
        for name, inputs, other, passed in cases:
            with self.subTest(name):
                shutil.rmtree(self.artifacts, ignore_errors=True)
                self.shards()
                if other:
                    artifact(self.artifacts, 'test-ratchet-d-all', cell=other, passed=passed)
                if name == 'a shard is missing':
                    shutil.rmtree(self.artifacts / 'test-ratchet-c-2_2')
                result, _ = self.report(**inputs)
                self.assertEqual(result.returncode, 1 if inputs else 0, result.stderr)
                self.assertEqual(self.rev(self.remote, 'main'), self.head)
                if not inputs:
                    self.assertIn('records unchanged: ', result.stdout)

    def test_a_pull_request_or_another_branch_leaves_both_files_untouched(self):
        self.shards()
        # The pull request moved on, so the run comments nothing and only gates.
        api = GitHub(self, {('GET', '/repos/owner/repo/pulls/1'): answer(
            {'head': {'sha': 'e' * 40}, 'base': {'sha': self.head}})})
        pull = {'GITHUB_EVENT_NAME': 'pull_request', 'GITHUB_API_URL': api.url,
                'github_token': 'tok'}
        for inputs in ({**pull, 'GITHUB_REF': 'refs/pull/1/merge'}, pull,
                       {'GITHUB_REF': 'refs/heads/topic'}, {'GITHUB_REF': 'refs/tags/main'}):
            with self.subTest(inputs=inputs):
                result, _ = self.report(**inputs)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_untouched(result)
        self.assertFalse(self.agents.exists())
        self.assertEqual(api.calls(), ['GET /repos/owner/repo/pulls/1'] * 2)

    def test_the_actions_own_records_commit_commits_nothing(self):
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', ratchet.SUBJECT)
        self.head = self.rev(self.repo, 'HEAD')
        git(self.repo, 'push', '-q', str(self.remote), 'HEAD:refs/heads/main')
        self.shards()
        result, _ = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('records commit', result.stdout)
        self.assert_untouched(result)
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', f'{ratchet.SUBJECT} by hand')
        self.head = self.rev(self.repo, 'HEAD')
        git(self.repo, 'push', '-q', str(self.remote), 'HEAD:refs/heads/main')
        result, _ = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.rev(self.remote, 'main'), self.head)

    def test_a_checkout_other_than_github_sha_is_refused(self):
        self.shards()
        result, _ = self.report(GITHUB_SHA='0' * 40)
        self.assertEqual(result.returncode, 2)
        self.assertIn(f'HEAD {self.head} is not github.sha {"0" * 40}', result.stderr)
        self.assert_untouched(result)

    def test_without_push_key_the_new_values_go_to_the_summary_only(self):
        self.shards()
        result, summary = self.report(push_key='')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_untouched(result)
        self.assertIn('not committed: input push-key is empty', summary)
        self.assertIn('python: measured 86.5 -> 90.0, floor 85.0 -> 88.5', summary)
        self.assertIn('times of cell c', summary)

    def refusing_hook(self, body):
        hook = self.remote / 'hooks' / 'pre-receive'
        hook.write_bytes(f'#!/bin/sh\n{body}\nexit 1\n'.encode())
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

    def test_a_refused_push_fails_while_the_branch_stands_still(self):
        self.shards()
        self.refusing_hook('')
        result, _ = self.report()
        self.assertEqual(result.returncode, 1)
        self.assertIn(f'refused while main stood still at {self.head}', result.stderr)
        self.assert_untouched(result)

    def test_a_refused_push_after_the_branch_moved_passes_with_a_notice(self):
        # An unrelated history, so a push built on github.sha is not even fetched with it.
        later = git(self.repo, 'commit-tree', '-m', 'later', f'{self.head}^{{tree}}')
        later = later.stdout.decode().strip()
        git(self.repo, 'push', '-q', str(self.remote), f'{later}:refs/heads/later')
        self.shards()
        self.refusing_hook(f'(unset GIT_QUARANTINE_PATH; git update-ref refs/heads/main {later})')
        # The hook moves main and refuses; the second run finds main moved before it pushes.
        for run in ('moved while pushing', 'moved before'):
            with self.subTest(run):
                result, summary = self.report()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('::notice::main moved past', result.stdout)
                self.assertIn('main moved past', summary)
                self.assertEqual(self.rev(self.remote, 'main'), later)

    def test_a_branch_moved_just_before_the_push_is_never_overwritten(self):
        later = git(self.repo, 'commit-tree', '-p', self.head, '-m', 'later',
                    f'{self.head}^{{tree}}').stdout.decode().strip()
        self.race.write_text(later, encoding='utf-8')
        self.shards()
        result, summary = self.report()
        self.assertFalse(self.race.exists(), result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('::notice::main moved past', result.stdout)
        self.assertIn('main moved past', summary)
        self.assertEqual(self.rev(self.remote, 'main'), later)

    def test_invalid_inputs_exit_2_naming_them(self):
        self.shards()
        odd_event = self.scratch / 'odd.json'
        odd_event.write_text('{"repository": {"default_branch": 5}}', encoding='utf-8')
        for inputs, named in (({'hysteresis': 'x'}, 'input hysteresis'),
                              ({'GITHUB_EVENT_PATH': str(odd_event)}, 'default_branch is 5'),
                              ({'push_remote': str(self.scratch / 'absent.git')},
                               'git fetch failed'),
                              ({'gap': '-1'}, 'input gap'), ({'gap': 'NaN'}, 'input gap'),
                              ({'push_key': 'not a key'}, 'input push-key'),
                              ({'GITHUB_EVENT_PATH': os.devnull}, 'GITHUB_EVENT_PATH'),
                              ({'GITHUB_SHA': ''}, 'GITHUB_SHA')):
            with self.subTest(inputs=inputs):
                result, _ = self.report(**inputs)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(named, result.stderr)
                self.assert_untouched(result)
        for pid in self.agents.read_text(encoding='utf-8').split():
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid), 0)

    def test_an_artifact_without_its_junit_report_is_refused(self):
        self.shards()
        (self.artifacts / 'test-ratchet-c-2_2' / 'junit.xml').unlink()
        result, summary = self.report()
        self.assertEqual(result.returncode, 2)
        self.assertIn('test-ratchet-c-2_2 has no junit.xml', result.stderr)
        self.assertIn('test-ratchet-c-2_2 has no junit.xml', summary)
        (self.artifacts / 'test-ratchet-c-2_2' / 'junit.xml').write_bytes(b'<html/>')
        result, summary = self.report()
        self.assertEqual(result.returncode, 2)
        self.assertIn('artifact test-ratchet-c-2_2: JUnit report', summary)
        self.assert_untouched(result)

    def test_an_ssh_agent_that_cannot_run_or_start_exits_2(self):
        self.shards()
        tools = self.scratch / 'tools'
        tools.mkdir()
        (tools / 'git').symlink_to(GIT or 'git')
        silent = self.scratch / 'silent'
        silent.mkdir()
        (silent / 'ssh-agent').write_bytes(b'#!/bin/sh\nexit 0\n')
        (silent / 'ssh-agent').chmod(0o755)
        no_add = self.scratch / 'no-add'
        no_add.mkdir()
        (no_add / 'git').symlink_to(GIT or 'git')
        (no_add / 'ssh-agent').symlink_to(self.scratch / 'bin' / 'ssh-agent')
        for path, named in ((str(tools), 'cannot run ssh-agent'),
                            (f'{silent}{os.pathsep}{self.path}', 'ssh-agent did not start'),
                            (str(no_add), 'cannot run ssh-add')):
            with self.subTest(named):
                self.path = path
                result, _ = self.report()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stderr.count('\n'), 1, result.stderr)
                self.assertIn(named, result.stderr)
                self.assert_untouched(result)
        [pid] = self.agents.read_text(encoding='utf-8').split()
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid), 0)
        # ssh-agent puts its socket, agent.<pid>, in a /tmp/ssh-* directory whatever TMPDIR says.
        self.assertEqual(list(Path('/tmp').glob(f'ssh-*/agent.{pid}')), [])


if __name__ == '__main__':
    unittest.main()
