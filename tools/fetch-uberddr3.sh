#!/bin/sh
# Fetch the UberDDR3 files named in a lock file at its pinned commit and check
# every sha256 (issue #60). Nothing fetched here is committed: UberDDR3 is
# GPL-3.0-or-later and this repository is Apache-2.0.
#
#   sh tools/fetch-uberddr3.sh [LOCK] [OUTDIR]
#
# Defaults: fpga/ax7203/ddr3/uberddr3.lock and build/uberddr3. A file already in
# OUTDIR with the right hash is kept; any mismatch (download or existing file)
# stops with an error and leaves no file with the wrong content in OUTDIR.
# UBERDDR3_BASE_URL overrides the raw-file base (default
# https://raw.githubusercontent.com/<owner>/<repo>/<commit>), e.g. a local mirror.
set -eu
cd "$(dirname "$0")/.."
lock=${1:-fpga/ax7203/ddr3/uberddr3.lock}
out=${2:-build/uberddr3}
[ -f "$lock" ] || { echo "fetch-uberddr3: no lock file $lock" >&2; exit 1; }

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
    else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

repo=$(awk '$1 == "repository" { print $2 }' "$lock")
commit=$(awk '$1 == "commit" { print $2 }' "$lock")
license=$(awk '$1 == "license" { print $2 }' "$lock")
echo "$commit" | grep -Eq '^[0-9a-f]{40}$' || { echo "fetch-uberddr3: bad commit '$commit' in $lock" >&2; exit 1; }
case "$repo" in
    https://github.com/*/*) slug=${repo#https://github.com/} ;;
    *) echo "fetch-uberddr3: repository must be a github.com URL, not '$repo'" >&2; exit 1 ;;
esac
base=${UBERDDR3_BASE_URL:-https://raw.githubusercontent.com/$slug/$commit}
mkdir -p "$out"
# SOURCE marks a complete, verified fetch: it is removed first and written last.
rm -f "$out/SOURCE"
count=0
awk '$1 == "file" { print $2, $3 }' "$lock" > "$out/.files"
while read -r want path; do
    echo "$want" | grep -Eq '^[0-9a-f]{64}$' || { echo "fetch-uberddr3: bad sha256 for $path" >&2; exit 1; }
    case "$path" in /*|*..*) echo "fetch-uberddr3: unsafe path $path" >&2; exit 1 ;; esac
    dest="$out/$path"
    mkdir -p "$(dirname "$dest")"
    if [ -f "$dest" ] && [ "$(sha256_of "$dest")" = "$want" ]; then
        count=$((count + 1)); continue
    fi
    rm -f "$dest" "$dest.part"
    curl -sSfL --retry 3 -o "$dest.part" "$base/$path" || { rm -f "$dest.part"; echo "fetch-uberddr3: download failed: $base/$path" >&2; exit 1; }
    got=$(sha256_of "$dest.part")
    if [ "$got" != "$want" ]; then
        rm -f "$dest.part"
        echo "fetch-uberddr3: sha256 mismatch for $path: expected $want, got $got" >&2
        exit 1
    fi
    mv "$dest.part" "$dest"
    count=$((count + 1))
done < "$out/.files"
rm -f "$out/.files"
[ "$count" -gt 0 ] || { echo "fetch-uberddr3: no files listed in $lock" >&2; exit 1; }
printf 'repository %s\ncommit %s\nlicense %s\n' "$repo" "$commit" "$license" > "$out/SOURCE"
echo "UberDDR3 $commit ($license): $count files verified in $out"
