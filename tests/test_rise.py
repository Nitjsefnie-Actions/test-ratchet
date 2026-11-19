"""Input records-only-rise: a thresholds file change may raise coverage records, never lower one."""
import base64
import json
import os
import shutil
import subprocess
import unittest

from _report_harness import HEAD, THRESHOLDS, ReportCase, rev
from test_ratchet import ConfigError, rise
from test_split import git

OLD = {'python': {'floor': 70.0, 'measured': 72.0}, 'shell': {'floor': 10}}
LABELS = ('old file', 'new file')
TOKEN = 's3cr3t-token'


def records(floor=70.0, measured=70.0, **more):
    return json.dumps({'schema_version': 1, 'coverage': {
        'python': {'floor': floor, 'measured': measured, 'cell': 'c'}, **more}}).encode()


class CompareTest(unittest.TestCase):
    def test_a_lowered_or_removed_record_is_named_with_both_values(self):
        python = OLD['python']
        for new, found in (
                ({**OLD, 'python': {'floor': 69.9, 'measured': 72.0}},
                 ['coverage.python.floor lowered from 70.0 to 69.9']),
                ({**OLD, 'python': {'floor': 70, 'measured': 71.9}},
                 ['coverage.python.measured lowered from 72.0 to 71.9']),
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
                # Raised, equal or new records pass, and a measured the old record lacks.
                ({**OLD, 'python': {'floor': 70.1, 'measured': 100}}, []),
                ({'python': {'floor': 70, 'measured': 72}, 'shell': {'floor': 10.0, 'measured': 5},
                  'javascript': {'floor': 0}}, [])):
            with self.subTest(new=new):
                self.assertEqual(rise.compare(OLD, new, LABELS), found)
        self.assertEqual(rise.compare({}, {'python': {'floor': 1}}, LABELS), [])

    def test_a_measured_that_is_no_percentage_in_either_file_is_refused(self):
        for value in ('72', True, None, -1, 100.5, float('nan')):
            for index, label in enumerate(LABELS):
                with self.subTest(value=value, label=label):
                    files = [OLD, {'python': {'floor': 70}, 'shell': {'floor': 10}}]
                    files[index] = {**files[index], 'shell': {'floor': 10, 'measured': value}}
                    with self.assertRaisesRegex(ConfigError, f'^{label}: key coverage'
                                                r'\.shell\.measured must be a number from 0'):
                        rise.compare(files[0], files[1], LABELS)


class RecordsOnlyRiseTest(ReportCase):
    def setUp(self):
        super().setUp()
        self.log = self.scratch / 'git.log'
        wrapper = self.scratch / 'bin' / 'git'
        wrapper.parent.mkdir()
        # Records every git the driver runs, with the credential configuration it was handed.
        wrapper.write_text('#!/bin/sh\nprintf \'%s\\t%s\\t%s\\n\' "$*" "${GIT_CONFIG_KEY_0-}"'
                           f' "${{GIT_CONFIG_VALUE_0-}}" >> {self.log}\n'
                           f'exec {shutil.which("git")} "$@"\n', encoding='utf-8')
        wrapper.chmod(0o755)
        self.path = {'PATH': f'{wrapper.parent}{os.pathsep}{os.environ["PATH"]}'}

    def commit(self, parent, data, repo=None):
        repo = repo or self.repo
        git(repo, 'checkout', '-q', '--detach', parent)
        (repo / THRESHOLDS).write_bytes(data)
        git(repo, 'commit', '-q', '--allow-empty', '-am', 'thresholds')
        return rev(repo)

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
                self.assertEqual('### test-ratchet records lowered' in summary, code == 1)
                # The coverage comment is written whatever the guard finds.
                self.assertEqual(len(self.comments), 1)

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

    def gating(self, head, **inputs):
        self.thread(head=head)
        run = {'event': 'pull_request', 'head_sha': head, 'pull_requests': [{'number': 7}]}
        return self.report('workflow_run', {'workflow_run': run}, self.path,
                           github_token=TOKEN, records_only_rise='true', **inputs)

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
        self.assertEqual([call[1:] for call in fetches], [[
            'http.https://github.example/.extraheader', f'AUTHORIZATION: basic {secret}']])
        self.assertTrue(fetches[0][0].endswith(' fetch --quiet --no-tags'
                                               ' --no-recurse-submodules origin'
                                               ' refs/pull/7/head'), fetches[0][0])
        self.assertEqual([call for call in calls if TOKEN in call[0] or secret in call[0]
                          or (call not in fetches and call[1:] != ['', ''])], [])
        # Only its objects arrived: no ref of the checkout names it.
        self.assertEqual(git(self.repo, 'for-each-ref', '--points-at', head).stdout, b'')

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

    def test_a_pull_request_head_the_checkout_cannot_fetch_is_refused(self):
        self.thread()
        result, _ = self.report('pull_request', {'pull_request': {
            'number': 7, 'head': {'sha': HEAD}, 'base': {'sha': self.base}}}, self.path,
            records_only_rise='true')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"the pull request's head {HEAD} is not in the checkout", result.stderr)


if __name__ == '__main__':
    unittest.main()
