"""Scripts the shell coverage suite runs under real bash."""
# Multi-line substitutions, forks and piped functions.
SUBSTITUTIONS = (b'out=$(\n  printf a\n  printf b\n  printf c\n)\nif [[ $out == x ]]; then\n'
                 b'  echo handle_x\nfi\nf() { echo f; }\ny=$(\n  f\n  printf z\n)\nfor x in $(\n'
                 b'  echo a\n  echo b\n); do\n  :\ndone\nwhile read -r l; do\n  :\ndone < <(\n'
                 b'  printf "a\\n"\n  printf "b\\n"\n)\n( printf c\n  printf d )\n'
                 b'printf "%s\\n" "$(echo q)" | while read -r l; do\n  echo "$l"\ndone\n'
                 b'x=$(printf q)\n( printf q\n  printf r )\nprintf s; ( printf s\n  printf t )\n'
                 b'v=$(\n  printf a\n  w=$(printf b)\n  printf "$w"\n)\necho tail\n'
                 b'{ echo b\n  echo c; } &\nwait\nfor i in 1; do\n  echo "$i"\ndone | cat\n'
                 b'{ echo d\n  echo e; } >/dev/null &\nwait\n'
                 b'h() {\n  echo in-h\n}\nh | cat\nh &\nwait\n')
# Shifted lines landing on a subshell and a matching loop.
COINCIDENCES = (b"n=$(awk '\nBEGIN { print 1 }\n'\nprintf a)\nif false; then\n  ( echo never )\n"
                b"  echo never2\nfi\nn=$(awk '\nBEGIN { print 1 }\n'\nprintf a)\nif false; then\n"
                b"  while awk; do :; done\n  echo never3\nfi\n")
# A fork before the first command.
FIRST_FORK = (b'while read -r l; do\n  echo "$l"\ndone < <(\n  printf "a\\n"\n  printf "b\\n"\n'
              b'  printf "c\\n"\n)\nif false; then\n  echo never\nfi\n')
# Eval-defined functions, reprinted lines, `|&` and coproc forks, a multi-line ERR trap.
VALIDATED = (''
             'eval "$(printf \'tool() {\\n  case "$1" in\\n'
             '    act) echo act ;;\\n  esac\\n}\\n\')"\n'
             'if [ -n "${CI_NOPE:-}" ]; then\n  echo never1\n  echo never2\n  echo never3\n'
             'fi\ntool act\ny="$(\n  printf b\n)"\nargs=(\n  --one\n  --two\n)\n'
             "for ((i = 0; i < 2; i++)); do\n  :\ndone\nx=$( echo a )\n(\n  echo 'a\nb'\n"
             '  echo x\n)\n( echo a\\\nb\n  echo y )\nprintf a |& { read -r r\n'
             '  echo "$r"; }\n{ echo a\n  echo b; } |& cat\n( echo "a\tb"\n  echo z )\n'
             'coproc { echo c; }\nwait\nfor v in $(printf \'1 2\'); do\n  echo "$v"\n'
             'done | sort\n( for w in $(printf \'x\'); do\n  echo "$w"\ndone )\n'
             'eval "$(printf \'\\n\\ntool2() {\\n  echo never6\\n}\\n\')"\nif false; then\n'
             '  :\n  echo never6\nfi\ntool2\ncoproc named { echo n; }\nwait\nset -eE\n'
             'trap \'\n  echo "failed" >&2\n\' ERR\nfalse\n`printf echo` never4\n'
             'echo never5\n').encode()

# Evals in a function and at the top level.
EVALS = (b'g() {\n  eval "x=1\n  y=2"\n  echo in-g\n}\ng\nv=$(\n  printf a\n)\n'
         b'eval "$(printf \'true\\ntrue\\necho ab\\n\')"\nif false; then\n  echo a\nfi\n'
         b'echo "a\tb"\necho c 2>/dev/null\necho end\n')
# Shifted substitutions and an eval: no `never` line may count.
UNPROVABLE = ("n=$(awk '\nBEGIN { print 1 }\n')\nif false; then\n  echo never1\nfi\n"
              'msg=$(printf "%s\n%s\n" a b)\nif false; then\n  echo never2\nfi\nv=$(\n'
              "  echo 'a\nb'\n  echo c\n)\nif false; then\n  echo never3\nfi\nw=`\n  printf a\n"
              '  printf b\n`\nif false; then\n  echo never4\nfi\necho "x`\n  printf a\n'
              '  printf b\n`"\nif false; then\n  echo never5\nfi\nexport e=`\n  printf a\n'
              "  printf b\n`\nif false; then\n  echo never6\nfi\np=$(python3 -c '\nprint(1)\n')\n"
              'if false; then\n  echo never7\nfi\nwhile read -r l; do\n  :\ndone <<EOF\n$(\n'
              '  echo a\n  echo b\n)\nEOF\nif false; then\n  echo never8\nfi\nb=`printf a\n'
              '  printf b\n  printf c`\ncat <(\n  printf a\n  printf b\n) >/dev/null\n'
              'eval "$(printf \'export A=1\\nexport B=2\\nexport C=3\\n\')"\nif false; then\n'
              '  export NEVER=1\nfi\nexport A=1\ng() {\n  eval "x=1\n  y=2"\n  echo in-g\n}\ng\n'
              'echo end\n').encode()

# Commands bash prints rewritten, evals whose lines land on untaken ones or on a function's
# entry line, a subshell-bodied function and functions a trap runs.
REWRITTEN = ("IFS=$'\\n\\t'\nf() {\n  local nl=$'\\n'\n"
             '  eval "$(printf \'true\\ntrue\\ntrue\\ntrue\\n\')"\n  if false; then\n'
             "    echo never\n    echo never\n  fi\n}\nf\nprintf $'a\\tb\\n' >/dev/null\n"
             'echo $"hi" "${x:-$\'y\'}" >/dev/null\nt=/dev/null\n> "$t"\necho \'a\\\n'
             "b' >/dev/null\n(( n = 1 ))\n"
             'eval "$(printf \'((a=1))\\n((b=2))\\n((c=3))\\nexport C=3\\nNL=x\\n'
             '> /dev/null\\necho "xy" >/dev/null\\necho "xy" >/dev/null\\narr=x\\n\')"\n'
             "if false; then\n  (( never++ ))\n  export C=$(echo never)\n  NL=$'\\n'\n"
             '  > /dev/full\n  echo "x\ny"\n  arr=(1 2)\nfi\nrun() { "$@"; }\ng() {\n'
             '  eval "$(printf \':\\n:\\n:\\n\')"\n  if false; then\n    echo never\n  fi\n'
             '}\nrun g\nunused() {\n  echo never\n}\nsub() (\n  echo s\n)\nsub\n'
             'o() { eval "$(printf \':\\n:\\n:\\n\')"; }\nif false; then\n  echo never\nfi\n'
             "o\nprintf '%s$' a >/dev/null\necho \\$'c' >/dev/null\n: `printf $'q'`\n"
             'outer() {\n  eval "$(printf \'true\\ntrue\\ntrue\\ntrue\\n\')"\n  inner() {\n'
             '    echo never\n    echo never\n  }\n}\nouter\n'
             'b() { eval "$(printf \'true; true\\ntrue\\ntrue\\n\')"\n  if false; then\n'
             '    echo never\n    echo never\n  fi\n}\nb\n((m=1))\n'
             'eval "$(printf \':\\n:\\n(( a = 1 ))\\n[[ -n $m ]]\\nfor i in 1; do :; done\\n'
             'case x in esac\\nselect z in 1; do break; done <<< 1\\n\')"\n'
             'if false; then\n  (( never++ ))\n  [[ -z $m ]]\n'
             '  for f in never; do echo; done\n  case $1 in esac\n'
             '  select y in never; do exit; done\nfi\n[[ $m ]]\n[[ $m && $m ]]\n[[ $m || $m ]]\n'
             'set -- p; for i; do :; done\nhelper() {\n  echo h\n}\ncleanup() {\n  helper\n'
             '  echo c\n}\ntrap cleanup EXIT\n').encode()

BRANCHY = (b'#!/usr/bin/env bash\nset -eu\ncd "$(dirname "$0")"\nif [ "${1:-}" = deep ]; then\n'
           b'  echo deep\nelse\n  echo shallow\nfi\necho done\n')

# Backquoted substitutions no bash clamps, whose lines land on a subshell, a plain line
# and a brace group.
FORKED = (b"a=`\n  awk 'BEGIN { print 1 }'\n  printf a`\nfalse && ( echo never\n  printf a )\n"
          b"b=`\n  awk 'BEGIN { print 1 }'\n  printf a`\nfalse && echo never\nfalse && printf a\n"
          b"c=`\n  awk 'BEGIN { print 1 }'\n  printf a`\nfalse && { awk 'BEGIN { print 1 }'\n"
          b"  printf a; }\n")

# Each row runs every measurable line, so on Linux the lines real bash reports must equal the
# expected set too: the first set is what bash 5.2 reports, the second, where it differs, what
# bash 5.3 does (it reports a simple command and a case at their first word).
RULES = (
    ('blank, comment and shebang lines',
     '#!/usr/bin/env bash\n# comment\n\necho a  # trailing\n', {4}),
    ('if/then/elif/else/fi lines',
     'for x in 1 2 3; do\n  if [ "$x" = 1 ]\n  then\n    echo one\n'
     '  elif [ "$x" = 2 ]; then\n    echo two\n  else\n    echo three\n  fi\ndone\n',
     {1, 2, 4, 5, 6, 8}),
    ('do/done, braces and a heredoc after done',
     'while read -r line\ndo\n  {\n    echo "$line"\n  }\ndone <<\'EOF\'\na\nEOF\n'
     'until true; do :; done\n', {1, 4, 9}),
    ('case patterns and ;;',
     'for v in a b c; do\n  case $v in\n    a) echo a ;;\n    b | z)\n      echo b\n      ;;\n'
     '    *) echo other ;;\n  esac\ndone\n', {1, 2, 3, 5, 7}),
    ('heredoc bodies; a pipe ending the line reports at the delimiter',
     'cat <<EOF\nbody $(echo x)\nEOF\ncat <<-EOF | cat\n\tbody\n\tEOF\n'
     'cat <<\'A\'; cat <<"B"\na\nA\nb\nB\necho end\n', {1, 4, 6, 7, 12},
     {1, 4, 7, 12}),
    ('continuation lines',
     'echo one \\\n  two\necho \\\n  three\nFOO=1 \\\n  true\n> /dev/null \\\n  echo x\n'
     'true &&\n  echo after\necho a |\n  cat\n', {1, 4, 5, 7, 9, 10, 11, 12},
     {1, 3, 5, 7, 9, 10, 11, 12}),
    ('words spanning lines',
     'echo "multi\nline" tail\nx=$(\n  echo sub\n)\narr=(\n  1  # c )\n  2\n)\n'
     '[[ -n a &&\n  -n b ]]\n((\n  1\n))\necho $((\n  1 + 2\n)) z\ny=$( (echo in)\n)\n',
     {2, 5, 9, 11, 14, 17, 19},
     {1, 5, 9, 11, 14, 15, 19}),
    ('for, select and case subject lines',
     'for x in \\\n  a; do :; done\nselect s \\\n  in a; do break; done <<< 1\n'
     'case \\\n  x in x) : ;; esac\ncase "a\nb" in *) : ;; esac\n'
     'for ((i = 0;\n  i < 1; i++)); do :; done\nfor x in do \\\n  y; do\n  :\ndone\n'
     'for ((j = 0; j < 1; j++)) do\n  :\ndone\n', {1, 2, 3, 4, 6, 8, 9, 10, 11, 13, 15, 16},
     {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 16}),
    ('function entry at the opening brace',
     'f() {\n  echo f\n}\ng()\n{\n  echo g\n}\nfunction h\n{\n  echo h\n}\nk() (\n  echo k\n)\n'
     'f; g; h; k\n', {1, 2, 5, 6, 9, 10, 13, 15}),
    ('subshells, lists and prefixes',
     '(\n  echo paren\n)\necho a; echo b\n! false\ntime true\necho x &\nwait\n',
     {2, 4, 5, 6, 7, 8}),
    ('quotes, substitutions and a fall-through clause',
     'echo $\'say "hi\' "x$(echo \')\')" `echo y` ${z:-"}"} 2>/dev/null\ndiff <(echo a\n'
     '  ) <(echo a) > /dev/null\ncase 1 in\n  1) echo one ;&\n  2) echo two\nesac\n'
     'function q() {\n  echo q\n}\nq\n', {1, 3, 4, 5, 6, 8, 9, 11},
     {1, 2, 4, 5, 6, 8, 9, 11}),
    ('escapes, quotes in substitutions and a spaced heredoc delimiter',
     'echo \\\'x "a\\"b"\necho \'y"\'\necho "$(echo \'"\')"\nx=$(echo \')\'\n)\n'
     'cat << EOF\nbody\nEOF\necho z\n', {1, 2, 3, 5, 6, 9}),
    ('an ANSI quote inside a substitution', 'y=$(echo $\'it\\\'s\')\necho "$y"\necho more\n',
     {1, 2, 3}),
    ('a function holding definitions reports the last body it holds',
     'outer() {\n  g() { :; }\n  echo o\n}\nouter\na() {\n  b() {\n    c() { :; }\n  }\n  b\n'
     '  c\n}\na\no() {\n  { echo x; }\n  g() { :; }\n}\no\n', {2, 3, 5, 8, 10, 11, 13, 15, 16, 18}),
    ('a case inside a substitution and $\' inside double quotes',
     'x=$(\n  case a in\n    a) echo y ;;\n  esac\n)\necho "cost $\'5"\necho end\n'
     'z=$(echo case)\necho "$z"\n', {5, 6, 7, 8, 9}),
    ('functions a trap runs',
     'cleanup() {\n  echo c\n}\ntrap cleanup EXIT\nonerr() {\n  echo e\n}\ntrap onerr ERR\n'
     'false || :\nls /nonexistent-test-ratchet 2>/dev/null || :\n(exit 3)\necho a\n',
     {1, 2, 4, 5, 6, 8, 9, 10, 11, 12}),
)

# The hits bash 5.3 gives where they differ from 5.2's: a command spanning lines counts on
# its first word's line, and a substitution opening one counts on its parent's line.
NEWER = {
    'COINCIDENCES': {
        4: 3, 5: 1, 6: 0, 7: 0, 12: 3, 13: 1, 14: 0, 15: 0},
    'VALIDATED': {
        1: 2, 2: 1, 3: 0, 4: 0, 5: 0, 7: 1, 10: 2, 14: 1, 15: 6, 16: 2, 18: 2, 20: 1, 22:
        1, 24: 1, 26: 1, 27: 2, 28: 1, 29: 1, 30: 2, 31: 1, 32: 1, 33: 1, 34: 1, 35: 3,
        36: 2, 37: 1, 38: 2, 39: 1, 41: 2, 42: 1, 43: 0, 44: 0, 46: 1, 47: 1, 48: 1, 49:
        1, 50: 1, 53: 1, 54: 0, 55: 0},
    'EVALS': {
        1: 1, 2: 1, 4: 1, 6: 1, 9: 2, 10: 2, 11: 1, 12: 0, 14: 1, 15: 1, 16: 1},
    'UNPROVABLE': {
        3: 2, 4: 1, 5: 0, 9: 2, 10: 1, 11: 0, 17: 3, 18: 1, 19: 0, 24: 1, 25: 1, 26: 0,
        28: 1, 32: 1, 33: 0, 35: 1, 39: 1, 40: 0, 44: 2, 45: 1, 46: 0, 48: 3, 49: 2, 56:
        1, 57: 0, 61: 4, 62: 3, 66: 2, 67: 1, 68: 0, 70: 1, 71: 1, 72: 1, 74: 1, 76: 1,
        77: 1},
    'REWRITTEN': {
        1: 1, 2: 1, 3: 1, 4: 2, 5: 1, 6: 0, 7: 0, 10: 1, 11: 1, 12: 1, 13: 1, 14: 1, 15:
        1, 17: 1, 18: 2, 19: 1, 20: 0, 21: 0, 22: 0, 23: 0, 24: 0, 26: 0, 28: 2, 29: 1,
        30: 2, 31: 1, 32: 0, 35: 1, 36: 0, 37: 0, 40: 1, 42: 1, 43: 4, 44: 1, 45: 0, 47:
        1, 48: 1, 49: 1, 50: 1, 52: 2, 53: 2, 54: 0, 55: 0, 58: 1, 59: 5, 60: 1, 61: 0,
        62: 0, 65: 1, 66: 1, 67: 2, 68: 1, 69: 0, 70: 0, 71: 0, 72: 0, 73: 0, 75: 1, 76:
        1, 77: 1, 78: 3, 79: 1, 80: 1, 82: 1, 83: 1, 84: 1, 86: 1},
}
