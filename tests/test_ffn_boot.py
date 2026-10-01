"""Boot profile gates must run before any FPGA configuration operation."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('ffn_boot',ROOT/'tools/fpga-matvec-boot.py')
boot=importlib.util.module_from_spec(spec);spec.loader.exec_module(boot)


class BootProfiles(unittest.TestCase):
    def test_profiles_hash_and_timing(self):
        prefix='x16 BYTE_LANES 2, PLL CLKFBOUT_MULT 6, DDR3 clock VCO/5 (4.167 ns), controller 16.667 ns, '
        for option,variant,tag in [('--ffn','FFN (issue 92)','F'),('--gf16-ffn','GF16_FFN (issue 111)','G')]:
            for failure in ('none','other-profile','hash','timing','missing-option'):
                with self.subTest(option=option,failure=failure),tempfile.TemporaryDirectory() as tmp:
                    folder=Path(tmp);bit=folder/'test.bit';bit.write_bytes(b'not a real bitstream')
                    report={'bitstream':{'sha256':hashlib.sha256(bit.read_bytes()).hexdigest()},
                        'nextpnr':{'clocks_routed':[{'verdict':'PASS'}]},
                        'ddr3':{'variant':prefix+variant,'netlist_build_id':'12345678'}}
                    if failure=='other-profile':report['ddr3']['variant']=prefix+('GF16_FFN (' if tag=='F' else 'FFN (')
                    if failure=='hash':report['bitstream']['sha256']='0'*64
                    if failure=='timing':report['nextpnr']['clocks_routed'][0]['verdict']='FAIL'
                    rp=folder/'report.json';rp.write_text(json.dumps(report))
                    argv=['boot','--report',str(rp),'--bit',str(bit),'--output',str(folder/'result')]
                    if failure!='missing-option':argv.append(option)
                    run={'uart_raw':b'','uart_entries':[{'line':'H123456780000000000'},
                        {'line':f'{tag}00000a000000001b00'}]}
                    with patch.object(sys,'argv',argv),patch.object(boot.capture,'expectations',return_value={}),\
                         patch.object(boot.capture,'board_run',return_value=(run,0)) as device,\
                         patch.dict(os.environ),patch('builtins.print'):
                        if failure=='none':
                            self.assertEqual(boot.main(),0);device.assert_called_once()
                        else:
                            with self.assertRaises(SystemExit):boot.main()
                            device.assert_not_called()


if __name__=='__main__':unittest.main()
