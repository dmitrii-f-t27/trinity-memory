/* Memory map of trios_soc_core.v: 32 KiB RAM at 0, reset vector 0. Whole image is loaded
   into RAM by the bitstream (block-RAM INIT); there is no copy from flash. */
OUTPUT_ARCH(riscv)
ENTRY(_start)
MEMORY { RAM (rwx) : ORIGIN = 0x00000000, LENGTH = 32K }
SECTIONS
{
  .text : { KEEP(*(.text.init)) *(.text .text.*) } > RAM
  .rodata : { *(.rodata .rodata.*) *(.srodata .srodata.*) } > RAM
  .data : { *(.data .data.*) *(.sdata .sdata.*) } > RAM
  .bss (NOLOAD) : { . = ALIGN(4); _sbss = .; *(.bss .bss.*) *(.sbss .sbss.*) *(COMMON) . = ALIGN(4); _ebss = .; } > RAM
  _stack_top = ORIGIN(RAM) + LENGTH(RAM);
  ASSERT(_ebss + 0x1800 <= _stack_top, "less than 6 KiB left for the stack")
  /DISCARD/ : { *(.eh_frame*) *(.comment) }
}
