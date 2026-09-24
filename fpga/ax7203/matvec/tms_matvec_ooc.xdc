## Out-of-context harness of the device matvec (tms_matvec_ooc.v): the AX7203's 200 MHz input,
## reset key, and the UART pins as the serial input and the one output. One port per line.
set_property PACKAGE_PIN R4 [get_ports clk200_p]
set_property IOSTANDARD DIFF_SSTL15 [get_ports clk200_p]
set_property PACKAGE_PIN T4 [get_ports clk200_n]
set_property IOSTANDARD DIFF_SSTL15 [get_ports clk200_n]
set_property PACKAGE_PIN T6 [get_ports rst_n]
set_property IOSTANDARD LVCMOS15 [get_ports rst_n]
set_property PACKAGE_PIN P20 [get_ports din]
set_property IOSTANDARD LVCMOS33 [get_ports din]
set_property PACKAGE_PIN N15 [get_ports dout]
set_property IOSTANDARD LVCMOS33 [get_ports dout]
create_clock -period 12.000 -name clk [get_ports clk200_p]
