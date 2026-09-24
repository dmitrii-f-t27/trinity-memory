#!/bin/sh
# Ternary Check Live (issues #48, #55): build tests/upstream/gguf_replay.c
# against the GGUF reader of every runtime pinned in specs/runtimes/*.json
# (each repository fetched at its pinned commit, ggml built for the CPU), so
# that `python3 -m trinity_memory.live --replay build/replay` can feed every
# cached header to the real readers and compare them with the t27 verdicts.
# Only ggml-base is built: it holds gguf.cpp and the type traits, all the
# replay needs. bitnet.cpp's llama.cpp lists kernel sources from its parent
# repository (../../../../src/ggml-bitnet-*.cpp) in the CPU backend; they are
# not part of the reader, so empty stand-ins let CMake configure. A runtime
# that fails to build is reported and skipped (its old binary is removed
# first, so a stale one is never replayed); the script fails only when none
# builds.
set -u
cd "$(dirname "$0")/.."
out=build/replay
mkdir -p "$out"
built=0
build() {
    name=$1 repo=$2 commit=$3
    src="$out/src-$name"
    echo "$name: $repo@$commit"
    rm -f "$out/gguf_replay_$name"
    if [ "$(git -C "$src" rev-parse -q --verify HEAD 2>/dev/null || true)" != "$commit" ]; then
        rm -rf "$src"
        git init -q "$src" &&
        git -C "$src" remote add origin "https://github.com/$repo.git" &&
        git -C "$src" fetch -q --depth 1 origin "$commit" &&
        git -C "$src" checkout -q --detach FETCH_HEAD || return 1
    fi
    mkdir -p "$out/src"
    for stub in ggml-bitnet-lut.cpp ggml-bitnet-mad.cpp; do [ -f "$out/src/$stub" ] || : > "$out/src/$stub"; done
    cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release -DGGML_METAL=OFF -DGGML_BLAS=OFF \
        -DGGML_ACCELERATE=OFF -DGGML_CUDA=OFF -DGGML_VULKAN=OFF -DGGML_OPENMP=OFF -DGGML_NATIVE=OFF \
        -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TOOLS=OFF \
        -DLLAMA_BUILD_SERVER=OFF > "$out/$name-cmake.log" 2>&1 || { tail -40 "$out/$name-cmake.log" >&2; return 1; }
    cmake --build "$src/build" --target ggml-base -j 4 > "$out/$name-build.log" 2>&1 || { tail -40 "$out/$name-build.log" >&2; return 1; }
    lib="$src/build/bin"
    [ -f "$lib/libggml-base.so" ] || [ -f "$lib/libggml-base.dylib" ] || lib="$src/build/ggml/src"
    ${CC:-cc} -O1 -I "$src/ggml/include" tests/upstream/gguf_replay.c -L "$lib" -lggml-base \
        -Wl,-rpath,"$(cd "$lib" && pwd)" -o "$out/gguf_replay_$name" || return 1
    echo "built $out/gguf_replay_$name"
}
for spec in specs/runtimes/*.json; do
    name=$(basename "$spec" .json)
    repo=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['repo'])" "$spec")
    commit=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['commit'])" "$spec")
    if build "$name" "$repo" "$commit"; then built=$((built + 1)); else echo "FAILED $name" >&2; fi
done
[ "$built" -gt 0 ]
