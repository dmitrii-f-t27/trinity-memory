# Trinity arithmetic kit for ProcessorCI. Needs Python 3 (standard library) and Icarus Verilog.
# `make check` is what CI runs; `make sim` is enough to reproduce the results without the t27 compiler.
PY    ?= python3
BUILD ?= build
GEN   := rtl/generated
UNITS := $(GEN)/fp8_e4m3_decode.v $(GEN)/fp8_e4m3_encode.v $(GEN)/dense5_decoder.v $(GEN)/baseline5_decoder.v $(GEN)/sparse41_decoder.v

.PHONY: vectors check-vectors check-generated sim sim-fp8 sim-harness lint-wrapper synth-stat check clean

vectors:
	$(PY) model/e4m3_ref.py && $(PY) model/ternary_ref.py && $(PY) model/make_vectors.py

check-vectors: vectors
	git diff --exit-code -- vectors

check-generated:
	$(PY) tools/sync_generated.py --check

sim: sim-fp8 sim-harness

sim-fp8:
	mkdir -p $(BUILD)
	iverilog -g2005 -I vectors -o $(BUILD)/tb_fp8 sim/tb_fp8_e4m3.v $(GEN)/fp8_e4m3_decode.v $(GEN)/fp8_e4m3_encode.v
	cd vectors && vvp ../$(BUILD)/tb_fp8 | tee ../$(BUILD)/sim-fp8.log
	grep -q '^ALL PASS' $(BUILD)/sim-fp8.log

sim-harness:
	mkdir -p $(BUILD)
	iverilog -g2005 -o $(BUILD)/tb_harness sim/tb_harness.v rtl/trinity_arith_harness.v $(UNITS)
	cd vectors && vvp ../$(BUILD)/tb_harness | tee ../$(BUILD)/sim-harness.log
	grep -q '^ALL PASS' $(BUILD)/sim-harness.log

lint-wrapper:
	mkdir -p $(BUILD)
	iverilog -g2012 -DSIMULATION -s processorci_top -o $(BUILD)/wrapper processorci/rtl/trinity_arith.sv rtl/trinity_arith_harness.v $(UNITS)

synth-stat:
	mkdir -p $(BUILD)
	yosys -q -p "read_verilog $(UNITS) rtl/trinity_arith_harness.v; synth_xilinx -family xc7 -top trinity_arith_harness; tee -o $(BUILD)/synth-stat.txt stat"

check: check-vectors check-generated sim lint-wrapper

clean:
	rm -rf $(BUILD)
