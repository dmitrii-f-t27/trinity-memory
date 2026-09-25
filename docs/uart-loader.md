# UART loader (issue #63)

A host-to-board loader over the AX7203's CP2102N UART with per-chunk
acknowledgement: every chunk is CRC-checked on the device, acknowledged or
refused with a reason, retransmitted by the host until it is acknowledged, and
readable back through CRC frames, so nothing is lost silently. Part 1 loads into
block RAM (the sections up to "Limits and open points"); part 2 puts a DDR3 Wishbone
master behind the same write and read ports and is built and run on the board
("DDR3 (part 2, built)", at the end).

| File | Role |
| --- | --- |
| [`t27/rtl/fpga_uart_rx.t27`](../t27/rtl/fpga_uart_rx.t27) | 8N1 receiver |
| [`t27/rtl/fpga_uart_loader.t27`](../t27/rtl/fpga_uart_loader.t27) | receive FIFO, frame parser, CRC-32, staging buffer, commit, read-back, responses, baud change |
| [`t27/rtl/fpga_loader_store.t27`](../t27/rtl/fpga_loader_store.t27) | 256 KiB block-RAM store behind the write and read ports |
| [`fpga/ax7203/tms_uart_loader.v`](../fpga/ax7203/tms_uart_loader.v) | wiring only (clocks, reset, transmitter, line emitter, the three modules above) |
| [`tools/uart_loader_protocol.py`](../tools/uart_loader_protocol.py) | the protocol in Python: frame encoders, stream decoder, a byte-level reference model of the device |
| [`tools/fpga-uart-loader.py`](../tools/fpga-uart-loader.py) | host tool: stop-and-wait, retransmit, read-back verify, fault injection, statistics |
| [`tools/fpga-loader-evidence.py`](../tools/fpga-loader-evidence.py) | build-record helpers: RAM cells and widths of a netlist, placement attempts and FASM differences |
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
| `S` (0x53) status | send the 23 counter lines | len 0 |
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
  and retransmits after a nak, or when no reply has come 0.5 s after the later of
  (write start plus the frame's wire time) and (the moment write() returned, the OS
  having taken every byte). Before it retransmits it waits until the OS has sent
  everything (tcdrain) and then stays silent for 0.15 s (three device timeouts), so
  that the device has dropped any partial frame and hunts again. A reply to an
  earlier copy of the chunk that arrives in that quiet time is recorded as a late
  reply and not taken for the next copy; one that arrives even later cannot be told
  apart from the reply to the next copy (same seq) and is taken for it, which can
  only mislabel that attempt in the record: the data are the same.

**Device to host.** Response lines are the benches' 20-byte lines (tag, 8 hex
digits `a`, 10 hex digits `b`, LF) through
[`fpga_line_emitter.t27`](../t27/rtl/fpga_line_emitter.t27), with
`b = (check << 32) | v` and `check` the low byte of the CRC-32 of the tag, `a`
(4 bytes) and `v` (4 bytes), so a corrupted line is recognised.

| Tag | `a` | `v` |
| --- | --- | --- |
| `H` (after reset) | build id | `(2 << 24) \| (12 << 16) \| (log2 store << 8) \| 10`: protocol 2, 4096-byte chunks, 2^18-byte store, 1024-entry FIFO |
| `A` ack, `N` nak | `(seq << 24) \| (reason << 20) \| (command << 17) \| (seq known << 16) \| len` | `(frames committed << 16) \| naks`, both mod 2^16 |
| `C` status | `(seq << 24) \| (23 << 16) \| index` | the counter |

Reasons: 0 `ok`, 1 `duplicate` (both in `A`), 2 `crc`, 3 `length`, 4 `timeout`,
5 `framing` (a byte with a low stop bit inside a frame), 6 `command`, 7
`overflow` (the receive FIFO dropped bytes of this frame), 8 `port` (the store's
write or read port made no progress for one timeout, 50 ms; "Write and read
ports"). Command numbers: 1 load, 2 read, 3 status, 4 baud, 0 unknown. Protocol 2
(this build) added reason 8 and the 23rd status line; protocol 1 was the build
22844259.

A read is answered with a binary frame in the host's format, command `r`
(0x72): `A5 5A 72 seq addr[4] len[2] data[len] crc32[4]`. Line tags are ASCII
letters, so a byte 0xA5 always starts a read-back frame. The host's decoder takes a
header only with 1 <= len <= 4096 (a length bit flipped on the way would otherwise
make it wait for up to 64 KiB and swallow every reply behind it), and decodes the
bytes of a frame whose CRC does not match again after its first byte, so that lines
inside it are not lost (a read-back cut short by a reset is completed with the bytes
that follow it: the `H` line and what comes after).

Status counters (`C` lines, index order): `rx_bytes`, `rx_framing_errors`,
`rx_false_starts`, `fifo_overflow_bytes`, `fifo_high_water`, `ignored_bytes`,
`frames_committed`, `frames_duplicate`, `nak_crc`, `nak_length`, `nak_timeout`,
`nak_framing`, `nak_command`, `nak_overflow`, `bytes_committed`, `readbacks`,
`baud_div`, `baud_reverts`, `last_seq` (bit 8: valid), `config` (as in `H`),
`clocks` (design clocks since reset, low 32 bits), `build_id`, `nak_port`.

## Design

**Receiver.** RX passes three flip-flops (the benches' rx1-rx3). A low line
starts a character; the start bit is sampled again `baud_div >> 1` clocks after the
line fell, plus the 0 to 1 clock between the fall and the clock edge that saw it, and
a high level there is a false start, not a byte. Data and stop bits are sampled
`baud_div` clocks apart after that; a low stop bit is a framing error, after which the
receiver waits for the line to go high, so a break gives one character. For an even
divisor D every sample therefore lies 0 to 1 clock after the middle of its bit (for
an odd one, within half a clock of it). Arithmetic, not measured: the stop bit is
sampled 9.5 D + 0..1 clocks after the start edge and must fall inside the host's
stop bit, 9 to 10 host bit periods, so the host's bit period may be off by
-(0.5 D - 1) / (10 D) (even D; -(0.5 D - 0.5) / (10 D) for odd D) to +0.5 / 9:
-4.4 % to +5.5 % at D = 16, -4.98 % to +5.5 % at D = 217 (115200 baud at 25 MHz). A pulse that ends before the start sample is a false
start: up to half a bit, or half a bit plus one clock depending on its phase. The
first build sampled one clock later than this (the count started at `baud_div >> 1`),
which left only -3.75 % on the fast side at D = 16 (review finding 3).

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
the 14 bytes of framing take 0.34 % of the forward line, and in stop-and-wait each
chunk also waits for its ack line (20 bytes on the return line, 1.74 ms) plus the
host's turnaround. Smaller chunks lose more
to turnaround, larger ones more to a retransmission, and the staging buffer would
need more blocks. One chunk of depth is enough: stop-and-wait never has two chunks
in flight, and the commit (3 clocks per byte, 0.49 ms for 4096 bytes at 25 MHz)
is 700 times shorter than the chunk's wire time.

**Write and read ports.** The commit writes one byte per transfer: `wr_valid`,
`wr_addr`, `wr_data` (low 8 bits) and `wr_last` (the chunk's last byte); a
transfer happens on a clock where `wr_valid` and `wr_ready` are both high, and the
loader acknowledges the chunk only after `wr_idle` (every write done). A read
issues `rd_req` with `rd_addr` until `rd_ready`, then waits for `rd_valid` with
`rd_data`. The contract the store behind the ports must keep:

- `wr_idle` is low from the clock after an accepted transfer until that write is
  done; the loader samples it on the clock after the last transfer, so a store that
  raised its "pending" state a clock later would let the loader acknowledge early.
- `rd_valid` comes exactly once per accepted request, in the accepting clock (a
  memory without latency, answered combinationally) or any later clock, never
  before; `rd_data` is valid with it.
- After a watchdog abort (below) the store must not deliver an answer to the
  abandoned request.

Latency is otherwise free. In simulation the same loader runs behind a memory that
withholds `wr_ready` and `rd_ready` at random, answers reads after 1-16 clocks and
keeps `wr_idle` low for up to 15 clocks after each write, and behind a memory that
answers every read in the accepting clock; every byte reads back right (numbers in
"Simulation"). The first build waited for `rd_valid` only from the clock after the
acceptance, so a memory answering in the accepting clock hung it (review finding 2).

**Port watchdog.** A port that makes no progress for one timeout (`timeout_clocks`,
50 ms at 25 MHz: `wr_ready` or `wr_idle` during a commit, `rd_ready` or `rd_valid`
during a read-back) ends the command with a `port` nak instead of a silent stall. A
commit ended that way is not recorded as the last load (its retransmission is
committed, not taken for a duplicate) and the store may hold part of it; a read-back
already under way is finished with zero bytes and a CRC with bit 0 flipped, so that
the host's decoder stays in step, and the nak follows. The block-RAM store answers
within two clocks and never trips it; it exists for the DDR3 port of part 2 and is
shown in simulation only.

**Store.** 256 KiB in four banks of 16 384 32-bit words, each byte written as a
read-modify-write of its word (two clocks), so that every memory of the design is
in the RAMB36 1K x 36 configuration; see "Board results" for why.

**Baud rate.** A `B` frame changes receiver and transmitter to a new divisor
after its ack has left. Unless a frame with a good CRC arrives within 3 s
(`LOADER_PROBATION`, 75 000 000 clocks), the device returns to the divisor it was
built with (217, 115200 baud at 25 MHz) and counts a revert; the reset button
returns to it too. The first good frame at the new rate ends the probation: from
then on only another `B` frame or reset changes the rate. A `B` frame back to the
built-in divisor needs no confirmation and turns LED 1 ("a rate other than the
built-in one") off; the first build left LED 1 lit after a switch back (review
finding 5). So a host that loses the device right after a switch (the `B` frame or
its ack lost) waits 3 s and goes back to 115200, which the host tool does; once a
good frame has passed at the new rate, the host must switch back with a `B` frame at
that rate, and the tool retries it there (checking first with a status at 115200
whether only the ack was lost).

**Clock.** 25 MHz from the 200 MHz oscillator (the benches' `CLOCK_MODE` 1).

## Simulation

`python3 -m unittest tests.test_uart_loader` (also run by `tools/test-t27.sh` and
`make -C fpga/ax7203 loader-sim`; needs `T27_ROOT` and Icarus, skipped without them).
A host model drives RX at the bit level from a script with its own bit period and
decodes TX; [`tools/uart_loader_protocol.py`](../tools/uart_loader_protocol.py)'s
`DeviceModel` predicts every byte the device sends for the same byte stream, and
every received line and read-back frame must equal the prediction (only the status
lines' FIFO high-water mark and clock counter depend on timing and are not
predicted; output cut off by a reset must be the start of the predicted output). The
simulation runs at 16 clocks per bit (1.5625 Mbaud) to keep it short; two scenarios
run the board divisor 217 with the board's 50 ms timeout. The summary is written to
`build/fpga/loader-sim/summary.json`, copied to
[`reports/fpga/uart-loader-sim-2026-09-24-1d474000.json`](../reports/fpga/uart-loader-sim-2026-09-24-1d474000.json)
(bytes and events per scenario, turnaround per load, and the stalling memory's own
counts).

| Scenario | Bench | What it shows |
| --- | --- | --- |
| `transfer_top_+0.05` | board top, host bit period +5 % | 12 loads of random lengths (1, 2, 3, 17, 255, 256, 1000, 4096 and four random) at random addresses, each read back identical; status |
| `transfer_ports_-0.04` | cores behind the stalling memory, host -4 % | the same, with `wr_ready`/`rd_ready` withheld at random, reads answered after 1-16 clocks and `wr_idle` low up to 15 clocks per write: the memory counted 12 008 writes, 34 500 clocks with a write waiting and one `wr_last` per chunk (12) |
| `transfer_ports0_+0.05` | cores behind a memory without latency, host +5 % | the same, every read answered in the clock that accepts it (10 616 writes, 0 waiting clocks) |
| `faults_-0.04` | board top, host -4 % | a corrupt byte (`crc`), a duplicate (`duplicate`, not committed), a dropped byte and a host stopping mid-frame (`timeout`), unknown command, lengths 0, 4097 and past the store, status with a length, divisor 8 (`command`, `length`), a low stop bit inside a frame (`framing`), garbage and stray `A5` bytes, glitches of 0.2 bit (a false start), 3 bits (byte 0xFC) and 30 bits (a break: one 0x00 with a framing error), glitches ending just before or after a sampling point (0.45 bit: false start; 0.62: 0xFF; 2.65: 0xFC; 9.3: 0x00 with a good stop bit; 9.7: 0x00 with a framing error), a 0.2-bit glitch inside a frame (the frame survives), the reset button during a frame (`H` again, the store keeps its data); every retransmission acknowledged, every read-back identical |
| `pipelined` | board top | six loads, a status, a read and a duplicate sent back to back without waiting: every response, in order |
| `overflow` | board top | a 2000-byte load sent while the device sends a 4096-byte read-back: the FIFO reaches 1024 entries and drops bytes, the load gets `overflow`, its retransmission is acknowledged and reads back identical |
| `baud` | board top, divisor 32 | a `B` frame to 16, traffic at 16, then a `B` frame to 20 that the host does not follow: after the probation time the device is back at 32 (`baud_reverts` 1) |
| `watchdog` | cores behind the stalling memory | the reset button while a commit waits for `wr_ready` (`wr_valid` high) and while it waits for `wr_idle`: `H`, the chunk loaded again; then a write port that stops accepting, a write that never finishes and a read port that stops accepting: `port` naks (the read-back finished with zeros and a bad CRC), every chunk loaded again and read back identical, `nak_port` 3 |
| `resets` | board top | the reset button 200 bytes into a 4096-byte read-back and 57 bytes into the status lines: the output stops where the model says, `H` follows, and the data loaded before the resets reads back identical |
| `board_rate_+5.0%`, `board_rate_-4.5%` | board top, divisor 217, 50 ms timeout | a 64-byte load and read-back at 115200 with the host 5 % slow and 4.5 % fast |

The glitch lengths are chosen so that no pulse ends within a clock of a sampling
point (the test refuses those, whose outcome depends on the phase); the host skews
sit 0.4 to 0.5 points inside the arithmetic limits above. With the first build's
receiver (one clock later) the `-4 %` scenarios fail, and with its read port the
memory without latency hangs the loader; both were checked by running the tests
against those two changes undone.

Turnaround from the end of a load frame to its ack (design clocks, from the
summary): 3 clocks per payload byte in the board top (4096 bytes: 12 284 clocks,
0.49 ms at 25 MHz), about 6 behind the stalling memory (4096 bytes: 24 377 clocks).

The host tool is tested in the same file (`HostTool`) against `DeviceModel` on a fake
line with wire time at 1 Mbaud (bytes reach the model when their last bit would
have, replies leave in order at the device's rate, the device's rate follows `B`
frames and its probation, bytes at the wrong rate or sent while the port is closed
and reopening are lost): corrupt, drop, abort, garbage and duplicate each end in an
acknowledged, identical chunk (the fake port takes 0.1 s to reopen, so the abort's nak
is lost and the host resends after its own wait, as happened on the board with the
22844259 tool); a reply 0.28 s late is recorded as a late reply and not taken for the
retransmission; baud trials load bytes that differ from the store's; a lost switch
ack and a refused switch back are recovered; a trial whose bytes never land fails the
run.

## Build

`make -C fpga/ax7203 loader-bit FREQ_MHZ=25 SEEDS=3 BRAM_PLACERS=heap` with native
yosys 0.69 and nextpnr-xilinx 45a986b8 (`NEXTPNR`, `CHIPDB` of the image as for the
block-RAM benches), prjxray of `regymm/openxc7@sha256:eced1cdd…`; `make loader-report`
writes the record. Build of record: commit 1d47400 (`BUILD_ID` 1d474000),
[`reports/fpga/loader-build-2026-09-24-1d474000/`](../reports/fpga/loader-build-2026-09-24-1d474000/build.json):
66 RAMB36E1, every one with read and write width 36, the 1K x 36 configuration
(`rams.json` there, from the netlist: 64 store, 1 staging buffer, 1 FIFO), 4 892
SLICE_LUTX, 1 873 SLICE_FFX, nextpnr's routed estimate 77.27 MHz for the 25 MHz clock
(PASS), `heap` placer seed 3, `router2`; bitstream sha256 `bcf6e804…` (from the sync
word `52f29ba8…`), FASM `1a068651…`. `placements.json` in the same directory holds the
placement attempts, made in parallel from the same netlist: seed 3 routed (router2 at 0
overused wires after 4 iterations, about 6 minutes from the start of the run); seeds 4
and 5 were still at 832 and 875 overused wires after 2 iterations when they were
stopped about 20 minutes in; a second seed-3 run in another directory also routed, with
the same Fmax lines but a FASM (`8ef5d2d2…`) that differs from the first in 964 and 960
lines of about 194 000. So nextpnr-xilinx 45a986b8 does not reproduce this design's
FASM bit for bit from the same netlist and seed on this machine; the flashed bitstream
is identified by its sha256 (the build record's `reproducible_identity` note says so).
The three synthesis runs gave the same netlist (sha256 `a098a297…`).

The other designs' make commands are unchanged: `make -n -B` of `gen`, `sim`, `bit`,
`bram-sim`, `bram-bit` (four variants), `bram-ooc`, `ddr3-bit` (three variants),
`ddr3-report` (two), `ddr3-sweep` and `clean` prints the same commands as on the base
branch (feat/ddr3-bringup), and none of their source files changed.
`tools/fpga-build-report.py` gained an `--identity-note` option whose default keeps
the old note.

Earlier builds, kept for their evidence: 22844259 (commit 2284425, protocol 1, the
receiver one clock late, the host tool that timed replies after tcdrain;
[`loader-build-2026-09-24-22844259`](../reports/fpga/loader-build-2026-09-24-22844259/build.json),
captures in [`uart-loader-2026-09-24-22844259/`](../reports/fpga/uart-loader-2026-09-24-22844259/)),
6bc63ca4 (below) and 9ca11cc (not flashed: with two read addresses per bank yosys built
the four store banks from 11 264 RAM64M LUT-RAM cells and 2 RAMB36E1,
[`loader-synth-2026-09-24-9ca11cc9/yosys_stat.txt`](../reports/fpga/loader-synth-2026-09-24-9ca11cc9/yosys_stat.txt),
from `make loader-synth BUILD_ID=9ca11cc9` on a `git archive` of 9ca11cc; commit
2284425 changed the store to one read address).

## Board results (2026-09-24, build 1d474000)

ALINX AX7203, IDCODE 0x3636093, DNA 0x00389c0c2d85e85c; SRAM load only, CP2102N at
`/dev/cu.usbserial-110`, macOS, pyserial. Every run is one `tools/fpga-uart-loader.py`
record (`trinity.uart-loader-capture.v2`) under
[`reports/fpga/uart-loader-2026-09-24-1d474000/`](../reports/fpga/uart-loader-2026-09-24-1d474000/)
with IDCODE, DNA and XADC before the run and XADC after it, each attempt with its times
and reply, the device's status before and after, the store's contents before the load
(`--read-before`: `store_before.bytes_the_load_changes`), and the received bytes
(`*.rx.bin.gz`; run 6's, 2.29 MB compressed, is not committed: its sha256 is in the
record, the file was kept outside the repository at
`/private/tmp/claude-501/wave2/63/keep-1d474000/`, which is not permanent). Run 1 loaded
the bitstream (sha256 `bcf6e804…`, the build of record) and received its `H` line: build
1d474000, protocol 2. The host tool and protocol files were those of commit 1d47400
(the records' `tracked_changes` is true because docs and the build-report tool were
being edited at the time).

**Payload.** Rows 0-319 of the layer-0 query projection of BitNet b1.58 2B4T
(`model.layers.0.self_attn.q_proj.weight`, 320 x 2560 = 819 200 trits: +1 284 129,
0 251 701, -1 283 370), from `tools/extract-bram-trits.py --rows 320` (the pinned
packed checkpoint through the t27 decoders, the GGUF's I2_S tensor agreeing trit for
trit), packed by the host tool into a TMEM v1 dense5 container
([format.md](format.md)): 24-byte header and 163 840 payload bytes, 163 864 bytes,
sha256 `6397485…` in each record (`payload`). At 4096 bytes per chunk that is 41 chunks
(40 of 4096, one of 24).

| Run | What | Chunks | Attempts | Bytes the load changed | Payload rate, load / read-back | Result |
| --- | --- | ---: | ---: | ---: | --- | --- |
| `run1-clean` | SRAM load, whole payload at 0x0, 115200 | 41 | 41 | 163 008 of 163 864 | 11 374.5 / 11 322.3 B/s | acked, read back identical |
| `run2-faults` | whole payload at 0x10000; port closed and opened 5 times first; corrupt chunks 3 and 25, drop 7 and 30, abort 11, garbage before 15, duplicate 19 | 41 | 46 | 163 091 | 8 249.2 / 11 345.4 B/s | acked, identical |
| `run3-clean` | first 98 304 bytes at 0x20000 | 24 | 24 | 97 874 | 11 370.8 / 11 353.8 B/s | identical |
| `run4-drain` | first 16 384 bytes at 0x3C000, the host waiting for tcdrain after each load frame (`--drain`) | 4 | 4 | 16 302 | 9 512.7 / 11 356.0 B/s | identical |
| `run5-baud` | first 16 384 bytes at 0x38000, then per rate 16 384 bytes of the payload XOR a key at 230400, 460800, 921600, 1000000, 1500000 | 4 + 5 x 4 | 1 each | 16 312; each trial 16 384 | see below | identical everywhere |
| `run6-baud-full` | whole payload at 0x0, then the whole payload XOR a key twice at each of 921600, 1000000, 1500000 | 41 + 6 x 41 | 1 each | 98 076 (0x0-0xFFFF still held run 1's bytes); each trial 163 864 | see below | identical everywhere |

"Bytes the load changed" is the number of payload bytes that differed from what the
store held before the load (read back first), so an identical read-back shows at least
that many bytes landed; the bytes that did not change (zeros in the payload over the
zeros of a freshly configured store, or run 1's bytes under run 6) are shown only to be
right, not to have been written. Die temperature (XADC): 51.3 C before run 1 and
47.1 C after it; 47.2 and 45.3 C around run 2; between 42.5 and 45.2 C around runs 3-6.

**Faults (run 2).** The two corrupt chunks got `crc` naks and the two chunks with a
dropped byte `timeout` naks; each was acknowledged on the next attempt. The aborted
chunk (2055 of 4110 bytes, then the port closed and opened) got its `timeout` nak too,
0.495 s after the start of the half frame, and was acknowledged on the next attempt;
with the previous tool (records of 22844259) that nak was lost while the port reopened
and the host resent after its own wait. Those five chunks took 1.06-1.72 s instead of
0.36 s (the nak, tcdrain, the 0.15 s quiet time and the resend). The garbage chunk was
not retransmitted: its 64 random bytes were counted as `ignored_bytes` 64 and the frame
after them was acknowledged at once. The duplicate (the chunk sent again after its ack)
was answered `duplicate` and not committed (`frames_duplicate` 1; `frames_committed`
rose by exactly 41 over the run). Retransmits by reason: `crc` 2, `timeout` 3; no reply
was late. The dropped-byte naks arrived 409.3 ms after the start of their frame's
write: 356.7 ms for the 4109 bytes at 115200, the device's 50 ms timeout and the 1.74 ms
nak line account for 408.4 ms of it (arithmetic), so the host kept the line busy.

**Port-open glitch.** The port was opened once per run (six times), five more times
before run 2 and once mid-frame in run 2. The device's `rx_framing_errors` and
`rx_false_starts` stayed 0 throughout, also at every baud rate tried. No open reached
RX as a glitch on this bench; the glitch handling is shown in simulation.

**Latency and rate, 115200.** The reader thread stamps each ack when it arrives.
Per 4096-byte chunk, from the start of the host's write to the ack's arrival: median
359.82 ms (run 1), 359.88 (run 2), 359.87 (run 3), 359.80 (run 5), 359.85 (run 6);
maximum 360.29, 368.20, 362.30, 359.83, 359.94 ms; the 24-byte last chunk 7.55-7.72
ms. The frame's wire time is 356.8 ms (4110 bytes at 10 bits); the rest
(`turnaround_s`, per full chunk sent once) has a median of 3.03-3.10 ms and a minimum of
2.96-2.98 ms: the commit (0.49 ms), the ack line (1.74 ms) and about 0.8 ms of USB,
driver and host (arithmetic from these medians). `write()` returned 0.07-5.0 ms after it
was called (`queued_s`; the OS took the whole frame), and the next chunk's write started
a median 0.08-0.09 ms after the ack arrived (`gap_s`). Load throughput 11 370.8-11 381.6 B/s
of payload in the clean runs against 11 480.8 B/s for 4110-byte frames at 115200
(arithmetic): 99.0-99.1 %. The read-back runs at 11 322-11 356 B/s.

**What the first measurements got wrong (review findings 0, 7, 16).** The tool of
the 22844259 records wrote each frame, then waited for tcdrain, and only then read the
port, so every reply was stamped when tcdrain returned: its "432 ms per chunk", "74-75
ms turnaround", "9 217 B/s" and the conclusion that the line ran at 83 % measured
tcdrain, not the line. Those records disprove it themselves: their two dropped-byte
naks, which the device sends only after 50 ms of silence, were read 0.03 and 0.14 ms
after tcdrain returned, so the last byte had reached the device at least 51.6 ms
earlier (at most 380.5 ms after the write started, at least 93.7 % of the line rate).
Run 4 here repeats the old behaviour on purpose (`--drain`): tcdrain returned
430.1-431.1 ms after the write started, the ack was read 0.06-0.07 ms after that (while
the host waits in tcdrain the reader thread receives nothing on this macOS and CP2102N
setup), and the load ran at 9 512.7 B/s. Without tcdrain the ack arrives 359.8 ms after
the start (runs 1-3, 5 and 6). The old tool also waited out a 10 ms read window after every
reply (review finding 10); the new one returns as soon as the reply is decoded.
Arithmetic from the rates, not measured: a whole 2560 x 2560 q_proj as dense5
(1 310 720 bytes plus header) would take about 115 s at 11.37 kB/s.

**Baud rates.** The device's divisor at 25 MHz gives 229 358 (109 clocks, -0.45 %
from 230400), 462 963 (54, +0.47 %), 925 926 (27, +0.47 %), 1 000 000 (25) and
1 470 588 baud (17, -1.96 % from 1500000); the CP2102N's own rates were not measured.
Every trial switched with a `B` frame and back to 115200 with another at the trial rate
(no retry, no fallback, `baud_reverts` 0). Each trial loaded the payload XOR a byte key
that differs per trial and from 0 (`pattern.xor` in the record), after reading the range
first: every trial changed every one of its bytes (16 384 of 16 384 in run 5, 163 864 of
163 864 in run 6) and read them back identical, all chunks acknowledged at the first
attempt. Payload rates, load / read-back: run 5 (16 384 bytes) 22.6 / 22.4 kB/s at
230400, 44.4 / 44.6 at 460800, 86.7 / 87.6 at 921600, 93.9 / 94.6 at 1000000, 139.2 / 138.4
at 1500000; run 6 (163 864 bytes, two transfers per rate) 86.6 and 86.6 / 87.5 and 87.5
kB/s at 921600, 93.8 and 93.8 / 93.8 and 94.4 at 1000000, 139.0 and 139.1 / 138.2 and
138.2 at 1500000. That is two whole-payload transfers per rate, not a reliability
study, so the default stays 115200. (The trials of the 22844259 records loaded the
bytes the main transfer had already stored at the same address, so their identical
read-backs showed the read path at those rates, not that the loads landed; review
findings 8 and 17.)

**The first build (6bc63ca4) failed the read-back.** It acknowledged all 41 chunks with
good CRCs in both directions, but read back every even byte as its odd neighbour
(81 510 of 163 864 bytes wrong; got[2k] = got[2k+1] = payload[2k+1]:
[`reports/fpga/uart-loader-2026-09-24-6bc63ca4/run1-clean.json`](../reports/fpga/uart-loader-2026-09-24-6bc63ca4/run1-clean.json),
[`loader-build-2026-09-24-6bc63ca4`](../reports/fpga/loader-build-2026-09-24-6bc63ca4/build.json)).
That build had the store in 64 RAMB36 with port width 1 (32K x 1), the staging buffer
in one RAMB36 of width 9 (4K x 9) and the receive FIFO in one RAMB18 of width 18
(yosys' choice once the cascade was excluded; `rams.json` in its build record, from its
netlist). The FIFO delivered every byte (all frame CRCs matched). Which of the two other
memories lost the bytes, and why, was not isolated; the builds since put every memory
into the RAMB36 1K x 36 configuration that the block-RAM benches had verified, and read
back identical. Narrower RAMB36 configurations in this open flow remain unverified.

## Limits and open points

- Part 1 is block RAM only (256 KiB); DDR3 is part 2 (next section, with its own limits).
- The host tool is Python with pyserial on macOS. With tcdrain out of the load loop
  it reaches 99 % of the line rate at 115200; while it waits in tcdrain, nothing is
  received (run 4), which is why the tool does not wait for it.
- A reply to an earlier copy of a chunk that arrives after that copy's quiet time
  cannot be told from the reply to the next copy (same seq); no run had a late reply.
- Baud rates above 115200 worked in every trial (two whole-payload transfers at
  921600, 1000000 and 1500000), but that is not a reliability measurement.
- The receiver's tolerance to the host's clock (arithmetic above) is checked in
  simulation at -4 %/+5 % (16 clocks per bit) and -4.5 %/+5 % (217); the CP2102N's
  actual rates were not measured.
- The port watchdog, the resets during a commit, a read-back or status lines, and a
  memory without latency are shown in simulation only; the block-RAM store never
  stalls.
- The simulation predicts the device byte for byte; it does not cover the RAMB36
  configuration problem above, which only the board showed.
- The FASM of this design is not reproducible bit for bit from the same netlist and
  seed with nextpnr-xilinx 45a986b8 ("Build").
- No port-open glitch was observed on this bench; the glitch handling is shown in
  simulation only.

## DDR3 (part 2, built)

Part 2 puts the loader in front of UberDDR3's memory, x16 (chip U6, 512 MiB):
`make -C fpga/ax7203 ddr3-bit ddr3-report DDR3_APP=loader`. The frame protocol is part 1's
with protocol number 3; the store behind the write and read ports is UberDDR3's user
Wishbone port instead of block RAM. The same loader with its `matvec` input high is protocol 4,
the device matvec of issue #64 (`DDR3_APP=matvec`, `[matvec]`-marked changes: activation frames X
and matvec frames M): `docs/bridge.md`, "The DDR3 matvec build".

| File | Role |
| --- | --- |
| [`t27/rtl/fpga_ddr3_loader.t27`](../t27/rtl/fpga_ddr3_loader.t27) | the part-1 loader with marked (`[ddr3]`) changes: `not_ready`, protocol 3 with the calibration watch and 14 more status lines, and registered or pipelined checks for 83.33 MHz |
| [`t27/rtl/fpga_loader_wb.t27`](../t27/rtl/fpga_loader_wb.t27) | Wishbone master behind the write and read ports: packer with byte selects, outstanding count, kept read word, late-ack drop, counters |
| [`t27/rtl/fpga_wb_arbiter.t27`](../t27/rtl/fpga_wb_arbiter.t27) | arbiter of UberDDR3's single user port between two masters (the seam of #64) |
| [`fpga/ax7203/ddr3/tms_ddr3_loader_ax7203.v`](../fpga/ax7203/ddr3/tms_ddr3_loader_ax7203.v), [`tms_ddr3_loader.xdc`](../fpga/ax7203/ddr3/tms_ddr3_loader.xdc) | wiring only: the PLL, reset and `ddr3_top` parameters of `tms_ddr3_ax7203.v`, the t27 cores; RX on P20 |
| [`tests/test_ddr3_loader.py`](../tests/test_ddr3_loader.py), [`tests/tb_ddr3_loader.v`](../tests/tb_ddr3_loader.v), [`tests/sim_ddr3_loader_model.v`](../tests/sim_ddr3_loader_model.v) | functions in C, the protocol model, Icarus runs of the whole top against our Wishbone memory model, the host tool against the model |
| [`tools/ddr3-seed-choice.py`](../tools/ddr3-seed-choice.py) | the seed to load, from a sweep, by the #61/#62 CK - DQS rule |
| [`tools/uart-loader-summary.py`](../tools/uart-loader-summary.py) | one JSON of a directory of board records |

`tms_ddr3_ax7203.v`, `fpga_uart_loader.t27` and `fpga_loader_store.t27` are not changed,
so the pattern-test, reader and block-RAM loader builds keep their netlists ("Build").

### Design

**Clock domain.** Receiver, loader, line emitter, transmitter, Wishbone master and arbiter
run on UberDDR3's controller clock (83.33 MHz; the reset of `tms_ddr3_ax7203.v` plus a
second two-flip-flop stage). Divisor after reset 723 (83,333,330 / 115,200 rounded,
115,260 baud, +0.05 %); timeout 4,166,666 clocks (50 ms), baud probation 249,999,990
clocks (3 s), the block-RAM build's times in the new clock. The host tool takes
`--design-hz 83333333.33` so that `--baud-try` computes divisors for this clock.

**Protocol 3** (the H line's config `0x030C1D0A`: protocol 3, 4096-byte chunks, 2^29-byte
store, 1024-entry FIFO). One new reason, 9 `not_ready`: a load or read whose CRC matched is
refused while UberDDR3's `o_calib_complete` is low (the check comes after the CRC, so the
parser stays in step; the host retransmits as for any nak, or waits first:
`--wait-calib`). Status lines 23-36 follow part 1's 23: `nak_not_ready`; `calib`
(`calib_complete << 24 | state << 16 | highest state << 8 | returns to IDLE`, the #60 `S`
line's layout, state = `o_debug1[4:0]`); `calib_clocks` (clocks since reset at the first
`calib_complete`, 0 before); `calib_lost_clocks` (clocks since then with `calib_complete`
low or a state other than 23, counted every clock, 32 bits); `wb_writes`, `wb_reads`,
`wb_read_hits`, `wb_acks_dropped`, `wb_acks_stray`, `wb_max_outstanding`,
`wb_read_latency_max` (clocks from a read presented to its ack), `wb_cmd_stalls` (clocks
with a request presented and stall high), `arb_switches`, `arb_stray`. The device answers
37 status lines; the host tool reads the count from the lines.

**Write side (packer).** Each byte the commit hands over (part 1's port: `wr_valid`,
`wr_addr`, `wr_data`, `wr_last`) is merged into a 16-byte buffer word with a byte-select
bit per byte. The buffer goes out as one Wishbone write (burst address = byte address
>> 4, `i_wb_sel` = the bytes that arrived) when a byte of another word arrives, with the
chunk's last byte, and before any read. A presented request stays unchanged until taken.
UberDDR3 turns `i_wb_sel` into the DDR3 data mask (`ddr3_controller.v` L1296 at 79d8fd3e),
so a partial word writes only its own bytes; #61 and #62 wrote whole words only, so this
build is the first on the board to rely on the data mask (checked below). The master counts
requests taken minus acks (UberDDR3 acknowledges in order); `wr_idle` is high only with no
byte buffered, nothing presented and nothing outstanding, and part 1's loader acknowledges
a chunk only after `wr_idle`: **an ack means the controller has acknowledged every write
of the chunk**, not that a buffer holds it. At most 8 requests are outstanding (the flush of
a buffered word waits for that too since the review fixes; before them a ninth could go out
after seven slow acks). Since the DDR3 matvec build (#64 wave 4) a byte first enters an input
register with its word, its byte shifted to its lane with the lane's mask and select bit, and
whether its word is the buffer's word as the buffer will be when the byte is merged; it is
merged from there in a later clock (when nothing is presented, no flush or read waits and fewer
than 8 requests are outstanding), and `wr_ready` is high while the input register is empty or
is merged in that clock. The bytes, the words and their order on the bus are the same; a byte
is taken one clock sooner and merged one clock later. The reason is timing (below).

**Read side.** A read is taken only when the write side is idle, so it never overtakes a
write. The last word read is kept: the next bytes of that word are answered from it in the
clock after the request, without a bus request. Any write byte taken for the kept word
invalidates it, and so does a write of the other arbiter master (`m1_write`). Port
watchdog (part 1's, one timeout, `port` nak): when the loader gives up on the port during a
commit or a read-back it raises `port_give_up` for one clock. A read already taken on the bus
is then still outstanding; it is marked, and its ack is dropped whenever it comes
(`wb_acks_dropped`), so it cannot answer a later request; the next request is taken after
it. A request that comes while a read waits marks it too. The bytes of a word not yet
written are dropped (the chunk was nakked; the host sends it again), so the master lets go
of the port once nothing is outstanding.

**Arbiter.** Two masters, one owner (a register). The owner's request reaches the
controller; the other master sees stall high (its request waits unchanged) and no ack.
Ownership changes only on a clock where the owner does not want the port (the loader's
`cyc`: a chunk or a read in progress; for a master that holds `cyc` high for ever, such
as the #62 reader, the wiring passes its `stb`), nothing of it is outstanding, and the
other master wants the port. So a master keeps the port for a whole chunk or phase and
every ack reaches the master that issued the request. In the board build master 1 is idle.

**Timing.** The first netlists of this top met 83.33 MHz on none of the seeds tried (routed
61.8-81.6 MHz over the intermediate netlists; session logs, not committed). The routed critical paths ran from
UberDDR3's `o_wb_stall` through adders of the outstanding counts, through the loader's
32-bit compares (header rules, CRC compare, duplicate compare, timeouts), through the
CRC-32 byte step written as eight conditional steps, through the 72-bit check byte of a
response line, and through the status line multiplexer. The DDR3 loader (and only it)
therefore registers those compares and reads them one clock after entering the state
(`settle_n`), computes the CRC byte step as parity sums (`crc_byte_par`, the same function:
checked against `crc_byte` and zlib), computes a response line's check byte in a 9-stage
pipeline that P_RESP waits 11 clocks for, selects the status line with a tree of 4:1
multiplexers (`status_all`, checked against part 1's `status_value`), and uses 8-bit state
registers. The Wishbone master and arbiter select precomputed `n + 1` / `n - 1` by the
request taken and the ack. Each response costs 11 clocks more than in part 1 (0.13 µs).
The DDR3 matvec build (#64 wave 4: this top with the device matvec, about 10,000 more LUTs and
21 more block RAMs) met 83.33 MHz on none of 7 seeds at first (routed 67.4-78.4 MHz with
nextpnr-xilinx 0.9.7; `docs/bridge.md`, "Timing"). Its failing endpoints were the enables and
data inputs of the master's 128-bit buffer and request registers (from the outstanding count's
compare and the byte's word compare, through the merge, across the die), the line emitter's
character (a subtraction and a variable shift of `pos`), the X header rule's 32-bit product and
the outstanding counts' 32-bit adders. The master now has the input register above, keeps its
count to four bits and registers `room` (fewer than 8 outstanding) and `none` (nothing
outstanding) from the next count; the arbiter's and the feed's counts are seven bits; the line
emitter shifts its digits out of `a_q` and `b_q` (the same bytes in the same clocks); the X
header rule's widths are masked; the read latency maximum is taken one clock after its ack.
fb1dd5a's netlist then met the clock on 5 of 12 seeds; of the seven that missed, seed 10 missed
at the X header rule (-1.9 ns, and inside UberDDR3) and seed 11 at the matvec, behind UberDDR3's
read acknowledge (`docs/bridge.md`, "Timing"). So the X rule's bound, 80 x (1024 - addr), is now
registered a clock ahead of the rule (`act_room_q`, from `h_addr`, which is complete at least four
clocks before the rule is taken), and the feed hands the matvec each word from a register, a
clock after its ack. A
`DDR3_APP=loader` build from this RTL is therefore not a9a56541's netlist (its timing and seed
records are those of a9a56541); it would need its own seed sweep. In simulation it behaves as
before: the loader's suite at fb1dd5a
([`uart-loader-ddr3-sim-2026-09-25-fb1dd5aa.json`](../reports/fpga/uart-loader-ddr3-sim-2026-09-25-fb1dd5aa.json))
differs from defd5c0d's summary only in 51 of 61 load turnarounds (-123 to +14 clocks) and in
how the loader's and the reader's requests interleave in `arbitration` (381 owner changes
against 385).

**Block RAM.** The loader's cores are read with `-nomem2reg` and every memory is mapped with
part 1's 1K x 36 library (`brams_x36.txt`): the receive FIFO and the staging buffer are 2
RAMB36E1 with read and write width 36 (`rams.json` of the build). UberDDR3 infers no
memory (0 RAM cells in the #62 builds as here).

### Simulation

`python3 -m unittest tests.test_ddr3_loader` (in `tools/test-t27.sh`; skipped without
`T27_ROOT` or Icarus). Icarus runs the whole top with UberDDR3 replaced by
[`tests/sim_ddr3_loader_model.v`](../tests/sim_ddr3_loader_model.v), our behavioural
Wishbone memory (not UberDDR3): the 2^25 bursts of x16 folded into 2^16 slots with a tag
each (an access to a slot holding another tag stops the run, so no aliasing passes
unnoticed), a background word for never-written bursts (a function of the address, so a
read-before is not trivially zero), byte selects, calibration states 1-22 for 3,000
clocks (400,000 in one scenario), stalls on 20 % of clocks and 24 of every 700, in-order
acks 6-13 clocks late, and bench controls that hold `o_wb_stall` or the ack queue. The PLL
is a pass-through, so the controller clock is 200 MHz there. The host is part 1's
bit-level `uart_host_model`; every byte the device sends is compared with
`tools/uart_loader_protocol.DeviceModel` for protocol 3 (a sparse store with the model's
background, `not_ready`, the Wishbone master's write and read counts and its kept word);
not predicted are the status values that depend on timing (FIFO high water, clocks,
`calib_clocks`, most outstanding, read latency, command stalls). Bench monitors: every ack
reaches the master that issued the request and ownership changes only with nothing
outstanding (`misrouted`), every read answered from the kept word equals the memory
model's word at that clock (`TBKEPT`), while the loader waits for a frame with nothing
presented or outstanding it does not hold the port for more than one clock (`TBPORT`), and the
status line `clocks` is the count of the clock before its line was sampled (`TBCLK`). The summary of the run at a9a56541 is
[`reports/fpga/uart-loader-ddr3-sim-2026-09-24-a9a56541.json`](../reports/fpga/uart-loader-ddr3-sim-2026-09-24-a9a56541.json);
the run after the review fixes, at defd5c0d, is
[`reports/fpga/uart-loader-ddr3-sim-2026-09-25-defd5c0d.json`](../reports/fpga/uart-loader-ddr3-sim-2026-09-25-defd5c0d.json).
It differs from the first only where the fixes and the new checks are: the `TBPORT`,
`TBRACE` and `TBCLK` fields in every scenario, `watchdog` (`drops` 2, and two fewer writes: the word
dropped after the `port` nak), `baud` (the second change and its read-back, and the
turnaround below) and the new `late_ack`.

| Scenario | What it shows |
| --- | --- |
| `transfer_+0.05`, `transfer_-0.04` | host 5 % slow / 4 % fast: 11 chunks of 1-4096 bytes at unaligned addresses in six regions from 0 to above 256 MiB (1 FF0 0000 region: 511 MiB), each read before (background), loaded and read back; 37 bytes ending at the last byte of the store; three 1,000-byte chunks and one of 13 back to back at an unaligned address (every chunk boundary inside a word); a load of one byte into the word the read side keeps, then read again (the new byte, not the kept one). 27 read-backs each, 28 partial-word writes, 18,423 and 25,767 kept-word hits, 0 mismatches |
| `faults_-0.04` | part 1's fault set: corrupt, duplicate, dropped byte, host stop, unknown command, lengths 0, 4097 and past 2^29, status with a length, divisor 8, framing error, reset during a frame (UberDDR3 is reset and calibrates again), garbage, stray `A5`, eight glitches, a 0.2-bit glitch inside a frame, reset 200 bytes into a read-back |
| `not_ready` | calibration after 400,000 clocks: a load and a read before it get `not_ready`, the status shows `calib_complete` 0 and `calib_clocks` 0; after it the same load is acknowledged, `nak_not_ready` 2 |
| `watchdog` | reset while a commit waits on a port that takes no request; `port` naks for a commit whose request is never taken and one whose acks are withheld, for a read-back whose request is never taken, and for a read taken on the bus whose ack is withheld; the next read's request arrives while that read waits; the acks of both reads the loader gave up on are dropped (`wb_acks_dropped` 2, the bench's `drops` 2; before the review fixes the first reached the idle loader as an unused answer) and the next read answers right; every chunk reloaded and identical; a never-written range reads as the background |
| `pipelined`, `overflow`, `baud` | part 1's: frames back to back, a FIFO overflow (nak, reload), a baud change and its fallback; then a second change after the fallback, which holds (`baud_reverts` stays 1) |
| `board_rate_+5.0%`, `board_rate_-4.5%` | the board's divisor 723 and 50 ms timeout, host 5 % slow / 4.5 % fast, 64 bytes at the top of the store |
| `arbitration` | the #62 reader as master 1 (1,237 trits, bursts 0-19, runs until stopped) while the loader loads six chunks elsewhere and reads 40 bytes of the reader's region after each (their data are not predicted; the kept-word monitor checks them): 10,768 reader and 1,417 loader requests, 385 owner changes, 0 misrouted acks; the reader's report lines (4,186) decode to complete runs (the test asks for at least 100), every one equal to `tools/ddr3_read_model.py`, 0 stray acks (the decoder of `tools/fpga-ddr3-capture.py`) |
| `late_ack` | the ack of a read the loader gave up on reaches the master in the clock the next read-back asks for its first data byte (the bench releases it then, `+race_late_ack=1`): it is dropped (`drops` 1) and both read-backs after it are right |

Also in the file: the source diff test (every hunk in which `fpga_ddr3_loader.t27`
differs from `fpga_uart_loader.t27` carries a `[ddr3]` mark), the generated C of the new
functions against Python and zlib (byte of a word, byte merge, select bits, outstanding
counts, parallel CRC step on 20,000 random inputs and zlib strings, status tree against
the part-1 status function, header rules at 2^29), the protocol-3 model on its own, and the
host tool against the model on part 1's timed fake line (`--wait-calib` polls until the
calibration, margins, `not_ready` naks retransmitted).

### Build

Native yosys 0.69 and nextpnr-xilinx 0.9.7 with its chip database, prjxray of
`regymm/openxc7@sha256:eced1cdd…`, as the #61/#62 builds (`docs/hardware.md`):

```sh
make -C fpga/ax7203 ddr3-sweep DDR3_APP=loader YOSYS='cd $(ROOT) && yosys' IMAGE=regymm/openxc7@sha256:eced1cdd4727549f2d983328e0cf170fb6f6f67d87f19b2bf24365163368c70c \
  DDR3_TOOLS=$T/nextpnr-xilinx-0.9.7 DDR3_XRAY=$T/prjxray-db-a90f27c1 DDR3_META=$T/nextpnr-xilinx-meta-a4af910c \
  DDR3_CHIPDB=$T/chipdb/chipdb-nextpnr-0.9.7/xc7a200tfbg484-2.bin DDR3_SEEDS='1 2 3 4 5 6 7 8 9 10 11 12'
python3 tools/ddr3-seed-choice.py --sweep reports/fpga/ddr3-seed-sweep-2026-09-24-a9a56541-x16-loader.json \
  --output reports/fpga/ddr3-seed-choice-2026-09-24-a9a56541-x16-loader.json
make -C fpga/ax7203 ddr3-bit ddr3-report DDR3_APP=loader ... DDR3_SEEDS=3 \
  DDR3_BUILD=$PWD/build/fpga/ddr3-x16-loader-a9a56541-seed3 DDR3_REPORT=$PWD/reports/fpga/ddr3-build-2026-09-24-a9a56541-x16-loader-seed3
```

(`...` = the tool variables of the first line; `$T` = `tools-native/` here.) The sweep of
record ran as three `make ddr3-sweep` invocations of seeds 1-4, 5-8 and 9-12 in parallel,
in three build directories whose netlists have the same sha256 (`9cfb2df2…`), and
`tools/fpga-seed-sweep.py` summarised the twelve logs together (its `note` says so).
Netlist of commit a9a56541 (`BUILD_ID`, the `H` line): yosys 7,541 LUT, 5,465 FF, 576
CARRY4, 2 RAMB36E1 (the x16 pattern test: 6,907 LUT; the #62 reader: 11,122); nextpnr
12,136 SLICE_LUTX. 10 of the 12 seeds meet 83.33 MHz (85.42-91.40 MHz), seeds 5 and 8 miss it
(81.01 and 81.41)
([`ddr3-seed-sweep-2026-09-24-a9a56541-x16-loader.json`](../reports/fpga/ddr3-seed-sweep-2026-09-24-a9a56541-x16-loader.json)).

**Seed chosen before loading, by the CK - DQS rule of #61/#62** (among the seeds that meet
83.33 MHz, the one whose larger |CK - DQS| of the two lanes is smallest, in nextpnr's delay
model; a correlation from #61 and #62, not a timing analysis):
[`ddr3-seed-choice-2026-09-24-a9a56541-x16-loader.json`](../reports/fpga/ddr3-seed-choice-2026-09-24-a9a56541-x16-loader.json),
written 20:08:51 UTC, 3 min 29 s before the first load of this netlist (20:12:20 UTC, run 1
below; no other seed of it was loaded): seed 3, -297 / +304 ps, 87.84 MHz; next 9 (-310 /
-305 ps), 1, 4, 2, 12, 7, 10, 11, 6. Built on its own (`DDR3_SEEDS=3`): FASM `9e005765…` =
the sweep's seed 3, `.bit` from the sync word `73d09b8d…`, whole file `07036242…`, PLL tables
PASS for MULT 5, VREF 0.675 V on bank 35 as in every DDR3 build here
([`ddr3-build-2026-09-24-a9a56541-x16-loader-seed3/`](../reports/fpga/ddr3-build-2026-09-24-a9a56541-x16-loader-seed3/build.json),
`rams.json`: 2 RAMB36E1, width 36).

**Existing designs unchanged.** With this branch's Makefile, `make -n -B` prints the same
commands as on the base branch (feat/uart-loader, e1c36ce) for `gen`, `sim`, `bit`,
`bram-sim`, `bram-bit` (four variants), `bram-ooc`, `loader-bit`, `loader-report`,
`loader-synth`, `ddr3-bit` (default, x32, the 45a986b8 line, `DDR3_PATTERN=0`, with
`DDR3_UART_DEBUG_BIST=1`, `DDR3_APP=reader`), `ddr3-report` (default, reader),
`ddr3-sweep` (default, reader), `ddr3-flash` and `clean` (24 command lines compared,
session scratch, not committed); the loader's recipe is a separate `ifeq` branch. None of
their source files changed, and a synthesis of the #61 pattern test (`BUILD_ID=7deeef16`),
the #62 reader (`BUILD_ID=42b6f5a9`) and the part-1 block-RAM loader (`BUILD_ID=1d474000`)
from this branch and from a `git archive` of e1c36ce gave netlists of the same sha256
(`10fc1353…`, `2282e99e…`, `a098a297…`; the last is also the netlist of part 1's build of
record) ([`ddr3-loader-netlist-identity-2026-09-24.json`](../reports/fpga/ddr3-loader-netlist-identity-2026-09-24.json)).

### Board results (2026-09-24, build a9a56541 seed 3)

These runs are of build a9a56541, the RTL before the review fixes below ("Review fixes after
the board runs"), which have run in Icarus only.

ALINX AX7203, IDCODE `0x3636093`, DNA `0x00389c0c2d85e85c`, SRAM load only, CP2102N at
`/dev/cu.usbserial-110`, 115200 baud, macOS, pyserial. Seven `tools/fpga-uart-loader.py`
records (`trinity.uart-loader-capture.v2`) under
[`reports/fpga/uart-loader-ddr3-2026-09-24-a9a56541-seed3/`](../reports/fpga/uart-loader-ddr3-2026-09-24-a9a56541-seed3/),
each with IDCODE, DNA and XADC before and XADC after, the device's 37 status lines before
and after, the range read before the load (`store_before.bytes_the_load_changes`), 64 bytes
on each side read before the load and after the read-back (`margins`), every attempt with
its times and reply, and the received bytes (`*.rx.bin.gz`; those of runs 3 and 7 are not
committed: their sha256 are in the records, the files were kept outside the repository at
`/private/tmp/claude-501/wave3/63b/keep/`, which is not permanent). The figures below are in
[`reports/fpga/uart-loader-ddr3-summary-2026-09-24-a9a56541.json`](../reports/fpga/uart-loader-ddr3-summary-2026-09-24-a9a56541.json)
(`tools/uart-loader-summary.py`, copied from the records). Run 1 loaded the bitstream
(its whole-file sha256 in run 1's record, `07036242…`, is the build report's) and received
the `H` line: build a9a56541, config `0x030C1D0A`. The whole session
was one SRAM load, 20:12:20-20:28:25 UTC; no reset in between.

**Payloads** (real weights, `tools/extract-bram-trits.py`: the pinned packed BitNet b1.58
2B4T checkpoint through the t27 decoders, the GGUF's I2_S tensor agreeing trit for trit):
layer-0 `q_proj` rows 0-319 (819,200 trits: +1 284,129, 0 251,701, -1 283,370) as TMEM v1
dense5 (163,864 bytes, sha256 `6397485…`, 41 chunks) and as TMEM v1 baseline2 (204,824
bytes, `47791ce…`, 51 chunks); the whole layer-0 `q_proj`, 2560 x 2560 (6,553,600 trits:
+1 1,652,240, 0 3,251,715, -1 1,649,645) as dense5 (1,310,744 bytes, `174a1a0…`, 321
chunks).

| Run | What | Address | Chunks / attempts | Bytes the load changed | Payload rate, load / read-back | Result |
| --- | --- | --- | --- | --- | --- | --- |
| `run1-rows-dense5` | SRAM load, wait for calibration, rows 0-319 dense5 | `0x01000007` (16 MiB + 7) | 41 / 41 | 163,215 of 163,864 | 11,388.9 / 11,365.3 B/s | acked, identical, margins unchanged |
| `run2-rows-baseline2` | rows 0-319 baseline2 | `0x09001003` (144 MiB + 4,099) | 51 / 51 | 204,111 of 204,824 | 11,389.0 / 11,367.1 B/s | acked, identical, margins unchanged |
| `run3-qproj-dense5-high` | whole q_proj dense5 | `0x1ABCD007` (427.8 MiB) | 321 / 321 | 1,306,376 of 1,310,744 | 11,391.7 / 11,360.9 B/s | acked, identical, margins unchanged |
| `run4-faults` | rows 0-319 dense5; port closed and opened 5 times first; corrupt chunks 3 and 25, drop 7 and 30, abort 11, garbage before 15, duplicate 19 | `0x0400000B` | 41 / 46 | 163,221 | 8,242.4 / 11,357.9 B/s | acked, identical, margins unchanged |
| `run5-recheck-rows-dense5` | run 1's payload at run 1's address again | `0x01000007` | 41 / 41 | **0** | 11,375.0 / 11,356.2 B/s | identical |
| `run6-recheck-rows-baseline2` | run 2's again | `0x09001003` | 51 / 51 | **0** | 11,376.6 / 11,357.8 B/s | identical |
| `run7-recheck-qproj-dense5-high` | run 3's again | `0x1ABCD007` | 321 / 321 | **0** | 11,378.7 / 11,363.0 B/s | identical |

"Bytes the load changed" counts the payload bytes that differed from what the range held
before the load (read first), so an identical read-back shows at least that many bytes
landed; the others matched already. In runs 1-4 almost every byte differed. By #61's model of
UberDDR3's self-test (`docs/hardware.md`, "Coverage"), which writes three quarters of U6 during
the calibration, the ranges of runs 1, 3 and 4 and the bank 2-5 half of run 2's held its data,
and the rest of run 2's range, where it never writes, whatever the chips held. The bytes read
before the load agree: every whole burst of runs 1 and 4 and the 6,655 bank 2-5 bursts of run 2
equal the self-test's data for their address, and none of run 2's other 6,145 bursts does (run
3's received bytes are not committed). Runs 5-7
read their ranges before loading and found every byte of runs 1-3 still there: 0 bytes
changed. Runs 5, 6 and 7 started 7 min 34 s, 7 min 23 s and 2 min 31 s after runs 1, 2 and 3
ended (record times), with other runs' loads and reads in between. That is retention within one load of the bitstream for minutes,
not a soak.

**Addresses and byte selects.** Every start address is unaligned (7, 3, 7 and 11 mod 16 in runs
1-4), so
every 4096-byte chunk starts and ends inside a 16-byte word and the next chunk writes the
rest of that word with its own byte selects: an identical read-back needs the data mask to
keep the first chunk's bytes. In run 1 the device counted 10,282 Wishbone writes: the 10,242
words the range touches plus the 40 chunk boundaries written twice (arithmetic from the
address; this is how the packer issues, so it checks the count path, not the memory). The
64 bytes on each side of each range (outside it, in its first and last words and the words
beyond) were read before the load and after the read-back and were unchanged in all seven
runs. Run 3 and 7's range lies above 256 MiB: burst addresses `0x1ABCD00`-`0x1AD0D01`, bit 24
of the 25 set.

**Calibration held throughout.** Before run 1's load the host polled the status every 0.57 s
(`--wait-calib`): states 17, 17, 18, 19, 19, 20, 20, 21, 21, then `calib_complete` 1 at state
23 (the poll 5.8 s after the `H` line arrived; the device's own count below puts it 5.196 s
after reset). In all 14 status reads (before and
after each run, 20:12:39 to 20:28:24 UTC) the calibration word was `calib_complete` 1, state
23, highest state 23, 0 returns to IDLE, `calib_clocks` 432,977,330 (the same calibration: 5.196
s after reset at the nominal 83.333 MHz, arithmetic) and `calib_lost_clocks` 0: the device
counted no clock since the calibration with `calib_complete` low or a state other than 23
(a 32-bit count that wraps; with the state read as 23 at every status, only a multiple of
2^32 lost clocks, 51.5 s each at 83.33 MHz, could hide a loss). With `BIST_MODE 1` state 23 and no return to
IDLE also means UberDDR3's one self-test pass inside the calibration had no wrong read (the
#61 check, re-run here with the loader in the build). `nak_not_ready` stayed 0: no frame
was sent before the calibration on the board; the `not_ready` path is shown in simulation
only.

**Faults (run 4).** The corrupt chunks got `crc` naks and the dropped-byte chunks and the
aborted one (half a frame, then the port closed and opened) `timeout` naks; each was
acknowledged on its next attempt (46 attempts for 41 chunks; retransmits `crc` 2, `timeout`
3; no late reply). The 64 garbage bytes were counted as `ignored_bytes` 64 and the frame
after them acknowledged. The duplicate (chunk 19 sent again after its ack) was answered
`duplicate` (`frames_duplicate` 1) and not committed (`frames_committed` +41 over the run).
`rx_framing_errors` and `rx_false_starts` stayed 0 across the five reopenings and the abort.

**Rates and latency, 115200.** Load payload rate 11,375.0-11,391.7 B/s in the runs without
faults, 99.1-99.2 % of the 11,480.8 B/s that 4110-byte frames give at 115200 (arithmetic);
read-back 11,356.2-11,367.1 B/s. The whole q_proj (1,310,744 bytes) loaded in 115.06 s (run
3) and 115.19 s (run 7); with its read-before and read-back run 3 took 5 min 47 s. Per
4096-byte chunk, from the start of the host's write to the ack's arrival (the reader
thread's stamp, not tcdrain): median 359.45-359.76 ms, maximum 361.74 ms; the frame's wire
time is 356.8 ms, and the rest (`turnaround_s`, per full chunk sent once) has a median of
2.68-2.99 ms (min 2.57 ms): the commit (3 clocks per byte, 0.15 ms at 83.33 MHz), the Wishbone
drain, the 20-byte ack line (1.74 ms) and USB and host (arithmetic). The device's own
counters: at most 2 Wishbone requests outstanding, a read answered at most 47 controller
clocks after it was presented (0.56 µs), 6,606,758 of the 7,047,248 bytes read answered from
the kept word (every read of the session, read-befores included, from run 7's status).

**Die temperature** (XADC): 38.0 °C before run 1, rising to 49.3 °C after run 7; VCCINT
0.993-0.995 V.

### Done-when of #63

The issue's done-when: (1) block RAM: on the board a chunk of real layer weights loads and
reads back byte-identical, injected corruption is detected and retransmitted, and the
capture reports payload throughput, retransmits and the latency distribution; (2) DDR3: the
same into DDR3, and DONE_CALIBRATE still holds with the loader added. (1) is met by part 1
(build 1d474000, "Board results"). (2) is met by the runs above: real q_proj weights in
DDR3 read back byte-identical, three payloads in two formats (runs 1-3, and again in runs 5-7),
corruption, dropped bytes and an abort detected and retransmitted (run 4), throughput,
retransmits and latency distributions in every record, and state 23 with no return to IDLE
and no clock off it from the calibration to the last status. **Both parts of the done-when
are met, for x16 on this board, by build a9a56541**, with the limits below (x32 not built;
`not_ready`, the Wishbone-side watchdog and the arbiter's second master shown in simulation
only; the review fixes after the board runs have run in Icarus only).

### Review fixes after the board runs (simulation only)

Three independent reviews of the branch after the board runs found four defects in the RTL,
none on the path the board runs used (loads and read-backs at 115200 with the port answering).
Each fix has a scenario above that fails on build a9a56541's RTL and passes after it, in Icarus;
none has run on the board.

- **A second baud change after a fallback was undone one clock after it took effect.** The
  registered probation compare (`prob_over_q`) still held the ended probation's result in the
  first clock of the new one, because `prob_count` was not cleared when a probation ended.
  It is now 0 outside a probation. Part 1 compares the live count and never had this. Scenario
  `baud`.
- **A late ack could answer the next read-back's first byte.** The ack of a read the loader had
  given up on was dropped only if the loader was already asking for another byte; arriving in
  the clock the next read-back raised its request, it came back as that request's answer, in a
  frame with a good CRC. The loader now says when it gives up (`port_give_up`), and the master
  drops that read's ack whenever it comes. Scenario `late_ack`.
- **After a `port` nak in the middle of a chunk the loader kept the port.** The master still
  held the unfinished word's bytes, so `wb_cyc` stayed high and the arbiter could not give the
  port to master 1 until the loader's next command (and that command then wrote the stale
  bytes). `port_give_up` now drops them. The `TBPORT` monitor, in every scenario (`watchdog`
  showed 349 clocks before the fix).
- **Status line `clocks` was one status line old**, not one clock as its comment said: a copy
  taken in the clock the line was sampled reached it on the next line. The line is now sent from
  that copy, taken in the clock before (the counter itself stays out of the module-level logic,
  which the generated Verilog would otherwise evaluate again in every clock). The `TBCLK`
  monitor, in every scenario, compares the line with the counter in the first clock of its
  response (2 clocks behind now; `pipelined` showed 3,230 before the fix).
- Minor: the flush of a buffered word did not wait for fewer than 8 outstanding requests
  ("Write side").

Three host-side findings are fixed in `tools/fpga-uart-loader.py` (the board records above
were written before them and pass the new checks, recomputed from their fields):
`calibration_held` took an equal `calib_clocks` to mean no reset, but the calibration takes the
same number of clocks after every reset; it now also requires no `H` line after the first
status read and `frames_committed` to grow by exactly the run's chunks (0, 41, 92, 413, 454,
495, 546, 867 across runs 1-7: no reset in the session). A baud trial now fails when its
margins changed, and the calibration is checked in every trial's status too. The margins stop
at the end of the store, and under `--wait-calib` the first status read stays in the record.

The simulation summaries' load turnaround subtracted the reply byte's 9.5 bits at the
scenario's last rate, not at the rate the load was sent at. Only `baud` changes rate, and its
one load (divisor 16) was reported with the divisor after the fallback (32): 1,422 clocks in
a9a56541's summary and 1,353 in part 1's
[`uart-loader-sim-2026-09-24-1d474000.json`](../reports/fpga/uart-loader-sim-2026-09-24-1d474000.json),
for replies that give 1,574 and 1,505 (part 1's record also holds the raw
`end_to_reply_stop_clocks`, 1,657 = 1,505 + 9.5 × 16). Both tests now keep the divisor with each load; no check
used the `baud` value, and the other scenarios run at one rate.

### Limits and open points (part 2)

- The review fixes above have run in Icarus only; the next DDR3 build carries them to the
  board (#64 builds the device matvec on this loader).
- x16 only (U6, 512 MiB); x32 not built. One placement (seed 3) of one netlist was loaded,
  once, for 16 minutes; the placement sensitivity of #61/#62 applies (the seed was chosen by
  the CK - DQS rule, which is a correlation). No soak, no second board.
- `not_ready`, the port watchdog on the Wishbone side, the late-ack drop and the arbiter with
  a second master are shown in simulation only; on the board master 1 is idle and no frame
  came before the calibration.
- A reset (button or PLL) resets UberDDR3, which calibrates again and runs its self-test. By
  #61's model of `BIST_MODE 1` (`docs/hardware.md`, "Coverage") that writes three quarters of
  U6: bursts [0, 2^23) (the first 128 MiB), banks 2-5 of every row, and bursts [3·2^23, 2^25)
  (384-512 MiB, run 3's range included), so data loaded there do not survive a reset. It never
  writes rows 8192-24575 of banks 0, 1, 6 and 7: bytes 128-384 MiB whose bank bits
  `(address >> 11) & 7` are 0, 1, 6 or 7. The bytes read before runs 1, 2 and 4 agree with the
  model ("Board results"); whether data survive a reset there was not measured.
- The kept read word is coherent with the loader's own writes and with the other master's
  writes through the arbiter's `m1_write`; a writer that bypasses the arbiter would not be
  seen (none exists).
- Retention was checked within minutes of loading, in one load of the bitstream.
- The simulation's memory is ours, not UberDDR3 (its stall and ack timing are invented), and
  the PLL stub runs everything at 200 MHz there.
- The DDR3 loader is a copy of part 1's with marked changes: a fix to one must be carried to
  the other by hand (the source diff test only checks that the differences are marked).
- 2 of the 12 seeds miss 83.33 MHz (5 and 8: 81.01 and 81.41 MHz); in the sweep record the
  critical path of seed 5 ends in UberDDR3's scheduler (`stage1_do_pre_d`) and that of seed 8
  starts at the Wishbone master's outstanding count (`master.outst`).
- The registered checks of the DDR3 loader are assigned at the end of `on_clock` from values
  that branches above may have written in the same tick: the Verilog (nonblocking) reads the
  previous clock's values, which the settle clocks rely on, while the generated C of
  `on_clock` would read the new ones. No test runs the C of `on_clock` (only its functions),
  so the file's "every statement reads a variable before the same tick writes it" does not
  hold for those lines; the Icarus runs are the check of the module.
