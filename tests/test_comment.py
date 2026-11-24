"""The pull request comment: resolved from the event, created once, then edited in place."""
import itertools
import os
import shutil
import unittest
import zipfile
from unittest import mock

from _github_double import answer
from _report_harness import BOT, COVERAGE, EARLIER, EVENT, FILES, HEAD, LATER, LISTED, OTHER, \
    PATCH_A, PATCH_B, RUN, TABLE, THRESHOLDS, ReportCase, commit, thresholds
from test_collect import artifact
from test_ratchet import ConfigError, comment, github, report

ZIPS, ROW = '/repos/o/r/actions/artifacts', '| python | 5 / 7 | 71.4% | 70.0 | pass |'


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


class CommentTest(ReportCase):
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
        self.assertIn(ROW, summary)
        headers = self.api.requests[0].headers
        self.assertEqual((headers['authorization'], headers['cache-control']),
                         ('Bearer tok', 'no-cache'))
        self.api.requests.clear()
        result, _ = self.report('pull_request', EVENT, base_ref=self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.api.calls(), listed + ['PATCH /repos/o/r/issues/comments/901'])
        self.assertEqual(len(self.comments), 1)
        self.assertIn('updated the comment', result.stdout)

    def test_the_comment_shows_the_figure_at_input_precision(self):
        self.thread()
        # 5 / 7 lines is 71.428...%, truncated at two decimals.
        result, _ = self.report('pull_request', EVENT, precision='2')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([item['body'] for item in self.comments],
                         [self.body(self.base, shown='71.42')])
        # The action passes the input through to the driver.
        done, _, _ = self.action({'base-ref': '', 'precision': '7'})
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("input precision must be a whole number from 1 to 6, got '7'", done.stderr)

    def test_a_pull_request_below_its_floor_is_commented_and_fails(self):
        base = commit(self.repo, thresholds(75.0))
        self.thread(base=base)
        result, summary = self.report('pull_request', EVENT)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual([x['body'] for x in self.comments], [self.body(base, '75.0', 'fail')])
        self.assertIn('| python | 5 / 7 | 71.4% | 75.0 | fail |', summary)

    def test_a_fork_pull_request_is_commented_through_workflow_run(self):
        # The pull request's base holds a floor of 75; its own head lowered it to 50.
        base = commit(self.repo, thresholds(75.0))
        commit(self.repo, thresholds(50.0))
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
        gone = {**RUN, 'pull_requests': [], 'head_repository': None}
        good = self.artifacts / 'test-ratchet-c-all'
        shapes = (
            (RUN, {}, lambda: artifact(self.artifacts, good.name, shard='9/2'), 'key shard', 0),
            (RUN, {}, lambda: (self.artifacts / 'x').symlink_to(good), 'x is a symlink', 0),
            (RUN, {}, lambda: artifact(self.artifacts, 'test-ratchet-c-2', coverage=COVERAGE),
             'are both pytest shard 1/1', 0),
            (gone, {}, None, 'the workflow_run payload has no', 0),
            (RUN, {}, lambda: self.api.routes.update({('GET', '/repos/o/r/pulls/7'): answer(
                {}, 500)}), 'pulls/7 answered 500', 0),
            (RUN, {'github_token': ' '}, None, 'input github-token is empty', 2),
            (RUN, {'cell': 'd'}, None, 'no downloaded artifact is of cell d', 0),
            (RUN, {'base_ref': 'nope'}, None, 'input base-ref nope names no commit', 2))
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
                                                   and event is RUN))

    def test_a_refusal_comment_the_api_refuses_to_write_fails_the_run(self):
        artifact(self.artifacts, 'test-ratchet-c-all', shard='9/2')
        self.thread(refuse=(('POST', '/repos/o/r/issues/7/comments'), 500))
        result, _ = self.report('workflow_run', {'workflow_run': RUN})
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
        self.api.routes = {('GET', '/x'): answer({}, 503)}
        with mock.patch.dict(os.environ, {'GITHUB_API_URL': self.api.url, 'no_proxy': '*'}):
            for cut in (ConnectionResetError('reset'), github.http.client.IncompleteRead(b'')):
                with mock.patch('http.client.HTTPResponse.read', side_effect=cut):
                    with self.assertRaisesRegex(github.ApiError, f'/x answered 503: {cut!s:.5}'):
                        github.Client('tok').call('GET', 'x')

    def test_a_pull_request_with_nothing_merged_says_so_and_fails_a_recorded_floor(self):
        artifact(self.artifacts, 'test-ratchet-c-all', passed=False, coverage=COVERAGE)
        self.thread()
        result, _ = self.report('pull_request', EVENT)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('| python | nothing measured | none | 70.0 | fail: recorded floor but'
                      ' nothing measured | none |', self.comments[0]['body'].splitlines())
        self.assertIn('- `test-ratchet-c-all: its tests failed`', self.comments[0]['body'])
        self.api.routes.update(self.pull(base=commit(self.repo, b'{"schema_version": 1}\n')))
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
        self.assertIn('| python | 0 / 2500 | 0.0% | none | not gated: no record | 0 / 2500 |', body)
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
        seen, listing = [], self.listing()

        def listed(body):
            seen.extend((path.is_symlink(), path.stat().st_mode & 0o777, os.listdir(path))
                        for path in self.scratch.glob('*/runner-temp/*'))
            return listing(body)

        self.api.routes.update(self.pull(head=OTHER))
        (self.scratch / 'json.py').write_bytes(b'raise SystemExit("planted json imported")\n')
        with mock.patch('tempfile.tempdir', str(self.scratch)):
            result, summary, slept = self.action({}, 'pull_request', EVENT,
                                                 {'PYTHONPATH': str(self.scratch)}, listed)
        self.assertEqual((result.returncode, seen, slept, ROW in summary),
                         (0, [(False, 0o700, [])], [], True), result.stdout + result.stderr)
        self.assertEqual(self.api.calls(), [
            f'GET {LISTED}', f'GET {ZIPS}/1/zip', 'GET /blob/1', 'GET /repos/o/r/pulls/7'])
        # The token stays with the API: the blob store it redirects to never sees it.
        self.assertEqual([item.headers.get('authorization') for item in self.api.requests],
                         ['Bearer tok', 'Bearer tok', None, 'Bearer tok'])

    def shard(self, number, shard, created=EARLIER, **manifest):
        name = f'test-ratchet-c-{shard.replace("/", "_")}'
        return self.upload(number, name, artifact(self.scratch / str(number), name, shard=shard,
                                                  **manifest), created)

    def test_of_one_name_the_artifact_created_last_is_read(self):
        self.api.routes.update(self.pull(head=OTHER))
        for (first, last), backwards, event in itertools.product(
                ((99, 11), (11, 99)), (False, True), ('', 'workflow_run')):
            with self.subTest(first=first, backwards=backwards, event=event):
                self.api.requests.clear()
                # Shard 2/2 was not re-run.
                both = [self.shard(first, '1/2', passed=False),
                        self.shard(last, '1/2', LATER, coverage=COVERAGE)][::-1 if backwards else 1]
                self.api.routes[('GET', f'{LISTED}&page=2')] = answer(
                    {'artifacts': [both[1], self.shard(50, '2/2')]})
                result, summary, _ = self.action({}, event, {'workflow_run': RUN}, listing=answer(
                    {'artifacts': [both[0], {'id': 3, 'name': 'other', 'created_at': LATER}]},
                    link=f'{self.api.url}{LISTED}&page=2'))
                self.assertEqual((result.returncode, ROW in summary, 'Left out' in summary),
                                 (0, True, False), result.stdout + result.stderr)
                self.assertEqual({call for call in self.api.calls() if call.endswith('/zip')},
                                 {f'GET {ZIPS}/{number}/zip' for number in (last, 50)})

    def test_a_failed_or_empty_download_alone_is_repeated_until_the_window_closes(self):
        def listed(*items):
            return answer({'artifacts': items})

        def other(number, sent):
            return listed(*good, self.upload(number, 'test-ratchet-x', sent))

        good, bad = [self.shard(1, '1/1', coverage=COVERAGE)], self.upload(9, 'test-ratchet-c', b'')
        empty, down, found = listed(), answer({}, 500), listed(*good)
        self.api.routes[('GET', f'{ZIPS}/7/zip')] = answer({}, 503)
        cut = self.upload(10, 'test-ratchet-c', b'')
        self.api.routes[('GET', '/blob/10')] = lambda _body: (200, b'9', {'Content-Length': '99'})
        partial = listed(self.shard(8, '1/2'), {**bad, 'id': 7, 'created_at': LATER})
        digest = listed({**bad, 'digest': 'sha256:00'})
        outside, absent = self.scratch / 'outside.zip', 'test-ratchet-x has no manifest.json'
        with zipfile.ZipFile(outside, 'w') as archive:
            archive.writestr('../test-ratchet-c-1_1/manifest.json', '{')
        for name, answers, status, shown in (
                ('empty once', [empty, found], 0, ROW),
                ('failed, then empty', [down, empty, found], 0, ROW),
                ('half of it', [empty, partial, found], 0, ROW),
                ('undigested', [listed({**good[0], 'digest': None}, {
                    **bad, 'name': good[0]['name'], 'created_at': None})], 0, ROW),
                ('the last attempt', [digest, down, empty, partial, found], 0, ROW),
                ('never uploaded', [empty] * 5, 2, 'no test-ratchet artifact was downloaded'),
                ('not a listing', [answer({})] * 5, 2, f'{LISTED[1:]} answered no list'),
                ('a path', [listed({**bad, 'name': 'test-ratchet-../c'})] * 5, 2, "../c' is no"),
                ('a drive', [listed({**bad, 'name': 'test-ratchet-\\c'})] * 5, 2, "c' is no"),
                ('never its digest', [digest] * 5, 2, 'does not have its digest sha256:00'),
                ('cut off', [listed({**cut, 'digest': None})] * 5, 2, 'zip failed: IncompleteRead'),
                ('crc', [other(5, outside.read_bytes().replace(b'{', b'['))] * 5, 2, 'be unpacked'),
                ('always half of it', [partial] * 5, 2, 'artifacts/7/zip answered 503'),
                ('a zip leaving its directory', [other(2, outside.read_bytes())], 2, absent),
                ('an empty zip', [other(3, b'PK\5\6' + b'\0' * 18)], 2, absent),
                ('a file not archived', [other(4, b'{}')], 2, absent),
                ('it alone', [listed(self.upload(6, 'test-ratchet-x', b'{}'))], 2, absent),
                ('only one', [listed(self.shard(12, '9/2'))], 2, '(the only artifact downloaded)')):
            with self.subTest(name):
                result, summary, slept = self.action(
                    {}, listing=lambda body, left=iter(answers): next(left)(body))
                self.assertEqual((result.returncode, slept), (status, [
                    f'{pause} drwx------' for pause in (5, 10, 20, 40)][:len(answers) - 1]),
                    result.stdout + result.stderr)
                self.assertIn(shown, summary + result.stderr)


if __name__ == '__main__':
    unittest.main()
