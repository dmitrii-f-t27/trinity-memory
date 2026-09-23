#!/bin/sh
# Fetch pinned upstream source files named by a lock file into build/upstream/
# and verify each against its sha256. Nothing fetched here is committed: the
# sources keep their own licenses and are only compiled by test harnesses.
#   sh tools/fetch-upstream.sh [tests/upstream/llama.cpp.lock.json]
# A file already present with the locked sha256 is not downloaded again. With
# TRINITY_UPSTREAM_OFFLINE=1 nothing is downloaded: a missing file or one with
# another sha256 fails.
set -eu
cd "$(dirname "$0")/.."
lock=${1:-tests/upstream/llama.cpp.lock.json}
[ -f "$lock" ] || { echo "No lock file: $lock" >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then
    digest() { sha256sum "$1" | cut -d ' ' -f 1; }
else
    digest() { shasum -a 256 "$1" | cut -d ' ' -f 1; }
fi
# The lock is JSON; python3 only lists "repo commit directory path sha256".
entries=$("${PYTHON:-python3}" -c '
import json, sys
lock = json.load(open(sys.argv[1]))
for path, meta in sorted(lock["files"].items()):
    print(lock["repo"], lock["commit"], lock["directory"], path, meta["sha256"])
' "$lock")
[ -n "$entries" ] || { echo "$lock lists no files" >&2; exit 1; }
printf '%s\n' "$entries" | while read -r repo commit directory path sha; do
    target="build/upstream/$directory/$path"
    if [ -f "$target" ] && [ "$(digest "$target")" = "$sha" ]; then
        echo "ok (cached) $repo@$commit $path"
        continue
    fi
    if [ "${TRINITY_UPSTREAM_OFFLINE:-0}" = 1 ]; then
        echo "TRINITY_UPSTREAM_OFFLINE=1: $target is missing or differs from the lock ($repo@$commit $path)" >&2
        exit 1
    fi
    mkdir -p "$(dirname "$target")"
    url="https://raw.githubusercontent.com/$repo/$commit/$path"
    curl -fsSL --retry 3 -o "$target.tmp" "$url"
    actual=$(digest "$target.tmp")
    if [ "$actual" != "$sha" ]; then
        rm -f "$target.tmp"
        echo "sha256 mismatch for $url: expected $sha, got $actual" >&2
        exit 1
    fi
    mv "$target.tmp" "$target"
    echo "ok (fetched) $repo@$commit $path"
done
