"""Release admission from GitHub evidence: no live read, no live write."""
import contextlib
from functools import partial
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPO = 'owner/repo'
SHA = 'a' * 40


def _load():
    path = ROOT / 'scripts' / 'ci' / 'release_dispatch.py'
    assert path.is_file(), 'release dispatch gate is missing'
    spec = importlib.util.spec_from_file_location('release_dispatch_under_test', path)
    assert spec and spec.loader, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class API:
    """Replay only named GitHub endpoints; unexpected reads fail the test."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, argv):
        assert argv[:2] == ['gh', 'api'] and 'Cache-Control: no-cache' in argv, argv
        assert argv[2] in self.responses, argv
        self.calls.append(argv)
        value = self.responses[argv[2]]
        return json.dumps(value() if callable(value) else value)


class Clock:
    """Replace only the elapsed-time boundary, never the admission logic."""

    def __init__(self):
        self.elapsed = 0
        self.sleeps = []

    def now(self):
        return self.elapsed

    def sleep(self, seconds):
        assert seconds > 0
        self.sleeps.append(seconds)
        self.elapsed += seconds


class Writer:
    """Record GitHub write argv and return the annotated object's distinct SHA."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return json.dumps({'sha': 'c' * 40})


def _rules(contexts=('required-one', 'required-two')):
    return {'id': 7, 'name': 'require-ci-aggregate', 'enforcement': 'active',
            'rules': [{'type': 'required_status_checks', 'parameters': {
                'required_status_checks': [{'context': name} for name in contexts]}}]}


def _run(suite=1, event='push', actor='human', sha=SHA):
    return {'check_suite_id': suite, 'event': event, 'actor': {'login': actor},
            'head_sha': sha}


def _check(name='gitleaks', suite=1, conclusion: str | None = 'success',
           status: str = 'completed', sha=SHA):
    return {'name': name, 'check_suite': {'id': suite}, 'conclusion': conclusion,
            'status': status, 'head_sha': sha}


def _gate_api(runs, checks):
    return API({f'repos/{REPO}/actions/runs?head_sha={SHA}': runs,
                f'repos/{REPO}/commits/{SHA}/check-runs?filter=all': checks})


class ReleaseDispatchTest(unittest.TestCase):
    def setUp(self):
        self.module = _load()

    def _refuses(self, operation, message):
        with self.assertRaises(self.module.ReleaseError) as caught:
            operation()
        self.assertIn(message, str(caught.exception))

    def _wait(self, api, *, required=None, timeout=60, grace=40, clock=None):
        clock = clock or Clock()
        with patch.object(self.module.time, 'monotonic', clock.now), \
                patch.object(self.module.time, 'sleep', clock.sleep):
            self.module.wait(api, REPO, SHA, required or {'gitleaks'},
                             timeout=timeout, grace=grace, interval=20)
        return clock

    def _verify_api(self, sha, contexts=('gitleaks',)):
        api = _gate_api([{'workflow_runs': [_run(sha=sha)]}],
                        [{'check_runs': [_check(sha=sha)]}])
        api.responses = {key.replace(SHA, sha): value for key, value in api.responses.items()}
        api.responses.update({f'repos/{REPO}/branches/main': {'commit': {'sha': sha}},
                              f'repos/{REPO}/tags': [[{'name': 'v2.10.0'}]],
                              f'repos/{REPO}/rulesets': [[_rules()]],
                              f'repos/{REPO}/rulesets/7': _rules(contexts)})
        return api

    def _clocked_verify(self, contexts):
        printed, clock = io.StringIO(), Clock()
        with patch.object(self.module, 'time',
                          SimpleNamespace(monotonic=clock.now, sleep=clock.sleep)), \
                contextlib.redirect_stdout(printed):
            self.module.verify(self._verify_api(self._head(), contexts), REPO, '2.10.1')
        return printed.getvalue()

    def _head(self):
        return subprocess.run(['git', 'rev-parse', 'HEAD'], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_version_requires_canonical_semver_and_a_later_successor(self):
        self.assertEqual(self.module.version_tuple('2.10.0'), (2, 10, 0))
        for version in ('v2.1.1', '2.1', '02.1.1', '', '2.1.1; echo bad', '2.1\uff10.1'):
            self._refuses(partial(self.module.version_tuple, version), 'version')
        pages = [[{'name': 'v2.9.0'}, {'name': 'v99.0.0-rc.1'}],
                 [{'name': 'v2.10.3'}, {'name': 'v2.1.0'}]]
        for version in ('2.10.4', '2.11.0', '3.0.0'):
            self.module.validate_version(API({f'repos/{REPO}/tags': pages}), REPO, version)
        for version in ('2.10.3', '2.9.1', '2.10.5', '2.11.3', '2.12.0', '4.0.0'):
            self._refuses(partial(self.module.validate_version,
                                  API({f'repos/{REPO}/tags': pages}), REPO, version),
                          'successor')
        self._refuses(partial(self.module.validate_version,
                              API({f'repos/{REPO}/tags': [[{'name': 'v1.0.0-preview'}], []]}),
                              REPO, '1.0.0'), 'no vX.Y.Z')
        api = API({})
        self._refuses(partial(self.module.validate_version, api, REPO, 'v1.0.0'), 'version')
        self.assertFalse(api.calls)

    def test_required_contexts_come_from_the_active_named_ruleset(self):
        api = API({f'repos/{REPO}/rulesets': [[{'id': 4, 'name': 'unrelated'}], [_rules()]],
                   f'repos/{REPO}/rulesets/7': _rules(('a-new-context',))})
        self.assertEqual(self.module.required_contexts(api, REPO), {'a-new-context'})
        self.assertIn('--paginate', api.calls[0])
        rows = (([], _rules()), ([_rules()], _rules(())),
                ([_rules()], {**_rules(), 'enforcement': 'disabled'}),
                ([{**_rules(), 'enforcement': 'evaluate'}], _rules()),
                ([_rules(), _rules()], _rules()), ([_rules()], {**_rules(), 'rules': []}))
        for listed, detail in rows:
            self._refuses(partial(self.module.required_contexts, API(
                {f'repos/{REPO}/rulesets': [listed], f'repos/{REPO}/rulesets/7': detail}),
                REPO), 'require-ci-aggregate')

    def test_only_push_checks_of_the_required_contexts_satisfy(self):
        api = _gate_api([{'workflow_runs': [_run()]}], [{'check_runs': [
            _check(), _check(name='informational', conclusion='failure')]}])
        self.assertEqual(self._wait(api).sleeps, [])
        for run, check in ((None, _check()), (_run(event='schedule'), _check()),
                           (_run(actor='dependabot[bot]'), _check()),
                           (_run(sha='b' * 40), _check()), (_run(), _check(sha='b' * 40))):
            clock = Clock()
            self._refuses(partial(self._wait,
                                  _gate_api([{'workflow_runs': [run] if run else []}],
                                            [{'check_runs': [check]}]), clock=clock),
                          'registration grace')
            self.assertEqual((clock.elapsed, clock.sleeps), (40, [20, 20]))
        api = _gate_api([{'workflow_runs': [_run()]}], [{'check_runs': [_check()]}])
        self._refuses(partial(self._wait, api, required={'gitleaks', 'new-context'}),
                      'new-context')

    def test_completed_non_success_refuses_and_pending_waits_bounded(self):
        for conclusion in ('failure', 'cancelled', 'skipped', None):
            api = _gate_api([{'workflow_runs': [_run()]}], [{'check_runs': [
                _check(), _check(conclusion=conclusion)]}])
            self._refuses(partial(self._wait, api), f'gitleaks: {conclusion}')
        clock = Clock()
        api = _gate_api([{'workflow_runs': [_run()]}], lambda: [{'check_runs': [
            _check(conclusion=None, status='in_progress') if clock.elapsed < 40 else _check()]}])
        self.assertEqual(self._wait(api, clock=clock).sleeps, [20, 20])
        clock = Clock()
        api = _gate_api([{'workflow_runs': [_run()]}], [{'check_runs': [
            _check(status='queued', conclusion=None)]}])
        self._refuses(partial(self._wait, api, timeout=65, grace=35, clock=clock), 'deadline')
        self.assertEqual(clock.elapsed, 65)

    def test_api_errors_and_malformed_json_refuse(self):
        def broken(_argv):
            raise OSError('network read refused')

        self._refuses(partial(self._wait, broken), 'cannot read repos/owner/repo/actions/runs')
        self._refuses(partial(self._wait, lambda _argv: 'not JSON'), 'cannot read')

    def test_verify_pins_main_and_skips_only_the_pull_request_gate(self):
        sha = self._head()
        api = self._verify_api(sha)
        self.assertEqual(self.module.verify(api, REPO, '2.10.1'), sha)
        self.assertEqual(api.calls[0][2], f'repos/{REPO}/branches/main')
        self.assertEqual(self.module.PULL_REQUEST_ONLY, {'gate'})
        self.assertIn('skipping pull-request-only required context: gate',
                      self._clocked_verify(('gitleaks', 'gate')))
        self._refuses(lambda: self.module.verify(
            API({f'repos/{REPO}/branches/main': {'commit': {'sha': 'f' * 40}}}),
            REPO, '2.10.1'), 'main moved')
        self._refuses(partial(self._clocked_verify, ('gate',)), 'no push-checked contexts')
        self._refuses(partial(self.module.verify, self._verify_api(sha), REPO, '9.0.0'),
                      'successor')
        red = self._verify_api(sha)
        red.responses[f'repos/{REPO}/commits/{sha}/check-runs?filter=all'] = [
            {'check_runs': [_check(sha=sha, conclusion='failure')]}]
        self._refuses(partial(self.module.verify, red, REPO, '2.10.1'), 'gitleaks: failure')

    def test_publish_creates_annotated_object_then_ref_then_generated_release(self):
        write = Writer()
        self.module.publish(API({f'repos/{REPO}/tags': [[{'name': 'v2.10.0'}]]}),
                            write, REPO, '2.10.1', SHA)
        self.assertEqual(write.calls, [
            ['gh', 'api', f'repos/{REPO}/git/tags', '-X', 'POST', '-f', 'tag=v2.10.1',
             '-f', 'message=Release v2.10.1', '-f', f'object={SHA}', '-f', 'type=commit'],
            ['gh', 'api', f'repos/{REPO}/git/refs', '-X', 'POST',
             '-f', 'ref=refs/tags/v2.10.1', '-f', 'sha=' + 'c' * 40],
            ['gh', 'api', f'repos/{REPO}/releases', '-X', 'POST', '-f', 'tag_name=v2.10.1',
             '-F', 'generate_release_notes=true']])

    def test_publish_refuses_bad_version_sha_and_existing_tag_before_any_write(self):
        cases = (('v2.10.1', SHA, [], 'version'), ('2.10.1', 'not-a-sha', [], 'SHA'),
                 ('2.10.1', SHA, [{'name': 'v2.10.1'}], 'already exists'))
        for version, sha, tags, message in cases:
            write = Writer()
            api = API({f'repos/{REPO}/tags': [[], tags]})
            self._refuses(partial(self.module.publish, api, write, REPO, version, sha), message)
            self.assertFalse(write.calls)

    def test_publish_write_errors_name_the_attempt_and_stop_later_writes(self):
        for fail_at, attempt in ((1, 'annotated tag v2.10.1'), (2, 'tag ref v2.10.1'),
                                 (3, 'release v2.10.1')):
            write = Writer()

            def broken(argv, write=write, fail_at=fail_at):
                result = write(argv)
                if len(write.calls) == fail_at:
                    raise subprocess.CalledProcessError(1, argv, stderr='HTTP 422')
                return result

            self._refuses(partial(self.module.publish, API({f'repos/{REPO}/tags': [[]]}),
                                  broken, REPO, '2.10.1', SHA), attempt)
            self.assertEqual(len(write.calls), fail_at)

    def test_cli_writes_pinned_output_only_after_verify_succeeds(self):
        sha = self._head()
        output = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'output'
        write = Writer()
        with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}):
            self.assertEqual(self.module.main(['verify', REPO, '2.10.1'],
                                              read=self._verify_api(sha), write=write), 0)
        self.assertEqual(output.read_text(encoding='utf-8'), f'sha={sha}\n')
        self.assertFalse(write.calls)
        output.unlink()
        errors = io.StringIO()
        api = API({f'repos/{REPO}/branches/main': {'commit': {'sha': 'f' * 40}}})
        with patch.dict(os.environ, {'GITHUB_OUTPUT': str(output)}), \
                contextlib.redirect_stderr(errors):
            self.assertEqual(self.module.main(['verify', REPO, '2.10.1'], read=api,
                                              write=write), 1)
        self.assertFalse(output.exists() or write.calls)
        self.assertIn('main moved', errors.getvalue())

    def test_cli_publish_and_the_command_seams(self):
        write = Writer()
        self.assertEqual(self.module.main(['publish', REPO, '2.10.1', SHA],
                                          read=API({f'repos/{REPO}/tags': [[]]}),
                                          write=write), 0)
        self.assertEqual(len(write.calls), 3)
        for name in ('_read', '_write'):
            command = getattr(self.module, name)
            self.assertEqual(command([sys.executable, '-c', 'print("response")']), 'response\n')
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                command([sys.executable, '-c', 'raise SystemExit(7)'])
            self.assertEqual(caught.exception.returncode, 7)


if __name__ == '__main__':
    unittest.main()
