"""Thin Python object adapters for the native t27 loopback Bridge."""
from __future__ import annotations
import re
from dataclasses import dataclass
from . import _native as n

# Used with fullmatch: `$` alone would also accept a trailing newline.
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
IDCODE_HEX = re.compile(r"0x[0-9a-f]{8}")
DNA_HEX = re.compile(r"0x[0-9a-f]{16}")
BUILD_ID_HEX = re.compile(r"[0-9a-f]{8}")
PROTOCOL_MATVEC = 4          # t27/fpga_link.t27 TL_PROTO_MATVEC, spec TMS_DEVICE_PROTOCOL_MATVEC
STANDARD_BAUDS = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
EVIDENCE_FIELDS = ("bitstream_sha256", "capture_sha256", "idcode", "dna", "build_id")

class BridgeError(RuntimeError):
    def __init__(self,code,message):
        super().__init__(message); self.code=code

def _strict_json(data): return n.strict_json(data)

def _timeout(value):
    if type(value) not in (int,float): raise ValueError("timeout must be positive and finite")
    try: real=float(value)
    except OverflowError: raise ValueError("timeout must be positive and finite") from None
    if not n.call("tm_api_timeout",n.C.c_bool,[n.C.c_double],real):
        raise ValueError("timeout must be positive and finite")
    return real

def _size(value,name,minimum=0):
    try: return n.checked_size(value,name,minimum)
    except ValueError: raise BridgeError(-32602,f"invalid {name}") from None

def evidence_complete(evidence) -> bool:
    """A device evidence block with every field present and well formed (docs/bridge.md)."""
    if not isinstance(evidence, dict):
        return False
    checks = {"bitstream_sha256": SHA256_HEX, "capture_sha256": SHA256_HEX, "idcode": IDCODE_HEX,
              "dna": DNA_HEX, "build_id": BUILD_ID_HEX}
    return all(type(evidence.get(key)) is str and pattern.fullmatch(evidence[key]) for key, pattern in checks.items())


@dataclass(frozen=True)
class FpgaDevice:
    """The fpga backend's device: the serial port of the AX7203's UART and the evidence that
    identifies what is on it. idcode and dna come from JTAG (openFPGALoader --detect and
    --read-dna; idcode as 0x and 8 digits: openFPGALoader prints this board's IDCODE without its
    revision nibble, see docs/bridge.md); build_id is the BUILD_ID of the loaded bitstream, which
    the device reports and the Bridge checks; bitstream_sha256 is that bitstream's sha256 from its
    build record. baud must be a rate this host's termios can set (macOS: at most 230400) and
    the rate the loaded bitstream's UART runs at (the link has no baud-switch frame yet);
    min_protocol is at least 4, the matvec extension."""
    port: str
    bitstream_sha256: str
    idcode: str
    dna: str
    build_id: str
    baud: int = 115200
    reply_timeout: float = 0.5
    quiet: float = 0.15
    attempts: int = 8
    min_protocol: int = PROTOCOL_MATVEC
    region: int = 0

    def native_arguments(self):
        if type(self.port) is not str or not self.port or "\0" in self.port:
            raise ValueError("device port must be a path")
        if not SHA256_HEX.fullmatch(self.bitstream_sha256 if type(self.bitstream_sha256) is str else ""):
            raise ValueError("bitstream_sha256 must be 64 lowercase hex digits")
        if not IDCODE_HEX.fullmatch(self.idcode if type(self.idcode) is str else ""):
            raise ValueError("idcode must be 0x and 8 lowercase hex digits")
        if not DNA_HEX.fullmatch(self.dna if type(self.dna) is str else ""):
            raise ValueError("dna must be 0x and 16 lowercase hex digits")
        if not BUILD_ID_HEX.fullmatch(self.build_id if type(self.build_id) is str else ""):
            raise ValueError("build_id must be 8 lowercase hex digits")
        for name in ("baud", "attempts", "min_protocol", "region"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.baud not in STANDARD_BAUDS:
            raise ValueError("baud must be a standard rate up to 921600")
        if not n.call("tm_os_serial_rate_ok", n.C.c_int32, [n.C.c_uint32], self.baud):
            raise ValueError(f"baud {self.baud} cannot be set on this host (its termios has no such rate)")
        if not PROTOCOL_MATVEC <= self.min_protocol < 1 << 32:
            raise ValueError(f"min_protocol must be {PROTOCOL_MATVEC} (the matvec extension) or more, below 2**32")
        if not 1 <= self.attempts <= 64 or self.region % 16 or self.region >= 1 << 32:
            raise ValueError("attempts must be 1..64 and region a 16-byte aligned 32-bit address")
        millis = []
        for name in ("reply_timeout", "quiet"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not 0 < value <= 60:
                raise ValueError(f"{name} must be positive seconds up to 60")
            millis.append(int(round(value * 1000)))
        path = self.port.encode("utf-8")
        return (n.octets(path), len(path), self.baud, millis[0], millis[1], self.attempts, self.min_protocol,
                self.region, n.octets(bytes.fromhex(self.bitstream_sha256)), int(self.idcode, 16),
                int(self.dna, 16), int(self.build_id, 16))


class BridgeServer:
    """Single native worker with caller-configured limits and process memory.

    backend="emulator" (the default) computes everything in process. backend="fpga" with an
    FpgaDevice sends compute.dot to the device over its UART (t27/fpga_link.t27) and checks the
    device on chip_info; containers are still validated and held in process memory."""
    def __init__(self,host="127.0.0.1",port=0,*,max_request_bytes=2*1024*1024,
                 max_object_bytes=512*1024,max_storage_bytes=8*1024*1024,
                 max_objects=16,max_trits=1_000_000,timeout=5.0,backend="emulator",device=None):
        text=host.encode() if type(host) is str else b""
        if not n.call("tm_api_server_host",n.C.c_bool,[n.U8,n.SZ],n.octets(text),len(text)):
            raise ValueError("Bridge supports only 127.0.0.1 or localhost")
        _size(port,"port")
        if port>65535: raise BridgeError(-32602,"invalid port")
        self.limits=dict(max_request_bytes=max_request_bytes,max_object_bytes=max_object_bytes,
                        max_storage_bytes=max_storage_bytes,max_objects=max_objects,max_trits=max_trits)
        for key,value in self.limits.items(): _size(value,key,1)
        self.host,self.port,self.timeout="127.0.0.1",port,_timeout(timeout)
        if backend=="emulator":
            if device is not None: raise ValueError("the emulator backend takes no device")
        elif backend=="fpga":
            if not isinstance(device,FpgaDevice): raise ValueError("the fpga backend needs an FpgaDevice")
            device.native_arguments()
        else: raise ValueError("backend must be 'emulator' or 'fpga'")
        self.backend,self.device=backend,device
        self._runtime=None
    @property
    def url(self):
        if self._runtime is None: raise RuntimeError("BridgeServer is not running")
        port=n.call("tm_runtime_port",n.C.c_int32,[n.C.c_void_p],self._runtime)
        return f"http://127.0.0.1:{port}/"
    @property
    def stored_bytes(self):
        return n.call("tm_runtime_stored_bytes",n.SZ,[n.C.c_void_p],self._runtime) if self._runtime else 0
    @property
    def object_count(self):
        return n.call("tm_runtime_object_count",n.SZ,[n.C.c_void_p],self._runtime) if self._runtime else 0
    def last_capture(self)->bytes:
        """Every byte the device sent during the last fpga call (the capture whose sha256 and count
        that call's evidence holds), for the record of a board run; b"" for the emulator."""
        if self._runtime is None or self.backend!="fpga": return b""
        size=n.call("tm_runtime_fpga_capture",n.SZ,[n.C.c_void_p,n.U8,n.SZ],self._runtime,None,0)
        buffer=(n.C.c_uint8*max(1,size))()
        got=n.call("tm_runtime_fpga_capture",n.SZ,[n.C.c_void_p,n.U8,n.SZ],self._runtime,buffer,size)
        if got!=size: raise RuntimeError("the capture changed while it was read (a request was running)")
        return bytes(buffer[:size])
    def __enter__(self): return self.start()
    def __exit__(self,*_): self.close()
    def start(self):
        if self._runtime is not None: raise RuntimeError("BridgeServer is already running")
        runtime=n.call("tm_runtime_new",n.C.c_void_p,[n.C.c_int32,n.SZ,n.SZ,n.SZ,n.SZ,n.SZ,n.C.c_double],
                       self.port,*self.limits.values(),self.timeout)
        if not runtime: raise ValueError("Native Bridge configuration exceeds allocation bounds")
        if self.backend=="fpga":
            if n.call("tm_runtime_fpga",n.C.c_int32,[n.C.c_void_p,n.U8,n.SZ,n.C.c_uint32,n.C.c_uint32,n.C.c_uint32,
                      n.C.c_uint32,n.C.c_uint32,n.C.c_uint32,n.U8,n.C.c_uint32,n.C.c_uint64,n.C.c_uint32],
                      runtime,*self.device.native_arguments()):
                n.call("tm_runtime_close",None,[n.C.c_void_p],runtime)
                raise ValueError("Native Bridge cannot configure the fpga backend")
        if n.call("tm_runtime_start",n.C.c_int32,[n.C.c_void_p],runtime):
            n.call("tm_runtime_close",None,[n.C.c_void_p],runtime)
            raise OSError("Cannot bind or start native loopback Bridge")
        self._runtime=runtime
        return self
    def close(self):
        if self._runtime is not None:
            n.call("tm_runtime_close",None,[n.C.c_void_p],self._runtime)
            self._runtime=None

class BridgeClient:
    """Synchronous native HTTP client, using no proxies or redirects."""
    def __init__(self,url,*,timeout=5.0,max_response_bytes=4*1024*1024):
        if type(url) is not str: raise ValueError("URL must be a plain loopback HTTP endpoint")
        try: text=url.encode("utf-8")
        except UnicodeError: raise ValueError("Invalid RPC URL") from None
        port=n.call("tm_http_url",n.C.c_int32,[n.U8,n.SZ],n.octets(text),len(text))
        if port<1: raise ValueError("URL must be a plain loopback HTTP endpoint with explicit port")
        self.url=f"http://127.0.0.1:{port}/"; self._port=port
        self.timeout=_timeout(timeout)
        self.max_response_bytes=_size(max_response_bytes,"max_response_bytes",1)
    def call(self,method,params=None):
        try:
            if type(method) is not str: raise ValueError()
            method_bytes=method.encode("utf-8"); params_bytes=n.json_bytes({} if params is None else params)
        except (ValueError,TypeError,OverflowError,UnicodeError,RecursionError):
            raise BridgeError(-32602,"request is not valid JSON") from None
        request_capacity=len(params_bytes)+len(method_bytes)*6+256
        work=n.rpc_workspace(len(params_bytes)+1)
        request=n.buffer(request_capacity); response=n.buffer(self.max_response_bytes); identifier=n.buffer(32)
        try:
            status=n.call("tm_rpc_random_id",n.C.c_int32,[n.U8],identifier)
            if status: raise BridgeError(status,"Cannot obtain native RPC request identifier")
            size=n.call("tm_rpc_prepare",n.C.c_int64,[n.U8,n.SZ,n.U8,n.SZ,n.U8,n.U8,n.SZ,n.C.POINTER(n.RPCWorkspace)],
                        n.octets(method_bytes),len(method_bytes),n.octets(params_bytes),len(params_bytes),identifier,request,request_capacity,work)
            if size<0: raise BridgeError(size,"Invalid or oversized JSON-RPC request")
            size=n.call("tm_runtime_http",n.C.c_int64,[n.C.c_int32,n.C.c_double,n.U8,n.SZ,n.U8,n.SZ],
                        self._port,self.timeout,request,size,response,self.max_response_bytes)
            if size<0: raise BridgeError(size,"Native RPC transport failed or response limit exceeded")
            n.rpc_free(work); work=None
            work=n.rpc_workspace(size+1)
            reply=n.RPCReply()
            status=n.call("tm_rpc_validate",n.C.c_int32,[n.U8,n.SZ,n.U8,n.C.POINTER(n.RPCWorkspace),n.C.POINTER(n.RPCReply)],
                          response,size,identifier,work,n.C.byref(reply))
            if status<0: raise BridgeError(status,"Invalid JSON-RPC response")
            if status==1:
                error=n.tree_value(work,reply.error_index)
                raise BridgeError(error["code"],error["message"])
            return n.tree_value(work,reply.result_index)
        finally:
            if work is not None: n.rpc_free(work)
    def capabilities(self): return self.call("trinity.capabilities")
    def upload(self,data):
        if type(data) is not bytes: raise TypeError("upload requires bytes")
        return self.call("memory.upload",{"data":n.base64_encode(data)})["handle"]
    def read(self,handle,offset=0,length=None):
        params={"handle":handle,"offset":offset}
        if length is not None: params["length"]=length
        return n.base64_decode(self.call("memory.read",params)["data"])
    def info(self,handle): return self.call("memory.info",{"handle":handle})
    def dot(self,handle,tensor_name,activations):
        return self.call("compute.dot",{"handle":handle,"tensor_name":tensor_name,"activations":activations})
    def delete(self,handle): return self.call("memory.delete",{"handle":handle})

def check_identity(info):
    """What SDKMemoryBackend accepts: the emulator with hardware false, or fpga with hardware true
    and a complete, well-formed evidence block. Raises BridgeError(-32000) otherwise."""
    if not isinstance(info,dict): raise BridgeError(-32000,"expected an identity object")
    backend,hardware=info.get("backend"),info.get("hardware")
    if backend=="emulator" and hardware is False: return info
    if backend=="fpga" and hardware is True:
        if evidence_complete(info.get("evidence")): return info
        raise BridgeError(-32000,"fpga identity without complete, well-formed device evidence")
    raise BridgeError(-32000,"expected an explicit memory emulator identity or an fpga identity with evidence")

class SDKMemoryBackend:
    """Adapter for the separate Trinity SDK. The 16-byte IDs are the public synthetic constants in
    both backends; an fpga identity is accepted only with its complete device evidence."""
    def __init__(self,client): self.client=client
    def get_chip_info(self):
        from trinity.types import ChipInfo
        info=check_identity(self.client.call("trinity_chipInfo"))
        return ChipInfo(phi_id=bytes.fromhex(info["phi_id"]),euler_id=bytes.fromhex(info["euler_id"]),
                        gamma_id=bytes.fromhex(info["gamma_id"]),anchor=info["anchor"])
    def prove_inference(self,**_):
        raise NotImplementedError("The memory Bridge (emulator or fpga backend) does not run model inference or produce ZK proofs")
    def submit_to_bittensor(self,**_):
        raise NotImplementedError("The memory Bridge (emulator or fpga backend) does not submit to Bittensor")
