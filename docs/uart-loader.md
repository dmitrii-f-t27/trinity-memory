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

- Block RAM only (256 KiB). DDR3 is part 2 (next section).
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
- The port contract ("Write and read ports") holds for the Wishbone side: `wr_idle`
  low from the clock after an accepted byte until its word has been acknowledged by
  the controller, `rd_valid` once per accepted request (a hit in the kept word may
  answer in the accepting clock), and no late answer after the loader's watchdog has
  given up on a request (the adapter counts outstanding reads and drops their acks).
  The loader's port watchdog (one timeout, `port` nak) already turns a hung port into
  a nak instead of a silent stall; its timeout scales with the controller clock.
