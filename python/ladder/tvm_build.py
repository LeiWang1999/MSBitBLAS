# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
import numpy as np
import regex as re
from typing import Union
import tvm

from .schedule import SchedulerBase
from .rasterization import Rasterization

_type_map = {
    "float32": "float",
    "float16": "half",
    "bfloat16": "__nv_bfloat162",
    "float64": "double",
    "int64": "int64_t",
    "int32": "int",
    "bool": "int8_t",
    "int8": "int8_t",
    "uint8": "uint8_t",
    "int16": "int16_t",
    "uchar": "uint8_t",
}
_type_bytes = {
    "float": 4,
    "double": 8,
    "half": 2,
    "int16": 2,
    "bfloat16": 2,
    "int": 4,
    "int64_t": 8,
    "bool": 1,
    "int8_t": 1,
    "uint8_t": 1,
    "signed char": 1,
    "uchar": 1,
}


def get_valid_name(var):
    if var.name.find(".") >= 0:
        name = var.name[: var.name.index(".")]
    else:
        name = var.name
    return name if var.value_index == 0 else name + "_" + str(var.value_index)


def get_block_flatten_code(block_size):
    if block_size[1] == 1 and block_size[2] == 1:
        return ""
    elif block_size[2] == 1:
        return "  int __flatten_tid = threadIdx.x;\n  const dim3 threadIdx(__flatten_tid % {}, __flatten_tid / {}, 0);\n".format(
            block_size[0], block_size[0]
        )
    else:  # not possible in our schedule
        raise NotImplementedError()


_c_op_map = {
    tvm.tir.FloorMod: "%",
    tvm.tir.FloorDiv: "/",
    tvm.tir.Add: "+",
    tvm.tir.Sub: "-",
    tvm.tir.Mul: "*",
}


def _lower_C_simple(expr: tvm.tir.PrimExpr) -> str:
    if isinstance(expr, tvm.tir.expr.BinaryOpExpr):
        left = _lower_C_simple(expr.a)
        right = _lower_C_simple(expr.b)
        if type(expr) in _c_op_map:
            return "({} {} {})".format(left, _c_op_map[type(expr)], right)
        else:
            raise NotImplementedError(expr)
    elif isinstance(expr, tvm.tir.expr.Var):
        assert expr.name == "block_idx"
        return "__bid"
    elif isinstance(expr, tvm.tir.expr.ConstExpr):
        return str(expr.value)
    elif isinstance(expr, tvm.tir.Cast):
        return f"({_type_map[expr.dtype]}({_lower_C_simple(expr.value)}))"
    else:
        raise NotImplementedError(expr)


def get_block_reorder_code(
    block_reoder_expr: Union[tvm.tir.PrimExpr, Rasterization]
) -> str:
    if isinstance(block_reoder_expr, Rasterization):
        return "  " + "\n  ".join(block_reoder_expr.get_code()) + "\n"
    return "  int __bid = blockIdx.x;\n  const dim3 blockIdx({}, 0, 0);\n".format(
        _lower_C_simple(block_reoder_expr)
    )


def match_global_kernel(source: str) -> int:
    pattern = r"__global__\s+void\s+[__launch_bounds__\(\d+\)\s+]\w+"
    matched = re.findall(pattern, source)
    assert len(matched) == 1
    return source.index(matched[0])


def tensor_remove_make_int4(source: str) -> str:
    source = source.replace(
        "make_int4((signed char)0, (signed char)0, (signed char)0, (signed char)0, (signed char)0, (signed char)0, (signed char)0, (signed char)0, (signed char)0, (signed char)0, (signed char)0, (signed char)0, (signed char)0, (signed char)0, (signed char)0, (signed char)0)",
        "make_int4(0, 0, 0, 0)",
    )
    return source


def tensor_replace_dp4a(source: str) -> str:
    # as under some senario, like block reduction, the dp4a tensorize will fail. but we still need it.
    import re

    pattern = r"""for\s*\(int\s*(?P<k_var>\w+)\s*=\s*0;\s*\1\s*<\s*4;\s*\+\+\1\)\s*\{\s*(?P<c_var>\w+)\[0\]\s*=\s*\(\2\[0\]\s*\+\s*\(\(\(int\)(?P<a_var>\w+)\[\(\((?P<idx_a_var>\w+)\s*\*\s*4\)\s*\+\s*\1\)\]\)\s*\*\s*\(\(int\)(?P<b_var>\w+)\[\(\((?P<idx_b_var>\w+)\s*\*\s*4\)\s*\+\s*\1\)\]\)\)\);\s*\}"""
    replacement = (
        r"""\2[0] = __dp4a(*(int *)&\3[((\4 * 4))],*(int *)&\5[((\6 * 4))], \2[0]);"""
    )
    source = re.sub(pattern, replacement, source)
    return source


def tensor_replace_hfma2(source: str) -> str:
    # as under some senario, like block reduction, the dp4a tensorize will fail. but we still need it.
    import re

    """
    for (int k_2_1 = 0; k_2_1 < 2; ++k_2_1) {
        in_thread_C_local[0] = (in_thread_C_local[0] + (A_local[((k_2_0 * 2) + k_2_1)] * B_decode_local[((k_2_0 * 2) + k_2_1)]));
      }
    """
    pattern = r"""for\s*\(int\s*(?P<k_var>\w+)\s*=\s*0;\s*\1\s*<\s*2;\s*\+\+\1\)\s*\{\s*(?P<c_var>\w+)\[0\]\s*=\s*\(\2\[0\]\s*\+\s*\((?P<a_var>\w+)\[\(\((?P<idx_a_var>\w+)\s*\*\s*2\)\s*\+\s*\1\)\]\s*\*\s*(?P<b_var>\w+)\[\(\((?P<idx_b_var>\w+)\s*\*\s*2\)\s*\+\s*\1\)\]\)\);\s*\}"""
    replacement = r"""\2[0] = __hfma2(*(__half2 *)&\3[((\4 * 2))],*(__half2 *)&\5[((\6 * 2))], \2[0]);"""
    source = re.sub(pattern, replacement, source)
    return source


def unset_tvm_cuda_compile():
    tvm.register_func("tvm_callback_cuda_compile", lambda *x: "", override=True)


def reset_tvm_cuda_compile():
    tvm.register_func(
        "tvm_callback_cuda_compile",
        tvm.contrib.nvcc.tvm_callback_cuda_compile,
        override=True,
    )


def tvm_build(
    sch: SchedulerBase,
    target: tvm.target.Target,
    name: str = "default_kernel",
    global_kernel=True,
    flatten_block=True,
    reuse_disabled_inputs=[],
) -> str:
    func_args = ", ".join(
        [
            "{}* __restrict__ {}".format(_type_map[var.dtype], get_valid_name(var))
            for var in sch.args
        ]
    )

    def is_independent_alloc(tensor_name):
        if (
            tensor_name.endswith(".wmma.accumulator.shared")
            and len(sch.shared_outputs) > 0
        ):
            return True
        return tensor_name in [x.name + ".shared" for x in sch.shared_inputs]

    def is_reuse_disabled(tensor_name):
        return tensor_name in [x.name + ".shared" for x in reuse_disabled_inputs]

    tvm._ffi.register_func(
        "memopt.is_independent_alloc", is_independent_alloc, override=True
    )
    tvm._ffi.register_func("memopt.is_reuse_disabled", is_reuse_disabled, override=True)

    unset_tvm_cuda_compile()
    src = sch.build(target)
    reset_tvm_cuda_compile()
    tvm._ffi.register_func(
        "memopt.is_independent_alloc", lambda x: False, override=True
    )
    tvm._ffi.register_func("memopt.is_reuse_disabled", lambda x: False, override=True)

    exteral_shared_memroy_size = {}
    total_internal_shared_memory = 0
    for idx in sch.shared_outputs:
        tile_shape = sch.config.block
        dtype_bytes = (tvm.DataType(sch.args[idx].dtype).bits + 7) // 8
        if idx in sch.config.output_strides:
            strides = sch.config.output_strides[idx].compute_strides_from_shape(
                tile_shape
            )
            exteral_shared_memroy_size[idx] = tile_shape[0] * strides[0] * dtype_bytes
        else:
            exteral_shared_memroy_size[idx] = int(np.prod(tile_shape)) * dtype_bytes

    index = match_global_kernel(src)
    index = src.index("{", index)
    if flatten_block:
        flat_block_code = get_block_flatten_code(sch.block_size)
        sch.block_size = [int(np.prod(sch.block_size)), 1, 1]
        src = src[: index + 2] + flat_block_code + src[index + 2 :]
    if sch.config.block_order is not None:
        block_reorder_code = get_block_reorder_code(sch.config.block_order)
        src = src[: index + 2] + block_reorder_code + src[index + 2 :]
    if global_kernel:
        prefix = "__global__ void __launch_bounds__(%d) " % np.prod(sch.block_size)
    else:
        prefix = "__device__ void "
        func_args += ", char* shared"
    src = prefix + name + "({}) ".format(func_args) + src[index:]
    # removing shared memory allocation
    # check wmma accumulator shared
    if len(sch.shared_outputs) > 0:
        reuse_output_name = get_valid_name(sch.args[sch.shared_outputs[0]])
        src = re.sub(
            r"__shared__ (\w+) (\w+wmma_accumulator_shared)\[\d+\];",
            r"\1* \2 = {};".format(reuse_output_name),
            src,
            1,
        )
    for tensor in sch.shared_inputs:
        shared_var_name = tensor.name + "_shared"
        matched = re.findall(
            r"__shared__ ((?:signed |unsigned )?\w+) {}\[(\d+)\];".format(
                shared_var_name
            ),
            src,
        )
        assert (
            len(matched) <= 1
        ), f"shared memory allocation not found, use schedule {sch}, {sch.shared_inputs}, {matched}, {src}"
        if len(matched):
            dtype, size = matched[0]
            exteral_shared_memroy_size[tensor] = int(size) * _type_bytes[dtype]
            src = re.sub(
                r"__shared__ ((?:signed |unsigned )?\w+) {}\[\d+\];".format(
                    shared_var_name
                ),
                r"\1* {} = (\1*){};".format(shared_var_name, tensor.name),
                src,
                1,
            )
    if not global_kernel:
        pattern = r"__shared__ ((?:signed |unsigned )?\w+) (\w+)\[(\d+)\];"
        offset = 0
        for dtype, var, size in re.findall(pattern, src):
            if var.startswith("red_buf"):
                continue
            src = re.sub(
                r"__shared__ ((?:signed |unsigned )?\w+) {}\[\d+\];".format(var),
                r"\1* {} = (\1*)(shared+{});".format(var, offset),
                src,
                1,
            )
            buffer_len = int(size) * _type_bytes[dtype]
            buffer_len = (buffer_len + 31) // 32 * 32
            offset += buffer_len
        total_internal_shared_memory = offset
    if global_kernel:
        pattern = r"__shared__ ((?:signed |unsigned )?\w+) (\w+)\[(\d+)\];"
        for dtype, var, size in re.findall(pattern, src):
            buffer_len = int(size) * _type_bytes[dtype]
            buffer_len = (buffer_len + 31) // 32 * 32
            src = re.sub(
                r"__shared__ ((?:signed |unsigned )?\w+) {}\[\d+\];".format(var),
                r"__shared__ \1 {}[{}];".format(var, buffer_len // _type_bytes[dtype]),
                src,
                1,
            )
    if target == "hip":
        pass
        # src = tensor_replace_vdot4(src)
    else:
        src = tensor_replace_dp4a(src)
    src = tensor_remove_make_int4(src)
    return src, exteral_shared_memroy_size, total_internal_shared_memory
