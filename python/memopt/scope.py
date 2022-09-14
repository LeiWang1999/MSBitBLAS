import tvm
import threading
from typing import Dict, List

class Scope(Dict):
    _thread_local = threading.local()
    def __init__(self, schedule):
        self.schedule = schedule
        # --------------------------------provided args before compile -----------------------------------------
        # shared input argument
        self.shared_mem_inputs: List[tvm.te.Tensor] = []
        # shared output argument INDEX e.g. [3]
        self.shared_mem_outputs: List[int] = []
        # subset of shared mem outputs
        self.reuse_disabled_inputs: List[tvm.te.Tensor] = []
        # strides info e.g. {"output0" : [72, 1]}
        self.strides = {}
        # --------------------------------return after compile -----------------------------------------
        # indicates extra workspace allocated for the kernel
        self.total_internal_shared_memory = 0
        # indicates output tensor allocation, format {x : bytes for x in self.shared_mem_outputs}
        self.exteral_shared_memroy_size: Dict[int, int] = {}

        self.bounds = tvm.te.schedule.InferBound(self.schedule.normalize())
        self._build_analyzer()
        self._get_grid_block_size()

    def _build_analyzer(self):
        self.analyzer = tvm.arith.Analyzer()
        for iterator, region in self.bounds.items():
            if isinstance(region.min, tvm.tir.expr.IntImm) and isinstance(region.extent, tvm.tir.expr.IntImm):
                if iterator.var.name.startswith("blockIdx"):
                    bound = tvm.arith.ConstIntBound(0, 0)
                else:
                    bound = tvm.arith.ConstIntBound(int(region.min), int(region.min) + int(region.extent) - 1)
                self.analyzer.update(iterator.var, bound)

    def _get_grid_block_size(self):
        grid_block_size = {
            "threadIdx.x" : 1, "threadIdx.y" : 1, "threadIdx.z" : 1,
            "blockIdx.x" : 1, "blockIdx.y" : 1, "blockIdx.z" : 1,
        }
        for iter_var, region in self.bounds.items():
            name = iter_var.var.name
            if name in grid_block_size:
                grid_block_size[name] = max(int(region.extent), grid_block_size[name])
        self.block_size = [grid_block_size[x] for x in ["threadIdx.x", "threadIdx.y", "threadIdx.z"]]
        self.grid_size = [grid_block_size[x] for x in ["blockIdx.x", "blockIdx.y", "blockIdx.z"]]

    def __enter__(self):
        assert not hasattr(Scope._thread_local, "scope"), "Scope should be entered only once"
        Scope._thread_local.scope = self
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        del Scope._thread_local.scope

def get_scope() -> Scope:
    if not hasattr(Scope._thread_local, "scope"):
        return None
    return Scope._thread_local.scope

@tvm._ffi.register_func("memopt.is_independent_alloc")
def is_independent_alloc(tensor_name):
    if tensor_name.endswith(".wmma.accumulator.shared") and len(get_scope().exteral_shared_memroy_size) > 0:
        return True
    if get_scope() is None:
        return False
    return tensor_name in  [x.name + ".shared" for x in get_scope().shared_mem_inputs]

@tvm._ffi.register_func("memopt.is_reuse_disabled")
def is_reuse_disabled(tensor_name):
    if get_scope() is None:
        return False
    return tensor_name in [x.name + ".shared" for x in get_scope().reuse_disabled_inputs]
