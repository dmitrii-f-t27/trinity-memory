# AX7203 (XC7A200T-FBG484-2) trace player: generate, simulate, synthesize, place
# and route, pack, flash, capture. The open flow (yosys, nextpnr-xilinx, prjxray)
# runs inside the regymm/openxc7 image, the same recipe gHashTag/trinity-fpga uses
# for this board; flashing and capture run on the host with openFPGALoader and
# pyserial. Every target is reproducible from a clean checkout.
#
#   make -C fpga/ax7203 gen        generated cores (pinned t27c) + ROM bank + manifest
#   make -C fpga/ax7203 sim        Icarus run of the whole player, host comparison
#   make -C fpga/ax7203 bit        yosys -> nextpnr-xilinx -> fasm2frames -> xc7frames2bit
#   make -C fpga/ax7203 flash      openFPGALoader over the board's FT232H (SRAM only)
#   make -C fpga/ax7203 capture    UART capture compared with the reference
ROOT      := $(abspath $(CURDIR)/../..)
BUILD     := $(ROOT)/build/fpga
RTL       := $(ROOT)/build/t27/specs/rtl
T27C      ?= $(T27_ROOT)/target/release/t27c
PART      ?= xc7a200tfbg484-2
DBPART    := xc7a200t
TOP       := tms_trace_player_ax7203
IMAGE     ?= regymm/openxc7:latest
DOCKER    ?= docker run --rm -v "$(ROOT):/work" -w /work $(IMAGE)
CHIPDB    ?= $(BUILD)/chipdb/$(PART).bin
SEEDS     ?= 1 2 3 4 5 6
PLACERS   ?= sa heap
YOSYS_SYNTH ?= synth_xilinx -flatten -abc9 -nocarry -nodsp -nowidelut -nosrl -family xc7
FREQ_MHZ  ?= 200.0
PORT      ?= /dev/cu.usbserial-10
CABLE     ?= digilent_hs2
BUILD_ID  ?= $(shell git -C $(ROOT) rev-parse --short=8 HEAD)
CLOCK_MODE ?= 1
SOURCES   := $(RTL)/dot_stream.v $(RTL)/stream_storage.v $(RTL)/stream_view.v \
             $(CURDIR)/tms_dut_wrappers.v $(CURDIR)/tms_uart_tx.v $(BUILD)/tms_trace_bank.v $(CURDIR)/tms_trace_player.v
REL       = $(patsubst $(ROOT)/%,%,$(1))

.PHONY: gen sim synth chipdb pnr bit flash capture detect clean

gen: $(BUILD)/tms_trace_bank.v $(RTL)/dot_stream.v $(RTL)/stream_storage.v $(RTL)/stream_view.v

$(BUILD)/tms_trace_bank.v $(BUILD)/tms_trace_manifest.json: $(ROOT)/conformance/memory_stream_compute.json $(ROOT)/tools/generate-fpga-trace-player.py
	mkdir -p $(BUILD)
	python3 $(ROOT)/tools/generate-fpga-trace-player.py --output-dir $(BUILD)

# One generated core per executable source; regenerated whenever the source changes.
$(RTL)/%.v: $(ROOT)/t27/rtl/%.t27
	@test -x "$(T27C)" || { echo "T27_ROOT must point at the pinned compiler checkout (native/compiler.lock)"; exit 1; }
	mkdir -p $(RTL)
	cd $(ROOT) && "$(T27C)" gen-verilog t27/rtl/$*.t27 > $@

sim: gen
	mkdir -p $(BUILD)/sim
	@for mode in 1 0; do \
	  iverilog -g2012 -Ptb_fpga_trace_player.CLOCK_MODE=$$mode -o $(BUILD)/sim/player_$$mode.vvp -s tb_fpga_trace_player $(CURDIR)/sim/xilinx_stubs.v $(SOURCES) $(ROOT)/tests/tb_fpga_trace_player.v || exit 1; \
	  (cd $(BUILD)/sim && vvp player_$$mode.vvp +capture=$(BUILD)/sim_capture_$$mode.txt | tail -2) || exit 1; \
	  python3 $(ROOT)/tools/fpga-capture.py --from-file $(BUILD)/sim_capture_$$mode.txt --manifest $(BUILD)/tms_trace_manifest.json --output $(BUILD)/sim_capture_$$mode.json | tail -1 || exit 1; \
	done
	cp $(BUILD)/sim_capture_$(CLOCK_MODE).txt $(BUILD)/sim_capture.txt; cp $(BUILD)/sim_capture_$(CLOCK_MODE).json $(BUILD)/sim_capture.json

synth: $(BUILD)/$(TOP).json
$(BUILD)/$(TOP).json: $(SOURCES)
	mkdir -p $(BUILD)
	$(DOCKER) yosys -q -l $(call REL,$(BUILD))/yosys.log -p "read_verilog $(foreach s,$(SOURCES),$(call REL,$(s))); chparam -set BUILD_ID 32'h$(BUILD_ID) -set CLOCK_MODE $(CLOCK_MODE) $(TOP); $(YOSYS_SYNTH) -top $(TOP); setundef -zero -params; tee -o $(call REL,$(BUILD))/yosys_stat.txt stat; write_json $(call REL,$(BUILD))/$(TOP).json"

chipdb: $(CHIPDB)
$(CHIPDB):
	mkdir -p $(dir $@)
	$(DOCKER) bash -c "cd /nextpnr-xilinx && python3 xilinx/python/bbaexport.py --device $(PART) --bba /work/$(call REL,$(BUILD))/chipdb/$(PART).bba && bbasm -l /work/$(call REL,$(BUILD))/chipdb/$(PART).bba /work/$(call REL,$@) && rm -f /work/$(call REL,$(BUILD))/chipdb/$(PART).bba"

pnr: $(BUILD)/$(TOP).fasm
$(BUILD)/$(TOP).fasm: $(BUILD)/$(TOP).json $(CHIPDB) $(CURDIR)/tms_trace_player.xdc
	@rm -f $@; ok=0; for p in $(PLACERS); do for s in $(SEEDS); do \
	  echo "nextpnr placer $$p seed $$s"; \
	  $(DOCKER) nextpnr-xilinx --chipdb $(call REL,$(CHIPDB)) --xdc $(call REL,$(CURDIR))/tms_trace_player.xdc \
	    --json $(call REL,$(BUILD))/$(TOP).json --write $(call REL,$(BUILD))/$(TOP)_routed.json \
	    --fasm $(call REL,$@) --freq $(FREQ_MHZ) --seed $$s --placer $$p --router router1 --timing-allow-fail \
	    > $(BUILD)/nextpnr_$${p}_seed$$s.log 2>&1 || true; \
	  if [ -s $@ ] && ! grep -q "Failed to find a route" $(BUILD)/nextpnr_$${p}_seed$$s.log; then ok=$$p/$$s; cp $(BUILD)/nextpnr_$${p}_seed$$s.log $(BUILD)/nextpnr.log; break 2; fi; \
	  rm -f $@; done; done; \
	  [ "$$ok" != 0 ] || { echo "no placer/seed routed"; exit 1; }; echo "routed with placer/seed $$ok"

bit: $(BUILD)/$(TOP).bit
$(BUILD)/$(TOP).bit: $(BUILD)/$(TOP).fasm
	$(DOCKER) bash -c "source /prjxray/env/bin/activate && fasm2frames --db-root /nextpnr-xilinx/xilinx/external/prjxray-db/artix7 --part $(PART) $(call REL,$<) $(call REL,$(BUILD))/$(TOP).frames && /prjxray/build/tools/xc7frames2bit --part_file /nextpnr-xilinx/xilinx/external/prjxray-db/artix7/$(PART)/part.yaml --part_name $(PART) --frm_file $(call REL,$(BUILD))/$(TOP).frames --output_file $(call REL,$@)"
	@echo "frames: $$(wc -l < $(BUILD)/$(TOP).frames) lines; bit: $$(wc -c < $@) bytes"

detect:
	openFPGALoader -c $(CABLE) --detect

flash: $(BUILD)/$(TOP).bit
	openFPGALoader -c $(CABLE) $<

capture:
	python3 $(ROOT)/tools/fpga-capture.py --port $(PORT) --manifest $(BUILD)/tms_trace_manifest.json --output $(BUILD)/capture.json --raw $(BUILD)/capture.txt --label "$(LABEL)"

clean:
	rm -rf $(BUILD)/sim $(BUILD)/$(TOP).* $(BUILD)/nextpnr*.log $(BUILD)/yosys*.log $(BUILD)/yosys_stat.txt
