"""Independent byte coverage cannot be replaced by a passing receipt flag."""
import unittest
from tools import verify_gf16_attn_board as verify


class RawReadback(unittest.TestCase):
    def test_damaged_frame_requires_crc_valid_retry_and_full_coverage(self):
        data=bytes(range(256))*300;address=8192
        chunks=[verify.proto.readback_frame(i&255,address+i,data[i:i+2048])
                for i in range(0,len(data),2048)]
        damaged=bytearray(chunks[0]);damaged[-1]^=1
        with self.assertRaisesRegex(ValueError,'incomplete'):
            verify.reconstruct_payload(bytes(damaged)+b''.join(chunks[1:]),data,address)
        # Feed boundary crosses a frame; valid retries and duplicates are allowed.
        raw=bytes(damaged)+b''.join(chunks)+chunks[0]
        result=verify.reconstruct_payload(raw,data,address)
        self.assertEqual(result['bytes'],len(data))
        self.assertEqual(result['crc_valid_blocks'],len(chunks)+1)
        self.assertEqual(result['crc_rejected_frames'],[address])
        self.assertEqual(result['payload_sha256'],verify.sha(data))

    def test_wrong_payload_truncation_and_overflow_are_rejected(self):
        frame=verify.proto.readback_frame
        data=bytes(range(64));address=128
        for raw in (frame(1,address,bytes(64)),frame(1,address,data)[:-1],
                    frame(1,address,data+bytes(16)),frame(1,address+128,data),b''):
            with self.subTest(raw_length=len(raw)),self.assertRaises(ValueError):
                verify.reconstruct_payload(raw,data,address)
