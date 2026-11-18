"""Step outputs, step summaries and annotations through the one shared helper."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_ratchet import ghio
from test_ratchet.split import ConfigError


class GhioTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.output = Path(self.root.name, 'output')
        self.summary = Path(self.root.name, 'summary')
        patcher = mock.patch.dict(os.environ, {'GITHUB_OUTPUT': str(self.output),
                                               'GITHUB_STEP_SUMMARY': str(self.summary)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.root.cleanup()

    def test_outputs_append_single_and_multiline_values(self):
        ghio.set_output('count', '3')
        ghio.set_output('matrix', '{"shard": ["1/1"]}')
        ghio.set_output('files', 'a.py\nb.py')
        lines = self.output.read_text(encoding='utf-8').splitlines()
        self.assertEqual(lines[:2], ['count=3', 'matrix={"shard": ["1/1"]}'])
        name, delimiter = lines[2].split('<<')
        self.assertEqual((name, lines[3:]), ('files', ['a.py', 'b.py', delimiter]))
        self.assertTrue(delimiter.startswith('ghadelimiter_'))

    def test_a_carriage_return_alone_takes_the_multiline_form(self):
        ghio.set_output('note', 'a\rb')
        name, rest = self.output.read_bytes().decode('utf-8').split('<<', 1)
        delimiter = rest.split('\n', 1)[0]
        self.assertEqual((name, rest), ('note', f'{delimiter}\na\rb\n{delimiter}\n'))

    def test_summaries_append_whole_lines(self):
        ghio.add_summary('| a |')
        ghio.add_summary('done\n')
        self.assertEqual(self.summary.read_text(encoding='utf-8'), '| a |\ndone\n')

    def test_an_unset_file_variable_is_a_configuration_error(self):
        for variable, call in (('GITHUB_OUTPUT', lambda: ghio.set_output('a', 'b')),
                               ('GITHUB_STEP_SUMMARY', lambda: ghio.add_summary('x'))):
            with self.subTest(variable=variable):
                with mock.patch.dict(os.environ, {variable: ''}):
                    with self.assertRaisesRegex(ConfigError, variable):
                        call()

    def test_annotations_escape_their_message(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ghio.notice('100% done\nnext')
            ghio.warning('a\r\nb')
        self.assertEqual(out.getvalue(), '::notice::100%25 done%0Anext\n::warning::a%0D%0Ab\n')


if __name__ == '__main__':
    unittest.main()
