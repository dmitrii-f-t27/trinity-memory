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
#   make -C fpga/ax7203 ddr3-all   UberDDR3 DDR3 builds x16 and x32 (issue #60), see the DDR3 section
ROOT      := $(abspath $(CURDIR)/../..)
BUILD     := $(ROOT)/build/fpga
RTL       := $(ROOT)/build/t27/specs/rtl
T27C      ?= $(T27_ROOT)/target/release/t27c
PART      ?= xc7a200tfbg484-2
DBPART    := xc7a200t
TOP       := tms_trace_player_ax7203
IMAGE     ?= regymm/openxc7:latest
DOCKER    ?= docker run --rm -v "$(ROOT):/work" -w /work $(IMAGE)
# Each tool runs from the repository root with repository-relative paths: in the
# image by default, or natively, e.g. (single quotes, so make expands $(ROOT))
#   make ... YOSYS='cd $(ROOT) && yosys' \
#            NEXTPNR='cd $(ROOT) && ../tools-native/nextpnr-xilinx/build/nextpnr-xilinx'
# with a host build of the image's nextpnr-xilinx revision (it reads the image's
# chip database).
YOSYS     ?= $(DOCKER) yosys
NEXTPNR   ?= $(DOCKER) nextpnr-xilinx
PACK      ?= $(DOCKER)
CHIPDB    ?= $(BUILD)/chipdb/$(PART).bin
SEEDS     ?= 1 2 3 4 5 6
PLACERS   ?= sa heap
YOSYS_SYNTH ?= synth_xilinx -flatten -abc9 -nocarry -nodsp -nowidelut -nosrl -family xc7
FREQ_MHZ  ?= 200.0
# The CP2102N's name follows the USB socket (-10, -110, ...): list /dev/cu.usbserial-*.
PORT      ?= /dev/cu.usbserial-10
CABLE     ?= digilent_hs2
BUILD_ID  ?= $(shell git -C $(ROOT) rev-parse --short=8 HEAD)
CLOCK_MODE ?= 1
CORES     := dot_stream stream_storage stream_view stream_join \
             fpga_tick fpga_reset fpga_uart_tx fpga_line_emitter fpga_edge_argmax fpga_trace_player
SOURCES   := $(foreach c,$(CORES),$(RTL)/$(c).v) $(BUILD)/fpga_trace_rom.v \
             $(CURDIR)/tms_dut_wrappers.v $(BUILD)/tms_trace_bank.v $(CURDIR)/tms_trace_player.v
REL       = $(patsubst $(ROOT)/%,%,$(1))
comma     := ,

.PHONY: gen sim synth chipdb pnr bit flash capture detect clean \
        bram-gen bram-sim bram-synth bram-pnr bram-bit bram-flash bram-capture bram-ooc \
        ddr3-fetch ddr3-cores ddr3-nextpnr ddr3-chipdb ddr3-synth ddr3-pnr ddr3-bit ddr3-report ddr3-flash ddr3-clean \
        ddr3-all ddr3-sweep

gen: $(BUILD)/tms_trace_bank.v $(BUILD)/fpga_trace_rom.v $(foreach c,$(CORES),$(RTL)/$(c).v)

$(BUILD)/tms_trace_bank.v $(BUILD)/tms_trace_manifest.json $(BUILD)/fpga_trace_rom.t27: $(ROOT)/conformance/memory_stream_compute.json $(ROOT)/conformance/memory_edge_demo.json $(ROOT)/tools/generate-fpga-trace-player.py
	mkdir -p $(BUILD)
	python3 $(ROOT)/tools/generate-fpga-trace-player.py --output-dir $(BUILD)

# The generated ROM core is executable t27 as well; the pinned compiler lowers it.
$(BUILD)/fpga_trace_rom.v: $(BUILD)/fpga_trace_rom.t27
	@test -x "$(T27C)" || { echo "T27_ROOT must point at the pinned compiler checkout (native/compiler.lock)"; exit 1; }
	"$(T27C)" gen-verilog $< > $@

# One generated core per executable source; regenerated whenever the source changes.
$(RTL)/%.v: $(ROOT)/t27/rtl/%.t27
	@test -x "$(T27C)" || { echo "T27_ROOT must point at the pinned compiler checkout (native/compiler.lock)"; exit 1; }
	mkdir -p $(RTL)
	cd $(ROOT) && "$(T27C)" gen-verilog t27/rtl/$*.t27 > $@

sim: gen
	mkdir -p $(BUILD)/sim
	@for mode in 1 0; do \
	  iverilog -g2012 -Ptb_fpga_trace_player.CLOCK_MODE=$$mode -o $(BUILD)/sim/player_$$mode.vvp -s tb_fpga_trace_player $(CURDIR)/sim/xilinx_stubs.v $(SOURCES) $(ROOT)/tests/tb_fpga_trace_player.v || exit 1; \
	  (cd $(BUILD)/sim && vvp player_$$mode.vvp +capture=$(BUILD)/sim_capture_$$mode.txt > vvp_$$mode.log 2>&1); st=$$?; \
	  tail -2 $(BUILD)/sim/vvp_$$mode.log; \
	  [ $$st -eq 0 ] && grep -q '^TB_PASS' $(BUILD)/sim/vvp_$$mode.log || { echo "player simulation failed (CLOCK_MODE=$$mode)"; exit 1; }; \
	  python3 $(ROOT)/tools/fpga-capture.py --from-file $(BUILD)/sim_capture_$$mode.txt --manifest $(BUILD)/tms_trace_manifest.json \
	    --output $(BUILD)/sim_capture_$$mode.json > $(BUILD)/sim/compare_$$mode.log 2>&1; st=$$?; \
	  tail -1 $(BUILD)/sim/compare_$$mode.log; [ $$st -eq 0 ] || exit 1; \
	done
	cp $(BUILD)/sim_capture_$(CLOCK_MODE).txt $(BUILD)/sim_capture.txt; cp $(BUILD)/sim_capture_$(CLOCK_MODE).json $(BUILD)/sim_capture.json

synth: $(BUILD)/$(TOP).json
$(BUILD)/$(TOP).json: $(SOURCES)
	mkdir -p $(BUILD)
	$(YOSYS) -q -l $(call REL,$(BUILD))/yosys.log -p "read_verilog $(foreach s,$(SOURCES),$(call REL,$(s))); chparam -set BUILD_ID 32'h$(BUILD_ID) -set CLOCK_MODE $(CLOCK_MODE) $(TOP); $(YOSYS_SYNTH) -top $(TOP); setundef -zero -params; tee -o $(call REL,$(BUILD))/yosys_stat.txt stat; write_json $(call REL,$(BUILD))/$(TOP).json"

chipdb: $(CHIPDB)
$(CHIPDB):
	mkdir -p $(dir $@)
	$(DOCKER) bash -c "cd /nextpnr-xilinx && python3 xilinx/python/bbaexport.py --device $(PART) --bba /work/$(call REL,$(BUILD))/chipdb/$(PART).bba && bbasm -l /work/$(call REL,$(BUILD))/chipdb/$(PART).bba /work/$(call REL,$@) && rm -f /work/$(call REL,$(BUILD))/chipdb/$(PART).bba"

pnr: $(BUILD)/$(TOP).fasm
$(BUILD)/$(TOP).fasm: $(BUILD)/$(TOP).json $(CHIPDB) $(CURDIR)/tms_trace_player.xdc
	@rm -f $@; ok=0; for p in $(PLACERS); do for s in $(SEEDS); do \
	  echo "nextpnr placer $$p seed $$s"; \
	  $(NEXTPNR) --chipdb $(call REL,$(CHIPDB)) --xdc $(call REL,$(CURDIR))/tms_trace_player.xdc \
	    --json $(call REL,$(BUILD))/$(TOP).json --write $(call REL,$(BUILD))/$(TOP)_routed.json \
	    --fasm $(call REL,$@) --freq $(FREQ_MHZ) --seed $$s --placer $$p --router router1 --timing-allow-fail \
	    > $(BUILD)/nextpnr_$${p}_seed$$s.log 2>&1 || true; \
	  if [ -s $@ ] && ! grep -q "Failed to find a route" $(BUILD)/nextpnr_$${p}_seed$$s.log; then ok=$$p/$$s; cp $(BUILD)/nextpnr_$${p}_seed$$s.log $(BUILD)/nextpnr.log; break 2; fi; \
	  rm -f $@; done; done; \
	  [ "$$ok" != 0 ] || { echo "no placer/seed routed"; exit 1; }; echo "routed with placer/seed $$ok"

bit: $(BUILD)/$(TOP).bit
$(BUILD)/$(TOP).bit: $(BUILD)/$(TOP).fasm
	$(PACK) bash -c "source /prjxray/env/bin/activate && fasm2frames --db-root /nextpnr-xilinx/xilinx/external/prjxray-db/artix7 --part $(PART) $(call REL,$<) $(call REL,$(BUILD))/$(TOP).frames && /prjxray/build/tools/xc7frames2bit --part_file /nextpnr-xilinx/xilinx/external/prjxray-db/artix7/$(PART)/part.yaml --part_name $(PART) --frm_file $(call REL,$(BUILD))/$(TOP).frames --output_file $(call REL,$@)"
	@echo "frames: $$(wc -l < $(BUILD)/$(TOP).frames) lines; bit: $$(wc -c < $@) bytes"

detect:
	openFPGALoader -c $(CABLE) --detect

flash: $(BUILD)/$(TOP).bit
	openFPGALoader -c $(CABLE) $<

capture:
	python3 $(ROOT)/tools/fpga-capture.py --port $(PORT) --manifest $(BUILD)/tms_trace_manifest.json --output $(BUILD)/capture.json --raw $(BUILD)/capture.txt --label "$(LABEL)"

clean:
	rm -rf $(BUILD)/sim $(BUILD)/$(TOP).* $(BUILD)/nextpnr*.log $(BUILD)/yosys*.log $(BUILD)/yosys_stat.txt \
	       $(BRAM_BUILD) $(BRAM_SIMBUILD) $(foreach s,-0 -1 -2,$(BUILD)/bram$(s) $(BUILD)/bram-sim$(s)) $(BUILD)/bram $(BUILD)/bram-sim

# ---- block-RAM trit packing bench (tms_bram_bench.v) ----
# Three engines store the same BRAM_TRITS trits in three 36-bit word layouts
# (b2: 18, d5: 20, d5d2: 22 trits per word); tools/generate-bram-bench.py writes
# one t27 engine per layout and the manifest the host compares with. The bench
# needs `read_verilog -nomem2reg`, otherwise yosys turns the engines' arrays
# (written in a process with an asynchronous reset) into flip-flops.
#
#   make -C fpga/ax7203 bram-sim                          all three layouts in Icarus, host comparison
#   make -C fpga/ax7203 bram-bit bram-flash bram-capture BRAM_ONLY=2 PORT=/dev/cu.usbserial-110
#
# BRAM_ONLY: -1 all three layouts in one design (simulation; 150 RAMB36E1, which
# nextpnr-xilinx does not route), 0 b2, 1 d5, 2 d5d2 (one layout: the device
# builds). Every value has its own build directories, so switching layouts never
# reuses another layout's netlist or bitstream.
BRAM_TRITS     ?= 1013760
BRAM_SIM_TRITS ?= 1980
BRAM_ONLY      ?= -1
override BRAM_ONLY := $(strip $(BRAM_ONLY))
ifneq ($(words $(BRAM_ONLY)) $(filter -1 0 1 2 3,$(BRAM_ONLY)),1 $(BRAM_ONLY))
$(error BRAM_ONLY must be -1 (all three layouts), 0 (b2), 1 (d5), 2 (d5d2) or 3 (d5p, with BRAM_ROM), not '$(BRAM_ONLY)')
endif
BRAM_SUFFIX    := $(if $(filter -1,$(BRAM_ONLY)),,-$(BRAM_ONLY))
# yosys chparam does not take a negative literal; -1 is the Verilog default anyway.
BRAM_CHPARAM   := $(if $(BRAM_SUFFIX),-set ONLY $(BRAM_ONLY),)
BRAM_EXPECT    := $(if $(BRAM_SUFFIX),$(BRAM_ONLY),0$(comma)1$(comma)2)
BRAM_PLACERS   ?= heap sa
BRAM_ROUTER    ?= router2
# BRAM_ROM: a file of trits (one signed byte each, tools/extract-bram-trits.py)
# that the stores hold as array initializers, loaded by configuration, instead
# of writing the LFSR stream (t27/rtl/bram_trit_rom.t27): the first BRAM_TRITS
# (BRAM_SIM_TRITS in simulation) trits of the file. Its builds have their own
# directories (bram-rom-N).
#   make -C fpga/ax7203 bram-sim BRAM_ONLY=2 BRAM_ROM=$(ROOT)/build/fpga/rom/trits.bin
BRAM_ROM       ?=
# BRAM_PIPE=1 (ROM mode): a register after the decoder and after the per-word sums.
# BAUD_DIV must match CLOCK_MODE (1: 25 MHz, 217; 2: 50 MHz, 434; 3: 100 MHz, 868).
BRAM_PIPE      ?= 0
BAUD_DIV       ?= 217
# BRAM_MATVEC: with BRAM_ROM, a file of int8 activations (tools/extract-bram-activations.py);
# the d5d2 store computes y = W x, W the BRAM_TRITS trits as rows of that many
# columns (t27/rtl/bram_trit_matvec.t27). d5d2 only (BRAM_ONLY=2); its simulation
# stores BRAM_SIM_TRITS = two groups of 22 rows.
#   make -C fpga/ax7203 bram-sim BRAM_ONLY=2 BRAM_ROM=$(ROOT)/build/fpga/rom/trits.bin BRAM_MATVEC=$(ROOT)/build/fpga/rom/act.bin
BRAM_MATVEC    ?=
ifneq ($(BRAM_MATVEC),)
ifeq ($(BRAM_ROM),)
$(error BRAM_MATVEC needs BRAM_ROM (the matrix))
endif
ifneq ($(BRAM_ONLY),2)
$(error BRAM_MATVEC is a d5d2 store: set BRAM_ONLY=2)
endif
ifeq ($(origin BRAM_SIM_TRITS),file)
BRAM_SIM_TRITS := 112640
endif
endif
BRAM_KIND      := $(if $(BRAM_MATVEC),bram-matvec,$(if $(BRAM_ROM),bram-rom$(if $(filter 1,$(BRAM_PIPE)),-pipe),bram))
BRAM_GEN_ARGS  := $(if $(BRAM_ROM),--rom-trits $(abspath $(BRAM_ROM)) --pipe $(BRAM_PIPE)) \
                  $(if $(BRAM_MATVEC),--matvec-act $(abspath $(BRAM_MATVEC)))
BRAM_BUILD     ?= $(BUILD)/$(BRAM_KIND)$(BRAM_SUFFIX)
BRAM_SIMBUILD  ?= $(BUILD)/$(BRAM_KIND)-sim$(BRAM_SUFFIX)
BRAM_TOP       := tms_bram_bench_ax7203
BRAM_CORES     := fpga_tick fpga_reset fpga_uart_tx fpga_line_emitter fpga_bram_bench bram_trit_codec
BRAM_FORMATS   := $(if $(BRAM_MATVEC),d5d2,b2 d5 d5d2 $(if $(BRAM_ROM),d5p))
BRAM_CORE_V    := $(foreach c,$(BRAM_CORES),$(RTL)/$(c).v)
# Every source whose hash the manifest records (tools/generate-bram-bench.py, SOURCES).
BRAM_GEN_DEPS  := $(ROOT)/t27/rtl/bram_trit_engine.t27 $(ROOT)/t27/rtl/bram_trit_codec.t27 $(ROOT)/t27/rtl/fpga_bram_bench.t27 \
                  $(ROOT)/tools/generate-bram-bench.py $(ROOT)/tools/bram_trit_model.py \
                  $(if $(BRAM_ROM),$(ROOT)/t27/rtl/bram_trit_rom.t27 $(abspath $(BRAM_ROM))) \
                  $(if $(BRAM_MATVEC),$(ROOT)/t27/rtl/bram_trit_matvec.t27 $(abspath $(BRAM_MATVEC)))
bram_sources    = $(BRAM_CORE_V) $(foreach f,$(BRAM_FORMATS),$(1)/bram_trit_engine_$(f).v) $(CURDIR)/tms_bram_bench.v
# Synthesis of one layout reads only that layout's engine: the adapter elaborates
# one slot, and in ROM mode yosys needs minutes per engine for the initializers.
BRAM_SYNTH_FORMATS := $(if $(filter 0,$(BRAM_ONLY)),b2,$(if $(filter 1,$(BRAM_ONLY)),d5,$(if $(filter 2,$(BRAM_ONLY)),d5d2,$(if $(filter 3,$(BRAM_ONLY)),d5p,$(BRAM_FORMATS)))))
bram_synth_sources = $(BRAM_CORE_V) $(foreach f,$(BRAM_SYNTH_FORMATS),$(1)/bram_trit_engine_$(f).v) $(CURDIR)/tms_bram_bench.v
# synth_xilinx with the block RAM library restricted to the 1K x 36 configuration
# (brams_x36.txt): every layout is mapped the same way, one 36-bit word per RAMB36
# word, parity bits in use. $(1) is the top module.
bram_synth      = $(YOSYS_SYNTH) -top $(1) -run :map_memory; \
                  memory_libmap -lib +/xilinx/lutrams_xc5v.txt -lib $(call REL,$(CURDIR))/brams_x36.txt; \
                  techmap -map +/xilinx/lutrams_xc5v_map.v -map +/xilinx/brams_xc6v_map.v; \
                  $(YOSYS_SYNTH) -top $(1) -run map_ffram:

# Stamps name the values a directory was built with, so changing BRAM_TRITS or
# CLOCK_MODE rebuilds what depends on them.
$(BRAM_BUILD)/trits-$(BRAM_TRITS).stamp $(BRAM_SIMBUILD)/trits-$(BRAM_SIM_TRITS).stamp $(BRAM_BUILD)/clock-$(CLOCK_MODE)-$(BAUD_DIV).stamp:
	mkdir -p $(dir $@); rm -f $(dir $@)$(firstword $(subst -, ,$(notdir $@)))-*.stamp; touch $@
$(BRAM_BUILD)/bram_bench_manifest.json: $(BRAM_GEN_DEPS) $(BRAM_BUILD)/trits-$(BRAM_TRITS).stamp
	python3 $(ROOT)/tools/generate-bram-bench.py --trits $(BRAM_TRITS) $(BRAM_GEN_ARGS) --output-dir $(BRAM_BUILD)
$(BRAM_SIMBUILD)/bram_bench_manifest.json: $(BRAM_GEN_DEPS) $(BRAM_SIMBUILD)/trits-$(BRAM_SIM_TRITS).stamp
	python3 $(ROOT)/tools/generate-bram-bench.py --trits $(BRAM_SIM_TRITS) $(BRAM_GEN_ARGS) --output-dir $(BRAM_SIMBUILD)
$(BRAM_BUILD)/bram_trit_engine_%.v: $(BRAM_BUILD)/bram_bench_manifest.json
	@test -x "$(T27C)" || { echo "T27_ROOT must point at the pinned compiler checkout (native/compiler.lock)"; exit 1; }
	"$(T27C)" gen-verilog $(BRAM_BUILD)/bram_trit_engine_$*.t27 > $@
$(BRAM_SIMBUILD)/bram_trit_engine_%.v: $(BRAM_SIMBUILD)/bram_bench_manifest.json
	@test -x "$(T27C)" || { echo "T27_ROOT must point at the pinned compiler checkout (native/compiler.lock)"; exit 1; }
	"$(T27C)" gen-verilog $(BRAM_SIMBUILD)/bram_trit_engine_$*.t27 > $@

bram-gen: $(call bram_sources,$(BRAM_BUILD))

bram-sim: $(call bram_sources,$(BRAM_SIMBUILD))
	@for mode in 1 0; do \
	  iverilog -g2012 -Ptb_fpga_bram_bench.CLOCK_MODE=$$mode -Ptb_fpga_bram_bench.ONLY=$(BRAM_ONLY) -o $(BRAM_SIMBUILD)/bench_$$mode.vvp -s tb_fpga_bram_bench \
	    $(CURDIR)/sim/xilinx_stubs.v $(call bram_sources,$(BRAM_SIMBUILD)) $(ROOT)/tests/tb_fpga_bram_bench.v || exit 1; \
	  (cd $(BRAM_SIMBUILD) && vvp bench_$$mode.vvp +capture=$(BRAM_SIMBUILD)/sim_capture_$$mode.txt > vvp_$$mode.log 2>&1); st=$$?; \
	  tail -2 $(BRAM_SIMBUILD)/vvp_$$mode.log; \
	  [ $$st -eq 0 ] && grep -q '^TB_PASS' $(BRAM_SIMBUILD)/vvp_$$mode.log || { echo "bench simulation failed (CLOCK_MODE=$$mode)"; exit 1; }; \
	  python3 $(ROOT)/tools/fpga-bram-capture.py --from-file $(BRAM_SIMBUILD)/sim_capture_$$mode.txt --expect-formats $(BRAM_EXPECT) \
	    --manifest $(BRAM_SIMBUILD)/bram_bench_manifest.json --output $(BRAM_SIMBUILD)/sim_capture_$$mode.json > $(BRAM_SIMBUILD)/compare_$$mode.log 2>&1; st=$$?; \
	  tail -1 $(BRAM_SIMBUILD)/compare_$$mode.log; [ $$st -eq 0 ] || exit 1; \
	done

bram-synth: $(BRAM_BUILD)/$(BRAM_TOP).json
$(BRAM_BUILD)/$(BRAM_TOP).json: $(call bram_sources,$(BRAM_BUILD)) $(CURDIR)/brams_x36.txt $(BRAM_BUILD)/clock-$(CLOCK_MODE)-$(BAUD_DIV).stamp
	$(YOSYS) -q -l $(call REL,$(BRAM_BUILD))/yosys.log -p "read_verilog -nomem2reg $(foreach s,$(call bram_synth_sources,$(BRAM_BUILD)),$(call REL,$(s))); chparam -set BUILD_ID 32'h$(BUILD_ID) -set CLOCK_MODE $(CLOCK_MODE) -set BAUD_DIV $(BAUD_DIV) $(BRAM_CHPARAM) $(BRAM_TOP); $(call bram_synth,$(BRAM_TOP)); setundef -zero -params; tee -o $(call REL,$(BRAM_BUILD))/yosys_stat.txt stat; write_json $(call REL,$(BRAM_BUILD))/$(BRAM_TOP).json"

# Cells of each layout on its own, synthesized out of context with the same
# yosys flags as the bench: the engine with its encoder and decoder
# (tms_bram_ooc.v, ooc_<f>.stat) and the codec alone (tms_bram_codec_ooc.v,
# codec_<f>_<op>.stat, op 0 encoder, 1 decoder).
bram-ooc: $(call bram_sources,$(BRAM_BUILD))
	@for f in 0 1 2; do \
	  $(YOSYS) -q -l $(call REL,$(BRAM_BUILD))/ooc_$$f.log -p "read_verilog -nomem2reg $(foreach s,$(filter-out %/tms_bram_bench.v,$(call bram_sources,$(BRAM_BUILD))),$(call REL,$(s))) $(call REL,$(CURDIR))/tms_bram_ooc.v; chparam -set FORMAT $$f tms_bram_ooc; $(call bram_synth,tms_bram_ooc); tee -o $(call REL,$(BRAM_BUILD))/ooc_$$f.stat stat" || exit 1; \
	  echo "layout $$f: $$(grep -E 'RAMB36E1|RAMB18E1' $(BRAM_BUILD)/ooc_$$f.stat | tr -s ' ' | tr '\n' ' ')"; \
	  for op in 0 1; do \
	    $(YOSYS) -q -l $(call REL,$(BRAM_BUILD))/codec_$${f}_$$op.log -p "read_verilog $(call REL,$(RTL))/bram_trit_codec.v $(call REL,$(CURDIR))/tms_bram_codec_ooc.v; chparam -set FORMAT $$f -set OP $$op tms_bram_codec_ooc; $(YOSYS_SYNTH) -top tms_bram_codec_ooc; tee -o $(call REL,$(BRAM_BUILD))/codec_$${f}_$$op.stat stat" || exit 1; \
	  done; \
	done

bram-pnr: $(BRAM_BUILD)/$(BRAM_TOP).fasm
$(BRAM_BUILD)/$(BRAM_TOP).fasm: $(BRAM_BUILD)/$(BRAM_TOP).json $(CHIPDB) $(CURDIR)/tms_trace_player.xdc
	@rm -f $@; ok=0; for p in $(BRAM_PLACERS); do for s in $(SEEDS); do \
	  echo "nextpnr placer $$p seed $$s router $(BRAM_ROUTER)"; \
	  $(NEXTPNR) --chipdb $(call REL,$(CHIPDB)) --xdc $(call REL,$(CURDIR))/tms_trace_player.xdc \
	    --json $(call REL,$(BRAM_BUILD))/$(BRAM_TOP).json --write $(call REL,$(BRAM_BUILD))/$(BRAM_TOP)_routed.json \
	    --fasm $(call REL,$@) --freq $(FREQ_MHZ) --seed $$s --placer $$p --router $(BRAM_ROUTER) --timing-allow-fail \
	    > $(BRAM_BUILD)/nextpnr_$${p}_seed$$s.log 2>&1 || true; \
	  if [ -s $@ ] && ! grep -q "Failed to find a route" $(BRAM_BUILD)/nextpnr_$${p}_seed$$s.log; then ok=$$p/$$s; cp $(BRAM_BUILD)/nextpnr_$${p}_seed$$s.log $(BRAM_BUILD)/nextpnr.log; break 2; fi; \
	  rm -f $@; done; done; \
	  [ "$$ok" != 0 ] || { echo "no placer/seed routed"; exit 1; }; echo "routed with placer/seed $$ok"

bram-bit: $(BRAM_BUILD)/$(BRAM_TOP).bit
$(BRAM_BUILD)/$(BRAM_TOP).bit: $(BRAM_BUILD)/$(BRAM_TOP).fasm
	$(PACK) bash -c "source /prjxray/env/bin/activate && fasm2frames --db-root /nextpnr-xilinx/xilinx/external/prjxray-db/artix7 --part $(PART) $(call REL,$<) $(call REL,$(BRAM_BUILD))/$(BRAM_TOP).frames && /prjxray/build/tools/xc7frames2bit --part_file /nextpnr-xilinx/xilinx/external/prjxray-db/artix7/$(PART)/part.yaml --part_name $(PART) --frm_file $(call REL,$(BRAM_BUILD))/$(BRAM_TOP).frames --output_file $(call REL,$@)"
	@echo "frames: $$(wc -l < $(BRAM_BUILD)/$(BRAM_TOP).frames) lines; bit: $$(wc -c < $@) bytes"

bram-flash: $(BRAM_BUILD)/$(BRAM_TOP).bit
	openFPGALoader -c $(CABLE) $<

bram-capture:
	python3 $(ROOT)/tools/fpga-bram-capture.py --port $(PORT) --manifest $(BRAM_BUILD)/bram_bench_manifest.json --expect-formats $(BRAM_EXPECT) \
	  --output $(BRAM_BUILD)/capture.json --raw $(BRAM_BUILD)/capture.txt --label "$(LABEL)"

# ---- UART loader into block RAM (tms_uart_loader.v), issue #63 part 1 ----
# The t27 receiver, frame parser with CRC-32, staging buffer, 256 KiB block-RAM store,
# ack/nak lines and read-back (docs/uart-loader.md). Same clocking as the block-RAM
# benches (CLOCK_MODE 1: 25 MHz); BAUD_DIV is the divisor after reset (217: 115200 at
# 25 MHz), LOADER_TIMEOUT the inter-byte timeout and LOADER_PROBATION the time a new
# baud rate has to be confirmed in, both in design clocks. Its own build directory,
# variables and targets: nothing here changes the other designs' commands.
#   make -C fpga/ax7203 loader-sim                        Icarus scenarios (tests/test_uart_loader.py)
#   make -C fpga/ax7203 loader-bit loader-flash FREQ_MHZ=25 SEEDS=3 BRAM_PLACERS=heap   (the build of record 1d474000: heap seed 3)
#   python3 tools/fpga-uart-loader.py --port /dev/cu.usbserial-110 ...   (host side)
.PHONY: loader-gen loader-sim loader-synth loader-pnr loader-bit loader-flash loader-report loader-clean
LOADER_TOP       := tms_uart_loader_ax7203
LOADER_CORES     := fpga_tick fpga_reset fpga_uart_tx fpga_uart_rx fpga_line_emitter fpga_uart_loader fpga_loader_store
LOADER_TIMEOUT   ?= 1250000
LOADER_PROBATION ?= 75000000
LOADER_BUILD     ?= $(BUILD)/loader-clk$(CLOCK_MODE)-div$(BAUD_DIV)
LOADER_REPORT    ?= $(ROOT)/reports/fpga/loader-build-$(shell date -u +%Y-%m-%d)-$(BUILD_ID)
LOADER_SOURCES   := $(foreach c,$(LOADER_CORES),$(RTL)/$(c).v) $(CURDIR)/tms_uart_loader.v
LOADER_IDENTITY_NOTE := nextpnr-xilinx 45a986b8 did not reproduce this design's FASM bit for bit: a second placement of the same netlist with the same seed differed in about 960 of 194 000 FASM lines (placements.json next to the build record); the FASM and frames hashes identify this build, bitstream.sha256 the file that was flashed
LOADER_STAMP     := $(LOADER_BUILD)/params-$(CLOCK_MODE)-$(BAUD_DIV)-$(LOADER_TIMEOUT)-$(LOADER_PROBATION)-$(BUILD_ID).stamp
# Synthesis as the block-RAM benches (bram_synth: the RAMB36E1 1K x 36 configuration
# only, brams_x36.txt), the one configuration verified on this board. Left to itself
# yosys built the 256 KiB store from cascaded RAMB36 pairs, which nextpnr-xilinx did not
# route ("Failed to route arc 0 of net store.mem...CAS_B"); without the cascade it chose
# 32K x 1, and that build (6bc63ca4) read back every even byte as its odd neighbour on
# the board (docs/uart-loader.md).

loader-gen: $(LOADER_SOURCES)

loader-sim:
	cd $(ROOT) && python3 -m unittest tests.test_uart_loader -v

$(LOADER_STAMP):
	mkdir -p $(dir $@); rm -f $(dir $@)params-*.stamp; touch $@

loader-synth: $(LOADER_BUILD)/$(LOADER_TOP).json
$(LOADER_BUILD)/$(LOADER_TOP).json: $(LOADER_SOURCES) $(CURDIR)/brams_x36.txt $(LOADER_STAMP)
	$(YOSYS) -q -l $(call REL,$(LOADER_BUILD))/yosys.log -p "read_verilog -nomem2reg $(foreach s,$(LOADER_SOURCES),$(call REL,$(s))); chparam -set BUILD_ID 32'h$(BUILD_ID) -set CLOCK_MODE $(CLOCK_MODE) -set BAUD_DIV $(BAUD_DIV) -set TIMEOUT_CLOCKS $(LOADER_TIMEOUT) -set PROBATION_CLOCKS $(LOADER_PROBATION) $(LOADER_TOP); $(call bram_synth,$(LOADER_TOP)); setundef -zero -params; tee -o $(call REL,$(LOADER_BUILD))/yosys_stat.txt stat; write_json $(call REL,$(LOADER_BUILD))/$(LOADER_TOP).json"

loader-pnr: $(LOADER_BUILD)/$(LOADER_TOP).fasm
$(LOADER_BUILD)/$(LOADER_TOP).fasm: $(LOADER_BUILD)/$(LOADER_TOP).json $(CHIPDB) $(CURDIR)/tms_trace_player.xdc
	@rm -f $@; ok=0; for p in $(BRAM_PLACERS); do for s in $(SEEDS); do \
	  echo "nextpnr placer $$p seed $$s router $(BRAM_ROUTER)"; \
	  $(NEXTPNR) --chipdb $(call REL,$(CHIPDB)) --xdc $(call REL,$(CURDIR))/tms_trace_player.xdc \
	    --json $(call REL,$(LOADER_BUILD))/$(LOADER_TOP).json --write $(call REL,$(LOADER_BUILD))/$(LOADER_TOP)_routed.json \
	    --fasm $(call REL,$@) --freq $(FREQ_MHZ) --seed $$s --placer $$p --router $(BRAM_ROUTER) --timing-allow-fail \
	    > $(LOADER_BUILD)/nextpnr_$${p}_seed$$s.log 2>&1 || true; \
	  if [ -s $@ ] && ! grep -q "Failed to find a route" $(LOADER_BUILD)/nextpnr_$${p}_seed$$s.log; then ok=$$p/$$s; cp $(LOADER_BUILD)/nextpnr_$${p}_seed$$s.log $(LOADER_BUILD)/nextpnr.log; break 2; fi; \
	  rm -f $@; done; done; \
	  [ "$$ok" != 0 ] || { echo "no placer/seed routed"; exit 1; }; echo "routed with placer/seed $$ok"

loader-bit: $(LOADER_BUILD)/$(LOADER_TOP).bit
$(LOADER_BUILD)/$(LOADER_TOP).bit: $(LOADER_BUILD)/$(LOADER_TOP).fasm
	$(PACK) bash -c "source /prjxray/env/bin/activate && fasm2frames --db-root /nextpnr-xilinx/xilinx/external/prjxray-db/artix7 --part $(PART) $(call REL,$<) $(call REL,$(LOADER_BUILD))/$(LOADER_TOP).frames && /prjxray/build/tools/xc7frames2bit --part_file /nextpnr-xilinx/xilinx/external/prjxray-db/artix7/$(PART)/part.yaml --part_name $(PART) --frm_file $(call REL,$(LOADER_BUILD))/$(LOADER_TOP).frames --output_file $(call REL,$@)"
	@echo "frames: $$(wc -l < $(LOADER_BUILD)/$(LOADER_TOP).frames) lines; bit: $$(wc -c < $@) bytes"

# SRAM only, never flash; never builds (the bitstream must exist).
loader-flash:
	@test -s $(LOADER_BUILD)/$(LOADER_TOP).bit || { echo "no bitstream in $(LOADER_BUILD): make loader-bit first"; exit 1; }
	openFPGALoader -c $(CABLE) $(LOADER_BUILD)/$(LOADER_TOP).bit

loader-report:
	python3 $(ROOT)/tools/fpga-build-report.py --artifact-dir $(LOADER_BUILD) --commit $(BUILD_ID) --source local \
	  --top $(LOADER_TOP) --output $(LOADER_REPORT) \
	  --identity-note "$(LOADER_IDENTITY_NOTE)" \
	  --flow "make -C fpga/ax7203 loader-bit CLOCK_MODE=$(CLOCK_MODE) BAUD_DIV=$(BAUD_DIV) LOADER_TIMEOUT=$(LOADER_TIMEOUT) LOADER_PROBATION=$(LOADER_PROBATION) FREQ_MHZ=$(FREQ_MHZ); yosys $(YOSYS_SYNTH) split around memory_libmap with fpga/ax7203/brams_x36.txt, read_verilog -nomem2reg; nextpnr-xilinx placers $(BRAM_PLACERS) seeds $(SEEDS) --router $(BRAM_ROUTER) --timing-allow-fail; prjxray fasm2frames + xc7frames2bit ($(IMAGE))"

loader-clean:
	rm -rf $(BUILD)/loader-clk*

# ---- DDR3 (UberDDR3) on the AX7203, issue #60: build only ----
# UberDDR3's ddr3_top (GPL-3.0-or-later, fetched at the commit in
# ddr3/uberddr3.lock into build/uberddr3/, never committed) with our top, PLL
# clocking, XDC, t27 status line and t27 pattern test (ddr3/tms_ddr3_ax7203.v). Two variants:
#   DDR3_WIDTH=16   BYTE_LANES 2, chip U6 (DQ[15:0])
#   DDR3_WIDTH=32   BYTE_LANES 4, both chips
# The pattern test (issue #61, t27/rtl/fpga_ddr3_pattern.t27) drives the user
# Wishbone port after calibration: DDR3_PATTERN=1 (default) builds it in,
# DDR3_PATTERN=0 leaves the port idle as the #60 builds had it (the same port
# behaviour, not the #60 netlist: the top changed since, so DDR3_PATTERN=0 gives
# another netlist, FASM and Fmax; the #60 netlists rebuild only from a commit
# whose DDR3 sources equal #60's, such as 00262ff, docs/hardware.md).
# DDR3_PATTERN_BURSTS (default 2^25, every burst address), DDR3_PATTERN_HOLD
# (controller clocks between a pass's writes and its reads, default 0) and
# DDR3_PATTERN_ROUNDS (0 = until reset) are baked into the netlist and named in
# the report's variant. Simulation: tests/test_ddr3_pattern.py (Icarus, a
# behavioural Wishbone memory in place of UberDDR3, with injected faults).
# DDR3_APP picks what drives the user port: pattern (default, the #61 builds
# above) or reader, the read path of issue #62 (t27/rtl/fpga_ddr3_reader.t27 and
# ddr3/tms_ddr3_reader.v, x16 only): yosys reads every source with
# `define DDR3_READER, the pattern test is not built, DDR3_READER_TRITS (logical
# trits per run, default 17694720 = 2560 x 6912), DDR3_READER_SEED, DDR3_READER_CAP
# (Wishbone requests outstanding at most, default 64) and DDR3_READER_RUNS (0 =
# until reset) are baked into the netlist and named in the variant (READER ...),
# and the build and report directories end in -reader, so the pattern-test builds
# keep their names and netlists. Simulation: tests/test_ddr3_reader.py.
#   make -C fpga/ax7203 ddr3-bit ddr3-report DDR3_APP=reader
# DDR3_APP=loader is the UART loader of issue #63 part 2 (docs/uart-loader.md, "DDR3"),
# x16 only: its own top ddr3/tms_ddr3_loader_ax7203.v (tms_ddr3_ax7203.v is not read, so
# the other builds keep their netlists) with the t27 receiver, loader, Wishbone master
# and port arbiter, UART RX on P20 (ddr3/tms_ddr3_loader.xdc). The loader's cores are
# read with -nomem2reg and every memory is mapped with the 1K x 36 block RAM library of
# the block-RAM builds (brams_x36.txt), as the part-1 loader; UberDDR3 is read as in the
# other DDR3 builds. Build and report directories end in -loader.
# Simulation: tests/test_ddr3_loader.py.
#   make -C fpga/ax7203 ddr3-bit ddr3-report DDR3_APP=loader
# Two place-and-route lines (DDR3_PNR_LINE):
#   release (default)  nextpnr-xilinx $(NEXTPNR_DDR3_REV), built from its release tag, with a chip
#                      database from its own bbaexport and prjxray-db; fasm2frames reads that same
#                      prjxray-db, so DDR3_TOOLS, DDR3_XRAY, DDR3_META and DDR3_CHIPDB belong together.
#   image              the image's nextpnr-xilinx 45a986b8 (or NEXTPNR, as for the block-RAM designs),
#                      the image's chip database ($(CHIPDB)) and prjxray-db: the nextpnr line of the
#                      only hardware precedent (UberDDR3's AX7103B demo, 2025), kept as the fallback
#                      for #61. Its builds go to ddr3-x<width>-nextpnr-45a986b8.
# 45a986b8 writes Vivado's CLKFBOUT_MULT=8 lock and loop-filter tables into every
# PLLE2 (fasm.cc, FIXME) and this design's PLL runs at MULT=5, which the report
# flags. The per-MULT tables harvested from Vivado (openXC7/nextpnr-xilinx#138)
# are in 0.9.3 and later, the derivation of clocks through PLLs (#155) in 0.9.4
# and later, the fix of the constant-holdout bug of 0.9.1-0.9.4 (#184) in 0.9.5
# and later. The block-RAM designs above stay on 45a986b8, untouched.
#
#   make -C fpga/ax7203 ddr3-fetch                         UberDDR3 at the pinned commit, sha256 checked
#   make -C fpga/ax7203 ddr3-nextpnr ddr3-chipdb           clone and build nextpnr-xilinx $(NEXTPNR_DDR3_REV), its chip database
#   make -C fpga/ax7203 ddr3-bit ddr3-report DDR3_WIDTH=16 yosys -> nextpnr -> prjxray -> .bit, then reports/fpga/...
#   make -C fpga/ax7203 ddr3-all                           both widths, bitstreams and reports (one command)
#   make -C fpga/ax7203 ddr3-sweep DDR3_WIDTH=16           every seed of DDR3_SEEDS on the current netlist, summarised
#   make -C fpga/ax7203 ddr3-flash DDR3_WIDTH=16 DDR3_FLASH_REPORT=<build.json> (or DDR3_EXPECT_SHA256=<sha256>)
#                                                          SRAM only; never builds; needs the board and approval (#61)
#   tools/fpga-ddr3-capture.py                             that load with the UART capture, IDCODE, DNA and XADC around it
#
# BUILD_ID (default: the short HEAD) is baked into the netlist and sent on the
# UART H line, so a build at another commit is a different netlist, FASM and
# bitstream. BUILD_ID=<id> rebuilds the netlist of an earlier commit when no
# DDR3 source changed since. The report takes its commit from the netlist and
# refuses a mismatch.
#
# Native Mac run (tools built once outside the repository, prjxray in the image), as for
# the reports of docs/hardware.md; nextpnr-xilinx 0.9.7's CMake needs pkg-config for eigen3:
#   make -C fpga/ax7203 ddr3-all YOSYS='cd $(ROOT) && yosys' IMAGE=regymm/openxc7@sha256:<digest> \
#        DDR3_TOOLS=/path/to/nextpnr-xilinx-0.9.7 DDR3_XRAY=/path/to/prjxray-db DDR3_META=/path/to/nextpnr-xilinx-meta
# DDR3_IN_IMAGE=1 (CI) runs every tool in the image: nextpnr is then built in
# the image too and run there from build/tools/.
# (ddr3-all sets the width itself: read before DDR3_WIDTH is overridden below)
DDR3_ALL_CLASH := $(filter command line,$(origin DDR3_WIDTH) $(origin DDR3_BUILD) $(origin DDR3_REPORT))
DDR3_WIDTH     ?= 16
override DDR3_WIDTH := $(strip $(DDR3_WIDTH))
ifneq ($(words $(DDR3_WIDTH)) $(filter 16 32,$(DDR3_WIDTH)),1 $(DDR3_WIDTH))
$(error DDR3_WIDTH must be 16 or 32, not '$(DDR3_WIDTH)')
endif
DDR3_PNR_LINE  ?= release
override DDR3_PNR_LINE := $(strip $(DDR3_PNR_LINE))
ifneq ($(words $(DDR3_PNR_LINE)) $(filter release image,$(DDR3_PNR_LINE)),1 $(DDR3_PNR_LINE))
$(error DDR3_PNR_LINE must be release or image, not '$(DDR3_PNR_LINE)')
endif
DDR3_LANES     := $(if $(filter 32,$(DDR3_WIDTH)),4,2)
# PLL: VCO = 200 MHz * DDR3_PLL_MULT; DDR3 clock VCO / DDR3_DDR_DIV; controller clock a quarter of it.
DDR3_PLL_MULT  ?= 5
DDR3_DDR_DIV   ?= 3
DDR3_DIR       := $(CURDIR)/ddr3
DDR3_LOCK      := $(DDR3_DIR)/uberddr3.lock
DDR3_SRC       := $(ROOT)/build/uberddr3
DDR3_SUFFIX    := $(if $(filter image,$(DDR3_PNR_LINE)),-nextpnr-45a986b8)
DDR3_APP       ?= pattern
override DDR3_APP := $(strip $(DDR3_APP))
ifneq ($(words $(DDR3_APP)) $(filter pattern reader loader matvec,$(DDR3_APP)),1 $(DDR3_APP))
$(error DDR3_APP must be pattern, reader, loader or matvec, not '$(DDR3_APP)')
endif
DDR3_READER    := $(filter reader,$(DDR3_APP))
DDR3_LOADER    := $(filter loader,$(DDR3_APP))
DDR3_MATVEC    := $(filter matvec,$(DDR3_APP))
DDR3_APP_SUFFIX := $(if $(DDR3_READER),-reader)$(if $(DDR3_LOADER),-loader)$(if $(DDR3_MATVEC),-matvec)
DDR3_BUILD     ?= $(BUILD)/ddr3-x$(DDR3_WIDTH)$(DDR3_SUFFIX)$(DDR3_APP_SUFFIX)
DDR3_TOP       := $(if $(filter loader matvec,$(DDR3_APP)),tms_ddr3_loader_ax7203,tms_ddr3_ax7203)
DDR3_LOADER_CORES := fpga_reset fpga_uart_tx fpga_uart_rx fpga_line_emitter fpga_ddr3_loader fpga_loader_wb fpga_wb_arbiter
DDR3_CORES     := $(if $(filter loader matvec,$(DDR3_APP)),$(DDR3_LOADER_CORES)$(if $(DDR3_MATVEC), fpga_ddr3_matvec),fpga_reset fpga_uart_tx fpga_line_emitter fpga_ddr3_status $(if $(DDR3_READER),fpga_ddr3_reader,fpga_ddr3_pattern))
DDR3_PATTERN   ?= 1
DDR3_PATTERN_BURSTS ?= 33554432
DDR3_PATTERN_HOLD   ?= 0
DDR3_PATTERN_ROUNDS ?= 0
override DDR3_PATTERN := $(strip $(DDR3_PATTERN))
ifneq ($(words $(DDR3_PATTERN)) $(filter 0 1,$(DDR3_PATTERN)),1 $(DDR3_PATTERN))
$(error DDR3_PATTERN must be 0 or 1, not '$(DDR3_PATTERN)')
endif
# The test's stop compare `round + 1 >= rounds` is lowered by t27c bff21b85 to a
# signed Verilog compare, so from 2^31 rounds on it would never stop (C and u32
# semantics would): only 0 .. 2^31-1 are accepted.
ifneq ($(shell awk 'BEGIN { r = "$(strip $(DDR3_PATTERN_ROUNDS))"; print (r ~ /^[0-9]+$$/ && r + 0 < 2147483648) ? "ok" : "bad" }'),ok)
$(error DDR3_PATTERN_ROUNDS must be a decimal number 0 .. 2147483647, not '$(DDR3_PATTERN_ROUNDS)')
endif
# DDR3_UART_DEBUG_BIST=1 (#61, the self-test counters): UberDDR3 read with
# `define UART_DEBUG_BIST` and its debug UART (9600 baud) on N15 in place of our
# lines (UBER_UART 1); the `uart_tx` module UberDDR3 then instantiates is
# ddr3/uart_tx_t27.v (our t27 transmitter). UberDDR3 then does not return to IDLE on a wrong self-test
# read (L3653-3655) and prints its phases and correct_read_data; a wrong read
# makes it report and stop in WAIT_UART. Needs DDR3_PATTERN=0 (no user port traffic).
DDR3_UART_DEBUG_BIST ?= 0
override DDR3_UART_DEBUG_BIST := $(strip $(DDR3_UART_DEBUG_BIST))
ifneq ($(words $(DDR3_UART_DEBUG_BIST)) $(filter 0 1,$(DDR3_UART_DEBUG_BIST)),1 $(DDR3_UART_DEBUG_BIST))
$(error DDR3_UART_DEBUG_BIST must be 0 or 1, not '$(DDR3_UART_DEBUG_BIST)')
endif
ifeq ($(DDR3_UART_DEBUG_BIST)$(DDR3_PATTERN),11)
$(error DDR3_UART_DEBUG_BIST=1 needs DDR3_PATTERN=0)
endif
# The read path (#62): x16 only, no pattern test, no UberDDR3 debug UART.
DDR3_READER_TRITS ?= 17694720
DDR3_READER_SEED  ?= 98
DDR3_READER_CAP   ?= 64
DDR3_READER_RUNS  ?= 0
ifneq ($(DDR3_READER),)
ifneq ($(DDR3_WIDTH),16)
$(error DDR3_APP=reader is built for x16 only (a 128-bit Wishbone word), not DDR3_WIDTH=$(DDR3_WIDTH))
endif
ifeq ($(origin DDR3_PATTERN),command line)
ifeq ($(DDR3_PATTERN),1)
$(error DDR3_APP=reader replaces the pattern test: leave DDR3_PATTERN unset)
endif
endif
override DDR3_PATTERN := 0
ifeq ($(DDR3_UART_DEBUG_BIST),1)
$(error DDR3_APP=reader needs DDR3_UART_DEBUG_BIST=0)
endif
# trits 1 .. 2^31-1 (the region arithmetic adds 79 in 32 bits), cap 1 .. 2^24-1 (the q line
# carries 24 bits), runs 0 .. 2^31-1, seed 0 .. 2^32-1, all decimal.
ifneq ($(shell awk 'BEGIN { t = "$(strip $(DDR3_READER_TRITS))"; c = "$(strip $(DDR3_READER_CAP))"; r = "$(strip $(DDR3_READER_RUNS))"; s = "$(strip $(DDR3_READER_SEED))"; \
  print (t ~ /^[0-9]+$$/ && t + 0 >= 1 && t + 0 < 2147483648 && c ~ /^[0-9]+$$/ && c + 0 >= 1 && c + 0 < 16777216 && \
         r ~ /^[0-9]+$$/ && r + 0 < 2147483648 && s ~ /^[0-9]+$$/ && s + 0 < 4294967296) ? "ok" : "bad" }'),ok)
$(error DDR3_READER_TRITS (1 .. 2^31-1), DDR3_READER_CAP (1 .. 2^24-1), DDR3_READER_RUNS (0 .. 2^31-1) and DDR3_READER_SEED must be decimal)
endif
endif
# The loader (#63 part 2): x16 only, no pattern test, no UberDDR3 debug UART.
ifneq ($(DDR3_LOADER),)
ifneq ($(DDR3_WIDTH),16)
$(error DDR3_APP=loader is built for x16 only (a 128-bit Wishbone word), not DDR3_WIDTH=$(DDR3_WIDTH))
endif
ifeq ($(origin DDR3_PATTERN),command line)
ifeq ($(DDR3_PATTERN),1)
$(error DDR3_APP=loader replaces the pattern test: leave DDR3_PATTERN unset)
endif
endif
override DDR3_PATTERN := 0
ifeq ($(DDR3_UART_DEBUG_BIST),1)
$(error DDR3_APP=loader needs DDR3_UART_DEBUG_BIST=0)
endif
endif
DDR3_READER_TAG := $(if $(DDR3_READER),$(DDR3_READER_TRITS)-$(DDR3_READER_SEED)-$(DDR3_READER_CAP)-$(DDR3_READER_RUNS))
DDR3_READER_DEFINE := $(if $(DDR3_READER),-DDDR3_READER )
DDR3_MATVEC_DEFINE := $(if $(DDR3_MATVEC),-DDDR3_LOADER_MATVEC )
DDR3_MATVEC_COLS   ?= 2560
DDR3_MATVEC_ROWS   ?= 8
DDR3_MATVEC_FMT    ?= 1
DDR3_MATVEC_WADDR  ?= 00004000
DDR3_MATVEC_AADDR  ?= 00001000
DDR3_MATVEC_DADDR  ?= 00000040
DDR3_MATVEC_MAGIC  ?= 74337633
DDR3_MATVEC_CAP    ?= 4
DDR3_MATVEC_CHPARAM := $(if $(DDR3_MATVEC),-set MATVEC_COLS $(DDR3_MATVEC_COLS) -set MATVEC_ROWS $(DDR3_MATVEC_ROWS) -set MATVEC_FMT $(DDR3_MATVEC_FMT) -set MATVEC_WADDR 32'h$(DDR3_MATVEC_WADDR) -set MATVEC_AADDR 32'h$(DDR3_MATVEC_AADDR) -set MATVEC_DADDR 32'h$(DDR3_MATVEC_DADDR) -set MATVEC_MAGIC 32'h$(DDR3_MATVEC_MAGIC) -set MATVEC_CAP $(DDR3_MATVEC_CAP) )
DDR3_READER_CHPARAM := $(if $(DDR3_READER),-set READER_TRITS $(DDR3_READER_TRITS) -set READER_SEED $(DDR3_READER_SEED) -set READER_CAP $(DDR3_READER_CAP) -set READER_RUNS $(DDR3_READER_RUNS) )
DDR3_PATTERN_TAG := $(DDR3_PATTERN)-$(DDR3_PATTERN_BURSTS)-$(DDR3_PATTERN_HOLD)-$(DDR3_PATTERN_ROUNDS)-uart$(DDR3_UART_DEBUG_BIST)
DDR3_PATTERN_VARIANT := PATTERN_TEST $(DDR3_PATTERN)$(if $(filter 1,$(DDR3_PATTERN)), (bursts $(DDR3_PATTERN_BURSTS)$(comma) hold $(DDR3_PATTERN_HOLD) clocks$(comma) rounds $(DDR3_PATTERN_ROUNDS)))$(if $(filter 1,$(DDR3_UART_DEBUG_BIST)),$(comma) UART_DEBUG_BIST 1 (ddr3_top uart_tx on N15$(comma) 9600 baud))
# The reader's variant names READER (tools/fpga-ddr3-capture.py reads it) and no PATTERN_TEST.
DDR3_APP_VARIANT := $(if $(DDR3_READER),READER (trits $(DDR3_READER_TRITS)$(comma) seed $(DDR3_READER_SEED)$(comma) cap $(DDR3_READER_CAP)$(comma) runs $(DDR3_READER_RUNS)),$(if $(DDR3_MATVEC),MATVEC (device dot of issue 64$(comma) cols $(DDR3_MATVEC_COLS)$(comma) rows $(DDR3_MATVEC_ROWS)$(comma) doorbell 0x$(DDR3_MATVEC_DADDR)$(comma) through the loader UART),$(if $(DDR3_LOADER),LOADER (UART 115200 baud$(comma) timeout 50 ms$(comma) probation 3 s$(comma) store 2^29 bytes$(comma) arbiter master 1 idle),$(DDR3_PATTERN_VARIANT))))
DDR3_UBER      := $(DDR3_SRC)/rtl/ddr3_top.v $(DDR3_SRC)/rtl/ddr3_controller.v $(DDR3_SRC)/rtl/ddr3_phy.v
DDR3_SOURCES   := $(foreach c,$(DDR3_CORES),$(RTL)/$(c).v) $(DDR3_DIR)/$(DDR3_TOP).v \
                  $(if $(filter 1,$(DDR3_UART_DEBUG_BIST)),$(DDR3_DIR)/uart_tx_t27.v) \
                  $(if $(DDR3_READER),$(DDR3_DIR)/tms_ddr3_reader.v)
# The synthesis of UberDDR3's openXC7 demos (carry chains and wide muxes allowed).
DDR3_YOSYS_SYNTH ?= synth_xilinx -flatten -abc9 -arch xc7
ifeq ($(DDR3_PNR_LINE),image)
override NEXTPNR_DDR3_REV := 45a986b8
DDR3_NEXTPNR   ?= $(NEXTPNR)
DDR3_CHIPDB    ?= $(CHIPDB)
DDR3_NEXTPNR_DEP :=
DDR3_PACK      ?= $(PACK)
DDR3_XRAY_IN   ?= /nextpnr-xilinx/xilinx/external/prjxray-db
DDR3_REV_NEXTPNR = echo "45a986b8 line: the image's nextpnr-xilinx (chip database and prjxray-db from /nextpnr-xilinx):"; \
                   cd $(ROOT) && $(DOCKER) git -C /nextpnr-xilinx rev-parse HEAD
DDR3_REV_XRAY  = cd $(ROOT) && $(DOCKER) git -C /nextpnr-xilinx/xilinx/external/prjxray-db rev-parse HEAD
else
NEXTPNR_DDR3_REV    ?= 0.9.7
NEXTPNR_DDR3_COMMIT ?= 0eae9fbb19dfb83cdd30d5048d8b0ba744180ad0
NEXTPNR_DDR3_URL    ?= https://github.com/openXC7/nextpnr-xilinx.git
DDR3_TOOLS     ?= $(ROOT)/build/tools/nextpnr-xilinx-$(NEXTPNR_DDR3_REV)
DDR3_XRAY      ?= $(DDR3_TOOLS)/xilinx/external/prjxray-db
DDR3_META      ?= $(DDR3_TOOLS)/xilinx/external/nextpnr-xilinx-meta
DDR3_CHIPDB    ?= $(BUILD)/chipdb-nextpnr-$(NEXTPNR_DDR3_REV)/$(PART).bin
# The binary names the commit it was built from; the chip database names its inputs.
DDR3_NEXTPNR_DEP := $(DDR3_TOOLS)/build/built-from-$(NEXTPNR_DDR3_COMMIT)
ifeq ($(DDR3_IN_IMAGE),1)
DDR3_CMAKE_FLAGS ?=
DDR3_TOOLS_RUN ?= $(DOCKER)
DDR3_NEXTPNR   ?= $(DOCKER) $(call REL,$(DDR3_TOOLS))/build/nextpnr-xilinx
endif
DDR3_TOOLS_RUN ?=
DDR3_NEXTPNR   ?= cd $(ROOT) && $(DDR3_TOOLS)/build/nextpnr-xilinx
DDR3_CMAKE_FLAGS ?= $(if $(filter Darwin,$(shell uname -s)),-DCMAKE_CXX_FLAGS=-I/opt/homebrew/include/eigen3,)
# fasm2frames and xc7frames2bit of the image, reading DDR3_XRAY (mounted at /xraydb).
DDR3_PACK      ?= docker run --rm --platform linux/amd64 -v "$(ROOT):/work" -v "$(DDR3_XRAY):/xraydb:ro" -w /work $(IMAGE)
DDR3_XRAY_IN   ?= /xraydb
DDR3_REV_NEXTPNR = sh $(ROOT)/tools/git-revision.sh --describe $(DDR3_TOOLS); sh $(ROOT)/tools/git-revision.sh $(DDR3_TOOLS); \
                   echo "binary built from $$(cd $(DDR3_TOOLS)/build 2>/dev/null && ls built-from-* 2>/dev/null | sed 's/^built-from-//' || echo unknown)"
DDR3_REV_XRAY  = sh $(ROOT)/tools/git-revision.sh $(DDR3_XRAY)
endif
DDR3_JOBS      ?= 4
# sa stops with 'No wire found for port O6' on this design (0.9.7), so heap only.
DDR3_SEEDS     ?= 1 2 3 4 5 6 7 8
DDR3_PLACERS   ?= heap
DDR3_PNR_ARGS  ?= --router router2 --timing-allow-fail
# Image digest recorded in the report (docker image inspect on the host that pulled it).
IMAGE_DIGEST   ?= $(shell docker image inspect --format '{{index .RepoDigests 0}}' $(IMAGE) 2>/dev/null)
DDR3_REPORT_ROOT ?= $(ROOT)/reports/fpga
DDR3_REPORT    ?= $(DDR3_REPORT_ROOT)/ddr3-build-$(shell date -u +%Y-%m-%d)-$(BUILD_ID)-x$(DDR3_WIDTH)$(DDR3_SUFFIX)$(DDR3_APP_SUFFIX)
DDR3_SWEEP_REPORT ?= $(DDR3_REPORT_ROOT)/ddr3-seed-sweep-$(shell date -u +%Y-%m-%d)-$(BUILD_ID)-x$(DDR3_WIDTH)$(DDR3_SUFFIX)$(DDR3_APP_SUFFIX).json
DDR3_SOURCE_LABEL ?= local
DDR3_CABLE     ?= $(CABLE)
OPENFPGALOADER ?= openFPGALoader
DDR3_EXPECT_SHA256 ?=
DDR3_FLASH_REPORT  ?=
# Clock periods of the PLL outputs in ns (awk, so that non-default PLL settings keep a consistent XDC).
ddr3_period    = $(shell awk 'BEGIN { printf "%.3f", $(1) }')
DDR3_DDR_NS    := $(call ddr3_period,5.0 / $(DDR3_PLL_MULT) * $(DDR3_DDR_DIV))
DDR3_CTRL_NS   := $(call ddr3_period,5.0 / $(DDR3_PLL_MULT) * $(DDR3_DDR_DIV) * 4)
DDR3_PNR_OUT   = --chipdb $(call REL,$(DDR3_CHIPDB)) --xdc $(call REL,$(DDR3_BUILD))/$(DDR3_TOP).xdc --json $(call REL,$(DDR3_BUILD))/$(DDR3_TOP).json

.PHONY: FORCE
FORCE:

# build/uberddr3/SOURCE is written last by the fetch script, after every hash matched.
ddr3-fetch: $(DDR3_SRC)/SOURCE
$(DDR3_SRC)/SOURCE: $(DDR3_LOCK) $(ROOT)/tools/fetch-uberddr3.sh
	sh $(ROOT)/tools/fetch-uberddr3.sh $(DDR3_LOCK) $(DDR3_SRC)
$(DDR3_UBER): $(DDR3_SRC)/SOURCE ;

ddr3-cores: $(foreach c,$(DDR3_CORES),$(RTL)/$(c).v)

ifneq ($(DDR3_PNR_LINE),image)
# nextpnr-xilinx for the DDR3 build: the release tag, checked against its
# commit, with the two submodules bbaexport and fasm2frames read (not the tests).
# The source is cloned next to an existing build/ directory without touching it,
# so a cached binary (CI) is kept. The binary is built when build/ has no
# built-from-<commit> stamp, that is when it is missing or was built from
# another commit; the stamp is written only after the source was checked.
ddr3-nextpnr: $(DDR3_NEXTPNR_DEP)
$(DDR3_TOOLS)/.source-$(NEXTPNR_DDR3_COMMIT):
	rm -rf $(DDR3_TOOLS).src; mkdir -p $(dir $(DDR3_TOOLS))
	git clone --depth 1 --branch $(NEXTPNR_DDR3_REV) --recurse-submodules=xilinx/external/prjxray-db \
	  --recurse-submodules=xilinx/external/nextpnr-xilinx-meta --shallow-submodules $(NEXTPNR_DDR3_URL) $(DDR3_TOOLS).src
	test "$$(git -C $(DDR3_TOOLS).src rev-parse HEAD)" = "$(NEXTPNR_DDR3_COMMIT)" || { echo "nextpnr-xilinx $(NEXTPNR_DDR3_REV) is not $(NEXTPNR_DDR3_COMMIT)"; exit 1; }
	mkdir -p $(DDR3_TOOLS); cp -a $(DDR3_TOOLS).src/. $(DDR3_TOOLS)/; rm -rf $(DDR3_TOOLS).src
	touch $@
$(DDR3_NEXTPNR_DEP): | $(DDR3_TOOLS)/.source-$(NEXTPNR_DDR3_COMMIT)
	@rev=$$(sh $(ROOT)/tools/git-revision.sh $(DDR3_TOOLS)); [ "$$rev" = "$(NEXTPNR_DDR3_COMMIT)" ] || \
	  { echo "$(DDR3_TOOLS) is at $$rev, not $(NEXTPNR_DDR3_COMMIT): remove it and run ddr3-nextpnr again"; exit 1; }
	@# build/ is made on the host: made by the image (root on a Linux runner), the host could not write the stamp.
	mkdir -p $(DDR3_TOOLS)/build
	cd $(ROOT) && $(DDR3_TOOLS_RUN) bash -c "cd $(call REL,$(DDR3_TOOLS)) && cmake -S . -B build -DARCH=xilinx -DBUILD_GUI=OFF -DBUILD_PYTHON=OFF \
	  -DUSE_OPENMP=OFF -DCMAKE_BUILD_TYPE=Release $(DDR3_CMAKE_FLAGS) && cmake --build build -j$(DDR3_JOBS)"
	rm -f $(DDR3_TOOLS)/build/built-from-*; touch $@

# The chip database is remade when one of its inputs changes: the nextpnr commit,
# the prjxray-db or the nextpnr-xilinx-meta revision. <chipdb>.inputs is
# rewritten only when its content changes (it is cached with the database in CI).
ddr3-chipdb: $(DDR3_CHIPDB)
$(DDR3_CHIPDB).inputs: FORCE | $(DDR3_NEXTPNR_DEP)
	@mkdir -p $(dir $@); { echo "nextpnr-xilinx $(NEXTPNR_DDR3_COMMIT)"; \
	  echo "prjxray-db $$(sh $(ROOT)/tools/git-revision.sh $(DDR3_XRAY))"; \
	  echo "nextpnr-xilinx-meta $$(sh $(ROOT)/tools/git-revision.sh $(DDR3_META))"; } > $@.tmp; \
	if cmp -s $@.tmp $@; then rm -f $@.tmp; else mv $@.tmp $@; echo "chip database inputs: $$(tr '\n' ' ' < $@)"; fi
$(DDR3_CHIPDB): $(DDR3_CHIPDB).inputs | $(DDR3_NEXTPNR_DEP)
	mkdir -p $(dir $@)
	cd $(ROOT) && $(DDR3_TOOLS_RUN) bash -c "python3 $(call REL,$(DDR3_TOOLS))/xilinx/python/bbaexport.py --device $(PART) \
	  --xray $(call REL,$(DDR3_XRAY))/artix7 --metadata $(call REL,$(DDR3_META))/artix7 --bba $(call REL,$@).bba \
	  && $(call REL,$(DDR3_TOOLS))/build/bbasm -l $(call REL,$@).bba $(call REL,$@) && rm -f $(call REL,$@).bba"
else
ddr3-nextpnr:
	@echo "DDR3_PNR_LINE=image uses the image's nextpnr-xilinx 45a986b8 (or NEXTPNR); nothing to build"
ddr3-chipdb: $(DDR3_CHIPDB)
endif

# Stamps name the settings a directory was built with.
$(DDR3_BUILD)/pll-$(DDR3_PLL_MULT)-$(DDR3_DDR_DIV).stamp:
	mkdir -p $(dir $@); rm -f $(dir $@)pll-*.stamp; touch $@
$(DDR3_BUILD)/build-id-$(BUILD_ID).stamp:
	mkdir -p $(dir $@); rm -f $(dir $@)build-id-*.stamp; touch $@
$(DDR3_BUILD)/pattern-$(DDR3_PATTERN_TAG).stamp:
	mkdir -p $(dir $@); rm -f $(dir $@)pattern-*.stamp; touch $@
$(DDR3_BUILD)/app-$(DDR3_APP)$(DDR3_READER_TAG).stamp:
	mkdir -p $(dir $@); rm -f $(dir $@)app-*.stamp; touch $@

# The XDC nextpnr reads: pins (x32 adds byte lanes 2-3) plus create_clock on
# the four PLL outputs by net name. The loader's uart_rx pin reaches the reader
# and matvec builds too: they synthesize the loader top with its receive port.
DDR3_LOADER_XDC := $(if $(filter loader matvec,$(DDR3_APP)),$(DDR3_DIR)/tms_ddr3_loader.xdc)
$(DDR3_BUILD)/$(DDR3_TOP).xdc: $(DDR3_DIR)/tms_ddr3_ax7203.xdc $(DDR3_DIR)/tms_ddr3_x32.xdc $(DDR3_BUILD)/pll-$(DDR3_PLL_MULT)-$(DDR3_DDR_DIV).stamp $(DDR3_LOADER_XDC)
	cat $(DDR3_DIR)/tms_ddr3_ax7203.xdc $(if $(filter 32,$(DDR3_WIDTH)),$(DDR3_DIR)/tms_ddr3_x32.xdc)$(if $(DDR3_LOADER_XDC), $(DDR3_DIR)/tms_ddr3_loader.xdc) > $@.tmp
	printf '%s\n' "# PLL outputs (DDR3_PLL_MULT=$(DDR3_PLL_MULT), DDR3_DDR_DIV=$(DDR3_DDR_DIV)), written by fpga/ax7203/Makefile" \
	  "create_clock -period $(DDR3_CTRL_NS) -name clk_ctrl [get_nets clk_ctrl]" \
	  "create_clock -period $(DDR3_DDR_NS) -name clk_ddr [get_nets clk_ddr]" \
	  "create_clock -period 5.000 -name clk_ref [get_nets clk_ref]" \
	  "create_clock -period $(DDR3_DDR_NS) -name clk_ddr_90 [get_nets clk_ddr_90]" >> $@.tmp
	mv $@.tmp $@

# Synthesis checks every UberDDR3 hash first (a locally edited file stops it)
# and records the source tree it ran on (HEAD and tracked changes).
ddr3-synth: $(DDR3_BUILD)/$(DDR3_TOP).json
$(DDR3_BUILD)/$(DDR3_TOP).json: $(DDR3_SOURCES) $(DDR3_UBER) $(DDR3_BUILD)/pll-$(DDR3_PLL_MULT)-$(DDR3_DDR_DIV).stamp $(DDR3_BUILD)/build-id-$(BUILD_ID).stamp $(DDR3_BUILD)/pattern-$(DDR3_PATTERN_TAG).stamp $(DDR3_BUILD)/app-$(DDR3_APP)$(DDR3_READER_TAG).stamp $(if $(filter loader matvec,$(DDR3_APP)),$(CURDIR)/brams_x36.txt)
	sh $(ROOT)/tools/fetch-uberddr3.sh --verify $(DDR3_LOCK) $(DDR3_SRC)
	mkdir -p $(DDR3_BUILD)
	{ git -C $(ROOT) rev-parse HEAD; git -C $(ROOT) status --porcelain --untracked-files=no; } > $(DDR3_BUILD)/source_tree.txt 2>/dev/null || true
ifeq ($(filter loader matvec,$(DDR3_APP)),)
	$(YOSYS) -q -l $(call REL,$(DDR3_BUILD))/yosys.log -p "read_verilog $(if $(filter 1,$(DDR3_UART_DEBUG_BIST)),-DUART_DEBUG_BIST )$(DDR3_READER_DEFINE)$(foreach s,$(DDR3_SOURCES) $(DDR3_UBER),$(call REL,$(s))); \
	  chparam -set NO_IOSERDES_LOOPBACK 1 ddr3_phy; \
	  chparam -set BYTE_LANES $(DDR3_LANES) -set PLL_MULT $(DDR3_PLL_MULT) -set DDR_DIV $(DDR3_DDR_DIV) -set BUILD_ID 32'h$(BUILD_ID) \
	    -set UBER_UART $(DDR3_UART_DEBUG_BIST) \
	    -set PATTERN_TEST $(DDR3_PATTERN) -set PATTERN_BURSTS $(DDR3_PATTERN_BURSTS) -set PATTERN_HOLD $(DDR3_PATTERN_HOLD) \
	    -set PATTERN_ROUNDS $(DDR3_PATTERN_ROUNDS) $(DDR3_READER_CHPARAM)$(DDR3_MATVEC_CHPARAM)$(DDR3_TOP); \
	  $(DDR3_YOSYS_SYNTH) -top $(DDR3_TOP); setundef -zero -params; tee -o $(call REL,$(DDR3_BUILD))/yosys_stat.txt stat; \
	  write_json $(call REL,$(DDR3_BUILD))/$(DDR3_TOP).json"
else
# The loader (#63 part 2): its t27 cores with -nomem2reg (block RAM for the receive FIFO and the
# staging buffer), every memory in the RAMB36 1K x 36 configuration (brams_x36.txt), as part 1.
	$(YOSYS) -q -l $(call REL,$(DDR3_BUILD))/yosys.log -p "read_verilog -nomem2reg $(foreach c,$(DDR3_CORES),$(call REL,$(RTL)/$(c).v)); \
	  read_verilog $(DDR3_MATVEC_DEFINE)$(call REL,$(DDR3_DIR)/$(DDR3_TOP).v) $(foreach s,$(DDR3_UBER),$(call REL,$(s))); \
	  chparam -set NO_IOSERDES_LOOPBACK 1 ddr3_phy; \
	  chparam -set PLL_MULT $(DDR3_PLL_MULT) -set DDR_DIV $(DDR3_DDR_DIV) -set BUILD_ID 32'h$(BUILD_ID) $(DDR3_MATVEC_CHPARAM)$(DDR3_TOP); \
	  $(DDR3_YOSYS_SYNTH) -top $(DDR3_TOP) -run :map_memory; \
	  memory_libmap -lib +/xilinx/lutrams_xc5v.txt -lib $(call REL,$(CURDIR))/brams_x36.txt; \
	  techmap -map +/xilinx/lutrams_xc5v_map.v -map +/xilinx/brams_xc6v_map.v; \
	  $(DDR3_YOSYS_SYNTH) -top $(DDR3_TOP) -run map_ffram:; \
	  setundef -zero -params; tee -o $(call REL,$(DDR3_BUILD))/yosys_stat.txt stat; \
	  write_json $(call REL,$(DDR3_BUILD))/$(DDR3_TOP).json"
endif
	grep -c "multiple conflicting drivers" $(DDR3_BUILD)/yosys.log > $(DDR3_BUILD)/yosys_conflicting_drivers.txt || true

# Seed search: the first placer/seed whose routed design meets every clock
# constraint wins; if none does, the first one that routed (its report then
# carries the FAIL). Only the routed-design analysis (after "Router2 time")
# counts, not the placer's estimate earlier in the log. "Timing met" covers
# register-to-register paths in the fabric only: nextpnr-xilinx gives the ports
# of OSERDESE2, ISERDESE2, IDELAYE2 and IDELAYCTRL no timing class, so paths into
# and out of the PHY primitives and the PHY's I/O timing are not analysed (the
# report counts their LUT levels). The winner's SDF and routed netlist are kept
# for the report (clock nets that leave the global network through a LUT); the
# options are the ones 45a986b8 has too (it has no --report).
ddr3-pnr: $(DDR3_BUILD)/$(DDR3_TOP).fasm
$(DDR3_BUILD)/$(DDR3_TOP).fasm: $(DDR3_BUILD)/$(DDR3_TOP).json $(DDR3_CHIPDB) $(DDR3_BUILD)/$(DDR3_TOP).xdc | $(DDR3_NEXTPNR_DEP)
	@rm -f $@ $(DDR3_BUILD)/try_*; pass=; first=; for p in $(DDR3_PLACERS); do for s in $(DDR3_SEEDS); do \
	  t=$${p}_seed$$s; echo "nextpnr-xilinx $(NEXTPNR_DDR3_REV) placer $$p seed $$s"; \
	  $(DDR3_NEXTPNR) $(DDR3_PNR_OUT) --fasm $(call REL,$(DDR3_BUILD))/try_$$t.fasm \
	    --sdf $(call REL,$(DDR3_BUILD))/try_$$t.sdf --write $(call REL,$(DDR3_BUILD))/try_$$t.routed.json \
	    --seed $$s --placer $$p $(DDR3_PNR_ARGS) > $(DDR3_BUILD)/nextpnr_$$t.log 2>&1; st=$$?; \
	  if [ $$st -eq 0 ] && [ -s $(DDR3_BUILD)/try_$$t.fasm ] && ! grep -q "Failed to find a route" $(DDR3_BUILD)/nextpnr_$$t.log; then \
	    sed -n '/Router2 time/,$$p' $(DDR3_BUILD)/nextpnr_$$t.log | grep "Max frequency" | sed 's/^/  /'; \
	    [ -n "$$first" ] || first=$$t; \
	    if ! sed -n '/Router2 time/,$$p' $(DDR3_BUILD)/nextpnr_$$t.log | grep "Max frequency" | grep -q FAIL; then pass=$$t; break 2; fi; \
	  else echo "  did not route (exit $$st)"; fi; \
	done; done; pick=$${pass:-$$first}; \
	[ -n "$$pick" ] || { echo "no placer/seed routed"; exit 1; }; \
	mv $(DDR3_BUILD)/try_$$pick.fasm $@; \
	mv $(DDR3_BUILD)/try_$$pick.sdf $(DDR3_BUILD)/$(DDR3_TOP).sdf; mv $(DDR3_BUILD)/try_$$pick.routed.json $(DDR3_BUILD)/$(DDR3_TOP).routed.json; \
	cp $(DDR3_BUILD)/nextpnr_$$pick.log $(DDR3_BUILD)/nextpnr.log; rm -f $(DDR3_BUILD)/try_*; \
	if [ -n "$$pass" ]; then v="timing met"; else v="no seed met timing"; fi; \
	echo "chosen placer/seed $$pick ($$v)"; echo "$$pick $$v; tried placers '$(DDR3_PLACERS)' seeds '$(DDR3_SEEDS)' in this order, stopping at the first that met timing" > $(DDR3_BUILD)/nextpnr_choice.txt
	grep -E "SLICE_LUTX:|SLICE_FFX:|CARRY4:|OSERDESE2:|ISERDESE2:|IDELAYE2:|PLLE2_ADV:" $(DDR3_BUILD)/nextpnr.log || true

ddr3-bit: $(DDR3_BUILD)/$(DDR3_TOP).bit
$(DDR3_BUILD)/$(DDR3_TOP).bit: $(DDR3_BUILD)/$(DDR3_TOP).fasm
	cd $(ROOT) && $(DDR3_PACK) bash -c "source /prjxray/env/bin/activate && \
	  fasm2frames --db-root $(DDR3_XRAY_IN)/artix7 --part $(PART) $(call REL,$<) $(call REL,$(DDR3_BUILD))/$(DDR3_TOP).frames && \
	  /prjxray/build/tools/xc7frames2bit --part_file $(DDR3_XRAY_IN)/artix7/$(PART)/part.yaml --part_name $(PART) \
	    --frm_file $(call REL,$(DDR3_BUILD))/$(DDR3_TOP).frames --output_file $(call REL,$@) && \
	  git -C /prjxray rev-parse HEAD > $(call REL,$(DDR3_BUILD))/prjxray_revision.txt"
	@echo "frames: $$(wc -l < $(DDR3_BUILD)/$(DDR3_TOP).frames) lines; bit: $$(wc -c < $@) bytes"

# Tool revisions and the report (trinity.fpga-build.v1). The report holds
# sha256 values, counts and logs, never the bitstream or the FASM. The .bit
# header carries the time xc7frames2bit ran, so its whole-file sha256 names one
# file; the FASM and frames sha256 (and the .bit's from its sync word on) are
# what a rebuild reproduces.
# An existing report is committed evidence (the default name carries today's
# date and BUILD_ID, so a rebuild with an earlier BUILD_ID on the same day would
# land on it): it is replaced only with DDR3_REPORT_OVERWRITE=1.
DDR3_REPORT_OVERWRITE ?= 0
ddr3-report: $(DDR3_BUILD)/$(DDR3_TOP).bit
	@if [ -e "$(DDR3_REPORT)/build.json" ] && [ "$(DDR3_REPORT_OVERWRITE)" != 1 ]; then \
	  echo "ddr3-report: $(DDR3_REPORT)/build.json exists; set DDR3_REPORT=<new directory> (or DDR3_REPORT_ROOT), or DDR3_REPORT_OVERWRITE=1 to replace it"; exit 1; fi
	cd $(ROOT) && $(YOSYS) -V > $(DDR3_BUILD)/yosys_version.txt
	{ $(DDR3_REV_NEXTPNR); } > $(DDR3_BUILD)/nextpnr_revision.txt 2>/dev/null || true
	$(DDR3_NEXTPNR) --version >> $(DDR3_BUILD)/nextpnr_revision.txt 2>&1 || true
	{ $(DDR3_REV_XRAY); } 2>/dev/null | head -1 > $(DDR3_BUILD)/prjxray_db_revision.txt
	python3 $(ROOT)/tools/fpga-build-report.py --artifact-dir $(DDR3_BUILD) --top $(DDR3_TOP) --commit $(BUILD_ID) \
	  --source "$(DDR3_SOURCE_LABEL)" --output $(DDR3_REPORT) \
	  --flow "yosys $(DDR3_YOSYS_SYNTH); nextpnr-xilinx $(NEXTPNR_DDR3_REV) placer/seed search over '$(DDR3_PLACERS)' x '$(DDR3_SEEDS)' $(DDR3_PNR_ARGS); prjxray fasm2frames + xc7frames2bit ($(IMAGE))" \
	  --ddr3 --uberddr3-dir $(DDR3_SRC) --lock $(DDR3_LOCK) --image-digest "$(IMAGE_DIGEST)" --chipdb $(DDR3_CHIPDB) \
	  --variant "x$(DDR3_WIDTH) BYTE_LANES $(DDR3_LANES), PLL CLKFBOUT_MULT $(DDR3_PLL_MULT), DDR3 clock VCO/$(DDR3_DDR_DIV) ($(DDR3_DDR_NS) ns), controller $(DDR3_CTRL_NS) ns, $(DDR3_APP_VARIANT)"

# Both widths with one command. Each width is its own make run, so
# DDR3_WIDTH, DDR3_BUILD and DDR3_REPORT cannot be set here: DDR3_REPORT_ROOT
# places both reports.
ddr3-all:
	$(if $(DDR3_ALL_CLASH),$(error ddr3-all builds x16 and x32: set DDR3_REPORT_ROOT, not DDR3_WIDTH, DDR3_BUILD or DDR3_REPORT))
	$(MAKE) ddr3-bit ddr3-report DDR3_WIDTH=16
	$(MAKE) ddr3-bit ddr3-report DDR3_WIDTH=32

# Every placer/seed of DDR3_PLACERS x DDR3_SEEDS on the current netlist, without
# the seed search's early stop, into $(DDR3_BUILD)/sweep/, and a summary per run
# (routed Fmax, critical path, utilisation, PLL tables, the fabric-clock LUT with
# its placement and SDF delays, sha256 of each log and FASM) in DDR3_SWEEP_REPORT. Evidence for the seed spread; the
# build itself is untouched.
ddr3-sweep: $(DDR3_BUILD)/$(DDR3_TOP).json $(DDR3_CHIPDB) $(DDR3_BUILD)/$(DDR3_TOP).xdc | $(DDR3_NEXTPNR_DEP)
	@rm -rf $(DDR3_BUILD)/sweep; mkdir -p $(DDR3_BUILD)/sweep; for p in $(DDR3_PLACERS); do for s in $(DDR3_SEEDS); do \
	  t=$${p}_seed$$s; echo "nextpnr-xilinx $(NEXTPNR_DDR3_REV) placer $$p seed $$s"; \
	  $(DDR3_NEXTPNR) $(DDR3_PNR_OUT) --fasm $(call REL,$(DDR3_BUILD))/sweep/$$t.fasm \
	    --sdf $(call REL,$(DDR3_BUILD))/sweep/$$t.sdf --write $(call REL,$(DDR3_BUILD))/sweep/$$t.routed.json \
	    --seed $$s --placer $$p $(DDR3_PNR_ARGS) > $(DDR3_BUILD)/sweep/$$t.log 2>&1 || echo "  exit $$?"; \
	  sed -n '/Router2 time/,$$p' $(DDR3_BUILD)/sweep/$$t.log | grep "Max frequency" | sed 's/^/  /'; \
	done; done
	python3 $(ROOT)/tools/fpga-seed-sweep.py --top $(DDR3_TOP) --commit $(BUILD_ID) \
	  --label "nextpnr-xilinx $(NEXTPNR_DDR3_REV), x$(DDR3_WIDTH), $(DDR3_PNR_ARGS)" \
	  --netlist $(DDR3_BUILD)/$(DDR3_TOP).json --chipdb $(DDR3_CHIPDB) --output $(DDR3_SWEEP_REPORT) $(DDR3_BUILD)/sweep/*.log

# SRAM only: nothing is written to the configuration flash. Needs the board and
# the founders' approval of the run (issue #61); never part of a build. It never
# builds either: it flashes the existing bitstream only when its sha256 equals
# DDR3_EXPECT_SHA256, or, with DDR3_FLASH_REPORT=<build.json>, when the whole file
# matches the report's bitstream sha256 or the file from its sync word on matches
# the report's sha256_from_sync (a rebuild differs from the report only in the
# header time; tools/ddr3-flash-check.py).
ddr3-flash:
	@bit=$(DDR3_BUILD)/$(DDR3_TOP).bit; \
	if [ -n "$(DDR3_EXPECT_SHA256)" ]; then how="--expect $(DDR3_EXPECT_SHA256)"; \
	elif [ -n "$(DDR3_FLASH_REPORT)" ]; then how="--report $(DDR3_FLASH_REPORT)"; \
	else echo "ddr3-flash: set DDR3_EXPECT_SHA256=<sha256> or DDR3_FLASH_REPORT=<build.json>; nothing was flashed"; exit 1; fi; \
	[ -f "$$bit" ] || { echo "ddr3-flash: no $$bit (ddr3-flash never builds: make ddr3-bit ddr3-report first); nothing was flashed"; exit 1; }; \
	python3 $(ROOT)/tools/ddr3-flash-check.py "$$bit" $$how || exit 1; \
	$(OPENFPGALOADER) -c $(DDR3_CABLE) "$$bit"

ddr3-clean:
	rm -rf $(DDR3_BUILD)
