# shellcheck shell=bash disable=SC2064,SC2154
{
  _test_ratchet_start=$_ _test_ratchet_env=${TEST_RATCHET_SHELL_BASH_ENV-}
  [[ -n $_test_ratchet_env && -r $_test_ratchet_env ]] || _test_ratchet_env=
} 2>/dev/null
${_test_ratchet_env:+.} ${_test_ratchet_env:+"$_test_ratchet_env"}
{
  if [[ -n ${TEST_RATCHET_SHELL_LOG_DIR-} && $- != *v* ]]; then
    _test_ratchet_log=$TEST_RATCHET_SHELL_LOG_DIR _test_ratchet_pid='' _test_ratchet_raw=''
    _test_ratchet_cmd='' _test_ratchet_true='' _test_ratchet_fresh='' _test_ratchet_at=" $0" _test_ratchet_hook=${BASH_SOURCE[0]-}
    declare -A _test_ratchet_abs=()
    # No function: a function body here would become the line a subshell-bodied function
    # reports. The action is joined to one line: a newline in it shifts every later LINENO.
    read -r -d '' _test_ratchet_action <<'EOF' || :
{ _test_ratchet_u=$_; if [[ ${BASH_SOURCE[0]-} != "$_test_ratchet_hook" ]]; then
  _test_ratchet_s=${BASH_SOURCE[0]-} _test_ratchet_n=$LINENO _test_ratchet_f= _test_ratchet_g=
  _test_ratchet_prev=$_test_ratchet_at _test_ratchet_at="${FUNCNAME[*]-} ${BASH_SOURCE[0]-}" _test_ratchet_d=${BASH_SOURCE[*]+${#BASH_SOURCE[@]}};
  if [[ $_test_ratchet_pid != "$BASHPID" ]]; then
    _test_ratchet_pid=$BASHPID _test_ratchet_base=- _test_ratchet_clamp=
    _test_ratchet_first=$_test_ratchet_n _test_ratchet_fc=${BASH_COMMAND%%$'\n'*};
    [[ $_test_ratchet_at != "$_test_ratchet_prev" ]] || _test_ratchet_base=$_test_ratchet_at;
    if [[ $_test_ratchet_n == "$_test_ratchet_raw" && $_test_ratchet_cmd == *"$BASH_COMMAND"* &&
          ( $_test_ratchet_cmd == *'$('* || $_test_ratchet_cmd == *'`'* ||
            $_test_ratchet_cmd == *[\<\>]\(* ) ]]; then
      _test_ratchet_clamp=$_test_ratchet_true;
    fi;
  fi;
  _test_ratchet_raw=$_test_ratchet_n _test_ratchet_cmd=$BASH_COMMAND;
  if (( BASH_SUBSHELL )) && [[ $_test_ratchet_at == "$_test_ratchet_base" ]]; then
    if [[ -n $_test_ratchet_clamp ]]; then _test_ratchet_n=$_test_ratchet_clamp _test_ratchet_f=c;
    else _test_ratchet_f=$_test_ratchet_first _test_ratchet_g=${_test_ratchet_fc//$'\t'/ }; fi;
  fi;
  _test_ratchet_true=$_test_ratchet_n;
  if [[ -n $_test_ratchet_s && $_test_ratchet_s != /* ]]; then
    if [[ -z ${_test_ratchet_abs[$_test_ratchet_s]+x} ||
          $_test_ratchet_d == "$_test_ratchet_fresh" ]]; then
      _test_ratchet_abs[$_test_ratchet_s]=${PWD-}/$_test_ratchet_s;
    fi;
    _test_ratchet_s=${_test_ratchet_abs[$_test_ratchet_s]};
  fi;
  _test_ratchet_fresh=;
  _test_ratchet_c=${BASH_COMMAND#builtin };
  [[ $_test_ratchet_c != 'source '* && $_test_ratchet_c != '. '* ]] ||
    _test_ratchet_fresh=$((_test_ratchet_d + 1));
  _test_ratchet_w=${BASH_COMMAND%%$'\n'*};
  [[ -z $_test_ratchet_s ]] || builtin printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$_test_ratchet_s"
    "$_test_ratchet_n" "$_test_ratchet_f" "${_test_ratchet_w//$'\t'/ }" "$_test_ratchet_g"
    "${FUNCNAME[0]-}"
    >>"$_test_ratchet_log/$BASHPID" || :;
  fi; builtin : "$_test_ratchet_u"; } 2>/dev/null
EOF
    set -o functrace
    trap "${_test_ratchet_action//$'\n'/ }" DEBUG
  fi
  builtin : "$_test_ratchet_start"
} 2>/dev/null
