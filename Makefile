PYTHON ?= python3

.PHONY: check test rtl report stack demo
check: test rtl

test:
	$(PYTHON) -m unittest discover -s tests -v

rtl:
	$(PYTHON) scripts/test_rtl.py
	$(PYTHON) scripts/test_dot_rtl.py

stack:
	$(PYTHON) -m trinity_memory conformance --rtl

demo:
	$(PYTHON) -m trinity_memory edge-demo --rtl

report:
	$(PYTHON) -m trinity_memory benchmark --count 65536 --repeats 3
	$(PYTHON) scripts/render_report.py

.PHONY: t27 t27-test check-specs
t27:
	sh tools/build-t27.sh

t27-test:
	PYTHON=$(PYTHON) sh tools/test-t27.sh

check-specs:
	PYTHON=$(PYTHON) sh tools/check-specs.sh

# llama.cpp issue 15193: upstream TQ1_0/TQ2_0 storage and CPU kernels against
# the t27 decoders (docs/upstream/llama.cpp-15193.md). Fetches pinned sources.
.PHONY: upstream-15193
upstream-15193: t27
	PYTHON=$(PYTHON) sh tests/upstream/run-llamacpp-15193.sh

# Ternary Check (issue #32) from a clean clone: fetch the pinned fixture
# ranges (strict), build the t27 library, store the BitNet tensors with the
# pinned llama.cpp quantizers, and write reports/ternary-check.json,
# reports/ternary-check.html and reports/ternary-check/repro/. OFFLINE=1 uses
# the caches only; ternary-check-verify compares with the committed files.
.PHONY: ternary-check ternary-check-verify
ternary-check:
	PYTHON=$(PYTHON) OFFLINE=$(OFFLINE) sh tools/ternary-check.sh

ternary-check-verify:
	PYTHON=$(PYTHON) OFFLINE=$(OFFLINE) sh tools/ternary-check.sh --check

# Ternary Check Live (issue #48): every public ternary GGUF on the Hugging Face
# Hub, checked from its header with the t27 verdicts (anonymous, throttled;
# headers cached in build/live/headers, report in build/live/scan.json).
.PHONY: live-scan live-replay
MIN_DOWNLOADS ?= 100
live-scan:
	$(PYTHON) -m trinity_memory.live --min-downloads $(MIN_DOWNLOADS)

# Feed the cached headers of build/live/scan.json to the real GGUF readers of
# the pinned runtimes (built by tools/live-replay.sh) and record the agreement.
live-replay:
	sh tools/live-replay.sh
	$(PYTHON) -m trinity_memory.live --replay build/replay
