"""V8 coverage dumps (Node's NODE_V8_COVERAGE and browser dumps) as repository line hits."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_ratchet import ConfigError, v8
from test_split import make_repo

APP_JS = '''const label = "h\u00e9llo \U0001F600";
function pick(flag) {
  function inner() {
    return 0;
  }
  if (flag) {
    return "yes";
  }
  // a comment
  return "no";
}

function never() {
  return 1;
}
require("dep");
console.log(label, pick(false));
'''
# Line 3's indentation lies in the called `pick` and its first character in the uncalled
# `inner`. The astral character on line 1 is two UTF-16 units, so counting code points instead
# puts line 8's `}` one unit past the untaken block's end; line 12 is blank.
APP_LINES = {1: 1, 2: 1, 3: 0, 4: 0, 5: 0, 6: 1, 7: 0, 8: 0, 9: 1, 10: 1, 11: 1,
             13: 0, 14: 0, 15: 0, 16: 1, 17: 1}
# Node keeps the byte order mark in the script source, so `never` starts at unit 14.
BOM_JS = '\ufeffconst x = 1;\nfunction never() {\n  return x;\n}\n'
# Node strips the byte order mark of an ES module, so its offsets start after it; counted with
# the mark, line 3's `}` would fall past the uncalled function's end.
BOM_MJS = '\ufefffunction never() {\n  return 1;\n}\nconst y = 2;\n'
ESM_JS = ('import { readFileSync } from "node:fs";\nexport function f(a) {\n'
          '  return a ? 1 : 2;\n}\nf(0);\n')

PAGE_JS = 'const s = "\U0001F600\U0001F600";\nfunction a() {\n  return 1;\n}\nconst t = 2;\n'
PAGE_LINES = {1: 1, 2: 0, 3: 0, 4: 0, 5: 1}
HTML = '<!doctype html>\n<p>hi</p>\n<script>\nlet n = 0;\nif (n) {\n  n++;\n}\n</script>\n'
INLINE = '\nlet n = 0;\nif (n) {\n  n++;\n}\n'


def units(text, fragment):
    """The UTF-16 offset of `fragment` in `text`, as V8 counts it."""
    return len(text[:text.index(fragment)].encode('utf-16-le')) // 2


def size(text):
    return len(text.encode('utf-16-le')) // 2


def page_functions(called=0):
    return [{'functionName': '', 'isBlockCoverage': True,
             'ranges': [{'startOffset': 0, 'endOffset': size(PAGE_JS), 'count': 1}]},
            {'functionName': 'a', 'isBlockCoverage': False,
             'ranges': [{'startOffset': units(PAGE_JS, 'function a'),
                         'endOffset': units(PAGE_JS, 'const t'), 'count': called}]}]


def inline_functions():
    return [{'functionName': '', 'isBlockCoverage': True, 'ranges': [
        {'startOffset': 0, 'endOffset': size(INLINE), 'count': 1},
        {'startOffset': units(INLINE, '{'), 'endOffset': size(INLINE) - 1, 'count': 0}]}]


class ConvertTest(unittest.TestCase):
    FILES = {'web/static/js/app.js': PAGE_JS.encode(), 'docs/js/app.js': PAGE_JS.encode(),
             'js/app.js': PAGE_JS.encode(),
             'web/index.html': HTML.encode(), 'a/x/lib.js': b'1;\n', 'b/x/lib.js': b'1;\n',
             'y.js': b'1;\n', 'twice.js': b'1;\n1;\n', 'bin.js': b'\xff\xfe;\n',
             'web/sp ace.js': PAGE_JS.encode(), 'bom.js': BOM_MJS.encode()}

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        Path(scratch.name, 'repo').mkdir()
        self.repo = make_repo(Path(scratch.name, 'repo'), self.FILES)
        self.dumps = Path(scratch.name, 'dumps')
        self.dumps.mkdir()
        self.out = io.StringIO()

    def convert(self, *documents):
        paths = []
        for index, document in enumerate(documents):
            path = self.dumps / f'dump-{index}.json'
            text = document if isinstance(document, (str, bytes)) else json.dumps(document)
            path.write_bytes(text.encode() if isinstance(text, str) else text)
            paths.append(path)
        with contextlib.redirect_stdout(self.out):
            return v8.convert(paths, self.repo)

    def file_url(self, name):
        return Path(self.repo, name).resolve().as_uri()

    def test_a_playwright_list_maps_an_http_url_by_its_longest_shared_suffix(self):
        # web/static/js/app.js shares three trailing segments; js/app.js only two.
        entry = {'url': 'http://localhost:8000/static/js/app.js', 'scriptId': '7',
                 'source': PAGE_JS, 'functions': page_functions()}
        self.assertEqual(self.convert([entry]), {'web/static/js/app.js': PAGE_LINES})
        self.assertEqual(self.out.getvalue(), '')

    def test_a_tie_goes_to_the_tracked_path_that_is_the_whole_shared_suffix(self):
        for url, expected in (('https://h.test/js/app.js', 'js/app.js'),
                              ('https://h.test/site/docs/js/app.js', 'docs/js/app.js')):
            with self.subTest(url=url):
                entry = {'url': url, 'functions': page_functions()}
                self.assertEqual(self.convert([entry]), {expected: PAGE_LINES})
        self.assertEqual(self.out.getvalue(), '')

    def test_nesting_is_by_extent_and_a_later_equal_range_overrides(self):
        start, end = units(PAGE_JS, 'function a'), units(PAGE_JS, 'const t')
        # Listed inner first: only ordering equal starts by extent puts the outer one below.
        functions = [*page_functions(),
                     {'ranges': [{'startOffset': start, 'endOffset': end, 'count': 7}]},
                     {'ranges': [{'startOffset': start, 'endOffset': size(PAGE_JS), 'count': 5}]}]
        entry = {'url': 'http://h/static/js/app.js', 'functions': functions}
        self.assertEqual(self.convert([entry]),
                         {'web/static/js/app.js': {1: 1, 2: 7, 3: 7, 4: 7, 5: 5}})

    def test_a_null_source_reads_the_file(self):
        entry = {'url': 'http://h/static/js/app.js', 'source': None, 'functions': page_functions()}
        self.assertEqual(self.convert([entry]), {'web/static/js/app.js': PAGE_LINES})

    def test_a_cdp_result_without_source_reads_the_file(self):
        entry = {'scriptId': '3', 'url': 'https://h.test/static/js/app.js?v=3#top',
                 'functions': page_functions(called=2)}
        self.assertEqual(self.convert({'result': [entry]}),
                         {'web/static/js/app.js': {1: 1, 2: 2, 3: 2, 4: 2, 5: 1}})

    def test_a_puppeteer_entry_reads_its_raw_script_coverage_over_its_text(self):
        entry = {'url': 'http://localhost/index.html', 'text': INLINE,
                 'ranges': [{'start': 0, 'end': 10}],
                 'rawScriptCoverage': {'scriptId': '4', 'url': 'x',
                                       'functions': inline_functions()}}
        self.assertEqual(self.convert([entry]), {'web/index.html': {4: 1, 5: 1, 6: 0, 7: 0}})

    def test_an_inline_script_maps_onto_its_lines_in_the_page(self):
        entry = {'url': 'http://localhost/index.html', 'source': INLINE,
                 'functions': inline_functions()}
        self.assertEqual(self.convert([entry]), {'web/index.html': {4: 1, 5: 1, 6: 0, 7: 0}})

    def test_a_source_that_is_not_once_in_the_file_is_skipped_and_logged_once(self):
        other = {'url': 'http://localhost/static/js/app.js', 'source': 'let other = 1;\n',
                 'functions': page_functions()}
        twice = {'url': self.file_url('twice.js'), 'source': '1;\n', 'functions': [
            {'ranges': [{'startOffset': 0, 'endOffset': 3, 'count': 1}]}]}
        self.assertEqual(self.convert([other, other], [twice]), {})
        lines = self.out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('::notice::'))
        self.assertIn('http://localhost/static/js/app.js', lines[0])
        self.assertIn('twice.js', lines[0])

    def test_unmatched_and_ambiguous_urls_are_skipped_and_each_logged_once(self):
        entries = [{'url': 'https://cdn.test/chart.min.js', 'functions': []},
                   {'url': 'http://h/x/lib.js', 'functions': []},
                   {'url': 'https://cdn.test/', 'functions': []}]
        self.assertEqual(self.convert(entries, entries), {})
        lines = self.out.getvalue().splitlines()
        self.assertEqual(len(lines), 2, lines)
        unmatched, ambiguous = sorted(lines, key=lambda line: 'cdn.test' not in line)
        self.assertIn('https://cdn.test/chart.min.js', unmatched)
        self.assertIn('https://cdn.test/,', unmatched)
        self.assertIn('http://h/x/lib.js', ambiguous)
        self.assertIn('a/x/lib.js', ambiguous)
        self.assertIn('b/x/lib.js', ambiguous)

    def test_other_schemes_untracked_and_outside_files_are_silently_ignored(self):
        outside = Path(self.repo).resolve().parent / 'y.js'
        outside.write_bytes(b'1;\n')
        Path(self.repo, 'untracked.js').write_bytes(b'1;\n')
        ranges = [{'functionName': '', 'ranges': [{'startOffset': 0, 'endOffset': 3, 'count': 1}]}]
        entries = [{'url': url, 'functions': ranges} for url in (
            'node:internal/modules/cjs/loader', 'internal/y.js', '', 'data:text/javascript,1',
            'chrome-extension://abc/y.js', outside.as_uri(), self.file_url('untracked.js'))]
        self.assertEqual(self.convert(entries), {})
        self.assertEqual(self.out.getvalue(), '')

    def test_a_file_url_is_decoded(self):
        entry = {'url': self.file_url('web/sp ace.js'), 'functions': page_functions()}
        self.assertIn('%20', entry['url'])
        self.assertEqual(self.convert([entry]), {'web/sp ace.js': PAGE_LINES})

    def test_windows_file_urls_name_a_drive_path(self):
        for url, expected in (('file:///D:/a/r/x%20y.js', 'D:/a/r/x y.js'),
                              ('file:///tmp/r/x.js', '/tmp/r/x.js')):
            with self.subTest(url=url):
                self.assertEqual(v8.file_path(url), expected)

    def test_entries_of_one_file_add_up_across_entries_and_dumps(self):
        entry = {'url': 'http://h/static/js/app.js', 'functions': page_functions(called=1)}
        self.assertEqual(self.convert([entry, entry], {'result': [entry]}),
                         {'web/static/js/app.js': {1: 3, 2: 3, 3: 3, 4: 3, 5: 3}})

    def test_a_byte_order_mark_is_blank_whether_the_offsets_count_it_or_not(self):
        # A CommonJS script's offsets count the mark, an ES module's do not.
        bare = BOM_MJS[1:]
        for skip, text in ((0, BOM_MJS), (1, bare)):
            with self.subTest(counted=not skip):
                entry = {'url': self.file_url('bom.js'), 'functions': [
                    {'ranges': [{'startOffset': 0, 'endOffset': size(text), 'count': 1}]},
                    {'ranges': [{'startOffset': units(text, 'function'),
                                 'endOffset': units(text, 'const'), 'count': 0}]}]}
                self.assertEqual(self.convert([entry]), {'bom.js': {1: 0, 2: 0, 3: 0, 4: 1}})

    def test_offsets_not_spanning_the_file_are_skipped_and_logged_once(self):
        # CDN scripts sharing only a basename (longer and shorter), a transpiled script.
        wrong = [{'url': 'https://cdn.test/lib/y.js', 'functions': [
                     {'ranges': [{'startOffset': 0, 'endOffset': 120, 'count': 1}]}]},
                 {'url': 'https://cdn.test/short/y.js', 'functions': [
                     {'ranges': [{'startOffset': 0, 'endOffset': 2, 'count': 1}]}]},
                 {'url': self.file_url('web/static/js/app.js'), 'functions': [
                     {'ranges': [{'startOffset': 0, 'endOffset': size(PAGE_JS) + 4,
                                  'count': 1}]}]},
                 {'url': self.file_url('y.js'), 'functions': []}]
        right = {'url': 'https://cdn.test/lib/y.js', 'functions': [
            {'ranges': [{'startOffset': 0, 'endOffset': 3, 'count': 2}]}]}
        self.assertEqual(self.convert(wrong, wrong, [right]), {'y.js': {1: 2}})
        lines = self.out.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('::notice::'))
        self.assertIn('https://cdn.test/lib/y.js (y.js)', lines[0])
        self.assertIn('https://cdn.test/short/y.js (y.js)', lines[0])
        self.assertIn('web/static/js/app.js', lines[0])
        # An entry with no functions measured nothing: it is not named.
        self.assertNotIn(self.file_url('y.js'), lines[0])

    def test_a_file_that_is_not_utf8_is_skipped_and_logged(self):
        entry = {'url': self.file_url('bin.js'), 'functions': page_functions()}
        self.assertEqual(self.convert([entry]), {})
        self.assertTrue(self.out.getvalue().startswith('::notice::'))
        self.assertIn('bin.js', self.out.getvalue())

    def test_invalid_dumps_are_configuration_errors_naming_the_file(self):
        good = {'url': 'u', 'functions': []}
        for document in ('not json', '{"results": []}', '"text"', [3], [{'functions': []}],
                         [{'url': 'u'}], [{'url': 'u', 'functions': {}}],
                         [{'url': 'u', 'functions': [{'ranges': [{'startOffset': -1,
                                                                  'endOffset': 2, 'count': 1}]}]}],
                         [{'url': 'u', 'functions': [{'ranges': [{'startOffset': True,
                                                                  'endOffset': 2, 'count': 1}]}]}],
                         [{'url': 'u', 'functions': [{'ranges': [{'startOffset': 3,
                                                                  'endOffset': 2, 'count': 1}]}]}],
                         [{'url': 'u', 'functions': [{'ranges': [{'startOffset': 0,
                                                                  'endOffset': 2,
                                                                  'count': -1}]}]}],
                         [{'url': 'u', 'functions': [{'ranges': [{'startOffset': 0,
                                                                  'endOffset': 2.5,
                                                                  'count': 1}]}]}],
                         [{'url': 'u', 'functions': [{}]}], [good, 'x'],
                         [{'url': 'u', 'functions': [], 'source': 5}],
                         [{'url': 'http://[x/a.js', 'functions': []}],
                         '[' * 200000 + ']' * 200000):
            with self.subTest(document=document):
                with self.assertRaises(ConfigError) as caught:
                    self.convert(document)
                self.assertIn('dump-0.json', str(caught.exception))

    def test_a_puppeteer_entry_without_raw_coverage_names_the_option(self):
        entry = {'url': 'http://h/static/js/app.js', 'text': PAGE_JS, 'ranges': []}
        with self.assertRaisesRegex(ConfigError, 'includeRawScriptCoverage'):
            self.convert([entry])


@unittest.skipUnless(shutil.which('node'), 'needs node on PATH')
class RealNodeTest(unittest.TestCase):
    def test_node_v8_coverage_gives_exact_lines_for_tracked_files_only(self):
        with tempfile.TemporaryDirectory() as scratch:
            Path(scratch, 'repo').mkdir()
            repo = make_repo(Path(scratch, 'repo'), {
                'src/app.js': APP_JS.encode(), 'src/bom.js': BOM_JS.encode(),
                'src/esm.mjs': ESM_JS.encode(), 'src/bom.mjs': BOM_MJS.encode(),
                'src/bomfn.js': BOM_MJS.encode()})
            Path(repo, 'node_modules/dep').mkdir(parents=True)
            Path(repo, 'node_modules/dep/index.js').write_bytes(b'module.exports = 1;\n')
            dumps = Path(scratch, 'dumps')
            env = {'PATH': os.environ['PATH'], 'NODE_V8_COVERAGE': str(dumps)}
            for script in ('src/app.js', 'src/app.js', 'src/bom.js', 'src/esm.mjs',
                           'src/bom.mjs', 'src/bomfn.js'):
                subprocess.run(['node', script], cwd=repo, env=env, check=True,
                               capture_output=True)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                lines = v8.convert(sorted(dumps.iterdir()), repo)
        self.assertEqual(lines, {
            'src/app.js': {line: hits * 2 for line, hits in APP_LINES.items()},
            'src/bom.js': {1: 1, 2: 0, 3: 0, 4: 0},
            'src/esm.mjs': {1: 1, 2: 1, 3: 1, 4: 1, 5: 1},
            'src/bom.mjs': {1: 0, 2: 0, 3: 0, 4: 1}, 'src/bomfn.js': {1: 0, 2: 0, 3: 0, 4: 1}})
        self.assertEqual(out.getvalue(), '')
