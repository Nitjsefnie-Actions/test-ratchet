"""The pull request comment: resolved from the event, created once, then edited in place."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _action_harness import ROOT, run_action
from _github_double import GitHub, answer
from test_collect import artifact
from test_ratchet import ConfigError, comment, github, report
from test_split import git, make_repo

MAIN = ROOT / 'report' / 'main.py'
THRESHOLDS = '.github/ci-thresholds.json'
HEAD, OTHER = 'a' * 40, 'b' * 40
BOT = {'login': 'github-actions[bot]'}
EVENT = {'pull_request': {'number': 7, 'head': {'sha': HEAD}}}
TABLE = ('Uncovered added lines:\n\n| Language | File | Covered | Added | Missed lines |\n'
         '|---|---|---|---|---|\n')
# Added lines 2, 3 and 13; a removed line and a no-newline marker advance nothing.
PATCH_A = ('@@ -1,3 +1,5 @@\n line1\n+added2\n+added3\n line4\n-removed\n'
           '@@ -10,2 +12,3 @@\n ctx12\n+added13')
PATCH_B = '@@ -1 +1,2 @@\n-old\n\\ No newline at end of file\n+one\n+two'
FILES = [{'filename': 'src/a.py', 'status': 'modified', 'patch': PATCH_A},
         {'filename': 'src/b.py', 'status': 'modified', 'patch': PATCH_B}]
COVERAGE = {'python': {'src/a.py': {1: 1, 2: 1, 3: 0, 4: 1, 12: 1, 13: 0}, 'src/b.py': {2: 3}}}


def thresholds(floor):
    return json.dumps({'schema_version': 1, 'coverage': {
        'python': {'floor': floor, 'measured': floor, 'cell': 'c'}}}).encode()


def rev(repo):
    return git(repo, 'rev-parse', 'HEAD').stdout.decode().strip()


class DiffCoverageTest(unittest.TestCase):
    def test_added_lines_are_read_from_the_hunk_headers(self):
        self.assertEqual(comment.added(PATCH_A), [2, 3, 13])
        self.assertEqual(comment.added(PATCH_B), [1, 2])
        # A blank context line with its space stripped counts; a header inside a line does not.
        self.assertEqual(comment.added('@@ -1,3 +1,4 @@\n a\n\n+b @@ -1 +9 @@\n+c'), [3, 4])

    def test_a_two_file_patch_gives_exact_numbers_per_language(self):
        # Only a file that adds lines GitHub sent no patch for is named, or one whose count is
        # missing: not a binary, a rename, a mode change or a deletion.
        files = FILES + [{'filename': 'big.json', 'status': 'modified', 'additions': 1},
                         {'filename': 'img.png', 'status': 'added', 'additions': 0},
                         {'filename': 'old.bin', 'status': 'removed', 'additions': 0},
                         {'filename': 'new.py', 'status': 'renamed', 'additions': 0},
                         {'filename': 'run.sh', 'status': 'changed', 'additions': 0},
                         {'filename': 'gen.txt', 'status': 'modified'},
                         {'filename': 'docs/c.md', 'status': 'added', 'patch': PATCH_B}]
        measured, unmeasured = comment.diff_coverage(files, {**COVERAGE,
                                                             'shell': {'run.sh': {1: 0}}})
        self.assertEqual(measured, {'python': (4, 2, {'src/a.py': (1, 3, [3, 13])}),
                                    'shell': (0, 0, {})})
        self.assertEqual(unmeasured, ['big.json', 'gen.txt'])

    def test_consecutive_line_numbers_are_joined(self):
        self.assertEqual(comment.ranges([9, 3, 4, 5, 7]), '3-5, 7, 9')

    def test_uncovered_added_lines_are_a_row_per_language_and_file(self):
        # Languages keep the totals table's order, files sort; src/b.py's lines are all covered.
        coverage = {'shell': {'x|y.sh': {1: 0, 2: 0}, 'src/a.py': {3: 0, 13: 2}}, **COVERAGE}
        result = report.Report('c', [], [], coverage, [], '', {})
        files = [{'filename': 'x|y.sh', 'patch': PATCH_B}] + FILES
        self.assertEqual(comment.render(result, HEAD, *comment.diff_coverage(files, coverage))[1],
                         f'\n{TABLE}| shell | `src/a.py` | 1 | 2 | 3 |\n'
                         '| shell | `x\\|y.sh` | 0 | 2 | 1-2 |\n'
                         '| python | `src/a.py` | 1 | 3 | 3, 13 |\n')
        full = {'python': {'src/a.py': {2: 1}, 'src/b.py': {2: 1}}}
        self.assertEqual(comment.render(result, HEAD, *comment.diff_coverage(FILES, full))[1], '')


class CommentTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        (self.scratch / 'repo').mkdir()
        self.repo = make_repo(self.scratch / 'repo', {THRESHOLDS: thresholds(70.0)})
        self.base = rev(self.repo)
        self.artifacts = self.scratch / 'artifacts'
        artifact(self.artifacts, 'test-ratchet-c-all', coverage=COVERAGE)
        self.api = GitHub(self, {})
        self.comments = []

    def pull(self, number=7, head=HEAD, base=None):
        return {('GET', f'/repos/o/r/pulls/{number}'): answer(
            {'head': {'sha': head}, 'base': {'sha': base or self.base}})}

    def thread(self, number=7, refuse=None, base=None):
        """Pull request, files and comments, two pages each; a comment created, then edited."""
        files = f'/repos/o/r/pulls/{number}/files?per_page=100'
        listing = f'/repos/o/r/issues/{number}/comments?per_page=100'
        others = [{'id': 1, 'user': {'login': 'someone'}, 'body': comment.MARKER + ' copied'},
                  {'id': 2, 'user': BOT, 'body': 'another bot comment'}]

        def post(body):
            self.comments.append({'id': 901, 'user': BOT, 'body': body['body']})
            return 201, self.comments[-1], {}

        def patch(body):
            self.comments[0]['body'] = body['body']
            return 200, self.comments[0], {}

        routes = {
            ('GET', files): answer(FILES[:1], link=f'{self.api.url}{files}&page=2'),
            ('GET', f'{files}&page=2'): answer(
                FILES[1:] + [{'filename': 'big.json', 'additions': 2}]),
            ('GET', listing): answer(others, link=f'{self.api.url}{listing}&page=2'),
            ('GET', f'{listing}&page=2'): lambda _body: (200, self.comments, {}),
            ('POST', f'/repos/o/r/issues/{number}/comments'): post,
            ('PATCH', '/repos/o/r/issues/comments/901'): patch}
        if refuse:
            route, status = refuse
            routes[route] = answer({'message': 'Resource not accessible by integration'},
                                   status)
        self.api.routes.update({**self.pull(number, base=base), **routes})

    def report(self, name, event, **inputs):
        path = self.scratch / 'event.json'
        path.unlink(missing_ok=True)
        if event is not None:
            path.write_text(json.dumps(event), encoding='utf-8')
        summary = self.scratch / 'summary.md'
        summary.write_text('', encoding='utf-8')
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('TEST_RATCHET_', 'GITHUB_'))}
        env.update(no_proxy='*', GITHUB_STEP_SUMMARY=str(summary), GITHUB_EVENT_NAME=name,
                   GITHUB_EVENT_PATH=str(path), GITHUB_REPOSITORY='o/r',
                   GITHUB_API_URL=self.api.url, GITHUB_SERVER_URL='https://github.example',
                   GITHUB_RUN_ID='77', TEST_RATCHET_ARTIFACTS=str(self.artifacts),
                   TEST_RATCHET_GITHUB_TOKEN='tok')
        env.update({f'TEST_RATCHET_{key.upper()}': value for key, value in inputs.items()})
        result = subprocess.run([sys.executable, '-I', str(MAIN)], cwd=self.repo, env=env,
                                capture_output=True, text=True, check=False)
        return result, summary.read_text(encoding='utf-8')

    def body(self, base, floor='70.0', verdict='pass', shown='71.4'):
        return (f'{comment.MARKER}\n### test-ratchet coverage, cell `c`\n\nCommit {HEAD}.\n\n'
                '| Language | Lines covered | Coverage | Floor | Result | Added lines covered |\n'
                '|---|---|---|---|---|---|\n'
                f'| python | 5 / 7 | {shown}% | {floor} | {verdict} | 2 / 4 |\n\n'
                f'Floors from the thresholds file {THRESHOLDS} at base-ref {base}.\n\n'
                f'{TABLE}| python | `src/a.py` | 1 | 3 | 3, 13 |\n\n'
                'Not measured, GitHub sent no patch: `big.json`\n')

    def test_the_comment_is_created_once_then_edited_in_place(self):
        self.thread()
        listed = ['GET /repos/o/r/pulls/7', 'GET /repos/o/r/pulls/7/files?per_page=100',
                  'GET /repos/o/r/pulls/7/files?per_page=100&page=2',
                  'GET /repos/o/r/issues/7/comments?per_page=100',
                  'GET /repos/o/r/issues/7/comments?per_page=100&page=2']
        result, summary = self.report('pull_request', EVENT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.api.calls(), listed + ['POST /repos/o/r/issues/7/comments'])
        self.assertEqual(self.comments, [{'id': 901, 'user': BOT, 'body': self.body(self.base)}])
        self.assertIn('| python | 5 / 7 | 71.4% | 70.0 | pass |', summary)
        headers = self.api.requests[0].headers
        self.assertEqual((headers['authorization'], headers['cache-control']),
                         ('Bearer tok', 'no-cache'))
        self.api.requests.clear()
        result, _ = self.report('pull_request', EVENT, base_ref=self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.api.calls(), listed + ['PATCH /repos/o/r/issues/comments/901'])
        self.assertEqual(len(self.comments), 1)
        self.assertIn('updated the comment', result.stdout)

    def test_a_pull_request_below_its_floor_is_commented_and_fails(self):
        (self.repo / THRESHOLDS).write_bytes(thresholds(75.0))
        git(self.repo, 'commit', '-q', '-am', 'base')
        base = rev(self.repo)
        self.thread(base=base)
        result, summary = self.report('pull_request', EVENT)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.comments), 1, result.stdout + result.stderr)
        self.assertEqual(self.comments[0]['body'], self.body(base, '75.0', 'fail'))
        self.assertIn('| python | 5 / 7 | 71.4% | 75.0 | fail |', summary)

    def test_a_fork_pull_request_is_commented_through_workflow_run(self):
        # The pull request's base holds a floor of 75; its own head lowered it to 50.
        (self.repo / THRESHOLDS).write_bytes(thresholds(75.0))
        git(self.repo, 'commit', '-q', '-am', 'base')
        base = rev(self.repo)
        (self.repo / THRESHOLDS).write_bytes(thresholds(50.0))
        git(self.repo, 'commit', '-q', '-am', 'lowered')
        pulls = f'/repos/fork/r/commits/{HEAD}/pulls?per_page=100'
        self.api.routes.update({('GET', pulls): answer([
            {'number': 3, 'base': {'repo': {'full_name': 'fork/r'}}},
            {'number': 7, 'base': {'repo': {'full_name': 'o/r'}}}])})
        self.thread(base=base)
        run = {'event': 'pull_request', 'head_sha': HEAD, 'pull_requests': [],
               'head_repository': {'full_name': 'fork/r'}}
        result, summary = self.report('workflow_run', {'workflow_run': run})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'below the floor 75.0 (thresholds file {THRESHOLDS} at base-ref {base})',
                      result.stderr)
        self.assertIn('a workflow_run event only comments', result.stdout)
        self.assertIn('| python | 5 / 7 | 71.4% | 75.0 | fail |', summary)
        self.assertEqual(self.api.calls()[:2], [f'GET {pulls}', 'GET /repos/o/r/pulls/7'])
        self.assertEqual(self.comments[0]['body'], self.body(base, '75.0', 'fail'))
        self.api.requests.clear()
        run['pull_requests'] = [{'number': 7}]
        result, _ = self.report('workflow_run', {'workflow_run': run})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.api.calls()[0], 'GET /repos/o/r/pulls/7')
        self.assertEqual(self.api.calls()[-1], 'PATCH /repos/o/r/issues/comments/901')

    def test_on_workflow_run_the_runs_own_data_only_comments_and_inputs_still_fail(self):
        # A fork's artifacts and its pull request's lookup land on the default branch's commit.
        run = {'event': 'pull_request', 'head_sha': HEAD, 'pull_requests': [{'number': 7}]}
        gone = {**run, 'pull_requests': [], 'head_repository': None}
        good = self.artifacts / 'test-ratchet-c-all'
        shapes = (
            (run, {}, lambda: artifact(self.artifacts, good.name, shard='9/2'), 'key shard', 0),
            (run, {}, lambda: (self.artifacts / 'x').symlink_to(good), 'x is a symlink', 0),
            (run, {}, lambda: artifact(self.artifacts, 'test-ratchet-c-2', coverage=COVERAGE),
             'are both pytest shard 1/1', 0),
            (gone, {}, None, 'the workflow_run payload has no', 0),
            (run, {}, lambda: self.api.routes.update({('GET', '/repos/o/r/pulls/7'): answer(
                {}, 500)}), 'pulls/7 answered 500', 0),
            (run, {'github_token': ' '}, None, 'input github-token is empty', 2),
            (run, {'cell': 'd'}, None, 'no downloaded artifact is of cell d', 0),
            (run, {'base_ref': 'nope'}, None, 'input base-ref nope names no commit', 2))
        for event, inputs, plant, named, code in shapes:
            with self.subTest(named):
                shutil.rmtree(self.artifacts)
                artifact(self.artifacts, good.name, coverage=COVERAGE)
                self.comments.clear()
                self.thread()
                if plant:
                    plant()
                result, summary = self.report('workflow_run', {'workflow_run': event}, **inputs)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(named, result.stderr)
                warned = [line for line in result.stdout.splitlines() if 'only comments' in line]
                self.assertEqual(len(warned), 1 - code // 2, result.stdout)
                self.assertTrue(all(named in line for line in warned))
                self.assertEqual(named in summary, not code)
                refused = [item['body'] for item in self.comments if 'refused' in item['body']]
                self.assertEqual(len(refused), int(not code and 'pulls/7' not in named
                                                   and event is run))

    def test_a_refusal_comment_the_api_refuses_to_write_fails_the_run(self):
        artifact(self.artifacts, 'test-ratchet-c-all', shard='9/2')
        self.thread(refuse=(('POST', '/repos/o/r/issues/7/comments'), 500))
        run = {'event': 'pull_request', 'head_sha': HEAD, 'pull_requests': [{'number': 7}]}
        result, _ = self.report('workflow_run', {'workflow_run': run})
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('issues/7/comments answered 500', result.stderr.splitlines()[-1])

    def test_a_stale_head_or_no_single_pull_request_gets_no_comment(self):
        self.api.routes.update(self.pull(head=OTHER))
        empty = f'/repos/fork/r/commits/{HEAD}/pulls?per_page=100'
        self.api.routes[('GET', empty)] = answer([])
        run = {'event': 'pull_request', 'head_sha': HEAD, 'head_repository': {
            'full_name': 'fork/r'}}
        for name, event, calls, notice in (
                ('pull_request', {'pull_request': {'number': 7, 'head': {'sha': HEAD}}},
                 ['GET /repos/o/r/pulls/7'], f'#7 is at {OTHER}, not at the run\'s head {HEAD}'),
                ('workflow_run', {'workflow_run': {**run, 'pull_requests': [{'number': 7}]}},
                 ['GET /repos/o/r/pulls/7'], f'#7 is at {OTHER}, not at the run\'s head {HEAD}'),
                ('workflow_run', {'workflow_run': {**run, 'pull_requests': []}}, [f'GET {empty}'],
                 f'0 pull requests of o/r hold {HEAD}'),
                ('workflow_run', {'workflow_run': {**run, 'pull_requests': [
                    {'number': 7}, {'number': 8}]}}, [], f'2 pull requests of o/r hold {HEAD}'),
                ('workflow_run', {'workflow_run': {**run, 'event': 'push'}}, [], ''),
                ('push', {'repository': {'default_branch': 'main'}}, [], '')):
            with self.subTest(name=name, event=event):
                self.api.requests.clear()
                result, summary = self.report(name, event, base_ref=self.base)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.api.calls(), calls)
                self.assertIn('| python | 5 / 7 |', summary)
                self.assertNotIn(comment.MARKER, summary)
                self.assertEqual('::notice::no pull request comment: ' in result.stdout,
                                 bool(notice))
                self.assertIn(notice, result.stdout)

    def test_a_refused_write_goes_to_the_job_summary_and_passes(self):
        for route, status in ((('POST', '/repos/o/r/issues/7/comments'), 403),
                              (('GET', '/repos/o/r/issues/7/comments?per_page=100'), 404)):
            with self.subTest(route=route):
                self.thread(refuse=(route, status))
                result, summary = self.report('pull_request', EVENT)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('::notice::the pull request comment could not be written',
                              result.stdout)
                self.assertIn(f'answered {status}', result.stdout)
                self.assertTrue(summary.endswith(self.body(self.base)), summary)
                self.assertEqual(self.comments, [])
        self.thread(refuse=(('POST', '/repos/o/r/issues/7/comments'), 500))
        result, summary = self.report('pull_request', EVENT)
        self.assertEqual(result.returncode, 2)
        self.assertIn('answered 500', result.stderr)
        self.assertNotIn(comment.MARKER, summary)

    def test_a_pull_request_event_needs_the_token(self):
        bad = {'pull_request': {'number': '7/files', 'head': {'sha': HEAD}}}
        for event, inputs, named in (
                (EVENT, {'github_token': ' '}, 'input github-token is empty'),
                (EVENT, {'github_token': ' ', 'artifacts': ''},
                 'github-token is empty(.|\n)*TEST_RATCHET_ARTIFACTS'),
                (bad, {}, "pull request number '7/files' is no integer")):
            result, _ = self.report('pull_request', event, **inputs)
            self.assertEqual(result.returncode, 2)
            self.assertRegex(result.stderr, named)
        self.assertEqual(self.api.calls(), [])

    def test_unreadable_events_and_api_answers_exit_2_naming_them(self):
        files = ('GET', '/repos/o/r/pulls/7/files?per_page=100')
        for routes, payload, named in (
                ({}, None, 'names no pull_request payload'),
                ({}, [], 'is not a pull_request payload'),
                ({}, {'pull_request': {}}, 'the pull_request payload has no'),
                ({('GET', '/repos/o/r/pulls/7'): answer({})}, EVENT, 'has no head and base SHA'),
                ({**self.pull(), files: answer([{'filename': 3}])}, EVENT,
                 'is not a filename and patch'),
                ({**self.pull(), files: answer({})}, EVENT, 'answered no list'),
                ({('GET', '/repos/o/r/pulls/7'): answer(b'<html>')}, EVENT, 'answered no JSON'),
                (self.pull(base='c' * 40), EVENT,
                 f"the pull request's base {'c' * 40} names no commit")):
            with self.subTest(named):
                self.api.routes = routes
                result, summary = self.report('pull_request', payload)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(named, result.stderr)
                # Only the measurement's own refusal leaves no totals to show.
                self.assertEqual('| python | 5 / 7 |' in summary, 'names no commit' not in named)
        with mock.patch.dict(os.environ, {'GITHUB_API_URL': 'http://127.0.0.1:9'}):
            with self.assertRaisesRegex(ConfigError, 'GET http://127.0.0.1:9/x failed'):
                github.Client('tok').call('GET', 'x')

    def test_a_pull_request_with_nothing_merged_says_so_and_fails_a_recorded_floor(self):
        artifact(self.artifacts, 'test-ratchet-c-all', passed=False, coverage=COVERAGE)
        self.thread()
        result, _ = self.report('pull_request', EVENT)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('| python | nothing measured | none | 70.0 | fail: recorded floor but'
                      ' nothing measured | none |', self.comments[0]['body'].splitlines())
        self.assertIn('- `test-ratchet-c-all: its tests failed`', self.comments[0]['body'])
        (self.repo / THRESHOLDS).write_bytes(b'{"schema_version": 1}\n')
        git(self.repo, 'commit', '-q', '-am', 'no records')
        self.api.routes.update(self.pull(base=rev(self.repo)))
        result, _ = self.report('pull_request', EVENT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No coverage was merged.', self.comments[0]['body'])

    def test_a_next_page_outside_the_api_is_refused(self):
        # A host that merely starts with the API's host is another host.
        outside = f'{self.api.url}.elsewhere.example/x'
        self.api.routes[('GET', '/x')] = answer([1], link=outside)
        with mock.patch.dict(os.environ, {'GITHUB_API_URL': self.api.url, 'no_proxy': '*'}):
            with self.assertRaisesRegex(ConfigError, f'next page {outside} is not under'):
                github.Client('tok').pages('x')

    def test_artifact_text_is_inert_and_an_oversized_comment_points_at_the_summary(self):
        files = [{'filename': f'src/module_{index:04}.py', 'patch': '@@ -0,0 +1 @@\n+x'}
                 for index in range(2500)]
        listing = '/repos/o/r/issues/7/comments?per_page=100'
        self.api.routes.update({('GET', '/repos/o/r/pulls/7/files?per_page=100'): answer(files),
                                ('GET', listing): answer([])})
        posted = '/repos/o/r/issues/7/comments'
        self.api.routes[('POST', posted)] = \
            lambda body: (201, self.comments.append(body['body']) or {'id': 5}, {})
        coverage = {'python': {item['filename']: {1: 0} for item in files}}
        result = report.Report('c`@team', [], ['x: [y](z) @someone'], coverage,
                               [report.Total('python', 0, 2500, '0.0', None)],
                               'thresholds file t', {})
        summary = self.scratch / 'summary.md'
        summary.write_text('', encoding='utf-8')
        with mock.patch.dict(os.environ, {
                'GITHUB_API_URL': self.api.url, 'GITHUB_STEP_SUMMARY': str(summary),
                'GITHUB_SERVER_URL': 'https://github.example', 'GITHUB_RUN_ID': '77',
                'no_proxy': '*'}):
            comment.publish(comment.Target(github.Client('tok'), 'o/r', 7, HEAD, 'b'), result)
        self.assertEqual(len(self.comments), 1)
        own = report.summary(result).splitlines()
        self.assertEqual([own[0], own[-1]], ["### test-ratchet coverage, cell `c'@team`",
                                             '- `x: [y](z) @someone`'])
        body = self.comments[0]
        self.assertLessEqual(len(body), comment.LIMIT)
        self.assertIn("cell `c'@team`", body)
        self.assertIn('| python | 0 / 2500 | 0.0% | none | not gated: no record | 0 / 2500 |',
                      body)
        self.assertNotIn('module_0000', body)
        self.assertIn('https://github.example/o/r/actions/runs/77', body)
        full = summary.read_text(encoding='utf-8')
        self.assertIn('| python | `src/module_2499.py` | 0 | 1 | 1 |\n', full)
        self.assertIn('- `x: [y](z) @someone`\n', full)
        self.assertTrue(full.startswith(comment.MARKER))
        self.assertEqual(full.count(comment.MARKER), 1)
        summary.write_text('', encoding='utf-8')
        self.api.routes[('POST', posted)] = answer({'message': 'refused'}, 403)
        with mock.patch.dict(os.environ, {'GITHUB_API_URL': self.api.url, 'no_proxy': '*',
                                          'GITHUB_STEP_SUMMARY': str(summary)}):
            comment.publish(comment.Target(github.Client('tok'), 'o/r', 7, HEAD, 'b'), result)
        full = summary.read_text(encoding='utf-8')
        self.assertEqual(full.count(comment.MARKER), 1)
        self.assertIn('| python | `src/module_2499.py` | 0 | 1 | 1 |\n', full)

    def test_the_action_downloads_into_a_fresh_directory_and_runs_the_driver(self):
        seen = []

        def download(values):
            path = Path(values['path'])
            seen.append(path.is_dir() and not path.is_symlink() and not any(path.iterdir()))
            shutil.copytree(self.artifacts, path, dirs_exist_ok=True)

        self.api.routes.update(self.pull(head=OTHER))
        event = self.scratch / 'event.json'
        event.write_text(json.dumps(EVENT), encoding='utf-8')
        summary = self.scratch / 'summary.md'
        summary.write_text('', encoding='utf-8')
        planted = self.scratch / 'planted'
        planted.mkdir()
        (planted / 'json.py').write_bytes(b'raise SystemExit("planted json imported")\n')
        result = run_action('report', {'github-token': 'tok', 'base-ref': self.base}, self.repo,
                            'python3', tools=('mktemp',),
                            uses={'actions/download-artifact': download},
                            runner_env={'GITHUB_STEP_SUMMARY': str(summary),
                                        'GITHUB_EVENT_NAME': 'pull_request',
                                        'GITHUB_EVENT_PATH': str(event),
                                        'GITHUB_REPOSITORY': 'o/r', 'GITHUB_API_URL': self.api.url,
                                        'no_proxy': '*', 'PYTHONPATH': str(planted)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(seen, [True])
        self.assertIn('| python | 5 / 7 | 71.4% | 70.0 | pass |',
                      summary.read_text(encoding='utf-8'))
        self.assertEqual(self.api.calls(), ['GET /repos/o/r/pulls/7'])
        self.assertEqual(self.api.requests[0].headers['authorization'], 'Bearer tok')


if __name__ == '__main__':
    unittest.main()
