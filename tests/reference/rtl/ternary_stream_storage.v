// Synchronous single-clock memory and fixed-length read sequencer.
// Memory is writable only while idle. Reset affects control, never RAM contents.
`timescale 1ns/1ps
`default_nettype none
module ternary_stream_storage #(
    parameter integer CODE_WIDTH = 8,
    parameter integer WORDS = 64,
    parameter integer ADDR_WIDTH = (WORDS > 1) ? $clog2(WORDS) : 1
) (
    input wire clk,
    input wire rst,
    input wire start,
    output wire busy,
    input wire load_en,
    input wire [ADDR_WIDTH-1:0] load_addr,
    input wire [CODE_WIDTH-1:0] load_code,
    output wire load_ready,
    output wire out_valid,
    output wire out_last,
    output wire [CODE_WIDTH-1:0] out_code
);
    reg [CODE_WIDTH-1:0] memory [0:WORDS-1];
    reg [CODE_WIDTH-1:0] read_code;
    reg [ADDR_WIDTH-1:0] read_addr;
    reg active;
    reg valid_q;
    reg last_q;

    assign busy = active | valid_q;
    assign load_ready = !rst && !busy && !start;
    assign out_valid = valid_q;
    assign out_last = valid_q && last_q;
    assign out_code = valid_q ? read_code : {CODE_WIDTH{1'b0}};

    // Keep RAM data outside the control reset to permit synchronous RAM inference.
    always @(posedge clk) begin
        if (load_en && load_ready && (load_addr < WORDS))
            memory[load_addr] <= load_code;
        if (!rst && active)
            read_code <= memory[read_addr];
    end

    always @(posedge clk) begin
        if (rst) begin
            read_addr <= {ADDR_WIDTH{1'b0}};
            active <= 1'b0;
            valid_q <= 1'b0;
            last_q <= 1'b0;
        end else begin
            valid_q <= 1'b0;
            last_q <= 1'b0;
            if (start && !busy) begin
                read_addr <= {ADDR_WIDTH{1'b0}};
                active <= 1'b1;
            end else if (active) begin
                valid_q <= 1'b1;
                if (read_addr == WORDS-1) begin
                    active <= 1'b0;
                    last_q <= 1'b1;
                end else begin
                    read_addr <= read_addr + 1'b1;
                end
            end
        end
    end
endmodule
`default_nettype wire
