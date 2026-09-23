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
        bram-gen bram-sim bram-synth bram-pnr bram-bit bram-flash bram-capture bram-ooc

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
