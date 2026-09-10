"""Mixed Python-int/binary64 top-k ordering must not round integer magnitudes."""
import ctypes as C
import math
import os
from pathlib import Path
import random
import struct
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tests/reference'))
from trinity_memory_reference.sparsity import project_topk as reference
LIB=Path(os.environ.get('TRINITY_TOPK_LIBRARY',ROOT/'build/t27'/('libtrinity_memory_t27.dylib' if sys.platform=='darwin' else 'libtrinity_memory_t27.so')))
lib=C.CDLL(str(LIB))
class Weight(C.Structure):
    _fields_=[('data',C.POINTER(C.c_uint8)),('size',C.c_size_t),('real',C.c_double),('is_integer',C.c_bool)]
lib.tm_project_topk_exact.argtypes=[C.POINTER(Weight),C.c_uint64,C.c_uint64,C.c_uint64,C.POINTER(C.c_int64),C.c_uint64]
lib.tm_project_topk_exact.restype=C.c_int32
lib.tm_exact_magnitude_compare.argtypes=[C.POINTER(Weight),C.POINTER(Weight)]
lib.tm_exact_magnitude_compare.restype=C.c_int32

def descriptors(values):
    rows=(Weight*max(1,len(values)))();keep=[]
    for index,value in enumerate(values):
        if type(value) is int:
            data=abs(value).to_bytes((abs(value).bit_length()+7)//8,'big')
            array=(C.c_uint8*max(1,len(data))).from_buffer_copy(data or b'\0');keep.append(array)
            rows[index]=Weight(array,len(data),float(value),True)
        else:rows[index]=Weight(None,0,value,False)
    return rows,keep

def native(values,block,k):
    rows,keep=descriptors(values);output=(C.c_int64*max(1,len(values)))(*([77]*len(values)))
    status=lib.tm_project_topk_exact(rows,len(values),block,k,output,len(values))
    if status:raise ValueError(status)
    return list(output[:len(values)])

class ExactTopK(unittest.TestCase):
    def test_deliberate_boundaries(self):
        largest=(1<<1024)-(1<<970)-1
        cases=[[],[2**53,2**53+1],[2**64,2**64+1],[2**1023,2**1023+1],
               [2**53+1,float(2**53),math.nextafter(float(2**53),math.inf)],
               [largest,sys.float_info.max,-largest],
               [1,math.nextafter(1.0,0.0),math.nextafter(1.0,math.inf),-1.0],
               [0,-0.0,math.ulp(0.0),-math.ulp(0.0)],
               [-2**64,2**64,0,-float(2**64)]]
        for values in cases:
            for block in (1,2,3,8,19):
                for k in range(block+1):
                    self.assertEqual(native(values,block,k),reference(values,block,k),(values,block,k))

    def test_random_exact_order_and_selection(self):
        rng=random.Random(270927)
        fixed=[0,1,-1,2**53+1,-2**64-1,2**1023,sys.float_info.max,math.ulp(0.0),-0.0]
        for run in range(2000):
            values=[]
            for index in range(rng.randrange(1,21)):
                choice=rng.randrange(4)
                if choice==0:value=rng.choice(fixed)
                elif choice==1:
                    value=rng.getrandbits(rng.randrange(1,1024))
                    if rng.randrange(2):value=-value
                elif choice==2:
                    bits=rng.getrandbits(64)
                    while (bits>>52)&2047==2047:bits=rng.getrandbits(64)
                    value=struct.unpack('>d',bits.to_bytes(8,'big'))[0]
                else:
                    exponent=rng.randrange(0,1024)
                    value=(1<<exponent)+rng.choice((-1,0,1))
                    if rng.randrange(2):value=-value
                values.append(value)
            rows,keep=descriptors(values)
            for _ in range(3):
                left=rng.randrange(len(values));right=rng.randrange(len(values))
                expected=(abs(values[left])>abs(values[right]))-(abs(values[left])<abs(values[right]))
                actual=lib.tm_exact_magnitude_compare(C.byref(rows[left]),C.byref(rows[right]))
                self.assertEqual(actual,expected,(values[left],values[right]))
            block=rng.randrange(1,25);k=rng.randrange(block+1)
            self.assertEqual(native(values,block,k),reference(values,block,k),(run,values,block,k))

if __name__=='__main__':unittest.main()
