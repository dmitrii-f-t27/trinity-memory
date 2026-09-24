#!/bin/sh
# Print the revision of a tool or database directory for a build report (issue #60).
#
#   sh tools/git-revision.sh [--describe] DIR
#
# Order: the commit named in DIR/ASSEMBLED-*.txt (a database assembled from files,
# not cloned: its line "commit <sha>"), then git, but only when DIR is itself the
# top of a work tree or a submodule checkout. A plain directory inside some other
# repository (for example under this repository's gitignored build/) would
# otherwise report that repository's HEAD. Prints "unknown" when neither applies.
# --describe prints `git describe --tags --always` instead of the full commit.
set -u
describe=0
if [ "${1:-}" = "--describe" ]; then describe=1; shift; fi
dir=${1:?usage: git-revision.sh [--describe] DIR}
if [ ! -d "$dir" ]; then echo unknown; exit 0; fi
for f in "$dir"/ASSEMBLED-*.txt; do
    [ -f "$f" ] || continue
    rev=$(awk '$1 == "commit" { print $2; exit }' "$f")
    if [ -n "$rev" ]; then
        if [ "$describe" = 1 ]; then echo "$rev (assembled)"; else echo "$rev"; fi
        exit 0
    fi
done
top=$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null) || top=
here=$(cd "$dir" && pwd -P)
if [ -n "$top" ] && [ "$(cd "$top" && pwd -P)" = "$here" ]; then
    if [ "$describe" = 1 ]; then
        git -C "$dir" describe --tags --always 2>/dev/null && exit 0
    else
        git -C "$dir" rev-parse --verify -q HEAD 2>/dev/null && exit 0
    fi
fi
echo unknown
