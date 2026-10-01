"""Optional pinned-runtime controls; required by the capture workflow."""
import importlib.util
import os
import unittest
import tempfile
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ['TORCH_COMPILE_DISABLE']='1'
AVAILABLE=all(importlib.util.find_spec(n) for n in ('torch','numpy','transformers'))
if not AVAILABLE and os.environ.get('TRINITY_REQUIRE_CAPTURE_RUNTIME')=='1':
    raise RuntimeError('capture workflow requires torch, numpy and transformers')
if AVAILABLE:
    import numpy as np
    import torch
    from tools import bitnet_ffn_runtime as rt, gf16_reference as gf


class InputIntegrity(unittest.TestCase):
    def test_nonfinite_wide_candidate_fails_after_saving_diagnostics(self):
        from tools import capture_bitnet_layer0 as capture
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'report.json'
            result={'cases':[{'profiles':{'gf16_wide_product':{'status':'nonfinite'}}}]}
            with patch.object(capture,'capture',return_value=result), patch('sys.argv',['capture','--output',str(output)]):
                self.assertEqual(capture.main(),1)
            self.assertTrue(output.is_file())

    def test_offline_ranges_reject_corruption_and_out_of_bounds(self):
        from tools import capture_bitnet_layer0 as capture
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);(folder/'ranges').mkdir()
            (folder/'ranges/0-4.bin').write_bytes(b'abcd')
            remote=SimpleNamespace(size=4,directory=folder/'legacy',entry=SimpleNamespace(ranges={}))
            item={'begin':0,'end':4,'sha256':hashlib.sha256(b'abcd').hexdigest()}
            with patch.object(capture,'CACHE',folder):
                self.assertEqual(capture.range_bytes(remote,item,False),b'abcd')
                (folder/'ranges/0-4.bin').write_bytes(b'bad!')
                with self.assertRaises(ValueError):capture.range_bytes(remote,item,False)
                with self.assertRaises(ValueError):capture.range_bytes(remote,{'begin':0,'end':5},False)


@unittest.skipUnless(AVAILABLE,'optional torch/numpy/transformers capture environment')
class Runtime(unittest.TestCase):
    def setUp(self):
        torch.set_num_threads(1)
        self.context=torch.no_grad();self.context.__enter__()
        self.addCleanup(self.context.__exit__,None,None,None)

    def test_vector_codec_matches_scalar_oracle(self):
        from tests.test_gf16_codec import encode_inputs
        bits=np.array(encode_inputs(),dtype=np.uint32)
        with np.errstate(invalid='ignore'):
            words=rt.gf16_words(bits.view(np.float32))
        expected=np.array([gf.encode(int(b)) for b in bits],dtype=np.uint16)
        np.testing.assert_array_equal(words,expected)
        values=np.array([gf.value(b) for b in range(65536)],dtype=np.float32)
        expected=np.array([0x7e01 if b&0x7e00==0x7e00 and b&511 else b for b in range(65536)],dtype=np.uint16)
        np.testing.assert_array_equal(rt.gf16_words(values),expected)

    def test_actquant_matches_upstream(self):
        from transformers.integrations.bitnet import ActQuant
        x=torch.tensor([[0,0,0,0],[127,2.5,-2.5,-127],[1e-7,-1e-7,2e-7,0]],dtype=torch.bfloat16)
        actual,codes,scale=rt.actquant(x.float(),'bf16')
        self.assertTrue(torch.equal(actual,ActQuant.apply(x).float()))
        self.assertEqual(codes[1].tolist(),[127,2,-2,-127])
        self.assertTrue(torch.isfinite(scale).all())

    def layer(self):
        from transformers.models.bitnet.modeling_bitnet import BitNetRMSNorm
        from transformers.integrations.bitnet import AutoBitLinear
        torch.manual_seed(107)
        norm=BitNetRMSNorm(8,eps=1e-5).to(torch.bfloat16)
        def linear():
            module=AutoBitLinear(8,8,bias=False,online_quant=False).to(torch.bfloat16)
            module.weight.copy_(torch.randint(-1,2,(8,8)).to(torch.bfloat16))
            module.weight_scale.fill_(0.5)
            return module
        return SimpleNamespace(post_attention_layernorm=norm,
            mlp=SimpleNamespace(gate_proj=linear(),up_proj=linear(),down_proj=linear(),ffn_sub_norm=norm))

    def test_explicit_boundaries_equal_upstream_small_ffn(self):
        layer=self.layer()
        x=torch.randn(1,5,8).to(torch.bfloat16)
        h=layer.post_attention_layernorm(x)
        g=layer.mlp.gate_proj(h);u=layer.mlp.up_proj(h)
        a=g.clamp(min=0).pow(2)*u
        s=layer.mlp.ffn_sub_norm(a);y=layer.mlp.down_proj(s)
        actual,_,_=rt.explicit_ffn(layer,x.float(),'bf16')
        for name,value in dict(h=h,g=g,u=u,a=a,s=s,y=y).items():
            self.assertTrue(torch.equal(actual[name],value.float()),name)

    def test_wide_product_control_preserves_finite_values(self):
        layer=self.layer()
        for module in [layer.mlp.gate_proj,layer.mlp.up_proj]:
            module.weight.fill_(1);module.weight_scale.fill_(2**16)
        x=torch.ones(1,1,8)
        with self.assertRaises(rt.RangeFailure) as failure:rt.explicit_ffn(layer,x,'gf16')
        self.assertEqual(failure.exception.boundary,'relu2')
        self.assertGreater(failure.exception.rounds['relu2']['overflow'],0)
        self.assertNotIn('y',failure.exception.stages)
        values,events,_=rt.explicit_ffn(layer,x,'gf16',wide_product=True)
        self.assertTrue(torch.isfinite(values['y']).all())
        self.assertEqual(events['a']['storage'],'f32')

    def test_invalid_metrics_and_profile_are_rejected(self):
        with self.assertRaises(ValueError):rt.metrics(torch.ones(1),torch.tensor([float('inf')]))
        with self.assertRaises(ValueError):rt.metrics(torch.ones(1),torch.ones(2))
        with self.assertRaises(ValueError):rt.round_storage(torch.ones(1),'invalid')


if __name__=='__main__':unittest.main()
