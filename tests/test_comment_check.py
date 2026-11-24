"""Input comment-check: a check run on the pull request's head says whether the comment was
written."""
import shutil
import unittest

from _github_double import answer
from _report_harness import BOT, COVERAGE, EVENT, HEAD, OTHER, ReportCase, commit, thresholds
from test_collect import artifact
from test_ratchet import comment

CHECK, NAME = ('POST', '/repos/o/r/check-runs'), 'coverage comment'
RUN = {'event': 'pull_request', 'head_sha': HEAD, 'pull_requests': [{'number': 7}]}
FORK = {**RUN, 'pull_requests': [], 'head_repository': {'full_name': 'fork/r'}}
WROTE = ('success', 'commented on pull request #7',
         'The comment on pull request #7 holds the coverage report.')


def check(conclusion, title, summary):
    return {'name': NAME, 'head_sha': HEAD, 'status': 'completed', 'conclusion': conclusion,
            'output': {'title': title, 'summary': summary}}


class CommentCheckTest(ReportCase):
    def compare(self, event, plant=None, inputs=None, name='workflow_run', answer_status=201,
                alike=True):
        """The run with input comment-check empty, then set: the second run and the check bodies
        it posted, the first having posted none and, when `alike`, ended as the second did."""
        runs, posted = [], []
        self.api.routes[CHECK] = lambda body: (posted.append(body) or answer_status, {}, {})
        for value in ('', NAME):
            shutil.rmtree(self.artifacts)
            artifact(self.artifacts, 'test-ratchet-c-all', coverage=COVERAGE)
            self.comments.clear()
            self.thread()
            if plant:
                plant()
            runs.append(self.report(name, {'workflow_run': event} if name == 'workflow_run'
                                    else event, comment_check=value, **(inputs or {})))
            if not value:
                self.assertEqual(posted, [], 'input comment-check empty')
        if alike:
            self.assertEqual(*[(result.returncode, result.stdout, result.stderr, summary)
                               for result, summary in runs])
        return runs[1][0], posted

    def refuse(self, route, status):
        return lambda: self.thread(refuse=(route, status))

    def test_a_written_comment_concludes_success(self):
        edited = ('success', 'updated the comment on pull request #7', WROTE[2])
        shard = ("artifact test-ratchet-c-all: manifest.json key shard must be i/N or null,"
                 " got '9/2'")
        high = commit(self.repo, thresholds(75.0))
        # The last two rows' steps are refused after the comment: the pull request's head is not
        # in the checkout for records-only-rise or gate-raised-floors.
        for plant, gate, written, code, inputs in (
                (None, 'false', WROTE, 0, {}), (None, 'true', WROTE, 0, {}),
                (lambda: self.comments.append({'id': 901, 'user': BOT, 'body': comment.MARKER}),
                 'false', edited, 0, {}),
                (lambda: artifact(self.artifacts, 'test-ratchet-c-all', shard='9/2'), 'false',
                 (*WROTE[:2], 'The comment on pull request #7 holds the refusal of the'
                  f' artifacts: `{shard}`.'), 0, {}),
                (lambda: self.thread(base=high), 'true', WROTE, 1, {}),
                (lambda: self.thread(base=high), 'false', WROTE, 0, {}),
                (None, 'true', WROTE, 2, {'records_only_rise': 'true'}),
                (None, 'true', WROTE, 2, {'gate_raised_floors': 'true'})):
            with self.subTest(written=written, gate=gate, code=code, inputs=inputs):
                result, posted = self.compare(RUN, plant, {**inputs, 'workflow_run_gate': gate})
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertEqual(posted, [check(*written)])

    def test_a_comment_that_could_not_be_written_concludes_failure(self):
        posting = '/repos/o/r/issues/7/comments'
        listing = f'{posting}?per_page=100'
        refused = '{"message": "Resource not accessible by integration"}'
        for plant, inputs, error, code in (
                (self.refuse(('POST', posting), 403), {},
                 f'GitHub API POST {self.api.url}{posting} answered 403: {refused}', 0),
                (self.refuse(('GET', listing), 404), {},
                 f'GitHub API GET {self.api.url}{listing} answered 404: {refused}', 0),
                (self.refuse(('POST', posting), 500), {},
                 f'GitHub API POST {self.api.url}{posting} answered 500: {refused}', 2),
                (None, {'base_ref': 'nope'}, 'input base-ref nope names no commit in this'
                 ' checkout: fetch it (actions/checkout fetches one commit unless fetch-depth'
                 ' says otherwise)', 2),
                (lambda: (self.refuse(('POST', posting), 500)(), artifact(
                    self.artifacts, 'test-ratchet-c-all', shard='9/2')), {},
                 f'GitHub API POST {self.api.url}{posting} answered 500: {refused}', 2)):
            with self.subTest(error):
                result, posted = self.compare(RUN, plant, inputs)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertEqual(posted, [check(
                    'failure', 'the comment could not be written',
                    f'The comment on pull request #7 could not be written: `{error}`')])
                self.assertEqual(self.comments, [])

    def test_a_failed_or_ambiguous_lookup_concludes_failure(self):
        failed = 'The pull request lookup failed, so no comment was written: '
        many = f'2 pull requests of o/r hold {HEAD}'
        for event, plant, gate, written, code in (
                (RUN, lambda: self.api.routes.update({('GET', '/repos/o/r/pulls/7'): answer(
                    {}, 500)}), 'false', ('the pull request lookup failed', f'{failed}`GitHub API'
                                          f' GET {self.api.url}/repos/o/r/pulls/7 answered 500:'
                                          ' {}`'), 0),
                ({'event': 'pull_request', 'head_sha': HEAD}, None, 'false',
                 ('the pull request lookup failed', f"{failed}`GITHUB_EVENT_PATH: the"
                  " workflow_run payload has no 'pull_requests'`"), 0),
                ({**FORK, 'head_repository': {}}, None, 'false',
                 ('the pull request lookup failed', f"{failed}`GITHUB_EVENT_PATH: the"
                  " workflow_run payload has no 'full_name'`"), 0),
                ({**RUN, 'pull_requests': [{'number': 7}, {'number': 8}]}, None, 'false',
                 (many, f'No comment was written: `{many}`'), 0),
                ({**RUN, 'pull_requests': [{'number': 7}, {'number': 8}]}, None, 'true',
                 (many, f'No comment was written: `{many}`'), 2),
                ({**RUN, 'pull_requests': [{'number': 7}, {'number': 8}]},
                 lambda: artifact(self.artifacts, 'test-ratchet-c-all', shard='9/2'), 'false',
                 (many, f'No comment was written: `{many}`'), 0)):
            with self.subTest(written=written, gate=gate):
                result, posted = self.compare(event, plant, {'workflow_run_gate': gate})
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertEqual(posted, [check('failure', *written)])
                self.assertEqual(self.comments, [])

    def test_no_pull_request_destination_publishes_no_check(self):
        held = ('GET', f'/repos/fork/r/commits/{HEAD}/pulls?per_page=100')
        for event, plant, named in (
                (FORK, lambda: self.api.routes.update({held: answer([])}),
                 f'0 pull requests of o/r hold {HEAD}'),
                (RUN, lambda: self.api.routes.update(self.pull(head=OTHER)),
                 f"#7 is at {OTHER}, not at the run's head {HEAD}"),
                ({**FORK, 'head_repository': None}, None, 'the workflow_run payload has no')):
            for gate, code in (('false', 0), ('true', 2)):
                with self.subTest(named, gate=gate):
                    result, posted = self.compare(event, plant, {'workflow_run_gate': gate})
                    self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                    self.assertIn(named, result.stdout + result.stderr)
                    self.assertEqual(posted, [])
        # A lookup that fails before reading the run's head leaves no commit to publish on.
        result, posted = self.compare({'event': 'pull_request', 'pull_requests': []}, alike=False)
        self.assertEqual((result.returncode, posted), (0, []), result.stdout + result.stderr)
        self.assertIn(f'::warning::no check run {NAME}: the workflow_run payload has no'
                      ' head_sha', result.stdout)

    def test_other_events_publish_no_check(self):
        for name, event in (('pull_request', EVENT), ('workflow_run', {**RUN, 'event': 'push'})):
            with self.subTest(name=name, event=event):
                result, posted = self.compare(event, name=name)
                self.assertEqual((result.returncode, posted), (0, []), result.stderr)

    def test_a_refused_check_write_fails_the_step_naming_the_permission(self):
        for status, named in ((403, 'needs checks: write'), (404, 'needs checks: write'),
                              (500, 'check-runs answered 500')):
            with self.subTest(status=status):
                result, posted = self.compare(RUN, answer_status=status, alike=False)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(posted, [check(*WROTE)])
                self.assertIn(named, result.stderr.splitlines()[-1])
                self.assertEqual(len(self.comments), 1)

    def test_the_action_passes_the_input_to_the_driver(self):
        posted = []
        self.thread()
        self.api.routes[CHECK] = lambda body: (posted.append(body) or 201, {}, {})
        result, _, _ = self.action({'comment-check': NAME}, 'workflow_run', {'workflow_run': RUN})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(posted, [check(*WROTE)])


if __name__ == '__main__':
    unittest.main()
