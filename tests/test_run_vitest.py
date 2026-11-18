"""The run sub-action for vitest, run through its action.yml on fixture projects that link the
repository's own node_modules (`npm ci` installs it)."""
import json
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PureWindowsPath
from unittest import mock

from _action_harness import ROOT
from test_ratchet import cobertura, run, split
from test_run import OS, Fixtures, suite_names

LIB = b'export function f(x) {\n  if (x) {\n    return 1;\n  }\n  return 2;\n}\n'
# The repository's own include, dir, provider (not installed) and threshold would run no test
# or fail every shard; its coverage exclude stays.
CONFIG = (b"import { defineConfig } from 'vitest/config';\n"
          b"export default defineConfig({ test: { include: ['none/**'], dir: 'src',"
          b" coverage: { provider: 'istanbul', exclude: ['src/skip.ts'],"
          b' thresholds: { lines: 100 } } } });\n')
TOP = (b"import { test, expect } from 'vitest';\nimport { f } from '../src/lib';\n"
       b"import '../src/skip';\n\ntest('top', () => {\n  expect(f(1)).toBe(1);\n});\n")
# A config function is called with vite's env: mode is 'test'.
EXITS = (b"export default ({ mode }) => ({ test: { globalSetup: mode === 'test' ? './setup.mjs'"
         b" : [] } });\n")
SETUP = (b"import { writeFileSync } from 'node:fs';\n"
         b"export default () => {\n  writeFileSync('setup-ran', '');\n  process.exit(5);\n};\n")
FAILING = b'''import { test, expect } from 'vitest';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { f } from '../src/lib.js';

test('good', () => {
  expect(f(1)).toBe(1);
  const entry = { url: 'http://localhost:8000/static/web/page.js',
                  functions: [{ ranges: [{ startOffset: 0, endOffset: 22, count: 1 }] }] };
  writeFileSync(join(process.env.TEST_RATCHET_V8_DIR, 'page.json'), JSON.stringify([entry]));
});

test('bad', () => {
  expect(f(0)).toBe(1);
});
'''


INLINE = (b"export const x = 1;\nif (import.meta.vitest) {\n"
          b"  const { test } = import.meta.vitest;\n  test('inline', () => {});\n}\n")
# vitest measures an in-source file without its import.meta.vitest block.
INLINE_RUN, INLINE_UNRUN = {1: 1}, {1: 0}
# Reports a type error in tests/b.test-d.ts below its working directory, as tsc would; only
# shell builtins, since the suite's PATH holds no find.
FAILING_CHECKER = (b'#!/bin/sh\necho "$PWD/tests/b.test-d.ts(2,1): error TS2344: Type \'number\''
                   b' does not satisfy the constraint \'string\'."\nexit 2\n')
WRONG_TYPE = (b"import { test, expectTypeOf } from 'vitest';\n\ntest('t', () => {\n"
              b"  expectTypeOf(1).toEqualTypeOf<string>();\n});\n")
TYPED = b"import { test, expectTypeOf } from 'vitest';\n\ntest('typed', () => {});\n"
SPECIALS = ('t{a,b}', 't*', '(p) q', '!n', '@(m)')
# Prints the generated config's resolved `test` key.
SHOW = ("const m = await import(process.argv[1]);"
        " console.log(JSON.stringify((await m.default({ mode: 'test' })).test));")


def one(name):
    return (f"import {{ test }} from 'vitest';\n\ntest('{name}', () => {{}});\n" + '//' * 99
            + '\n').encode()


class VitestRunTest(Fixtures):
    def project(self, files, modules=True):
        repo = super().project(files)
        if modules:
            (repo / 'node_modules').symlink_to(ROOT / 'node_modules')
        return repo

    def test_each_shard_runs_exactly_its_files_with_the_actions_include_and_dir(self):
        # A path filter would also run the deep and .tsx siblings, and a[1] as a glob is a1.
        repo = self.project({'vitest.config.ts': CONFIG, 'src/lib.ts': LIB,
                             'src/skip.ts': b'export const skip = 1;\n',
                             'tests/a.test.ts': TOP + b'//' * 400 + b'\n',
                             'deep/tests/a.test.ts': one('deep') + b'//' * 200,
                             'tests/a.test.tsx': one('tsx'), 'tests/a1.test.ts': one('a1'),
                             'tests/a[1].test.ts': one('bracket')})
        tests = 'tests/*.test.ts\ntests/*.test.tsx\ndeep/tests/*.test.ts'
        node = subprocess.run(['node', '-p', 'process.versions.node'], capture_output=True,
                              text=True, check=True).stdout
        cell = OS + '-' + '.'.join(node.split('.')[:2])
        seen = []
        for shard in ('1/2', '2/2'):
            result = self.run_in(repo, runner='vitest', tests=tests, shard=shard, source='src/**')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.outputs, {'name': f'test-ratchet-{cell}-{shard[0]}_2',
                                              'passed': 'true'})
            artifact = self.artifact(result)
            manifest = json.loads((artifact / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual({key: manifest[key] for key in ('runner', 'cell', 'shard')},
                             {'runner': 'vitest', 'cell': cell, 'shard': shard})
            self.assertEqual(manifest['files'], split.shard_files(repo, tests, None, cell, shard))
            self.assertEqual(sorted({path for path, _ in suite_names(artifact / 'junit.xml')}),
                             manifest['files'])
            seen.append(manifest['files'])
            self.assertNotIn('::warning::', result.stdout)
            hits = cobertura.parse(artifact / 'coverage-javascript.xml', '/elsewhere')
            self.assertEqual(hits, {'src/lib.ts': {2: 1, 3: 1, 5: 0} if 'tests/a.test.ts'
                                    in manifest['files'] else {2: 0, 3: 0, 5: 0}})
        self.assertEqual(seen, [['tests/a.test.ts', 'tests/a1.test.ts'],
                                ['deep/tests/a.test.ts', 'tests/a.test.tsx',
                                 'tests/a[1].test.ts']])

    def test_a_failing_test_uploads_its_coverage_and_args_reach_vitest_after_the_actions(self):
        repo = self.project({'src/lib.js': LIB, 'tools/t.js': b'export const t = 1;\n',
                             'web/page.js': b'var a = 1;\nvar b = 2;\n',
                             'tests/x.test.js': FAILING})
        inputs = {'runner': 'vitest', 'tests': 'tests/*.test.js', 'cell': 'c',
                  'source': ' src/** , tools/** '}
        result = self.run_in(repo, **inputs)
        self.assertEqual((result.returncode, result.outputs['passed']), (1, 'false'))
        self.assertIn('::error::the tests failed', result.stdout)
        artifact = self.artifact(result)
        self.assertEqual(sorted(path.name for path in artifact.iterdir()),
                         ['coverage-javascript.xml', 'junit.xml', 'manifest.json'])
        # vitest's own report and the browser dump, merged into one.
        self.assertEqual(cobertura.parse(artifact / 'coverage-javascript.xml', '/elsewhere'), {
            'src/lib.js': {2: 2, 3: 1, 5: 1}, 'tools/t.js': {1: 0}, 'web/page.js': {1: 1, 2: 1}})
        failed = [case.get('name') for case in ET.parse(artifact / 'junit.xml').iter('testcase')
                  if case.find('failure') is not None]
        self.assertEqual(failed, ['bad'])
        result = self.run_in(repo, args='-t good', name='narrowed', **inputs)
        self.assertEqual((result.returncode, result.outputs['passed']), (0, 'true'))

    def test_a_missing_package_or_node_fails_naming_it(self):
        bare = self.project({'tests/x.test.js': one('x'), 'sub/x.js': b''}, modules=False)
        partial = self.project({'tests/x.test.js': one('x')}, modules=False)
        (partial / 'node_modules').mkdir()
        (partial / 'node_modules' / 'vitest').symlink_to(ROOT / 'node_modules' / 'vitest')
        # Packages resolve from the repository root, not from a directory below it.
        (bare / 'sub' / 'node_modules').symlink_to(ROOT / 'node_modules')
        for cwd, tools, code, named in ((bare, ('node', 'bash'), 1, 'package vitest'),
                                        (bare / 'sub', ('node', 'bash'), 1, 'package vitest'),
                                        (partial, ('node', 'bash'), 1,
                                         'package @vitest/coverage-v8'),
                                        (bare, ('bash',), 2, 'node')):
            with self.subTest(cwd=cwd.name, named=named):
                result = self.run_in(cwd, tools=tools, runner='vitest', tests='tests/*.js')
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(named, result.stderr)
                self.assertEqual((result.outputs, self.uploads), ({'name': '', 'passed': ''}, {}))

    def test_a_projects_config_is_refused_before_any_test_runs(self):
        ran = b"import { writeFileSync } from 'node:fs';\nwriteFileSync('ran', '');\n"
        for key, config in (('projects', 'vitest.config.mjs'), ('workspace', 'vite.config.js')):
            with self.subTest(key=key):
                repo = self.project({'tests/x.test.js': ran + one('x'), config:
                                     f'export default {{ test: {{ {key}: [{{}}] }} }};\n'
                                     .encode()})
                result = self.run_in(repo, runner='vitest', tests='tests/*.js')
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('test.projects', result.stderr)
                self.assertFalse((repo / 'ran').exists())
                self.assertEqual(self.uploads, {})

    def test_a_vitest_exit_code_is_not_read_as_pytests(self):
        repo = self.project({'tests/x.test.js': one('x'), 'vitest.config.mjs': EXITS,
                             'setup.mjs': SETUP})
        result = self.run_in(repo, runner='vitest', tests='tests/*.js')
        self.assertEqual((result.returncode, result.outputs['passed']), (1, 'false'))
        self.assertIn('::error::the tests failed', result.stdout)
        self.assertNotIn('pytest', result.stdout)
        # It died in the setup, before writing any report.
        self.assertTrue((repo / 'setup-ran').exists())
        self.assertEqual([path.name for path in self.artifact(result).iterdir()],
                         ['manifest.json'])

    def test_in_source_and_typecheck_files_outside_the_shard_stay_out(self):
        repo = self.project({'vitest.config.mjs': b"export default { test: { includeSource:"
                             b" ['src/**/*.ts'], typecheck: { enabled: true } } };\n",
                             'src/inline.ts': INLINE, 'src/shard.ts': INLINE,
                             'tests/a.test.ts': one('a'), 'tests/z.test-d.ts': TYPED})
        result = self.run_in(repo, runner='vitest', tests='tests/*.test.ts\nsrc/shard.ts',
                             source='src/**')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The shard's in-source file runs once; the other and the typecheck file not at all.
        self.assertEqual(suite_names(self.artifact(result) / 'junit.xml'),
                         [('src/shard.ts', 'inline'), ('tests/a.test.ts', 'a')])
        # As an in-source file, not a test, the shard's one is measured.
        self.assertEqual(cobertura.parse(self.artifact(result) / 'coverage-javascript.xml', '/'),
                         {'src/inline.ts': INLINE_UNRUN, 'src/shard.ts': INLINE_RUN})

    def test_a_file_both_a_test_and_a_typecheck_file_runs_as_both(self):
        repo = self.project({'vitest.config.mjs': b"export default { test: { typecheck: {"
                             b" enabled: true, checker: './checker', include: ['**/*.test.ts'] }"
                             b' } };\n', 'checker': b'#!/bin/sh\nexit 0\n',
                             'src/l.ts': b'export const l = (x: number) => x + 1;\n',
                             'tests/a.test.ts': b"import { test, expect } from 'vitest';\n"
                                                b"import { l } from '../src/l';\n\n"
                                                b"test('a', () => {\n  expect(l(1)).toBe(3);"
                                                b"\n});\n"})
        (repo / 'checker').chmod(0o755)
        result = self.run_in(repo, runner='vitest', tests='tests/*.ts', source='src/**')
        # The type check passes (the checker reports nothing); the run's failure fails the shard.
        self.assertEqual((result.returncode, result.outputs['passed']), (1, 'false'))
        artifact = self.artifact(result)
        cases = [(case.get('file'), case.find('failure') is not None)
                 for case in ET.parse(artifact / 'junit.xml').iter('testcase')]
        self.assertEqual(sorted(cases), [('tests/a.test.ts', False), ('tests/a.test.ts', True)])
        self.assertEqual(cobertura.parse(artifact / 'coverage-javascript.xml', '/'),
                         {'src/l.ts': {1: 1}})

    def test_a_type_error_fails_the_shard_under_a_root_or_typecheck_from_args(self):
        # A pattern relative to the config's root, and typecheck turned on by args alone.
        for config, typecheck, test, args in (
                (b"export default { root: 'pkg', test: { typecheck: { enabled: true, checker:"
                 b" '../checker', include: ['tests/**/*.test-d.ts'] } } };\n",
                 'pkg/tests/b.test-d.ts', 'pkg/tests/*.ts', ''),
                (b"export default { test: { typecheck: { checker: './checker' } } };\n",
                 'tests/b.test-d.ts', 'tests/*.ts', '--typecheck')):
            with self.subTest(args=args):
                repo = self.project({'vitest.config.mjs': config, 'checker': FAILING_CHECKER,
                                     typecheck: WRONG_TYPE})
                (repo / 'checker').chmod(0o755)
                result = self.run_in(repo, runner='vitest', tests=test, args=args, name=args)
                self.assertEqual((result.returncode, result.outputs['passed']), (1, 'false'),
                                 result.stdout + result.stderr)
                cases = [(case.get('file'), case.find('failure') is not None)
                         for case in ET.parse(self.artifact(result) / 'junit.xml')
                         .iter('testcase')]
                self.assertEqual({path for path, _ in cases}, {typecheck})
                self.assertIn((typecheck, True), cases)

    def test_a_backslash_in_a_shard_path_selects_no_other_file(self):
        repo = self.project({'tests/a\\(b.test.ts': one('backslash'),
                             'tests/a(b.test.ts': one('plain')})
        result = self.run_in(repo, runner='vitest', tests='tests/a?(b.test.ts')
        artifact = self.artifact(result)
        self.assertEqual(json.loads((artifact / 'manifest.json').read_text(encoding='utf-8'))
                         ['files'], ['tests/a\\(b.test.ts'])
        # vite cannot load a backslash name (it reads it as a separator), but nothing else ran.
        classnames = [case.get('classname') for case in
                      ET.parse(artifact / 'junit.xml').iter('testcase')]
        self.assertTrue(classnames)
        self.assertNotIn('tests/a(b.test.ts', classnames)

    def test_glob_characters_in_a_shard_path_match_only_that_file(self):
        names = [f'tests/{name}.test.ts' for name in SPECIALS + ('ta', 'tx', 'p q', 'n', 'm')]
        repo = self.project({**{name: one(name) for name in names}, 'vitest.config.mjs':
                             b"export default { test: { coverage: { provider: 'istanbul' } } };\n"})
        result = self.run_in(repo, runner='vitest', tests='tests/*[{*(!@]*.test.ts')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ran = suite_names(self.artifact(result) / 'junit.xml')
        self.assertEqual(sorted({path for path, _ in ran}),
                         sorted(f'tests/{name}.test.ts' for name in SPECIALS))

    def test_args_vitest_refuses_or_that_leave_no_file_are_named(self):
        repo = self.project({'tests/x.test.js': one('x')})
        result = self.run_in(repo, runner='vitest', tests='tests/*.js', args='--dir=tests')
        self.assertEqual((result.returncode, result.outputs['passed']), (1, 'false'))
        self.assertIn('::error::vitest exited 1 before running: check input args', result.stdout)
        self.assertEqual([path.name for path in self.artifact(result).iterdir()],
                         ['manifest.json'])
        result = self.run_in(repo, runner='vitest', tests='tests/*.js', args='--exclude=tests/**',
                             name='excluded')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("::warning::input args '--exclude=tests/**' left none of the shard's files"
                      ' to run', result.stdout)
        self.assertIn('::error::the tests failed', result.stdout)
        # With no args, a shard the repository's own exclude empties is not blamed on args.
        repo = self.project({'tests/x.test.js': one('x'), 'vitest.config.mjs':
                             b"export default { test: { exclude: ['**/x.*'] } };\n"})
        result = self.run_in(repo, runner='vitest', tests='tests/*.js', name='own')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('<testsuites', (self.artifact(result) / 'junit.xml').read_text(
            encoding='utf-8'))
        self.assertNotIn('::warning::', result.stdout)

    def test_unexpected_output_from_node_fails_in_one_line(self):
        said = 'x' * 397 + 'end'
        repo = self.project({'tests/x.test.js': one('x'), 'say.cjs': b'console.log("hello");\n',
                             'warn.cjs': f'process.stderr.write("{said}\\n");\n'
                                         'console.log("hello");\n'.encode()})
        prefix = f'test-ratchet run: node exited 0 without naming the vitest packages of {repo}: '
        # stdout when stderr is empty, else stderr's last 300 characters.
        for preload, tail in (('say.cjs', "'hello"), ('warn.cjs', repr(said[-300:]))):
            with self.subTest(preload=preload), mock.patch.dict(
                    os.environ, {'NODE_OPTIONS': f'--require {repo / preload}'}):
                result = self.run_in(repo, runner='vitest', tests='tests/*.js')
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                [line] = result.stderr.splitlines()
                self.assertTrue(line.startswith(prefix + tail), line)
                self.assertEqual(line == prefix + tail, preload == 'warn.cjs', line)


class VitestConfigTest(Fixtures):
    """The generated config, loaded by node: which shard file runs how."""

    def resolved(self, base, files, windows=False):
        root = Path(tempfile.mkdtemp(dir=self.scratch)).resolve()
        for name in list(files) + ['tests/z.check.ts']:
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(files.get(name, b''))
        (root / 'base.mjs').write_text(base, encoding='utf-8')
        config = root / 'wrapper.mjs'
        config.write_text(run.vitest_config(root, root / 'base.mjs',
                                            ROOT / 'node_modules' / 'vitest' / 'package.json',
                                            list(files), root / 'refused', windows),
                          encoding='utf-8')
        shown = subprocess.run(['node', '--input-type=module', '-e', SHOW, config.as_uri()],
                               capture_output=True, text=True, check=True).stdout
        return json.loads(shown)

    def test_each_shard_file_takes_every_mode_the_repositorys_own_globs_give_it(self):
        files = {'tests/a.test.ts': b'', 'tests/b.check.ts': b'', 'tests/c[1].test.ts': b'',
                 '.d/s.ts': INLINE, 'src/e.ts': b'export const e = import.meta.env;\n'}
        a, b, c, s, e = ('tests/a.test.ts', 'tests/b.check.ts', 'tests/c\\[1\\].test.ts',
                         '.d/s.ts', 'src/e.ts')
        # Per base: (include, includeSource, typecheck include); a file no glob holds is a test,
        # and typecheck files are listed whether or not it is on. Globs are vitest's: from dir,
        # else root, dot files, no directory expanded, in-source files holding import.meta.vitest.
        for base, expected in (
                ("{ test: { typecheck: { include: ['**/*.check.ts'] }, includeSource: ['**/*.ts'],"
                 " coverage: { thresholds: { lines: 100 } } } }", ([a, c, e], [s], [b])),
                ("() => ({ root: 'nowhere', test: { dir: 'tests', typecheck: { enabled: true,"
                 " include: ['*.check.ts'] } } })", ([a, c, s, e], [], [b])),
                ("{ root: 'tests', test: { typecheck: { include: ['*.check.ts'] } } }",
                 ([a, c, s, e], [], [b])),
                ("{ test: { root: 'tests', typecheck: { include: ['*.check.ts'] } } }",
                 ([a, c, s, e], [], [b])),
                ("{ test: { typecheck: { include: ['**/*.check.ts'], exclude: ['**/b.*'] } } }",
                 ([a, b, c, s, e], [], [])),
                ("{ test: { typecheck: { include: ['tests'] } } }", ([a, b, c, s, e], [], [])),
                ("{ test: { typecheck: { include: ['**/*.test.ts'] } } }",
                 ([a, b, c, s, e], [], [a, c])),
                ("{ test: { include: ['**/*.ts'], exclude: ['**/a.*'], typecheck: {"
                 " include: ['**/a.*', '**/*.check.ts'] } } }", ([b, c, s, e], [], [a, b]))):
            with self.subTest(base=base):
                test = self.resolved(f'export default {base};\n', files)
                self.assertEqual((test['include'], test['includeSource'],
                                  test['typecheck']['include']), expected)
                self.assertEqual(test['coverage'].get('thresholds'), {})

    def test_a_path_is_escaped_by_tinyglobby_on_windows_and_fully_elsewhere(self):
        files = {'tests/a+b\\c|d.test.ts': b''}
        # tinyglobby (run here with its POSIX rules) leaves the + alone and escapes the
        # backslash and the |; the full escape also takes the +.
        for windows, expected in ((False, ['tests/a\\+b\\\\c\\|d.test.ts']),
                                  (True, ['tests/a+b\\\\c\\|d.test.ts'])):
            with self.subTest(windows=windows):
                test = self.resolved('export default {};\n', files, windows=windows)
                self.assertEqual(test['include'], expected)

    def test_a_windows_root_is_imported_by_url_and_globbed_with_slashes(self):
        text = run.vitest_config(PureWindowsPath('C:/repo dir'),
                                 PureWindowsPath('C:/repo dir/vitest.config.ts'),
                                 PureWindowsPath('C:/repo dir/node_modules/vitest/package.json'),
                                 ['tests/a.test.ts'], PureWindowsPath('C:/temp/refused'),
                                 windows=True)
        lines = text.splitlines()
        self.assertEqual(lines[0], 'import base from "file:///C:/repo%20dir/vitest.config.ts";')
        for line in ('const vitest = createRequire('
                     '"file:///C:/repo%20dir/node_modules/vitest/package.json");',
                     'const root = "C:/repo dir";', 'const files = ["tests/a.test.ts"];',
                     '    writeFileSync("C:\\\\temp\\\\refused", \'\');',
                     '  const escape = escapePath;'):
            self.assertIn(line, lines)
