"""Which lines of JavaScript source hold code."""
import unittest

from test_ratchet import ConfigError, jslines

# A scanner missing each form would read some line wrong.
CODE = (
    ('function f() {\n  // explain\n  return 1;\n}\nf();\n', {1, 3, 4, 5}),
    ('a(); /* one\ntwo\n*/ b();\n/* x */\n', {1, 3}),
    ('a = "/*";\nb();\nc = "*/";\n', {1, 2, 3}),
    ('a = \'\\\'/*\';\nb();\nc = "*/";\n', {1, 2, 3}),
    ('a = `/*`;\nb();\nc = `*/`;\n', {1, 2, 3}),
    ('a = `\\`/*`;\nb();\nc = "*/";\n', {1, 2, 3}),
    # Template text is code; a comment in ${} is not.
    ('a = `\n// text\n`;\n', {1, 2, 3}),
    ('a = `${ b /* c */ + `x${ d }` }\n${\n// only a comment\ne}`;\n', {1, 2, 4}),
    ('a = `${ {k: 1}.k +\n// comment\n1 }`;\n', {1, 3}),
    ('r = /[/*]/;\nb();\n// */\n', {1, 2}),
    ('r = /a\\/\\/b/g;\nn = 1e-5 / 2;\n', {1, 2}),
    # A division read as a regex, or a regex as division, opens what runs to the line's end.
    ('a = (b) / 2;\nc = d[0] / 2;\ne = f / 2;\ng = 4 / 2;\nh = i++ / 2;\nj = this / 2;\n'
     'k = l.return / 2;\nm = n?.return / 2;\no = {}.return / 2;\n', set(range(1, 10))),
    ('function f() {\n  return /"/.test(s);\n}\nif (a) /"/.test(b);\nwhile ((a)) /\'/.test(b);\n'
     '{ /"/.test(c); }\n', {1, 2, 3, 4, 5, 6}),
    ('r = /[/]"/;\n', {1}),
    ('a();\r\n// c\r\nb();\r\n', {1, 3}),
    # A string continues past an escaped CRLF or CR and holds U+2028 and U+2029.
    ('a = "x\\\r\ny" + \'x\\\r\ny\' + "z\\\r";\n', {1, 2, 3}),
    ('a = "x\u2028y" + \'\u2029\';\n// c\n', {1}),
    # \r and U+2028 end a comment, not a line of the report.
    ('// c\rb();\n// d\u2028e();\n// f\n', {1, 2}),
    ('#!/usr/bin/env node\nrun();\n', {2}),
    ('\ufeff// c\nx();\n', {2}),
)
REFUSED = (
    ('a();\n/* x\n', 2, 'unterminated block comment'),
    ('a = "x\n";\n', 1, 'unterminated string'), ('a = "x\ry";\n', 1, 'unterminated string'),
    ("a();\nb = 'x\\", 2, 'unterminated string'),
    ('a = "x\\\ny\n";\n', 1, 'unterminated string'),
    ('a = `x\n', 1, 'unterminated template'),
    ('a();\nb = `${ c\n', 2, 'unterminated template'),
    ('a = /x\n/;\n', 1, 'unterminated regex'),
    ('a = /x\\\n/;\n', 1, 'unterminated regex'),
    ('a = /x', 1, 'unterminated regex'),
    ('{}\n/x/.test(a);\n', 2, 'slash after } cannot be classified'),
    ('a;\nlet /x/;\n', 2, 'unclassified slash'),
    ('f(.../x/.exec(s));\n', 1, 'unclassified slash'),
    ('\\u0061 = 1;\n', 1, 'escaped identifier is not modelled'),
)


class CodeLinesTest(unittest.TestCase):
    def test_lines_holding_code_are_kept_and_comment_only_lines_left_out(self):
        for source, expected in CODE:
            with self.subTest(source=source):
                self.assertEqual(jslines.code_lines(source, 'page.js'), expected)

    def test_lines_count_from_the_first_line_given(self):
        self.assertEqual(jslines.code_lines('// c\na();\n', 'page.html', 7), {8})

    def test_what_the_scanner_cannot_read_is_refused_naming_the_file_and_line(self):
        for source, line, shape in REFUSED:
            with self.subTest(source=source):
                with self.assertRaises(ConfigError) as caught:
                    jslines.code_lines(source, 'web/page.js', 10)
                self.assertEqual(str(caught.exception), f'web/page.js: line {line + 9}: {shape}')


if __name__ == '__main__':
    unittest.main()
