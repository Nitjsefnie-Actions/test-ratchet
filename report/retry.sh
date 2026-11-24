#!/usr/bin/env bash
set -e
shopt -s nullglob
files=("$TEST_RATCHET_ARTIFACTS"/*)
if [ "$TEST_RATCHET_OUTCOME" = success ] && [ "${#files[@]}" -gt 0 ]; then
  exit 0
fi
echo "test-ratchet report: the download step ended in $TEST_RATCHET_OUTCOME and left" \
  "${#files[@]} entries: downloading again in $1 s"
rm -rf "$TEST_RATCHET_ARTIFACTS"
mkdir -m 700 "$TEST_RATCHET_ARTIFACTS"
sleep "$1"
echo 'retry=true' >> "$GITHUB_OUTPUT"
