"""Input records-only-rise: a thresholds file change may raise coverage records, never lower one."""
import base64
import json
import os
import shutil
import subprocess
import threading
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from _report_harness import HEAD, THRESHOLDS, ReportCase, rev
from test_ratchet import ConfigError, rise
from test_split import git

OLD = {'python': {'floor': 70.0, 'measured': 72.0}, 'shell': {'floor': 10}}
LABELS = ('old file', 'new file')
INPUTS = {'hysteresis': Decimal('1.5'), 'gap': Decimal('1.5')}
TOKEN = 's3cr3t-token'


def records(floor=70.0, measured=70.0, keys=None, **more):
    return json.dumps({'schema_version': 1, **(keys or {}), 'coverage': {
        'python': {'floor': floor, 'measured': measured, 'cell': 'c'}, **more}}).encode()


def compare(old, new, keys=({}, {}), inputs=None, precision=1):
    return rise.compare((old, keys[0]), (new, keys[1]), LABELS, inputs or INPUTS, precision)


class CompareTest(unittest.TestCase):
    def test_a_lowered_or_removed_record_is_named_with_both_values(self):
        python = OLD['python']
        for new, found in (
                ({**OLD, 'python': {'floor': 69.9, 'measured': 72.0}},
                 ['coverage.python.floor lowered from 70.0 to 69.9']),
                ({**OLD, 'python': {'floor': 70, 'measured': 71.9}},
                 ['coverage.python.measured lowered from 72.0 to 71.9',
                  'coverage.python.measured changed from 72.0 to 71.9 with floor 70 below its'
                  ' implied floor 70.4']),
                ({**OLD, 'python': {'floor': 70.0}},
                 ['coverage.python.measured lowered from 72.0 to none']),
                ({**OLD, 'python': {'floor': 1, 'measured': 0}},
                 ['coverage.python.floor lowered from 70.0 to 1',
                  'coverage.python.measured lowered from 72.0 to 0']),
                ({'python': python},
                 ['coverage.shell removed: it held floor 10 and measured none']),
                ({'shell': {'floor': 99}},
                 ['coverage.python removed: it held floor 70.0 and measured 72.0']),
                ({}, ['coverage.python removed: it held floor 70.0 and measured 72.0',
                      'coverage.shell removed: it held floor 10 and measured none']),
                # A raised measured needs the floor a raise gives it.
                ({**OLD, 'python': {'floor': 70.1, 'measured': 100}},
                 ['coverage.python.measured changed from 72.0 to 100 with floor 70.1 below its'
                  ' implied floor 98.5']),
                # Raised, equal or new records pass, and a measured the old record lacks.
                ({**OLD, 'python': {'floor': 98.5, 'measured': 100}}, []),
                ({'python': {'floor': 70, 'measured': 72}, 'shell': {'floor': 10.0, 'measured': 5},
                  'javascript': {'floor': 0}}, [])):
            with self.subTest(new=new):
                self.assertEqual(compare(OLD, new), found)
        self.assertEqual(compare({}, {'python': {'floor': 1}}), [])

    def test_a_new_or_changed_measured_needs_the_floor_a_raise_gives(self):
        old = {'python': {'floor': 90.0, 'measured': 91.0}}
        line = ('coverage.python.measured changed from {} to {} with floor {} below its implied'
                ' floor {}').format
        # Each row: the old and new records, the new file's keys, input gap and precision.
        for was, now, keys, gap, precision, found in (
                (old, {'floor': 90.0, 'measured': 99.0}, {}, '1.5', 1,
                 [line(91.0, 99.0, 90.0, '97.5')]),
                (old, {'floor': 97.5, 'measured': 99.0}, {}, '1.5', 1, []),
                (old, {'floor': 97.4, 'measured': 99.0}, {}, '1.5', 1,
                 [line(91.0, 99.0, 97.4, '97.5')]),
                ({}, {'floor': 0.0, 'measured': 100}, {}, '1.5', 1,
                 [line('none', 100, 0.0, '98.5')]),
                ({}, {'floor': 98.5, 'measured': 100}, {}, '1.5', 1, []),
                ({}, {'floor': 0}, {}, '1.5', 1, []),
                # An unchanged measured is not compared, however stale its floor.
                ({'python': {'floor': 50, 'measured': 99}}, {'floor': 50, 'measured': 99.0}, {},
                 '1.5', 1, []),
                ({'python': {'floor': 50}}, {'floor': 50, 'measured': 99}, {}, '1.5', 1,
                 [line('none', 99, 50, '97.5')]),
                # G is the new file's gap key, never the old file's 10 or the input.
                (old, {'floor': 94.0, 'measured': 99.0}, {'gap': '5'}, '1.5', 1, []),
                (old, {'floor': 93.9, 'measured': 99.0}, {'gap': '5'}, '1.5', 1,
                 [line(91.0, 99.0, 93.9, '94.0')]),
                (old, {'floor': 96.0, 'measured': 99.0}, {}, '3', 1, []),
                (old, {'floor': 95.9, 'measured': 99.0}, {}, '3', 1,
                 [line(91.0, 99.0, 95.9, '96.0')]),
                # Rounded down to this run's precision: 99.115 is 99.11, at one decimal 99.1.
                (old, {'floor': 97.87, 'measured': 99.37}, {}, '1.5', 2, []),
                (old, {'floor': 97.86, 'measured': 99.37}, {}, '1.5', 2,
                 [line(91.0, 99.37, 97.86, '97.87')]),
                (old, {'floor': 99.11, 'measured': 99.37}, {}, '0.255', 2, []),
                (old, {'floor': 99.1, 'measured': 99.37}, {}, '0.255', 2,
                 [line(91.0, 99.37, 99.1, '99.11')]),
                (old, {'floor': 99.1, 'measured': 99.37}, {}, '0.255', 1, [])):
            with self.subTest(was=was, now=now, keys=keys, gap=gap, precision=precision):
                parsed = {name: Decimal(value) for name, value in keys.items()}
                files = ({'gap': Decimal(10)} if keys else {}, parsed)
                self.assertEqual(compare(was, {'python': now}, files,
                                         {**INPUTS, 'gap': Decimal(gap)}, precision), found)

    def test_a_gap_or_hysteresis_key_may_fall_never_rise_or_go(self):
        inputs = {'hysteresis': Decimal('1.5'), 'gap': Decimal('2.5')}
        for name in ('hysteresis', 'gap'):
            given = inputs[name]
            for old, new, found in (
                    ('2', '3', [f'{name} raised from 2 to 3']),
                    ('2', None, [f'{name} removed: it held 2']),
                    ('2', '1', []), ('2', '2.0', []), (None, None, []), (None, str(given), []),
                    (None, '1', []),
                    (None, str(given + Decimal('0.1')),
                     [f'{name} added at {given + Decimal("0.1")}, above the input {name}'
                      f' {given}'])):
                with self.subTest(name=name, old=old, new=new):
                    keys = tuple({} if value is None else {name: Decimal(value)}
                                 for value in (old, new))
                    self.assertEqual(compare({}, {}, keys, inputs), found)

    def test_a_measured_that_is_no_percentage_in_either_file_is_refused(self):
        for value in ('72', True, None, -1, 100.5, float('nan')):
            for index, label in enumerate(LABELS):
                with self.subTest(value=value, label=label):
                    files = [OLD, {'python': {'floor': 70}, 'shell': {'floor': 10}}]
                    files[index] = {**files[index], 'shell': {'floor': 10, 'measured': value}}
                    with self.assertRaisesRegex(ConfigError, f'^{label}: key coverage'
                                                r'\.shell\.measured must be a number from 0'):
                        compare(files[0], files[1])


class ThresholdsCase(ReportCase):
    """A pull request's head and merge base as git commits, and runs over them."""

    def setUp(self):
        super().setUp()
        self.log = self.scratch / 'git.log'
        wrapper = self.scratch / 'bin' / 'git'
        wrapper.parent.mkdir()
        # Records every git the driver runs, with the credential configuration it was handed.
        fields = ' '.join(f'"${{GIT_CONFIG_{name}-}}"' for name in (
            'COUNT', 'KEY_0', 'VALUE_0', 'KEY_1', 'VALUE_1'))
        wrapper.write_text(f'#!/bin/sh\nprintf \'%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n\' "$*" {fields}'
                           f' >> {self.log}\nexec {shutil.which("git")} "$@"\n', encoding='utf-8')
        wrapper.chmod(0o755)
        self.path = {'PATH': f'{wrapper.parent}{os.pathsep}{os.environ["PATH"]}'}

    def commit(self, parent, data, repo=None):
        repo = repo or self.repo
        git(repo, 'checkout', '-q', '--detach', parent)
        (repo / THRESHOLDS).write_bytes(data)
        git(repo, 'commit', '-q', '--allow-empty', '-am', 'thresholds')
        return rev(repo)

    def fork(self, head_data, ref_data=None):
        """origin holds refs/pull/7/head, a commit the checkout does not have."""
        origin, fork = self.scratch / 'origin.git', self.scratch / 'fork'
        git(self.scratch, 'init', '-q', '--bare', str(origin))
        git(self.scratch, 'clone', '-q', str(self.repo), str(fork))
        head = self.commit(self.base, head_data, fork)
        pushed = self.commit(self.base, ref_data, fork) if ref_data else head
        git(fork, 'push', '-q', str(origin), f'{pushed}:refs/pull/7/head')
        git(self.repo, 'remote', 'add', 'origin', str(origin))
        return head

    def gating(self, head, environ=None, **inputs):
        self.thread(head=head)
        run = {'event': 'pull_request', 'head_sha': head, 'pull_requests': [{'number': 7}]}
        return self.report('workflow_run', {'workflow_run': run}, {**self.path, **(environ or {})},
                           github_token=TOKEN, records_only_rise='true', **inputs)


class RecordsOnlyRiseTest(ThresholdsCase):
    def test_a_pull_request_is_compared_with_its_merge_base(self):
        lowered = self.commit(self.base, records(60.0, 71.0))
        # The base raised its floor after the branch forked: the branch lowers nothing.
        raised = self.commit(self.base, records(71.0, 71.0))
        forked = self.commit(self.base, records(70.0, 70.0, shell={'floor': 1}))
        git(self.repo, 'rm', '-q', THRESHOLDS)
        git(self.repo, 'commit', '-q', '-m', 'no thresholds file')
        deleted = rev(self.repo)
        orphan = git(self.repo, 'commit-tree', '-m', 'x', rev(self.repo) + '^{tree}')
        orphan = orphan.stdout.decode().strip()
        for head, base, on, code, named in (
                (lowered, self.base, 'true', 1, 'coverage.python.floor lowered from 70.0 to'
                 f' 60.0 (thresholds file {THRESHOLDS} at {lowered} against thresholds file'
                 f' {THRESHOLDS} at the merge base {self.base})'),
                (lowered, self.base, 'false', 0, ''),
                (deleted, self.base, 'true', 1, 'coverage.python removed: it held floor 70.0 and'
                 ' measured 70.0'),
                (forked, raised, 'true', 0, ''),
                (orphan, self.base, 'true', 2, f'{orphan} and base {self.base} have no merge'),
                (lowered, 'c' * 40, 'true', 2, f"the pull request's base {'c' * 40} names no"),
                (lowered, None, 'true', 2, "the pull_request payload has no 'base'")):
            with self.subTest(head=head, base=base, on=on):
                self.comments.clear()
                self.thread(head=head, base=self.base)
                event = {'number': 7, 'head': {'sha': head}, 'base': {'sha': base}}
                if base is None:
                    del event['base']
                result, summary = self.report('pull_request', {'pull_request': event}, self.path,
                                              records_only_rise=on)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(named, result.stderr)
                self.assertEqual('### test-ratchet records-only-rise refused' in summary, code == 1)
                # The coverage comment is written whatever the guard finds.
                self.assertEqual(len(self.comments), 1)

    def test_the_guard_reads_the_payload_and_a_refusal_wins(self):
        lowered = self.commit(self.base, records(60.0, 71.0))
        high = self.commit(self.base, records(75.0, 75.0))
        # Each row: the API's head and base, the payload's base, status, what is named.
        for head, base, payload, code, named in (
                (lowered, high, self.base, 1, ('below the floor 75.0', 'floor lowered')),
                (lowered, high, 'c' * 40, 2, ('below the floor 75.0',
                                              f"the pull request's base {'c' * 40} names no")),
                # The pull request moved past the run, so nothing is commented; still compared.
                (HEAD, self.base, self.base, 1, ('no pull request comment', 'floor lowered'))):
            with self.subTest(head=head, base=base, payload=payload):
                self.thread(head=head, base=base)
                result, _ = self.report('pull_request', {'pull_request': {
                    'number': 7, 'head': {'sha': lowered}, 'base': {'sha': payload}}},
                    self.path, records_only_rise='true')
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                for text in named:
                    self.assertIn(text, result.stdout + result.stderr)

    def test_a_gating_run_fetches_a_forks_head_by_its_token_never_in_argv(self):
        head = self.fork(records(50.0, 50.0))
        self.assertRaises(subprocess.CalledProcessError, git, self.repo, 'cat-file', '-e', head)
        result, _ = self.gating(head, workflow_run_gate='true')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('coverage.python.floor lowered from 70.0 to 50.0', result.stderr)
        self.assertIn('coverage.python.measured lowered from 70.0 to 50.0', result.stderr)
        calls = [line.split('\t') for line in self.log.read_text(encoding='utf-8').splitlines()]
        secret = base64.b64encode(f'x-access-token:{TOKEN}'.encode()).decode()
        fetches = [call for call in calls if ' fetch ' in f' {call[0]} ']
        # An empty value first resets the header list a persisted checkout credential set.
        key = 'http.https://github.example/.extraheader'
        self.assertEqual([call[1:] for call in fetches],
                         [['2', key, '', key, f'AUTHORIZATION: basic {secret}']])
        self.assertTrue(fetches[0][0].endswith(' fetch --quiet --no-tags'
                                               ' --no-recurse-submodules origin'
                                               ' refs/pull/7/head'), fetches[0][0])
        self.assertEqual([call for call in calls if TOKEN in call[0] or secret in call[0]
                          or (call not in fetches and call[1:] != [''] * 5)], [])
        # Only its objects arrived: no ref of the checkout names it.
        self.assertEqual(git(self.repo, 'for-each-ref', '--points-at', head).stdout, b'')

    def test_a_checkouts_persisted_header_is_replaced_not_sent_twice(self):
        head, seen = self.fork(records(50.0, 50.0)), []

        class Probe(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get_all('Authorization'))
                self.send_error(404)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Probe)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_address[1]}'
        git(self.repo, 'remote', 'set-url', 'origin', f'{url}/o/r.git')
        # What actions/checkout leaves in the checkout when it persists credentials.
        git(self.repo, 'config', f'http.{url}/.extraheader', 'AUTHORIZATION: basic PERSISTED')
        result, _ = self.gating(head, {'GITHUB_SERVER_URL': url}, workflow_run_gate='true')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        secret = base64.b64encode(f'x-access-token:{TOKEN}'.encode()).decode()
        self.assertTrue(seen)
        self.assertEqual(seen, [[f'basic {secret}']] * len(seen))

    def test_a_forks_ref_that_moved_or_is_missing_is_refused(self):
        head = self.fork(records(50.0, 50.0), records(80.0, 80.0))
        result, _ = self.gating(head, workflow_run_gate='true')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertRegex(result.stderr, f"origin's refs/pull/7/head is [0-9a-f]{{40}}, not the"
                                        f" pull request's head {head}")
        git(self.repo, 'remote', 'set-url', 'origin', str(self.scratch / 'none.git'))
        result, _ = self.gating(head, workflow_run_gate='true')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"the pull request's head {head} is not in the checkout, and fetching"
                      " origin's refs/pull/7/head failed", result.stderr)

    def test_a_workflow_run_run_that_does_not_gate_compares_nothing(self):
        head = self.fork(records(50.0, 50.0))
        for inputs, event in (({}, 'pull_request'), ({'workflow_run_gate': 'true'}, 'push')):
            with self.subTest(inputs=inputs, event=event):
                self.thread(head=head)
                run = {'event': event, 'head_sha': head, 'pull_requests': [{'number': 7}]}
                result, _ = self.report('workflow_run', {'workflow_run': run}, self.path,
                                        records_only_rise='true', **inputs)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn('lowered', result.stderr)
                self.assertNotIn(' fetch ', self.log.read_text(encoding='utf-8'))

    def test_a_default_branch_push_is_compared_with_the_previous_tip(self):
        lowered = self.commit(self.base, records(60.0, 60.0))
        raised = self.commit(self.base, records(70.0, 70.5))
        for sha, before, ref, on, code, moved, named in (
                (lowered, self.base, 'main', 'true', 1, False, 'coverage.python.floor lowered'
                 f' from 70.0 to 60.0 (thresholds file {THRESHOLDS} at {lowered} against'
                 f' thresholds file {THRESHOLDS} at the previous tip {self.base})'),
                (lowered, self.base, 'main', 'false', 0, True, ''),
                (lowered, self.base, 'other', 'true', 0, False, ''),
                (lowered, '0' * 40, 'main', 'true', 0, True, 'the push created the branch'),
                (raised, self.base, 'main', 'true', 0, True, ''),
                (lowered, 'd' * 40, 'main', 'true', 2, False,
                 f"the push's before {'d' * 40} names no commit"),
                (lowered, '0' + 'd' * 39, 'main', 'true', 2, False,
                 f"the push's before 0{'d' * 39} names no commit"),
                (lowered, None, 'main', 'true', 2, False, 'the push payload has no before, got'
                 ' None')):
            with self.subTest(sha=sha, before=before, ref=ref, on=on):
                git(self.repo, 'checkout', '-q', '--detach', sha)
                result, summary = self.report('push', {
                    'repository': {'default_branch': 'main'}, 'before': before},
                    {**self.path, 'GITHUB_REF': f'refs/heads/{ref}', 'GITHUB_SHA': sha},
                    records_only_rise=on, hysteresis='0')
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(named, result.stdout + result.stderr)
                # A lowered record moves no record, as a figure below its floor does not.
                self.assertEqual('Records moved' in summary, moved, summary)

    def test_a_push_with_a_stale_floor_or_a_loosened_key_fails_and_moves_nothing(self):
        stale = self.commit(self.base, records(70.0, 99.0))
        added = self.commit(self.base, records(keys={'gap': 3}))
        exact = self.commit(self.base, records(70.45, 71.99))
        low = self.commit(self.base, records(70.0, 72.0))
        # The figure 71.4 exceeds 71.0, so only the failure keeps this record from moving.
        below = self.commit(self.base, records(70.0, 71.0))
        prefix = 'coverage.python.measured changed from 70.0 to'
        for sha, inputs, code, named in (
                (stale, {}, 1, f'{prefix} 99.0 with floor 70.0 below its implied floor 97.5'
                 f' (thresholds file {THRESHOLDS} at {stale} against thresholds file'
                 f' {THRESHOLDS} at the previous tip {self.base})'),
                (added, {}, 1, 'gap added at 3, above the input gap 1.5'),
                (added, {'gap': '3'}, 0, ''),
                # 71.99 - 1.5 is 70.49 at two decimals, 70.4 at one.
                (exact, {'precision': '2'}, 1, f'{prefix} 71.99 with floor 70.45 below its'
                 ' implied floor 70.49'),
                (exact, {}, 0, ''), (low, {}, 1, 'floor 70.0 below its implied floor 70.5'),
                (low, {'gap': '3'}, 0, ''),
                (below, {'gap': '0.5'}, 1, f'{prefix} 71.0 with floor 70.0 below its implied floor'
                 ' 70.5')):
            with self.subTest(sha=sha, inputs=inputs):
                git(self.repo, 'checkout', '-q', '--detach', sha)
                result, summary = self.report('push', {
                    'repository': {'default_branch': 'main'}, 'before': self.base},
                    {**self.path, 'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': sha},
                    records_only_rise='true', hysteresis='0', **inputs)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(named, result.stderr)
                self.assertEqual('### test-ratchet records-only-rise refused' in summary, code == 1)
                if code:
                    self.assertNotIn('Records moved', summary)

    def test_a_bad_key_in_either_file_compared_is_refused(self):
        bad = self.commit(self.base, records(keys={'hysteresis': -1}))
        good = self.commit(bad, records())
        self.thread(head=bad)
        result, _ = self.report('pull_request', {'pull_request': {
            'number': 7, 'head': {'sha': bad}, 'base': {'sha': self.base}}}, self.path,
            records_only_rise='true')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f'thresholds file {THRESHOLDS} at {bad}: key hysteresis must be a'
                      ' non-negative number, got -1', result.stderr)
        git(self.repo, 'checkout', '-q', '--detach', good)
        result, _ = self.report('push', {'repository': {'default_branch': 'main'}, 'before': bad},
                                {**self.path, 'GITHUB_REF': 'refs/heads/main',
                                 'GITHUB_SHA': good}, records_only_rise='true')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f'thresholds file {THRESHOLDS} at the previous tip {bad}: key hysteresis',
                      result.stderr)

    def test_a_pull_request_head_the_checkout_cannot_fetch_is_refused(self):
        self.thread()
        result, _ = self.report('pull_request', {'pull_request': {
            'number': 7, 'head': {'sha': HEAD}, 'base': {'sha': self.base}}}, self.path,
            records_only_rise='true')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"the pull request's head {HEAD} is not in the checkout", result.stderr)


if __name__ == '__main__':
    unittest.main()
