"""The lines of JavaScript source holding code: a line whose non-blank characters all belong to
comments is not one."""
from typing import List, Optional, Set

from test_ratchet import ConfigError

_LINE_ENDS = '\n\r\u2028\u2029'
_EXPRESSION_KEYWORDS = {'false', 'null', 'super', 'this', 'true'}
_REGEX_KEYWORDS = {
    'await', 'case', 'delete', 'do', 'else', 'in', 'instanceof', 'new',
    'of', 'return', 'throw', 'typeof', 'void', 'yield',
}
_STATEMENT_KEYWORDS = {'for', 'if', 'while', 'with'}
_OTHER_KEYWORDS = {
    'break', 'catch', 'class', 'const', 'continue', 'debugger', 'default',
    'enum', 'export', 'extends', 'finally', 'function', 'implements',
    'import', 'interface', 'let', 'package', 'private', 'protected',
    'public', 'static', 'switch', 'try', 'var',
}
_REGEX_PUNCTUATORS = set('(,=:[!&|?+-*/%<>^~;{')
_EXPRESSION_PUNCTUATORS = set(')]')


def _identifier_start(char: str) -> bool:
    return char in '_$' or char.isalpha() or ord(char) >= 128


def _identifier_part(char: str) -> bool:
    return _identifier_start(char) or char.isdigit()


def _javascript_whitespace(char: str) -> bool:
    return char == '\ufeff' or char.isspace()


class _Scanner:
    def __init__(self, source: str, path: str, first_line: int):
        self.source = source
        self.path = path
        self.index = 0
        self.line = first_line
        self.lines: Set[int] = set()

    def scan(self) -> Set[int]:
        if self.source.startswith('#!'):
            self._line_comment()
        self._code()
        return self.lines

    def _peek(self, distance: int = 0) -> str:
        index = self.index + distance
        return self.source[index] if index < len(self.source) else ''

    def _advance(self, code: bool = False) -> None:
        char = self._peek()
        if not char:
            return
        # Lines are counted as the report splits them, on \n alone.
        if char == '\n':
            self.line += 1
        elif code and not _javascript_whitespace(char):
            self.lines.add(self.line)
        self.index += 1

    def _error(self, shape: str, line: Optional[int] = None) -> ConfigError:
        return ConfigError(f'{self.path}: line {self.line if line is None else line}: {shape}')

    def _code(self, template_line: Optional[int] = None) -> None:
        previous = 'start' if template_line is None else 'prefix'
        brace_depth = 0
        paren_heads: List[bool] = []
        while self.index < len(self.source):
            char = self._peek()
            following = self._peek(1)
            if _javascript_whitespace(char):
                self._advance()
                continue
            if char == '/' and following == '/':
                self._line_comment()
                continue
            if char == '/' and following == '*':
                self._block_comment()
                continue
            if char in "'\"":
                self._string(char)
                previous = 'expression'
                continue
            if char == '`':
                self._template()
                previous = 'expression'
                continue
            if (char == '?' and following == '.'
                    and previous in ('expression', 'brace')):
                self._advance(code=True)
                self._advance(code=True)
                previous = 'property'
                continue
            if char == '.' and previous in ('expression', 'brace'):
                self._advance(code=True)
                previous = 'property'
                continue
            if char == '/':
                if previous in ('start', 'prefix'):
                    self._regex()
                    previous = 'expression'
                elif previous == 'expression':
                    self._advance(code=True)
                    previous = 'prefix'
                elif previous == 'brace':
                    raise self._error('slash after } cannot be classified')
                else:
                    raise self._error('unclassified slash')
                continue
            if char in '+-' and following == char:
                self._advance(code=True)
                self._advance(code=True)
                previous = 'expression'
                continue
            if char == '\\':
                raise self._error('escaped identifier is not modelled')
            if _identifier_start(char):
                token = self._identifier()
                if previous == 'property':
                    previous = 'expression'
                elif token in _REGEX_KEYWORDS:
                    previous = 'prefix'
                elif token in _EXPRESSION_KEYWORDS:
                    previous = 'expression'
                elif token in _STATEMENT_KEYWORDS:
                    previous = 'statement'
                elif token in _OTHER_KEYWORDS:
                    previous = 'unknown'
                else:
                    previous = 'expression'
                continue
            if char.isdigit():
                self._number()
                previous = 'expression'
                continue
            self._advance(code=True)
            if char == '{':
                brace_depth += 1
                previous = 'prefix'
            elif char == '}':
                if template_line is not None and brace_depth == 0:
                    return
                brace_depth -= 1
                previous = 'brace'
            elif char == '(':
                paren_heads.append(previous == 'statement')
                previous = 'prefix'
            elif char == ')':
                # The body of if/while/for/with (...) may open with a regex literal.
                previous = 'prefix' if paren_heads and paren_heads.pop() else 'expression'
            elif char in _EXPRESSION_PUNCTUATORS:
                previous = 'expression'
            elif char in _REGEX_PUNCTUATORS:
                previous = 'prefix'
            else:
                previous = 'unknown'
        if template_line is not None:
            raise self._error('unterminated template', template_line)

    def _identifier(self) -> str:
        start = self.index
        while self._peek() and _identifier_part(self._peek()):
            self._advance(code=True)
        return self.source[start:self.index]

    def _number(self) -> None:
        previous = ''
        while self._peek():
            char = self._peek()
            if char.isalnum() or char in '._':
                previous = char
                self._advance(code=True)
                continue
            if char in '+-' and previous in 'eE':
                previous = char
                self._advance(code=True)
                continue
            return

    def _line_comment(self) -> None:
        self._advance()
        self._advance()
        while self._peek() and self._peek() not in _LINE_ENDS:
            self._advance()

    def _block_comment(self) -> None:
        opening_line = self.line
        self._advance()
        self._advance()
        while self.index < len(self.source):
            if self._peek() == '*' and self._peek(1) == '/':
                self._advance()
                self._advance()
                return
            self._advance()
        raise self._error('unterminated block comment', opening_line)

    def _string(self, quote: str) -> None:
        opening_line = self.line
        self._advance(code=True)
        while self.index < len(self.source):
            char = self._peek()
            if char == quote:
                self._advance(code=True)
                return
            if char in _LINE_ENDS:
                raise self._error('unterminated string', opening_line)
            if char == '\\':
                self._advance(code=True)
                self._advance(code=True)
                continue
            self._advance(code=True)
        raise self._error('unterminated string', opening_line)

    def _template(self) -> None:
        opening_line = self.line
        self._advance(code=True)
        while self.index < len(self.source):
            char = self._peek()
            if char == '`':
                self._advance(code=True)
                return
            if char == '\\':
                self._advance(code=True)
                self._advance(code=True)
                continue
            if char == '$' and self._peek(1) == '{':
                self._advance(code=True)
                self._advance(code=True)
                self._code(template_line=opening_line)
                continue
            self._advance(code=True)
        raise self._error('unterminated template', opening_line)

    def _regex(self) -> None:
        opening_line = self.line
        in_class = False
        self._advance(code=True)
        while self.index < len(self.source):
            char = self._peek()
            if char in _LINE_ENDS:
                raise self._error('unterminated regex', opening_line)
            if char == '\\':
                self._advance(code=True)
                if self._peek() in _LINE_ENDS:
                    raise self._error('unterminated regex', opening_line)
                self._advance(code=True)
                continue
            if char == '[':
                in_class = True
            elif char == ']':
                in_class = False
            elif char == '/' and not in_class:
                self._advance(code=True)
                while self._peek() and _identifier_part(self._peek()):
                    self._advance(code=True)
                return
            self._advance(code=True)
        raise self._error('unterminated regex', opening_line)


def code_lines(source: str, path: str, first_line: int = 1) -> Set[int]:
    """The lines, numbered from `first_line` at \\n, holding code; ConfigError names the file
    and line of a shape the scanner does not model (ported from ai-researcher's js_lines.py)."""
    return _Scanner(source, path, first_line).scan()
