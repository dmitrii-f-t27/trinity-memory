"""Boundary-rounding controls; real-weight replay is an explicit CI command."""
import math
import unittest
from unittest.mock import patch

from tools import ffn_quantization as q, gf16_reference as gf


def tiny_model():
    return {'gate': [1,-1,1,1], 'up': [1,1,-1,1], 'down': [1,1,-1,1],
            'w_post': [0.5,1.0], 'w_sub': [1.0,0.25],
            'scales': {'gate': 0.125,'up': 0.5,'down': 0.25},
            'shapes': {'gate': [2,2],'up': [2,2],'down': [2,2]}}


class Bf16(unittest.TestCase):
    def test_every_decode_roundtrip(self):
        for word in range(65536):
            expected = 0x7fc0 if word & 0x7f80 == 0x7f80 and word & 127 else word
            self.assertEqual(q.bf16_encode(word << 16), expected)

    def test_every_finite_boundary_both_signs(self):
        # Nearest binary32 neighbors around all positive BF16 midpoints;
        # includes zero/subnormal, exponent carries and overflow to infinity.
        for lo in range(0x7f80):
            midpoint = (lo << 16) + 0x8000
            for bits, expected in ((midpoint-1,lo),(midpoint,lo+(lo&1)),(midpoint+1,lo+1)):
                self.assertEqual(q.bf16_encode(bits), expected)
                self.assertEqual(q.bf16_encode(bits | 0x80000000), expected | 0x8000)


class Measurements(unittest.TestCase):
    def test_metrics_never_discard_nonfinite_lanes(self):
        m = q.metrics([1,2], [1,math.inf])
        self.assertEqual(m['nonfinite_candidate'], 1)
        self.assertIsNone(m['nmse'])
        self.assertIsNone(m['max_abs_error'])
        self.assertEqual(q.metrics([0,0],[0,0])['nmse'],0)
        self.assertIsNone(q.metrics([0],[1])['nmse'])
        self.assertEqual(q.metrics([1,2],[2,4])['nmse'],1)
        with self.assertRaises(ValueError): q.metrics([1],[1,2])
        with self.assertRaises(ValueError): q.metrics([],[])

    def test_distinguish_subnormals_and_flushed_values(self):
        values = [0, -0.0, 2**-39, -2**-39, 2**-40, -2**-40, 2**-30, 2**40, -2**40]
        out, record = q.quantize(values, 'gf16')
        self.assertEqual(record['events']['subnormal_output'], 2)
        self.assertEqual(record['events']['tiny_input'], 4)
        self.assertEqual(record['events']['underflow_to_zero'], 2)
        self.assertEqual(record['events']['format_overflow'], 2)
        self.assertEqual(math.copysign(1,out[5]), -1)
        self.assertIsNone(record['vs_binary32']['nmse'])
        _, record = q.quantize([2**140,2**-160], 'bf16')
        self.assertEqual(record['events']['f32_overflow'], 1)
        self.assertEqual(record['events']['f32_underflow_to_zero'], 1)
        self.assertEqual(record['events']['format_overflow'], 0)

    def test_gf16_boundary_conversion_matches_first_iteration(self):
        for x in [1.001, -3.14159, 2**-40, 1e-8, 1e8]:
            out, _ = q.quantize([x], 'gf16')
            self.assertEqual(out[0], gf.value(gf.encode(gf.f32_bits(x))))

    def test_no_quantization_control_equals_existing_f64_oracle(self):
        model, x = tiny_model(), [3.0,-2.0]
        reference = q.fr.reference_f64(model,x)
        with patch.object(q, 'quantize', side_effect=lambda values, fmt: (values,{})):
            path = q.propagate(model,x,'gf16',reference)
        self.assertEqual(path['status'],'finite')
        for name in reference:
            self.assertEqual(path['boundaries'][name]['vs_f64_oracle']['nmse'],0,name)

    def test_zero_and_range_stress_are_not_hidden(self):
        case = q.case_report(tiny_model(),'zero',[0.0,0.0])
        for path in case['propagated'].values():
            self.assertEqual(path['status'],'finite')
            self.assertEqual(path['boundaries']['y']['vs_f64_oracle']['nmse'],0)
        case = q.case_report(tiny_model(),'stress',[2**40,-2**40])
        for name in ('gf16_stage16','gf16_wide_a'):
            path = case['propagated'][name]
            self.assertEqual(path['blocked_at'],'x')
            self.assertNotIn('y',path['boundaries'])
            self.assertEqual(path['boundaries']['x']['events']['format_overflow'],2)
        self.assertEqual(case['propagated']['bf16_stage16']['status'],'finite')

    def test_wide_product_is_explicit_and_not_counted_as_16_bits(self):
        model, x = tiny_model(), [3.0,-2.0]
        model['scales']['gate'] = model['scales']['up'] = 2**16
        reference = q.fr.reference_f64(model,x)
        narrow = q.propagate(model,x,'gf16',reference)
        wide = q.propagate(model,x,'gf16',reference,wide_a=True)
        self.assertEqual(narrow['blocked_at'],'a')
        self.assertEqual(wide['status'],'finite')
        self.assertEqual(wide['boundaries']['a']['storage'],'binary64')
        self.assertNotIn('bits_sha256_le16',wide['boundaries']['a'])

    def test_input_validation_and_provenance(self):
        model=tiny_model()
        for x in ([1], [math.nan,0]):
            with self.assertRaises(ValueError): q.case_report(model,'invalid',x)
        model['shapes']['up']=[3,2]
        with self.assertRaises(ValueError): q.validate(model,[1,2])
        info=q.provenance()
        self.assertEqual(info['repo'],'microsoft/bitnet-b1.58-2B-4T')
        self.assertEqual(len(info['revision']),40)
        self.assertEqual(len(info['ranges']),9)


if __name__ == '__main__':
    unittest.main()
