PYTHON ?= python3

.PHONY: check test rtl report
check: test rtl

test:
	$(PYTHON) -m unittest discover -s tests -v

rtl:
	$(PYTHON) scripts/test_rtl.py

report:
	$(PYTHON) -m trinity_memory benchmark --count 65536 --repeats 3
	$(PYTHON) scripts/render_report.py
