"""The action harness reads what it models and refuses everything else."""
import tempfile
import unittest
from pathlib import Path

from _action_harness import SKIP, parse, run_action

HEADER = 'name: t\ndescription: d\ninputs:\n  word:\n    description: w\n    default: hi\n'


def action(*steps, outputs=''):
    """action.yml text with the given step texts (each already indented under `steps:`) and
    the text of an `outputs:` block."""
    return HEADER + outputs + 'runs:\n  using: composite\n  steps:\n' + ''.join(steps)


def outputs_block(**values):
    entries = (f'  {name}:\n    value: {value}\n' for name, value in values.items())
    return 'outputs:\n' + ''.join(entries)


def writes(step_id, **values):
    lines = ''.join(f'        echo "{key}={value}" >> "$GITHUB_OUTPUT"\n'
                    for key, value in values.items())
    return f'    - id: {step_id}\n      shell: bash\n      run: |\n{lines}'


ECHO = '    - shell: bash\n      env:\n        W: ${{ inputs.word }}\n      run: echo "$W"\n'


class ParseTest(unittest.TestCase):
    def test_the_modelled_shapes_are_read_exactly(self):
        text = ('# leading comment\n'
                'one:\n'
                '  plain: some words # trailing comment\n'
                "  single: 'it''s # kept' # comment\n"
                '  double: "a b"\n'
                '  empty: \'\'\n'
                '  url: a/b@0123 # v1.2.3\n'
                '  expr: ${{ inputs.x == \'b:c\' }}\n'
                'items:\n'
                '  - name: first\n'
                '    run: |\n'
                '      echo one\n'
                '\n'
                '        indented # not a comment\n'
                '    keep: |-\n'
                '      no newline\n'
                '  # between items\n'
                '  - name: second\n')
        self.assertEqual(parse(text), {
            'one': {'plain': 'some words', 'single': "it's # kept", 'double': 'a b', 'empty': '',
                    'url': 'a/b@0123', 'expr': "${{ inputs.x == 'b:c' }}"},
            'items': [{'name': 'first', 'run': 'echo one\n\n  indented # not a comment\n',
                       'keep': 'no newline'}, {'name': 'second'}]})

    def test_unmodelled_shapes_are_refused(self):
        for name, text in (
                ('wrapped plain scalar in a step', 'steps:\n  - name: long\n      wrapped\n'
                                                   '    run: echo hi\n'),
                ('wrapped plain scalar at the top', 'description: long\n  wrapped\nruns: x\n'),
                ('keep-chomp block', 'run: |+\n  echo hi\n'),
                ('indent-indicator block', 'run: |2\n    echo hi\n'),
                ('folded block', 'run: >-\n  echo hi\n'),
                ('block indicator with text', 'run: | x\n  echo hi\n'),
                ('empty block', 'run: |\nnext: 1\n'),
                ('block line dedented below the first', 'run: |\n    a\n  b\n'),
                ('double-quote escape', 'description: "a\\tb"\n'),
                ('single quote then text', "description: 'a' b\n"),
                ('unterminated quote', "description: 'a\n"),
                ('flow list', 'x: [a, b]\n'),
                ('flow map', 'x: {a: b}\n'),
                ('anchor', 'x: &a b\n'),
                ('alias', 'x: *a\n'),
                ('tag', 'x: !!str b\n'),
                ('mapping indicator in a plain value', 'x: a: b\n'),
                ('a value that is only a comment', 'x: # c\n'),
                ('key with no value', 'x:\ny: 1\n'),
                ('a line that is no key', 'x: 1\njust words\n'),
                ('duplicate key', 'x: 1\nx: 2\n'),
                ('tab indentation', 'x: 1\n\ty: 2\n'),
                ('carriage return', 'x: 1\r\n'),
                ('a scalar list item', 'x:\n  - a\n'),
                ('a nested list item', 'x:\n  - - a\n'),
                ('a list at its key\'s indent', 'x:\n- a: 1\n'),
                ('an empty document', '# only a comment\n\n'),
                ('an indented document', '  x: 1\n')):
            with self.subTest(name):
                with self.assertRaises(AssertionError):
                    parse(text)


class RunActionTest(unittest.TestCase):
    def run_text(self, text, inputs=None, **options):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / 'a').mkdir()
            (root / 'a' / 'action.yml').write_text(text, encoding='utf-8')
            return run_action('a', inputs or {}, root, root=root, **options)

    def refused(self, text, fragment, inputs=None, **options):
        with self.assertRaisesRegex(AssertionError, fragment):
            self.run_text(text, inputs, **options)

    def test_a_run_step_gets_its_inputs_as_env(self):
        for inputs, expected in (({}, 'hi'), ({'word': 'yo'}, 'yo')):
            with self.subTest(inputs=inputs):
                result = self.run_text(action(ECHO), inputs)
                self.assertEqual((result.returncode, result.stdout.strip(), result.outputs),
                                 (0, expected, {}))
                self.assertEqual(result.ran, ['step 1'])

    def test_the_declared_outputs_are_read_from_their_steps_own_output_file(self):
        steps = (writes('a', m='1', extra='x'), writes('b', m='2', n='3'))
        text = action(*steps, outputs=outputs_block(first='${{ steps.a.outputs.m }}',
                                                    second='${{ steps.b.outputs.m }}'))
        result = self.run_text(text)
        self.assertEqual(result.outputs, {'first': '1', 'second': '2'})

    def test_a_failed_run_returns_only_the_declared_outputs_it_wrote(self):
        failing = '    - id: c\n      shell: bash\n      run: exit 4\n'
        text = action(writes('a', m='1'), failing,
                      outputs=outputs_block(got='${{ steps.a.outputs.m }}',
                                            lost='${{ steps.c.outputs.m }}'))
        result = self.run_text(text)
        self.assertEqual((result.returncode, result.outputs), (4, {'got': '1'}))

    def test_an_output_that_does_not_resolve_to_a_step_output_is_refused(self):
        step = writes('a', m='1')
        for name, value, fragment in (
                ('unknown step id', '${{ steps.nosuch.outputs.m }}', 'no step with id nosuch'),
                ('step without that output', '${{ steps.a.outputs.zz }}', 'did not write zz'),
                ('another expression', '${{ inputs.word }}', 'unmodelled value'),
                ('literal text', 'fixed', 'unmodelled value'),
                ('extra text', '${{ steps.a.outputs.m }}x', 'unmodelled value'),
                ('no value', '', 'unmodelled value')):
            with self.subTest(name):
                text = action(step, outputs=f'outputs:\n  o:\n    value: {value}\n'
                              if value else 'outputs:\n  o:\n    description: d\n')
                self.refused(text, fragment)

    def test_a_step_id_must_be_unique(self):
        self.refused(action(writes('a', m='1'), writes('a', m='2')), 'repeats a step id')

    def test_a_uses_step_has_no_outputs_to_read(self):
        step = '    - id: u\n      uses: acme/up@0123\n'
        text = action(step, outputs=outputs_block(o='${{ steps.u.outputs.m }}'))
        self.refused(text, 'did not write m', uses={'acme/up': SKIP})

    def test_a_failing_step_ends_the_run(self):
        failing = '    - shell: bash\n      run: exit 3\n'
        result = self.run_text(action(failing, ECHO))
        self.assertEqual((result.returncode, result.stdout, result.ran), (3, '', ['step 1']))

    def test_a_wrapped_description_is_refused_not_run_as_nothing(self):
        text = action(ECHO).replace('description: d\n', 'description: long\n  wrapped\n', 1)
        self.refused(text, 'unmodelled YAML')

    def test_an_action_without_composite_steps_is_refused(self):
        for text in (HEADER + 'runs:\n  using: node20\n  main: x.js\n',
                     HEADER + 'runs:\n  using: composite\n  steps: x\n'):
            with self.subTest(text=text):
                self.refused(text, 'not a composite action')

    def test_a_step_key_that_is_not_modelled_is_refused(self):
        for key in ('working-directory: sub', 'continue-on-error: true', 'timeout-minutes: 1'):
            with self.subTest(key=key):
                self.refused(action(ECHO + f'      {key}\n'), 'not modelled')

    def test_a_shell_other_than_bash_is_refused(self):
        self.refused(action(ECHO.replace('shell: bash', 'shell: sh')), 'shell must be bash')
        self.refused(action('    - run: echo hi\n'), 'shell must be bash')

    def test_a_step_that_is_not_one_kind_is_refused(self):
        self.refused(action('    - name: neither\n'), 'neither a run: nor a uses:')
        both = '    - uses: a/b@1\n      shell: bash\n      run: echo\n'
        self.refused(action(both), 'neither a run: nor a uses:')

    def test_an_undeclared_input_is_refused(self):
        self.refused(action(ECHO), 'declares no input', {'wrod': 'x'})

    def test_an_expression_other_than_an_input_is_refused(self):
        self.refused(action(ECHO.replace('inputs.word', 'github.sha')), 'unmodelled expression')

    def test_an_if_expression_needs_a_stated_treatment(self):
        guarded = ECHO + "      if: ${{ always() }}\n"
        self.refused(action(guarded), 'no treatment stated for if', conditions={})
        self.refused(action(guarded), 'no treatment stated for if')
        expression = '${{ always() }}'
        ran = self.run_text(action(guarded), conditions={expression: True})
        skipped = self.run_text(action(guarded), conditions={expression: False})
        self.assertEqual((ran.ran, skipped.ran), (['step 1'], []))

    def test_an_if_step_after_a_failing_step_is_refused(self):
        failing = '    - shell: bash\n      run: exit 1\n'
        self.refused(action(failing, ECHO + '      if: ${{ always() }}\n'), 'always',
                     conditions={'${{ always() }}': True})

    def test_a_uses_step_needs_a_stated_treatment(self):
        step = '    - uses: acme/up@0123 # v1.0.0\n      with:\n        name: ${{ inputs.word }}\n'
        self.refused(action(step), 'no treatment stated for uses')
        self.refused(action(step), 'no treatment stated for uses', uses={'acme/other': SKIP})
        seen = []
        result = self.run_text(action(step, ECHO), uses={'acme/up': seen.append})
        self.assertEqual(seen, [{'name': 'hi'}])
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'hi'))
        self.assertEqual(self.run_text(action(step), uses={'acme/up': SKIP}).ran, ['step 1'])

    def test_a_github_output_line_that_is_not_key_value_is_refused(self):
        step = ('    - id: a\n      shell: bash\n'
                '      run: printf "k<<EOF\\nv\\nEOF\\n" >> "$GITHUB_OUTPUT"\n')
        self.refused(action(step), 'GITHUB_OUTPUT')


if __name__ == '__main__':
    unittest.main()
