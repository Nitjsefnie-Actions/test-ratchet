"""Input gate-raised-floors: a pull request is gated by the floors its own thresholds file raises
or adds."""
import json
import unittest

from _report_harness import THRESHOLDS, rev
from test_collect import artifact, lines
from test_rise import ThresholdsCase, records
from test_split import git


def floors(**languages):
    return json.dumps({'schema_version': 1, 'coverage': {
        language: {'floor': floor} for language, floor in languages.items()}}).encode()


class GateRaisedFloorsTest(ThresholdsCase):
    def test_a_pull_request_is_gated_by_the_floors_its_head_raises_or_adds(self):
        base = self.commit(self.base, floors(python=93.5))
        empty = self.commit(self.base, floors())
        # Forked from `fork`, after which the base lowered, raised or removed the record.
        fork = self.commit(self.base, floors(python=97.0))
        lowered = self.commit(fork, floors(python=93.5))
        early = self.commit(self.base, floors(python=93.5))
        later = self.commit(early, floors(python=97.0))
        removed = self.commit(fork, floors())
        git(self.repo, 'rm', '-q', THRESHOLDS)
        git(self.repo, 'commit', '-q', '-m', 'no thresholds file')
        deleted = rev(self.repo)
        broken = self.commit(base, b'{"schema_version": 1, "coverage": {"python": {}}}')
        python = '| python | {} | {} | {} |'.format
        # Each row: base and head, inputs, covered of 1000 lines, status, what is named.
        for parent, head, inputs, covered, code, named in (
                (base, floors(python=96.0), {}, 950, 1, [
                    python('950 / 1000 | 95.0%', '96.0 from the head (base 93.5)', 'fail'),
                    'python coverage 95.0% is below the floor 96.0 from the head (base 93.5)'
                    f' (thresholds file {THRESHOLDS} at base-ref {base})',
                    'python 95.0% (950 / 1000 lines), floor 96.0 from the head (base 93.5)\n']),
                (base, floors(python=96.0), {'gate_raised_floors': 'false'}, 950, 0,
                 [python('950 / 1000 | 95.0%', '93.5', 'pass')]),
                (base, floors(python=95.0), {}, 950, 0,
                 [python('950 / 1000 | 95.0%', '95.0 from the head (base 93.5)', 'pass')]),
                (base, floors(python=93.5), {}, 950, 0,
                 [python('950 / 1000 | 95.0%', '93.5', 'pass')]),
                (base, floors(python=90.0), {}, 950, 0,
                 [python('950 / 1000 | 95.0%', '93.5', 'pass')]),
                (base, floors(python=90.0), {'records_only_rise': 'true'}, 950, 1,
                 ['coverage.python.floor lowered from 93.5 to 90.0']),
                (empty, floors(python=96.0), {}, 950, 1,
                 [python('950 / 1000 | 95.0%', '96.0 from the head (base none)', 'fail')]),
                (empty, floors(python=95.0), {}, 950, 0,
                 [python('950 / 1000 | 95.0%', '95.0 from the head (base none)', 'pass')]),
                (base, floors(python=93.5, javascript=10), {}, 950, 1, [
                    '| javascript | nothing measured | none | 10 from the head (base none) |'
                    ' fail: recorded floor but nothing measured |',
                    'javascript has a recorded floor 10 from the head (base none) but nothing'
                    ' measured it']),
                # The head's 97.0 is the merge base's, not a raise: the base's 93.5 gates.
                (lowered, None, {}, 950, 0, [python('950 / 1000 | 95.0%', '93.5', 'pass')]),
                (removed, None, {}, 950, 0,
                 [python('950 / 1000 | 95.0%', 'none', 'not gated: no record')]),
                # Raised from the merge base's 93.5 to 96.0 while the base went to 97.0.
                (later, floors(python=96.0), {}, 965, 1,
                 [python('965 / 1000 | 96.5%', '97.0 from the base (head 96.0)', 'fail')]),
                (base, None, {}, 950, 0, [python('950 / 1000 | 95.0%', '93.5', 'pass')]),
                (base, b'{', {}, 950, 2, ['is not JSON'])):
            with self.subTest(parent=parent, head=head, inputs=inputs):
                fork_point = {lowered: fork, removed: fork, later: early}.get(parent, parent)
                tip = (deleted if head is None and parent == base
                       else fork if head is None else self.commit(fork_point, head))
                artifact(self.artifacts, 'test-ratchet-c-all',
                         coverage={'python': lines(covered, 1000)})
                self.comments.clear()
                self.thread(head=tip, base=parent)
                result, summary = self.report('pull_request', {'pull_request': {
                    'number': 7, 'head': {'sha': tip}, 'base': {'sha': parent}}}, self.path,
                    **{'gate_raised_floors': 'true', **inputs})
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                for text in named:
                    self.assertIn(text, result.stdout + result.stderr + summary
                                  + ''.join(item['body'] for item in self.comments))
                if code != 2 and named[0].startswith('|'):
                    self.assertIn(named[0], summary.splitlines())
                    self.assertIn(named[0] + ' ', self.comments[0]['body'])
        self.thread(head=broken, base=base)
        result, _ = self.report('pull_request', {'pull_request': {
            'number': 7, 'head': {'sha': broken}, 'base': {'sha': base}}}, self.path,
            gate_raised_floors='true')
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f'thresholds file {THRESHOLDS} at {broken}: key coverage.python.floor must'
                      ' be a number', result.stderr)

    def test_a_raised_floor_is_compared_as_the_base_floor_is_at_precision_2(self):
        base = self.commit(self.base, floors(python=93.5))
        artifact(self.artifacts, 'test-ratchet-c-all', coverage={'python': lines(23464, 25000)})
        # 93.856% shows 93.85 at two decimals; the exact value is gated.
        for floor, code in ((93.86, 1), (93.856, 0), (93.8561, 1), (93.85, 0)):
            with self.subTest(floor=floor):
                head = self.commit(base, floors(python=floor))
                self.thread(head=head, base=base)
                result, summary = self.report('pull_request', {'pull_request': {
                    'number': 7, 'head': {'sha': head}, 'base': {'sha': base}}}, self.path,
                    gate_raised_floors='true', precision='2')
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(f'| python | 23464 / 25000 | 93.85% | {floor} from the head (base'
                              f' 93.5) | {"fail" if code else "pass"} |', summary.splitlines())

    def test_a_gating_run_gates_a_forks_raised_floor_and_other_runs_ignore_the_input(self):
        head = self.fork(records(72.0, 72.0))
        result, summary = self.gating(head, workflow_run_gate='true', gate_raised_floors='true')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('python coverage 71.4% is below the floor 72.0 from the head (base 70.0)',
                      result.stderr)
        self.assertIn(' fetch ', self.log.read_text(encoding='utf-8'))
        self.log.unlink()
        # Not gating: it only comments, with the base's floor, and fetches nothing.
        result, _ = self.gating(head, gate_raised_floors='true')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('python 71.4% (5 / 7 lines), floor 70.0\n', result.stdout)
        self.assertNotIn(' fetch ', self.log.read_text(encoding='utf-8'))
        self.assertNotIn('from the head', result.stdout + result.stderr)
        # A push gates its own checkout's file, the input changing nothing.
        git(self.repo, 'checkout', '-q', '--detach', self.commit(self.base, records(60.0, 60.0)))
        sha = rev(self.repo)
        for flag in ('true', 'false'):
            with self.subTest(flag=flag):
                result, summary = self.report('push', {
                    'repository': {'default_branch': 'main'}, 'before': self.base},
                    {**self.path, 'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': sha},
                    gate_raised_floors=flag)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('| python | 5 / 7 | 71.4% | 60.0 | pass |', summary.splitlines())


if __name__ == '__main__':
    unittest.main()
