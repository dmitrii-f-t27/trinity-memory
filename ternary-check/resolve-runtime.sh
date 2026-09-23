#!/usr/bin/env bash
# Resolves the Python package that runs the Ternary Check (ternary-check/action.yml).
#   RUNTIME=release  the platform wheel of the version in pyproject.toml next to this
#                    directory, i.e. the Action's ref, from RELEASE_URL/vVERSION/,
#                    checked against that release's SHA256SUMS, unpacked, no pip. The
#                    tests set TERNARY_CHECK_RELEASE_VERSION to another version, and
#                    TERNARY_CHECK_MACOS_VERSION in place of sw_vers; action.yml
#                    clears both, so a caller's environment cannot redirect the Action.
#   RUNTIME=PATH     a directory holding trinity_memory/ with its native runtime: a
#                    source checkout after tools/build-t27.sh, or an unpacked wheel
# Prints pythonpath=DIR, also to $GITHUB_OUTPUT when set. Needs bash, curl and PYTHON.
set -euo pipefail
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
python=${PYTHON:-python3}
runtime=${RUNTIME:-release}
release_url=${RELEASE_URL:-https://github.com/dmitrii-f-t27/trinity-memory/releases/download}
fail() { echo "::error title=ternary-check runtime::$*" >&2; exit 1; }

"$python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' ||
    fail "$python is older than Python 3.10; set the python input"

if [ "$runtime" = release ]; then
    version=${TERNARY_CHECK_RELEASE_VERSION:-}
    if [ -z "$version" ]; then
        version=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$here/../pyproject.toml")
    fi
    [ -n "$version" ] || fail "cannot read the version from $here/../pyproject.toml"
    case "${RUNNER_OS:-$(uname -s)}-${RUNNER_ARCH:-$(uname -m)}" in
        Linux-X64 | Linux-x86_64) pattern='-py3-none-(manylinux[0-9_]*_x86_64|linux_x86_64)\.whl$' ;;
        macOS-ARM64 | Darwin-arm64) pattern='-py3-none-macosx_[0-9]+_[0-9]+_arm64\.whl$' ;;
        *) fail "v$version publishes wheels for Linux x86_64 and macOS arm64 only; on ${RUNNER_OS:-$(uname -s)} ${RUNNER_ARCH:-$(uname -m)} build from source and set runtime to the checkout" ;;
    esac
    dir="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/ternary-check-runtime-$version"
    rm -rf "$dir"
    mkdir -p "$dir"
    base="$release_url/v$version"
    curl -fsSL --retry 3 -o "$dir/SHA256SUMS" "$base/SHA256SUMS" || fail "cannot download $base/SHA256SUMS"
    names=$(awk '{ name = $2; sub(/^\*/, "", name); print name }' "$dir/SHA256SUMS")
    escaped=$(printf '%s' "$version" | sed 's/\./\\./g')
    wheel=$(printf '%s\n' "$names" | grep -E "^trinity_ternary_memory-$escaped$pattern" | sort | head -n 1 || true)
    [ -n "$wheel" ] || fail "SHA256SUMS of v$version lists no wheel for this platform"
    # A macosx_M_N tag names the oldest macOS the wheel's binaries load on.
    need=$(printf '%s\n' "$wheel" | sed -n 's/.*-macosx_\([0-9]*\)_[0-9]*_arm64\.whl$/\1/p')
    if [ -n "$need" ]; then
        have=${TERNARY_CHECK_MACOS_VERSION:-$(sw_vers -productVersion 2>/dev/null || true)}
        [ -z "$have" ] || [ "${have%%.*}" -ge "$need" ] ||
            fail "$wheel needs macOS $need or later; this runner has macOS $have (use a newer runner, or build from source and set runtime to the checkout)"
    fi
    curl -fsSL --retry 3 -o "$dir/$wheel" "$base/$wheel" || fail "cannot download $base/$wheel"
    expected=$(awk -v f="$wheel" '{ name = $2; sub(/^\*/, "", name); if (name == f) print $1 }' "$dir/SHA256SUMS")
    if command -v sha256sum >/dev/null 2>&1; then
        actual=$(sha256sum "$dir/$wheel" | awk '{ print $1 }')
    else
        actual=$(shasum -a 256 "$dir/$wheel" | awk '{ print $1 }')
    fi
    [ "$expected" = "$actual" ] || fail "SHA-256 of $wheel is $actual; SHA256SUMS of v$version says $expected"
    echo "verified $wheel ($actual) against SHA256SUMS of v$version"
    "$python" -m zipfile -e "$dir/$wheel" "$dir/site"
    # zipfile does not restore modes; the Ternary Check needs only the library.
    chmod +x "$dir/site/trinity_memory/_native_runtime/trinity-memory-t27" 2>/dev/null || true
    pythonpath="$dir/site"
else
    pythonpath=$(cd "$runtime" 2>/dev/null && pwd) || fail "runtime $runtime is neither release nor a directory"
fi
[ -f "$pythonpath/trinity_memory/ternary_contract.py" ] ||
    fail "$pythonpath has no trinity_memory package with the Ternary Check contract"
PYTHONPATH="$pythonpath" "$python" -c 'from trinity_memory import ternary_contract as t; assert t.error_token(-50) == "format"' ||
    fail "the native t27 library under $pythonpath does not load (build it with tools/build-t27.sh)"
echo "pythonpath=$pythonpath"
if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "pythonpath=$pythonpath" >> "$GITHUB_OUTPUT"
fi
