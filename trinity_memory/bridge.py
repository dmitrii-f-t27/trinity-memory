"""Thin Python object adapters for the native t27 loopback Bridge."""
from __future__ import annotations
from . import _native as n

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

class BridgeServer:
    """Single native worker with caller-configured limits and process memory."""
    def __init__(self,host="127.0.0.1",port=0,*,max_request_bytes=2*1024*1024,
                 max_object_bytes=512*1024,max_storage_bytes=8*1024*1024,
                 max_objects=16,max_trits=1_000_000,timeout=5.0):
        text=host.encode() if type(host) is str else b""
        if not n.call("tm_api_server_host",n.C.c_bool,[n.U8,n.SZ],n.octets(text),len(text)):
            raise ValueError("Bridge supports only 127.0.0.1 or localhost")
        _size(port,"port")
        if port>65535: raise BridgeError(-32602,"invalid port")
        self.limits=dict(max_request_bytes=max_request_bytes,max_object_bytes=max_object_bytes,
                        max_storage_bytes=max_storage_bytes,max_objects=max_objects,max_trits=max_trits)
        for key,value in self.limits.items(): _size(value,key,1)
        self.host,self.port,self.timeout="127.0.0.1",port,_timeout(timeout)
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
    def __enter__(self): return self.start()
    def __exit__(self,*_): self.close()
    def start(self):
        if self._runtime is not None: raise RuntimeError("BridgeServer is already running")
        runtime=n.call("tm_runtime_new",n.C.c_void_p,[n.C.c_int32,n.SZ,n.SZ,n.SZ,n.SZ,n.SZ,n.C.c_double],
                       self.port,*self.limits.values(),self.timeout)
        if not runtime: raise ValueError("Native Bridge configuration exceeds allocation bounds")
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

class SDKMemoryBackend:
    """Adapter for the separate Trinity SDK; identity remains explicitly emulated."""
    def __init__(self,client): self.client=client
    def get_chip_info(self):
        from trinity.types import ChipInfo
        info=self.client.call("trinity_chipInfo")
        if info.get("backend")!="emulator" or info.get("hardware") is not False:
            raise BridgeError(-32000,"expected an explicit memory emulator identity")
        return ChipInfo(phi_id=bytes.fromhex(info["phi_id"]),euler_id=bytes.fromhex(info["euler_id"]),
                        gamma_id=bytes.fromhex(info["gamma_id"]),anchor=info["anchor"])
    def prove_inference(self,**_):
        raise NotImplementedError("Memory emulator does not run model inference or produce ZK proofs")
    def submit_to_bittensor(self,**_):
        raise NotImplementedError("Memory emulator does not submit to Bittensor")
