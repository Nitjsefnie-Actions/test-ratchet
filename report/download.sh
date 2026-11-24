#!/usr/bin/env bash
set -e
shopt -s nullglob
for pause in 5 10 20 40 ''; do
  status=0
  "$TEST_RATCHET_PYTHON" -I "$GITHUB_ACTION_PATH/download.py" || status=$?
  files=("$TEST_RATCHET_ARTIFACTS"/*)
  if [ -z "$pause" ] || { [ "$status" -eq 0 ] && [ "${#files[@]}" -gt 0 ]; }; then
    exit "$status"
  fi
  echo "test-ratchet report: the download ended with status $status and left" \
    "${#files[@]} entries: downloading again in $pause s"
  rm -rf "$TEST_RATCHET_ARTIFACTS"
  mkdir -m 700 "$TEST_RATCHET_ARTIFACTS"
  sleep "$pause"
done
