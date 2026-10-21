#!/usr/bin/env python3
"""Offline tests of board_parse.py: formats seen from the installed openFPGALoader (with a made-up DNA) and negative cases."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import board_parse as bp

XADC_OK = """empty
Jtag frequency : requested 6.00MHz    -> real 6.00MHz   
{"temp": 61.25, 
    "maxtemp": 62.5, 
    "mintemp": 26.2851, 
"raw":  {"0": 1, "1": 2, "2": 3, "3": 0, "4": 0, "5": 0, "6": 4, "7": 0},
"vccint": 0.994629, 
   "maxvccint": 0.996094, 
   "minvccint": 0.993896, 
}
"""
DNA_OK = 'empty\nJtag frequency : requested 6.00MHz    -> real 6.00MHz   \n{"dna": "0x0123456789abcdef"}\n'
DETECT_OK = "index 0:\n\tidcode 0x3636093\n\tmanufacturer xilinx\n\tfamily artix a7 200t\n"


def raises(fn, text):
    try:
        fn(text)
    except ValueError:
        return True
    return False


assert bp.parse_xadc(XADC_OK)["temp"] == 61.25
assert bp.parse_dna(DNA_OK) == "0x0123456789abcdef"
assert bp.parse_dna('{"dna": "0xabc"}') == "0x0000000000000abc"
assert bp.parse_idcode(DETECT_OK) == "0x3636093"
# negative cases: each must stop the caller
assert raises(bp.parse_xadc, "")
assert raises(bp.parse_xadc, "Jtag frequency : requested 6.00MHz\nError: cable not found\n")
assert raises(bp.parse_xadc, '{"maxtemp": 60.0, }')                     # no temp
assert raises(bp.parse_xadc, '{"temp": "hot", }')
assert raises(bp.parse_xadc, '{"temp": NaN, }')
assert raises(bp.parse_xadc, '{"temp": 1e999, }')
assert raises(bp.parse_xadc, '{"temp": 400.0, }')
assert raises(bp.parse_xadc, '{"temp": 61.2')                            # truncated
assert raises(bp.parse_dna, "")
assert raises(bp.parse_dna, 'Error: read failed\n')
assert raises(bp.parse_dna, '{"dna": "zz"}')
assert raises(bp.parse_idcode, "no device")
print("board parser tests passed")
