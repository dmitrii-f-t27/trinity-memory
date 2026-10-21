## ALINX AX7203 -- XC7A200T-FBG484-2. Pins as in fpga/ax7203/tms_trace_player.xdc, which
## gHashTag/trinity-fpga verified on hardware (clock, LEDs, UART). One port per line.
set_property PACKAGE_PIN R4 [get_ports clk200_p]
set_property IOSTANDARD DIFF_SSTL15 [get_ports clk200_p]
set_property PACKAGE_PIN T4 [get_ports clk200_n]
set_property IOSTANDARD DIFF_SSTL15 [get_ports clk200_n]

set_property PACKAGE_PIN B13 [get_ports led[0]]
set_property IOSTANDARD LVCMOS18 [get_ports led[0]]
set_property PACKAGE_PIN C13 [get_ports led[1]]
set_property IOSTANDARD LVCMOS18 [get_ports led[1]]
set_property PACKAGE_PIN D14 [get_ports led[2]]
set_property IOSTANDARD LVCMOS18 [get_ports led[2]]
set_property PACKAGE_PIN D15 [get_ports led[3]]
set_property IOSTANDARD LVCMOS18 [get_ports led[3]]

set_property PACKAGE_PIN N15 [get_ports uart_tx]
set_property IOSTANDARD LVCMOS33 [get_ports uart_tx]
set_property PACKAGE_PIN P20 [get_ports uart_rx]
set_property IOSTANDARD LVCMOS33 [get_ports uart_rx]

## Clock domains by net name (the way fpga/ax7203/Makefile constrains the DDR3 clocks):
## clk200 = BUFG output of the 200 MHz oscillator (feeds the 3-bit divider only),
## clk25  = BUFG output of divider bit 2 = 25 MHz (feeds the CPU, RAM and UART).
create_clock -period 5.000 -name clk200 [get_nets clk200]
create_clock -period 40.000 -name clk25 [get_nets clk25]
