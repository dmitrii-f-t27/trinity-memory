#!/bin/sh
# Ternary Check Live (issues #48, #55): build tests/upstream/gguf_replay.c
# against the GGUF reader of every runtime pinned in specs/runtimes/*.json
# (each repository fetched at its pinned commit, ggml built for the CPU), so
# that `python3 -m trinity_memory.live --replay build/replay` can feed every
# cached header to the real readers and compare them with the t27 verdicts.
set -eu
cd "$(dirname "$0")/.."
out=build/replay
mkdir -p "$out"
for spec in specs/runtimes/*.json; do
    name=$(basename "$spec" .json)
    repo=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['repo'])" "$spec")
    commit=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['commit'])" "$spec")
    src="$out/src-$name"
    if [ "$(git -C "$src" rev-parse -q --verify HEAD 2>/dev/null || true)" != "$commit" ]; then
        rm -rf "$src"
        git init -q "$src"
        git -C "$src" remote add origin "https://github.com/$repo.git"
        git -C "$src" fetch -q --depth 1 origin "$commit"
        git -C "$src" checkout -q --detach FETCH_HEAD
    fi
    cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release -DGGML_METAL=OFF -DGGML_BLAS=OFF \
        -DGGML_ACCELERATE=OFF -DGGML_CUDA=OFF -DGGML_VULKAN=OFF -DGGML_OPENMP=OFF -DGGML_NATIVE=OFF \
        -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TOOLS=OFF \
        -DLLAMA_BUILD_SERVER=OFF > "$out/$name-cmake.log" 2>&1
    cmake --build "$src/build" --target ggml -j 4 > "$out/$name-build.log" 2>&1
    lib="$src/build/bin"
    [ -d "$lib" ] || lib="$src/build/ggml/src"
    ${CC:-cc} -O1 -I "$src/ggml/include" tests/upstream/gguf_replay.c -L "$lib" -lggml -lggml-base \
        -Wl,-rpath,"$(cd "$lib" && pwd)" -o "$out/gguf_replay_$name"
    echo "built $out/gguf_replay_$name ($repo@$commit)"
done
