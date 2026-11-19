"""The report driver run as a step: a repository, its artifacts, a GitHub double and an
event payload."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _action_harness import ROOT
from _github_double import GitHub, answer
from test_collect import artifact
from test_ratchet import comment
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


class ReportCase(unittest.TestCase):
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

    def thread(self, number=7, refuse=None, base=None, head=HEAD):
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
        self.api.routes.update({**self.pull(number, head, base), **routes})

    def report(self, name, event, environ=None, **inputs):
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
        env.update({f'TEST_RATCHET_{key.upper()}': value for key, value in inputs.items()},
                   **(environ or {}))
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
