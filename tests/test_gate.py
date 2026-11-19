"""Input workflow-run-gate: a workflow_run run of a pull request's run gates as that run does."""
import shutil
from functools import partial

from _action_harness import run_action
from _github_double import answer
from _report_harness import COVERAGE, HEAD, OTHER, THRESHOLDS, ReportCase, rev, thresholds
from test_collect import artifact
from test_split import git


class WorkflowRunGateTest(ReportCase):
    def test_a_gating_run_fails_and_refuses_as_a_pull_request_run_does(self):
        (self.repo / THRESHOLDS).write_bytes(thresholds(75.0))
        git(self.repo, 'commit', '-q', '-am', 'raised')
        high, good = rev(self.repo), self.artifacts / 'test-ratchet-c-all'
        run = {'event': 'pull_request', 'head_sha': HEAD, 'pull_requests': [{'number': 7}]}
        fork = {**run, 'pull_requests': [], 'head_repository': {'full_name': 'fork/r'}}
        held = ('GET', f'/repos/fork/r/commits/{HEAD}/pulls?per_page=100')
        mine = {'base': {'repo': {'full_name': 'o/r'}}}
        api = self.api.routes.update
        # Each row: payload, the pull request's base, plant, what is named, status, comment.
        rows = (
            (run, None, None, 'python 71.4% (5 / 7 lines), floor 70.0', 0, 'pass'),
            (run, high, None, 'python coverage 71.4% is below the floor 75.0', 1, 'fail'),
            (run, None, (good / 'coverage-python.xml').unlink,
             'python has a recorded floor 70.0 but nothing measured', 1, 'fail: recorded'),
            (run, None, lambda: artifact(self.artifacts, good.name, shard='9/2'), 'key shard',
             2, 'refused'),
            (run, None, lambda: (self.artifacts / 'x').symlink_to(good), 'x is a symlink', 2,
             'refused'),
            (run, None, partial(shutil.rmtree, good), 'no test-ratchet artifact was downloaded',
             2, 'refused'),
            (run, None, 'd', 'no downloaded artifact is of cell d', 2, 'refused'),
            (run, None, lambda: api({('GET', '/repos/o/r/pulls/7'): answer({}, 500)}),
             'pulls/7 answered 500', 2, ''),
            ({**fork, 'head_repository': None}, None, None, 'the workflow_run payload has no',
             2, ''),
            (fork, None, lambda: api({held: answer([])}), f'0 pull requests of o/r hold {HEAD}',
             2, ''),
            (fork, None, lambda: api({held: answer([{'number': 7, **mine},
                                                    {'number': 8, **mine}])}),
             f'2 pull requests of o/r hold {HEAD}', 2, ''),
            (run, None, lambda: api(self.pull(head=OTHER)),
             f"#7 is at {OTHER}, not at the run's head {HEAD}", 2, ''),
            # Triggered by a push: no pull request, the checkout's floor of 75, comment-only.
            ({**run, 'event': 'push'}, None, None, 'below the floor 75.0', 0, None))
        for event, base, plant, named, code, written in rows:
            with self.subTest(named):
                shutil.rmtree(self.artifacts)
                artifact(self.artifacts, good.name, coverage=COVERAGE)
                self.comments.clear()
                self.thread(base=base)
                inputs = {'cell': plant} if isinstance(plant, str) else {}
                if callable(plant):
                    plant()
                result, summary = self.report('workflow_run', {'workflow_run': event},
                                              workflow_run_gate=' true ', **inputs)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(named, result.stdout + result.stderr)
                self.assertEqual('only comments' in result.stdout, written is None)
                bodies = [item['body'] for item in self.comments]
                self.assertEqual(len(bodies), int(bool(written)), bodies)
                if written:
                    self.assertIn(f'| {written}' if written != 'refused'
                                  else 'refused the artifacts', bodies[0])
                if code == 2:
                    self.assertIn(named, summary)

    def test_an_input_other_than_true_or_false_exits_2_naming_it(self):
        run = {'event': 'push', 'head_sha': HEAD, 'pull_requests': []}
        for name in ('workflow-run-gate', 'records-only-rise'):
            for event, value in (('', 'yes'), ('workflow_run', 'True'), ('push', '1')):
                with self.subTest(name=name, event=event, value=value):
                    result, _ = self.report(event, {'workflow_run': run},
                                            **{name.replace('-', '_'): value})
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(f"input {name} must be 'true' or 'false', got {value!r}",
                                  result.stderr)
            # The action passes the input through to the driver.
            done = run_action('report', {'github-token': 'tok', 'base-ref': '', name: 'no'},
                              self.repo, 'python3', tools=('mktemp',),
                              uses={'actions/download-artifact': lambda _values: None})
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn(f"input {name} must be 'true' or 'false', got 'no'", done.stderr)
