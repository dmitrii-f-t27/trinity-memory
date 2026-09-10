"""Trinity ternary memory: reference software, not an inference engine."""

from .codecs import CODECS, CodecError, pack, unpack
from .container import decode_file, encode_file, inspect_file

__all__ = ["CODECS", "CodecError", "pack", "unpack", "encode_file", "decode_file", "inspect_file"]
__version__ = "0.2.0"
