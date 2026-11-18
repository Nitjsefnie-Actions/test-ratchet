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

BRANCHY = (b'#!/usr/bin/env bash\nset -eu\ncd "$(dirname "$0")"\nif [ "${1:-}" = deep ]; then\n'
           b'  echo deep\nelse\n  echo shallow\nfi\necho done\n')
