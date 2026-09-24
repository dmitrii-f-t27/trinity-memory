# UART loader (issue #63)

A host-to-board loader over the AX7203's CP2102N UART with per-chunk
acknowledgement: every chunk is CRC-checked on the device, acknowledged or
refused with a reason, retransmitted by the host until it is acknowledged, and
readable back through CRC frames, so nothing is lost silently. Part 1 (this
document's results) loads into block RAM; part 2 puts a DDR3 Wishbone write master
behind the same write port ("DDR3", below).

| File | Role |
| --- | --- |
| [`t27/rtl/fpga_uart_rx.t27`](../t27/rtl/fpga_uart_rx.t27) | 8N1 receiver |
| [`t27/rtl/fpga_uart_loader.t27`](../t27/rtl/fpga_uart_loader.t27) | receive FIFO, frame parser, CRC-32, staging buffer, commit, read-back, responses, baud change |
| [`t27/rtl/fpga_loader_store.t27`](../t27/rtl/fpga_loader_store.t27) | 256 KiB block-RAM store behind the write and read ports |
| [`fpga/ax7203/tms_uart_loader.v`](../fpga/ax7203/tms_uart_loader.v) | wiring only (clocks, reset, transmitter, line emitter, the three modules above) |
| [`tools/uart_loader_protocol.py`](../tools/uart_loader_protocol.py) | the protocol in Python: frame encoders, stream decoder, a byte-level reference model of the device |
| [`tools/fpga-uart-loader.py`](../tools/fpga-uart-loader.py) | host tool: stop-and-wait, retransmit, read-back verify, fault injection, statistics |
| [`tests/test_uart_loader.py`](../tests/test_uart_loader.py), [`tests/tb_uart_loader.v`](../tests/tb_uart_loader.v) | generated C against the model; Icarus runs of the board top and of the cores behind a stalling memory; the host tool against the model |

Every state machine and rule is t27 compiled by the pinned t27c; the Verilog top
only wires modules, as in the other AX7203 designs.

## Protocol

All integers are little endian. CRC-32 is zlib's (reflected polynomial
0xEDB88320, initial value and final XOR 0xFFFFFFFF), the one of
[`t27/container.t27`](../t27/container.t27); on the device it runs one byte per
clock.

**Host to device, one frame per command:**

```
A5 5A | cmd | seq | addr[4] | len[2] | payload[len]  (load only) | crc32[4]
        \______________ CRC covers these bytes ______________/
```

| cmd | Meaning | Rules |
| --- | --- | --- |
| `L` (0x4C) load | write `len` payload bytes to the store at `addr` | 1 <= len <= 4096, addr + len <= store size |
| `R` (0x52) read | send back `len` stored bytes from `addr` | same bounds, no payload |
| `S` (0x53) status | send the 22 counter lines | len 0 |
| `B` (0x42) baud | after the ack, change the bit period to `addr` clocks | len 0, 16 <= addr <= 65535 |

- **No command byte, no action.** While it hunts, the parser ignores every byte
  that does not start `A5 5A` (counted as ignored bytes). An `A5` followed by
  anything else is ignored as well; `A5 A5 5A` still starts a frame. This replaces
  the benches' rule "any falling edge on RX starts a run".
- **Resynchronisation.** After a bad header (unknown command, length out of
  bounds) the device answers at once and hunts for the next `A5 5A`; the rest of
  that frame is hunted through as garbage.
- **Inter-byte timeout.** Once `A5 5A` has arrived, a gap of 50 ms (1 250 000
  clocks at 25 MHz, `LOADER_TIMEOUT`) with no byte ends the frame with a
  `timeout` nak (after a lone `A5`, silently). This drops the partial frame of a
  dropped byte, of a host that stopped or restarted mid-frame, and of the glitch
  a port can make when it opens.
- **Duplicates.** A load whose seq, addr, len and CRC equal the last committed
  load's is acknowledged again with reason `duplicate` and not committed (its
  first ack was lost). The host needs no other state: a retransmission after a
  lost ack costs one frame time and never writes twice.
- **Stop-and-wait.** The host sends one chunk, waits for the ack with that seq,
  and retransmits after a nak or after the frame's wire time plus 0.5 s without
  an ack, but first stays silent for 0.15 s (three device timeouts) so that the
  device has dropped any partial frame and hunts again.

**Device to host.** Response lines are the benches' 20-byte lines (tag, 8 hex
digits `a`, 10 hex digits `b`, LF) through
[`fpga_line_emitter.t27`](../t27/rtl/fpga_line_emitter.t27), with
`b = (check << 32) | v` and `check` the low byte of the CRC-32 of the tag, `a`
(4 bytes) and `v` (4 bytes), so a corrupted line is recognised.

| Tag | `a` | `v` |
| --- | --- | --- |
| `H` (after reset) | build id | `(1 << 24) \| (12 << 16) \| (log2 store << 8) \| 10`: protocol 1, 4096-byte chunks, 2^18-byte store, 1024-entry FIFO |
| `A` ack, `N` nak | `(seq << 24) \| (reason << 20) \| (command << 17) \| (seq known << 16) \| len` | `(frames committed << 16) \| naks`, both mod 2^16 |
| `C` status | `(seq << 24) \| (22 << 16) \| index` | the counter |

Reasons: 0 `ok`, 1 `duplicate` (both in `A`), 2 `crc`, 3 `length`, 4 `timeout`,
5 `framing` (a byte with a low stop bit inside a frame), 6 `command`, 7
`overflow` (the receive FIFO dropped bytes of this frame). Command numbers: 1
load, 2 read, 3 status, 4 baud, 0 unknown.

A read is answered with a binary frame in the host's format, command `r`
(0x72): `A5 5A 72 seq addr[4] len[2] data[len] crc32[4]`. Line tags are ASCII
letters, so a byte 0xA5 always starts a read-back frame.

Status counters (`C` lines, index order): `rx_bytes`, `rx_framing_errors`,
`rx_false_starts`, `fifo_overflow_bytes`, `fifo_high_water`, `ignored_bytes`,
`frames_committed`, `frames_duplicate`, `nak_crc`, `nak_length`, `nak_timeout`,
`nak_framing`, `nak_command`, `nak_overflow`, `bytes_committed`, `readbacks`,
`baud_div`, `baud_reverts`, `last_seq` (bit 8: valid), `config` (as in `H`),
`clocks` (design clocks since reset, low 32 bits), `build_id`.

## Design

**Receiver.** RX passes three flip-flops (the benches' rx1-rx3). A low line
starts a character; the start bit is sampled again half a bit later and a high
level there is a false start (a glitch shorter than half a bit), not a byte. Data
and stop bits are sampled in the middle of their bits, `baud_div` clocks apart; a
low stop bit is a framing error, after which the receiver waits for the line to
go high, so a break gives one character.

**Receive FIFO and backpressure; why no ack is lost.** Received bytes enter a
FIFO of 1024 entries in block RAM (the byte, its framing error and a gap mark). The
parser takes an entry every other clock while it receives, and none while it
commits a chunk, sends a read-back or waits for the line emitter. A response is
handed to the emitter only when the emitter is idle and the parser waits for that,
so a response is never overwritten, dropped or cut short by a busy transmitter;
the price is that the parser stops reading the FIFO meanwhile. Only a FIFO
overflow can lose a received byte, and then the next byte carries the gap mark and
the frame it belonged to gets an `overflow` nak (if nothing followed the lost
bytes, the timeout does it, with the same reason). With stop-and-wait the host
sends nothing while the device answers, so the FIFO holds at most a few bytes; the
1024 entries absorb 89 ms of line at 115200 baud, more than any response except a
read-back of more than 1024 bytes takes (a host that sends during such a read-back
gets `overflow`, as the simulation shows).

**Staging buffer, chunk size.** A load's payload goes into a 4096-byte staging
buffer and reaches the store only after its CRC matched, so a corrupt chunk never
touches the store. 4096 bytes is one RAMB36 in the 1K x 36 configuration (4 bytes
per word). At 115200 baud a 4096-byte chunk is 4110 bytes on the wire (356.8 ms);
the 14 bytes of framing and the 20-byte ack cost 0.34 % and, in stop-and-wait, one
ack line (1.74 ms) plus the host's turnaround per chunk. Smaller chunks lose more
to turnaround, larger ones more to a retransmission, and the staging buffer would
need more blocks. One chunk of depth is enough: stop-and-wait never has two chunks
in flight, and the commit (3 clocks per byte, 0.49 ms for 4096 bytes at 25 MHz)
is 700 times shorter than the chunk's wire time.

**Write and read ports.** The commit writes one byte per transfer: `wr_valid`,
`wr_addr`, `wr_data` (low 8 bits) and `wr_last` (the chunk's last byte); a
transfer happens on a clock where `wr_valid` and `wr_ready` are both high, and the
loader acknowledges the chunk only after `wr_idle` (every write done). A read
issues `rd_req` with `rd_addr` until `rd_ready`, then waits for `rd_valid` with
`rd_data`. The loader holds no assumption about latency: in simulation the same
loader runs behind a memory that withholds `wr_ready` and `rd_ready` at random,
answers reads after 1-16 clocks and keeps `wr_idle` low for up to 15 clocks after
each write (34 714 stalled write clocks in one run, every byte read back right).

**Store.** 256 KiB in four banks of 16 384 32-bit words, each byte written as a
read-modify-write of its word (two clocks), so that every memory of the design is
in the RAMB36 1K x 36 configuration; see "Board results" for why.

**Baud rate.** A `B` frame changes receiver and transmitter to a new divisor
after its ack has left. Unless a frame with a good CRC arrives within 3 s
(`LOADER_PROBATION`, 75 000 000 clocks), the device returns to the divisor it was
built with (217, 115200 baud at 25 MHz) and counts a revert; the reset button
returns to it too. A host that loses the device at a new rate therefore only has
to wait 3 s and go back to 115200.

**Clock.** 25 MHz from the 200 MHz oscillator (the benches' `CLOCK_MODE` 1).

## Simulation

`python3 -m unittest tests.test_uart_loader` (also run by `tools/test-t27.sh` and
`make -C fpga/ax7203 loader-sim`; needs `T27_ROOT` and Icarus, skipped without them).
A host model drives RX at the bit level from a script with its own bit period and
decodes TX; [`tools/uart_loader_protocol.py`](../tools/uart_loader_protocol.py)'s
`DeviceModel` predicts every byte the device sends for the same byte stream, and
every received line and read-back frame must equal the prediction (only the status
lines' FIFO high-water mark and clock counter depend on timing and are not
predicted). The simulation runs at 16 clocks per bit (1.5625 Mbaud) to keep it
short; two scenarios run the board divisor 217 with the board's 50 ms timeout.

| Scenario | Bench | What it shows |
| --- | --- | --- |
| `transfer_top_+0.03` | board top, host bit period +3 % | 12 loads of random lengths (1, 2, 3, 17, 255, 256, 1000, 4096 and four random) at random addresses, each read back identical; status |
| `transfer_ports_-0.03` | cores behind the stalling memory, host -3 % | the same, with `wr_ready`/`rd_ready` withheld at random, reads answered after 1-16 clocks and `wr_idle` low up to 15 clocks per write: 12 008 writes, 34 714 stalled write clocks, one `wr_last` per chunk |
| `faults_-0.03` | board top, host -3 % | a corrupt byte (`crc`), a duplicate (`duplicate`, not committed), a dropped byte and a host stopping mid-frame (`timeout`), unknown command, lengths 0, 4097 and past the store, status with a length, divisor 8 (`command`, `length`), a low stop bit inside a frame (`framing`), garbage and stray `A5` bytes, glitches of 0.2 bit (a false start, no byte), 3 bits (byte 0xFC) and 30 bits (a break: one 0x00 with a framing error), a 0.2-bit glitch inside a frame (the frame survives), the reset button during a frame (`H` again, the store keeps its data); every retransmission acknowledged, every read-back identical |
| `pipelined` | board top | six loads, a status, a read and a duplicate sent back to back without waiting: every response, in order |
| `overflow` | board top | a 2000-byte load sent while the device sends a 4096-byte read-back: the FIFO reaches 1024 entries and drops bytes, the load gets `overflow`, its retransmission is acknowledged and reads back identical |
| `baud` | board top, divisor 32 | a `B` frame to 16, traffic at 16, then a `B` frame to 20 that the host does not follow: after the probation time the device is back at 32 (`baud_reverts` 1) |
| `board_rate_+0.02`, `board_rate_-0.02` | board top, divisor 217, 50 ms timeout | a 64-byte load and read-back at 115200 with the host 2 % fast and 2 % slow |

The turnaround from the end of a load frame to its ack (design clocks; the summary is
written to `build/fpga/loader-sim/summary.json`, copied to
[`reports/fpga/uart-loader-sim-2026-09-24-22844259.json`](../reports/fpga/uart-loader-sim-2026-09-24-22844259.json)):
3 clocks per payload byte in the board top (4096 bytes: 12 288 clocks, 0.49 ms at
25 MHz), 5.9 behind the stalling memory (4096 bytes: 24 000 clocks). The host tool
itself is tested against `DeviceModel` behind a fake port (`HostTool` in the same
file): corrupt, drop, abort, garbage and duplicate each end in an acknowledged,
identical chunk.

## Build

`make -C fpga/ax7203 loader-bit FREQ_MHZ=25 SEEDS=3` with native yosys 0.69 and
nextpnr-xilinx 45a986b8 (`NEXTPNR`, `CHIPDB` of the image as for the block-RAM
benches), prjxray of `regymm/openxc7@sha256:eced1cdd…`; `make loader-report` writes
the record. Build of record: commit 2284425 (`BUILD_ID` 22844259),
[`reports/fpga/loader-build-2026-09-24-22844259/`](../reports/fpga/loader-build-2026-09-24-22844259/build.json):
66 RAMB36E1 (64 store, 1 staging buffer, 1 FIFO; all 1K x 36), 4 547 SLICE_LUTX,
1 780 SLICE_FFX, nextpnr's routed estimate 79.55 MHz for the 25 MHz clock (PASS),
`heap` placer seed 3, `router2`; bitstream sha256 `ac8ff0af…` (from the sync word
`d3edc995…`), FASM `b07f434e…`. Seed 1 did not route: router2 still had 97
overused wires after 13 iterations (about 25 minutes) and was stopped; seed 3
routed in about 5 minutes. Two more placements of the same netlist with seed 3 in
scratch directories gave two other FASMs (`04435078…`, estimate 78.85 MHz;
`bcfa5903…`, 79.03 MHz), so the FASM of this design is not reproducible bit for bit
from the netlist on this machine (the flashed bitstream is identified by its sha256
above). The other designs' make
commands are unchanged: `make -n -B` of `gen`, `sim`, `bit`, `bram-sim`,
`bram-bit` (four variants), `bram-ooc`, `ddr3-bit` (three variants), `ddr3-report`
(two), `ddr3-sweep` and `clean` prints the same commands before and after this
change, and none of their source files changed.

## Board results (2026-09-24)

ALINX AX7203, IDCODE 0x3636093, DNA 0x00389c0c2d85e85c; SRAM load only, CP2102N at
`/dev/cu.usbserial-110`, macOS, pyserial. Every run is one
`tools/fpga-uart-loader.py` record under
[`reports/fpga/uart-loader-2026-09-24-22844259/`](../reports/fpga/uart-loader-2026-09-24-22844259/)
with IDCODE, DNA and XADC before and after, each attempt with its times and reply,
the device's status before and after, and the received bytes (`*.rx.bin.gz`; run 6's
1.15 MB file is not committed, its sha256 is in the record).

**Payload.** Rows 0-319 of the layer-0 query projection of BitNet b1.58 2B4T
(`model.layers.0.self_attn.q_proj.weight`, 320 x 2560 = 819 200 trits: +1 284 129,
0 251 701, -1 283 370), from `tools/extract-bram-trits.py --rows 320` (the pinned
packed checkpoint through the t27 decoders, the GGUF's I2_S tensor agreeing trit for
trit), packed by the host tool into a TMEM v1 dense5 container
([format.md](format.md)): 24-byte header and 163 840 payload bytes, 163 864 bytes,
sha256 in each record (`payload`). At 4096 bytes per chunk that is 41 chunks (40 of
4096, one of 24); runs 3-5 load its first 98 304 or 16 384 bytes.

| Run | What | Chunks | Attempts | Payload rate, load / read-back | Result |
| --- | --- | ---: | ---: | --- | --- |
| `run1-clean` | SRAM load (H line: build 22844259), whole payload at 0x0, 115200 | 41 | 41 | 9 217 / 11 197 B/s | acked, read back identical |
| `run2-faults` | whole payload at 0x10000; port closed and opened 5 times first; corrupt chunks 3 and 25, drop 7 and 30, abort 11, garbage before 15, duplicate 19 | 41 | 46 | 7 509 / 11 218 B/s | acked, identical |
| `run3-clean`, `run4-clean` | 98 304 bytes at 0x20000 | 24 each | 24 each | 9 271 and 9 263 / 11 124 and 11 129 B/s | identical |
| `run5-baud` | 16 384 bytes at 0x30000, then 16 384 bytes at each of 230400, 460800, 921600, 1000000, 1500000 | 4 + 5 x 4 | 1 each | see below | identical at every rate |
| `run6-baud-full` | whole payload at 115200, then the whole payload twice at each of 921600, 1000000, 1500000 | 41 + 6 x 41 | 1 each | see below | identical every time |

Die temperature (XADC) 50.3 C before run 1 and between 42.1 and 46.6 C around the others.

**Faults (run 2).** Each injected fault was detected and the chunk retransmitted
until acknowledged: the two corrupt chunks got `crc` naks, the two with a dropped
byte `timeout` naks, each then acknowledged on the next attempt (1.03 s per chunk
instead of 0.43 s: the nak, the 0.15 s quiet time and the resend). The aborted
chunk (half a frame, then the port closed and opened) got its `timeout` nak from the
device (the device's `nak_timeout` counter rose by three over the run: two drops,
one abort), but the host did not receive it, because the port was being reopened;
the host resent after its own timeout (1.50 s for that chunk). The 64 garbage bytes
were counted as `ignored_bytes` 64 and caused no nak. The duplicate was answered
`duplicate`, `frames_duplicate` became 1, and `frames_committed` rose by exactly 41
over the run. Retransmits by reason: `crc` 2, `timeout` 2, no reply 1.

**Port-open glitch.** The port was opened once per run (six times), five more
times before run 2 and once mid-frame in run 2. The device's `rx_framing_errors`
and `rx_false_starts` stayed 0 throughout, also at every baud rate tried, and in
every run its `rx_bytes` rose by exactly the bytes the host wrote (status after
minus status before = bytes written minus the first status frame; between runs it
rose by 14, the next status frame). No open reached RX as a glitch on this bench;
the glitch handling is shown in simulation.

**Latency, 115200.** Per 4096-byte chunk, from the host's write to the ack line's
arrival: median 432.26 ms (run 1), 432.19 ms (run 2), 431.08 and 431.10 ms (runs 3,
4); maximum 432.33, 433.28, 431.23 and 438.38 ms; the 24-byte last chunk 7.79 and
7.61 ms. The frame's own wire time is 356.8 ms (4110 bytes at 10 bits); the rest,
74-75 ms per 4096-byte chunk and 4.3-4.5 ms for the 24-byte one (`turnaround_s` in
the records), is the device (0.49 ms commit, 1.74 ms ack line) and the host side.
The read-back runs at the line rate (11 197 B/s of payload against 11 481 for 4110-byte
frames at 115200), the load does not (9 217 B/s): the host-to-device direction
delivered 4110 bytes in about 430 ms (432.3 ms less the 1.74 ms ack line and the 0.49 ms commit), 9.6 kB/s, 83 % of the nominal 11 520 B/s. The
device kept up throughout (FIFO high-water mark 1 entry, no nak in the clean runs),
so the gaps are on the host side; whether the CP2102N or the macOS driver makes them
was not measured. Arithmetic from these rates, not measured: a whole 2560 x 2560
q_proj as dense5 (1 310 720 bytes plus header) would take about 142 s at 9.2 kB/s.

**Baud rates.** The device's divisor at 25 MHz gives 229 358 (109 clocks, -0.45 %
from 230400), 462 963 (54, +0.47 %), 925 926 (27, +0.47 %), 1 000 000 (25) and
1 470 588 baud (17, -1.96 % from 1500000); the CP2102N's own rates were not measured.
Every trial switched with a `B` frame and back to 115200 with another (no fallback
needed, `baud_reverts` 0), and every transfer read back identical with no
retransmission. Payload rates, load / read-back: run 5 (16 384 bytes) 17.7 / 21.7 kB/s
at 230400, 33.9 / 42.8 at 460800, 59.4 / 79.6 at 921600, 58.8 / 89.0 at 1000000, 71.2 /
124.7 at 1500000; run 6 (163 864 bytes, two transfers per rate) 57.4 and 57.9 / 80.6 and
80.0 kB/s at 921600, 59.9 and 56.6 / 88.2 and 88.4 at 1000000, 69.2 and 72.1 / 122.1 and
126.4 at 1500000. That is two whole-payload transfers per rate, not a reliability
study, so the default stays 115200.

**The first build (6bc63ca4) failed the read-back.** It acknowledged all 41 chunks with
good CRCs in both directions, but read back every even byte as its odd neighbour
(81 510 of 163 864 bytes wrong; got[2k] = got[2k+1] = payload[2k+1]:
[`reports/fpga/uart-loader-2026-09-24-6bc63ca4/run1-clean.json`](../reports/fpga/uart-loader-2026-09-24-6bc63ca4/run1-clean.json),
[`loader-build-2026-09-24-6bc63ca4`](../reports/fpga/loader-build-2026-09-24-6bc63ca4/build.json)).
That build had the store in RAMB36 32K x 1 and the staging buffer in RAMB36 4K x 9
(yosys' choice once the cascade was excluded); the receive FIFO, in RAMB18 1K x 18,
delivered every byte (all frame CRCs matched). Which of the two memories lost the
bytes, and why, was not isolated; the build of record puts every memory into the
RAMB36 1K x 36 configuration that the block-RAM benches had verified, and reads back
identical. Narrower RAMB36 configurations in this open flow remain unverified.

## Limits and open points

- Block RAM only (256 KiB). DDR3 is part 2 (next section).
- The host tool is Python with pyserial on macOS; the load direction ran at 83 % of
  the line rate at 115200 for reasons on the host side that were not measured.
- Baud rates above 115200 worked in every trial (two whole-payload transfers at
  921600, 1000000 and 1500000), but that is not a reliability measurement.
- The simulation predicts the device byte for byte; it does not cover the RAMB36
  configuration problem above, which only the board showed.
- The FASM of the build of record did not reproduce in two more placements with the
  same seed (see "Build").
- No port-open glitch was observed on this bench; the glitch handling is shown in
  simulation only.

## DDR3 (part 2, after #62)

Part 2 replaces [`fpga_loader_store.t27`](../t27/rtl/fpga_loader_store.t27) by a
Wishbone master on UberDDR3's user port behind the same write and read ports; the
loader, receiver and protocol stay as they are. Plan, not built:

- The loader, receiver, transmitter and line emitter run in the DDR3 controller's
  clock domain, as the #61 pattern test and status reporter do; `default_div`
  becomes the controller clock over 115200 (83.33 MHz / 115200 = 723) and the
  timeouts scale with it.
- Write side: a t27 packer collects the bytes of a transfer into the data word of
  one Wishbone write (8 bytes per lane, 16 bytes at x16), with a byte-select bit
  per byte that arrived; it issues the write when the next byte falls outside the
  word or on `wr_last`, and holds `wr_ready` low while a write waits for `!stall`.
  `wr_idle` goes high when every write has been acknowledged (UberDDR3 acknowledges
  in order, so counting requests and acks suffices). The loader then acknowledges
  the chunk, so an ack means the data is in DDR3, not in a buffer.
- Read side: one Wishbone read of the word that holds `rd_addr`, the word kept
  for the following bytes of the same word; `rd_valid` when the ack arrives.
- Store size: `store_log2` 29 (512 MiB, x16) or 30 (x32).
- Arbitration: the port is shared with the pattern test (#61) and the burst reader
  of #62; a t27 arbiter grants it per chunk and the loader waits on `wr_ready` or
  `rd_ready` meanwhile.
- Calibration: the loader refuses loads until `o_calib_complete` (a new nak
  reason `not_ready`), and the status lines add the calibration state; the #61
  self-test is re-run with the loader in the build to show that DONE_CALIBRATE
  still holds (the UberDDR3 demo changed calibration behaviour when a UART was
  added, #63 "Risks").
- A watchdog on the Wishbone phases, as in the pattern test, turns a hung port into
  a nak instead of a silent stall.
