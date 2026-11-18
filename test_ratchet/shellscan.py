"""What bash 5.2 and 5.3 report as each command's LINENO in a script, read without running it."""
import bisect
import re
from typing import Dict, Iterator, List, Optional, Set, Tuple

Token = Tuple[str, str, int, int]  # kind ('word', 'op', 'arith'), text, first line, last line
Printed = Tuple[str, bool]  # how BASH_COMMAND starts, and whether a word ends there

_OPS = (';;&', ';;', ';&', '&&', '||', '|&', '&>>', '&>', '>>', '<<<', '<<-', '<<', '<&', '>&',
        '<>', '>|', ';', '&', '|', '(', ')', '<', '>')
_REDIRS = {op for op in _OPS if '<' in op or '>' in op}
_BREAKS = {'\n', ';', '&', '|', '&&', '||', '|&', '('}
_CLAUSE_ENDS = {';;', ';&', ';;&'}
_OPENERS = {'if', 'then', 'else', 'elif', 'do', 'while', 'until', '{', '!', 'time'}
_CLOSERS = {'fi', 'done', '}', 'esac'}
_FD = re.compile(r'[0-9]+(?=[<>])')
_CASE_WORD = re.compile(r'(case|esac)(?=[ \t\n;)])')
_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(\[[^]]*\])?\+?=')
_REPRINTED = re.compile(r'(?<!\\)\$[\'"]|\(|\\?\n')


class _Unclosed(Exception):
    pass


def _line_end(text: str, pos: int) -> int:
    end = text.find('\n', pos)
    return len(text) if end < 0 else end


class _Lexer:
    def __init__(self, text: str):
        self.text = text
        self.newlines = [index for index, char in enumerate(text) if char == '\n']
        self.heredocs: List[Tuple[str, bool]] = []
        self.start = 0

    def line(self, pos: int) -> int:
        return bisect.bisect_left(self.newlines, pos) + 1

    def closing(self, pos: int, close: str) -> int:
        """The index after the `close` (`'`, `$'`, `"`, a backquote, `}` or `)`) ending the
        quote or group opened before pos."""
        text, depth, cases = self.text, 1, []  # cases: the depth each open `case` began at
        while pos < len(text):
            char = text[pos]
            pos += 1
            keyword = _CASE_WORD.match(text, pos - 1) if close == ')' else None
            if keyword and text[:pos - 1].rstrip(' \t')[-1:] in ('', '\n', ';', '(', '&', '|'):
                if keyword.group(1) == 'case':
                    cases.append(depth)
                elif cases:
                    cases.pop()
                pos = keyword.end()
                continue
            if close == "'":
                if char == "'":
                    return pos
            elif char == '\\':
                pos += 1
            elif char == close[-1] and (close != ')' or depth == 1 and cases[-1:] != [1]):
                return pos
            elif close == "$'":
                pass
            elif char == '$' and (text[pos:pos + 1] in ('(', '{')
                                  or text[pos:pos + 1] == "'" and close != '"'):
                pos = self.closing(pos + 1, {'(': ')', '{': '}', "'": "$'"}[text[pos]])
            elif char in '`"' or char == "'" and close != '"':
                pos = self.closing(pos, char)
            elif close == ')' and char in '()' and not (char == ')' and cases[-1:] == [depth]):
                depth += 1 if char == '(' else -1
            elif close == ')' and char == '#' and text[pos - 2] in ' \t\n(':
                pos = _line_end(text, pos)
        raise _Unclosed

    def word(self, pos: int) -> int:
        text, start = self.text, pos
        while pos < len(text) and text[pos] not in ' \t\n;&|()<>':
            char, pos = text[pos], pos + 1
            if char == '\\':
                pos += 1
            elif char in '\'"`':
                pos = self.closing(pos, char)
            elif char == '$' and text[pos:pos + 1] in ('(', '{', "'"):
                pos = self.closing(pos + 1, {'(': ')', '{': '}', "'": "$'"}[text[pos]])
            if text[pos:pos + 1] == '(' and _ASSIGNMENT.fullmatch(text[start:pos]):
                pos = self.closing(pos + 1, ')')
        return min(pos, len(text))

    def tokens(self) -> Iterator[Token]:
        text, pos = self.text, 0
        while pos < len(text):
            char, self.start = text[pos], pos
            fd = _FD.match(text, pos)  # `2>` is a redirection, `2 >` a word and one
            start = fd.end() if fd else pos
            op = next((op for op in _OPS if text.startswith(op, start)), None)
            if char in ' \t' or text.startswith('\\\n', pos):
                pos += 1 + (char == '\\')
            elif char == '#':
                pos = _line_end(text, pos)
            elif char == '\n':
                line = self.line(pos)
                pos, last = self.bodies(pos + 1, line)
                yield 'op', '\n', line, last
            elif text.startswith('((', pos):
                end = self.closing(pos + 1, ')')
                yield 'arith', text[pos:end], self.line(pos), self.line(end - 1)
                pos = end
            elif op and not (op in ('<', '>') and text.startswith('(', start + 1)):
                yield 'op', op, self.line(pos), self.line(pos)
                pos = start + len(op)
                if op in ('<<', '<<-'):
                    self.pending(pos, op == '<<-')
            else:
                end = self.closing(pos + 2, ')') if op else self.word(pos)
                yield 'word', text[pos:end], self.line(pos), self.line(end - 1)
                pos = end

    def pending(self, pos: int, strip: bool) -> None:
        while self.text[pos:pos + 1] in (' ', '\t'):
            pos += 1
        self.heredocs.append((re.sub(r'[\'"\\]', '', self.text[pos:self.word(pos)]), strip))

    def bodies(self, pos: int, line: int) -> Tuple[int, int]:
        """Where reading resumes after the heredoc bodies starting at pos, and their last line."""
        for delimiter, strip in self.heredocs:
            body = None
            while body != delimiter:
                end = self.text.find('\n', pos)
                if end < 0:
                    raise _Unclosed
                body = self.text[pos:end].lstrip('\t') if strip else self.text[pos:end]
                line, pos = line + 1, end + 1
        self.heredocs = []
        return pos, line


def _is(tokens: List[Token], index: int, *texts: str) -> bool:
    return [token[1] for token in tokens[index:index + len(texts)]] == list(texts)


def _substitutes(token: Token) -> bool:
    return token[0] == 'word' and any(mark in token[1] for mark in ('$(', '`', '<(', '>('))


def _printed(text: str) -> Printed:
    """`text` as far as bash prints it as written: to a line end, a `\\<newline>`, a `$'` or
    `$"` (printed as the plain quote, after no other quote), or through a `(`."""
    cut = _REPRINTED.search(text)
    if cut is None or cut.group() == '\n':
        return text[:cut.start() if cut else None], True
    head, mark = text[:cut.start()], cut.group()
    if mark[0] == '$' and not re.search('[\'"`]', head):
        return head + mark[1], False
    return head + mark * (mark == '('), False


def _text(tokens: List[Token], index: int) -> Printed:
    """The command at tokens[index] as BASH_COMMAND starts: its words, else a redirection."""
    words = []
    first = [token[1] for token in tokens[index:index + 2]]
    while index < len(tokens) and (tokens[index][0] != 'op' or tokens[index][1] in _REDIRS):
        if tokens[index][1] in _REDIRS:
            index += 1
        else:
            words.append(tokens[index][1])
        index += 1
    return _printed(' '.join(words or first))


class Scan:
    """A script as bash 5.3 runs it, or 5.2 with `lookahead`: measurable `lines`; per line,
    how BASH_COMMAND starts (`words`); the reporting lines of list or redirection
    substitutions (`bases`); forked compounds' first lines (`forks`); each function's entry
    lines (`functions`); the line the lexer stopped at (`stop`)."""

    def __init__(self, text: str, lookahead: bool = False):
        self.lookahead = lookahead
        lexer = _Lexer(text)
        self.tokens: List[Token] = []
        self.stop: Optional[int] = None
        try:
            self.tokens.extend(lexer.tokens())
        except _Unclosed:
            self.stop = lexer.line(lexer.start)
        self.lines: Set[int] = set()
        self.words: Dict[int, Set[Printed]] = {}
        self.bases: Set[int] = set()
        self.forks: Set[int] = set()
        self.functions: Dict[str, Set[int]] = {}
        self.braces: List[Optional[list]] = []  # open function bodies: [name, entry line]
        self.spans: List[list] = []  # open compounds: [closer, first line, piped]
        self.run()
        for body in self.braces:
            self.enter(body)
        if self.stop is not None:
            self.lines = {line for line in self.lines if line < self.stop}

    def enter(self, body: Optional[list]) -> None:
        if body:
            self.functions[body[0]].add(body[1])
            self.lines.add(body[1])

    def mark(self, line: int, text: str, whole: bool = True) -> None:
        self.lines.add(line)
        self.words.setdefault(line, set()).add((text.replace('\t', ' '), whole))
        for span in self.spans:
            span[1] = line if span[1] is None else span[1]

    def open(self, closer: str, index: int) -> None:
        before = self.tokens[index - 1][1] if index else ''
        self.spans.append([closer, None, before in ('|', '|&', 'coproc')])

    def close(self, closer: str, index: int) -> None:
        """Close the compound at tokens[index]: a subshell, pipeline part or job is a fork."""
        if not self.spans:
            return
        _, first, piped = self.spans.pop()
        after = index + 1
        while self.tokens[after:after + 1] and self.tokens[after][1] in _REDIRS:
            after += 2
        if first is not None and (closer == ')' or piped or self.tokens[after:after + 1] and
                                  self.tokens[after][1] in ('|', '|&', '&')):
            self.forks.add(first)

    def find(self, index: int, kind: str, text: str) -> int:
        """The index of the next `kind` token `text`; a `do` must follow a separator."""
        tokens = self.tokens
        while index < len(tokens):
            before = tokens[index - 1]
            if tokens[index][:2] == (kind, text) and (
                    text != 'do' or before[1] in (';', '\n') or before[0] == 'arith'):
                return index
            index += 1
        return index

    def run(self) -> None:
        tokens, state, index = self.tokens, 'command', 0
        while index < len(tokens):
            kind, value = tokens[index][:2]
            index += 1
            if state == 'pattern':
                if value == 'esac':
                    self.close('esac', index - 1)
                    state = 'after'
                elif value != '\n':
                    index, state = self.find(index - 1, 'op', ')') + 1, 'command'
            elif kind == 'op':
                if value in _REDIRS:
                    target = tokens[index:index + 1]
                    if state == 'command' and target:
                        self.mark(target[0][3], *_text(tokens, index - 1))
                        state = 'args'
                    elif state == 'after' and target and _substitutes(target[0]):
                        self.mark(target[0][3], '')
                        self.bases.add(target[0][3])
                    index += 1
                    continue
                if value == '(':
                    self.open(')', index - 1)
                elif value == ')':
                    self.close(')', index - 1)
                state = ('pattern' if value in _CLAUSE_ENDS else
                         'command' if value in _BREAKS else 'after')
            elif state != 'args':
                state, index = self.command(index - 1)

    def command(self, index: int) -> Tuple[str, int]:
        """Record the command at tokens[index]; the state and index after it."""
        tokens = self.tokens
        kind, value, first, last = tokens[index]
        if kind == 'arith':
            self.mark(last, value.split('\n', 1)[0])
            return 'after', index + 1
        if value in _OPENERS:
            if value in ('if', 'while', 'until', '{'):
                self.open({'if': 'fi', '{': '}'}.get(value, 'done'), index)
            self.braces.extend([None] * (value == '{'))
            return 'command', index + 1
        if value in _CLOSERS:
            self.close(value, index)
            self.enter(self.braces.pop() if value == '}' and self.braces else None)
            return 'after', index + 1
        if value == '[[':
            end = self.find(index, 'word', ']]')
            if end < len(tokens):
                # A word tested alone prints as `-n word`.
                alone = any(_is(tokens, index + 2, text) for text in (']]', '&&', '||'))
                self.mark(tokens[end][3], *_printed('[[ ' + '-n ' * alone + tokens[index + 1][1]))
            return 'after', end + 1
        if value in ('for', 'select', 'case'):
            self.open('esac' if value == 'case' else 'done', index)
        if value in ('for', 'select'):
            end = self.find(index + 1, 'word', 'do')
            words = [token[1] for token in tokens[index:end] if token[0] == 'word']
            if tokens[index + 1:index + 2] and tokens[index + 1][0] == 'arith':
                self.mark(first, '((', False)
            else:
                self.mark(first, *_printed(' '.join(words)))
            if any(_substitutes(token) for token in tokens[index + 1:end]):
                self.bases.add(first)
            return 'command', end + 1
        if value == 'case':
            if index + 1 < len(tokens):
                self.mark(tokens[index + 1][3] if self.lookahead else first,
                          *_printed('case ' + tokens[index + 1][1]))
            return 'pattern', self.find(index + 2, 'word', 'in') + 1
        index += 1
        if value == 'coproc':
            # A coprocess runs its compound, after an optional name, as a fork of its own.
            index += _is(tokens, index + 1, '{') or _is(tokens, index + 1, '(')
            return 'command', index
        if value == 'function' or _is(tokens, index, '(', ')'):
            name = tokens[index][1] if value == 'function' else value
            self.functions.setdefault(name, set())
            index += value == 'function'
            index += 2 * _is(tokens, index, '(', ')')
            while _is(tokens, index, '\n'):
                index += 1
            if tokens[index:index + 1] and tokens[index][:2] == ('word', '{'):
                # A function reports the opening line of the last body its definition holds.
                start = tokens[index][2]
                for body in filter(None, self.braces):
                    body[1] = start
                self.braces.append([name, start])
                self.open('}', index)
                index += 1
            return 'command', index
        # Up to 5.2 a first plain word reports the last line of the token read after it.
        ahead = self.lookahead and not _ASSIGNMENT.match(value) and index < len(tokens)
        self.mark(tokens[index][3] if ahead else last, *_text(tokens, index - 1))
        return 'args', index


def measurable(text: str, lookahead: bool = False) -> Set[int]:
    """The lines bash reports as a command's LINENO."""
    return Scan(text, lookahead).lines
