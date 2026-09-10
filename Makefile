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

.PHONY: t27 t27-test
t27:
	sh tools/build-t27.sh

t27-test:
	PYTHON=$(PYTHON) sh tools/test-t27.sh
