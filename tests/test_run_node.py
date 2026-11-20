"""The run sub-action for node:test, run through its action.yml on fixture projects."""
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from unittest import mock

from test_ratchet import cobertura, collect, junit
from test_run import OS, Fixtures, suite_names
from test_run_vitest import LIB

NESTED = (b"import { describe, test } from 'node:test';\nimport { f } from '../src/lib.mjs';\n"
          b"describe('o', () => describe('i', () => test('deep', () => f(1))));\n"
          b"test('outer', (t) => t.test('mid', (t) => t.test('leaf', () => new Promise((done) =>"
          b" setTimeout(done, 500)))));\n")
GOOD = (b"import { describe, test } from 'node:test';\ntest('ok', () => {});\n"
        b"test('todo', { todo: true }, () => { throw new Error('no'); });\n"
        b"test('skip', { skip: true }, () => {});\ndescribe('none', () => {});\n")
# A node:test that never emits test:summary, as before Node 22.10.
NO_SUMMARY = (b"const { EventEmitter } = require('node:events');\n"
              b"const { emit } = EventEmitter.prototype;\n"
              b"EventEmitter.prototype.emit = function (name, ...rest) {\n"
              b"  return name !== 'test:summary' && emit.call(this, name, ...rest);\n};\n")
ODD = '<&"\t\n\r\x01>'  # every character XML escapes, and one it cannot hold


def one(name):
    return f"import {{ test }} from 'node:test';\ntest({json.dumps(name)}, () => {{}});\n".encode()


def cases(report):
    return [(case.get('file'), case.get('name', '').rsplit('/', 1)[-1], [part.tag for part in case])
            for case in ET.parse(report).iter('testcase')]


class NodeRunTest(Fixtures):
    def run_node(self, repo, **inputs):
        return self.run_in(repo, **{'runner': 'node', 'tests': 'tests/*.mjs', **inputs})

    def test_a_shard_runs_exactly_its_files_each_case_named_by_its_file_and_measured(self):
        # node --test would read each path as a glob: [x] as x, t* as ta too, {a,b} as a and b.
        repo = self.project({'src/lib.mjs': LIB, 'tests/[x].test.mjs': NESTED,
                             'tests/{a,b}.test.mjs': one(ODD),
                             **{f'tests/{name}.test.mjs': one(name)
                                for name in ('t*', 'p q', 'x', 'ta', 'a', 'b')}})
        node = subprocess.run(['node', '-p', 'process.versions.node'], capture_output=True,
                              text=True, check=True).stdout
        cell = OS + '-' + '.'.join(node.split('.')[:2])
        result = self.run_node(repo, tests='tests/*[[{* ]*.test.mjs')
        self.assertEqual(result.outputs, {'name': f'test-ratchet-{cell}-all', 'passed': 'true'},
                         result.stdout + result.stderr)
        artifact = self.artifact(result)
        manifest = json.loads((artifact / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual((manifest['runner'], manifest['cell']), ('node', cell))
        self.assertEqual([found.runner for found in collect.read(artifact.parent)], ['node'])
        self.assertEqual(sorted(path.name for path in artifact.iterdir()),
                         ['coverage-javascript.xml', 'junit.xml', 'manifest.json'])
        # Each case's own seconds: the leaf's half second, not its parents' too.
        seconds = junit.seconds_per_file(artifact / 'junit.xml')['tests/[x].test.mjs']
        self.assertTrue(0.49 <= seconds < 1, seconds)
        self.assertEqual(suite_names(artifact / 'junit.xml'), [
            ('tests/[x].test.mjs', 'deep'), ('tests/[x].test.mjs', 'leaf'),
            ('tests/p q.test.mjs', 'p q'), ('tests/t*.test.mjs', 't*'),
            ('tests/{a,b}.test.mjs', '<&"\t\n\r>')])
        hits = cobertura.parse(artifact / 'coverage-javascript.xml', '/elsewhere')
        self.assertEqual((hits['src/lib.mjs'], 'tests/p q.test.mjs' in hits),
                         ({1: 1, 2: 1, 3: 1, 4: 1, 5: 0, 6: 1}, True))
        result = self.run_node(repo, tests='tests/*[[{* ]*.test.mjs', javascript='src/lib.*',
                               name='kept')
        self.assertEqual(cobertura.parse(self.artifact(result) / 'coverage-javascript.xml', '/'),
                         {'src/lib.mjs': hits['src/lib.mjs']})

    def test_a_failing_test_or_file_fails_the_shard_and_a_failing_todo_does_not(self):
        # z, failing, sorts after good: the shard's status is the whole run's, not a file's.
        repo = self.project({'tests/good.test.mjs': GOOD, 'tests/boom.test.mjs': b'throw 1;\n',
                             'tests/bust.test.mjs': b"import { describe } from 'node:test';\n"
                                                    b"describe('d', () => { throw 1; });\n",
                             'tests/z.test.mjs': b"import { test } from 'node:test';\n"
                                                 b"test('bad', () => { throw new Error(); });\n"})
        good = [('good', 'ok', []), ('good', 'todo', ['skipped']), ('good', 'skip', ['skipped'])]
        for name, expected in (('good', good), ('[gz]*', good + [('z', 'bad', ['failure'])]),
                               ('b*', [('boom', 'boom.test.mjs', ['failure']),
                                       ('bust', 'd', ['failure'])])):
            with self.subTest(name=name):
                result = self.run_node(repo, tests=f'tests/{name}.test.mjs', name=name)
                failed = name != 'good'
                self.assertEqual((result.returncode, result.outputs['passed']),
                                 (int(failed), str(not failed).lower()), result.stdout)
                self.assertEqual('::error::the tests failed' in result.stdout, failed)
                artifact = self.artifact(result)
                self.assertEqual(sorted(path.name for path in artifact.iterdir()),
                                 ['coverage-javascript.xml', 'junit.xml', 'manifest.json'])
                self.assertEqual(cases(artifact / 'junit.xml'),
                                 [(f'tests/{file}.test.mjs', *case) for file, *case in expected])

    def test_what_runner_node_cannot_take_or_find_fails_before_any_test_runs(self):
        repo = self.project({'tests/x.test.mjs': one('x')})
        for inputs, line in (({'runner': 'nose'}, 'runner must be pytest, vitest or node, got '),
                             ({'source': 'src/**'}, 'source is not taken by runner node: input'
                                                    ' javascript narrows its coverage report'),
                             ({'args': '--x'}, 'args is not taken by runner node: it takes no'
                                               " options and runs exactly the shard's files"),
                             ({'tools': ('bash',)}, 'runner node needs node on PATH: ')):
            with self.subTest(inputs=inputs):
                result = self.run_node(repo, **inputs)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertTrue(result.stderr.splitlines()[-1].startswith(
                    'test-ratchet run: input ' + line), result.stderr)
                self.assertEqual((result.outputs, self.uploads), ({'name': '', 'passed': ''}, {}))

    def test_a_driver_that_dies_before_reporting_fails_the_shard_whatever_its_status(self):
        exit_ = b"if (process.argv.at(-2)?.endsWith('files.json')) process.exit(0);\n"
        repo = self.project({'tests/x.test.mjs': one('x'), 'exit.cjs': exit_,
                             'say.cjs': b'console.log("hello");\n', 'quiet.cjs': NO_SUMMARY})
        with mock.patch.dict(os.environ, {'NODE_OPTIONS': f'--require {repo / "exit.cjs"}'}):
            result = self.run_node(repo)
        self.assertEqual((result.returncode, result.outputs['passed']), (1, 'false'))
        self.assertIn("::error::node exited 0 before running the shard's tests", result.stdout)
        self.assertNotIn('junit.xml', [path.name for path in self.artifact(result).iterdir()])
        # A passing shard with no summary to say so fails.
        with mock.patch.dict(os.environ, {'NODE_OPTIONS': f'--require {repo / "quiet.cjs"}'}):
            result = self.run_node(repo, name='quiet')
        self.assertEqual((result.returncode, result.outputs['passed']), (1, 'false'))
        # A node whose answer is not the action's names no vitest package to a node shard.
        with mock.patch.dict(os.environ, {'NODE_OPTIONS': f'--require {repo / "say.cjs"}'}):
            result = self.run_node(repo, name='said')
        self.assertTrue(result.stderr.splitlines()[-1].startswith(
            "test-ratchet run: node exited 0 without naming its version: 'hello"), result.stderr)
