# UART receive pin of the DDR3 loader build (issue #63 part 2, DDR3_APP=loader),
# appended by fpga/ax7203/Makefile to tms_ddr3_ax7203.xdc; the pin and standard of
# the verified tms_trace_player.xdc (the CP2102N's TXD on P20).
set_property PACKAGE_PIN P20 [get_ports uart_rx]
set_property IOSTANDARD LVCMOS33 [get_ports uart_rx]
