"""Trinity ternary memory: native t27 software and RTL demonstrator."""
from .codecs import CODECS,CodecError,pack,unpack
from .container import decode_file,encode_file,inspect_file
__all__=["CODECS","CodecError","pack","unpack","encode_file","decode_file","inspect_file"]
__version__="0.3.0"
