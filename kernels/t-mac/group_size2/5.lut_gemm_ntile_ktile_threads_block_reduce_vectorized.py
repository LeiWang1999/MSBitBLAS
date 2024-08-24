# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ruff: noqa
import torch

torch.random.manual_seed(0)
M = 1
N = 256
K = 1024
GROUP = 2
Bits = 1
input_fp16 = torch.rand(M, K, dtype=torch.float16, device="cuda")
# -1 or 1
weight_int1 = torch.randint(0, 2, (N, K), dtype=torch.int8, device="cuda")
weight_int2 = torch.zeros((N, K // GROUP), dtype=torch.int8, device="cuda")

for n in range(N):
    for k in range(K // GROUP):
        weight_chunk = weight_int1[n, k * GROUP:(k + 1) * GROUP]
        weight_2bit = 0
        for i in range(GROUP):
            weight_2bit |= (weight_chunk[i] << (GROUP - 1 - i))
        weight_int2[n, k] = weight_2bit

weight_int2_packed = torch.zeros((N, (K // GROUP) // 4), dtype=torch.int8, device="cuda")

for n in range(N):
    for k in range((K // GROUP) // 4):
        weight_8bit = 0
        for i in range(4):
            weight_8bit |= (weight_int2[n, k * 4 + i] << (2 * i))
        weight_int2_packed[n, k] = weight_8bit

ref_output = torch.matmul(input_fp16, weight_int1.T.to(torch.float16))

print(ref_output)

# create precompute table
table_fp16 = torch.zeros((M, K // GROUP, 2**GROUP), dtype=torch.float16, device="cuda")
for k in range(K // GROUP):
    table_fp16[:, k, 0] = 0
    table_fp16[:, k, 1] = input_fp16[:, k * GROUP + 1]
    table_fp16[:, k, 2] = input_fp16[:, k * GROUP + 0]
    table_fp16[:, k, 3] = input_fp16[:, k * GROUP + 0] + input_fp16[:, k * GROUP + 1]

from bitblas import tvm as tvm
from tvm import tl
import tvm.tl.language as T

TABLE_shape = (M, K // GROUP, 2**GROUP)
dtype_table = "float16"
# B_shape = (N, (K // GROUP) // 2)
B_shape = (N, (K // GROUP))
dtype_b = "int8"
threads = 64

N_Tile = 32
chunk = 4
reduce_k = 2

query_vectorize_size = 4  # as we should fetch int8
K_Tile = query_vectorize_size * chunk

thread_num_y = reduce_k
thread_num_x = threads // thread_num_y


@T.prim_func
def main_nTile_kTile_threads_reducek(TABLE: T.Buffer(TABLE_shape, dtype_table),
                                     B: T.Buffer(B_shape, dtype_b), C: T.Buffer((M, N),
                                                                                dtype_table)):
    accum_res = T.alloc_fragment((N_Tile // thread_num_x,), dtype_table, "local")
    reduced_accum_res = T.alloc_fragment(0, dtype_table, "local")
    query = T.alloc_fragment((query_vectorize_size,), "int8", "local")
    with T.Kernel(M, T.ceildiv(N, N_Tile), threads=threads) as (bx, by):
        for n in T.serial(N_Tile // thread_num_x):
            accum_res[n] = T.float16(0)
        for kr in T.thread_binding(0, reduce_k, thread="threadIdx.y"):
            for ko in T.serial((((K // GROUP) // reduce_k) // K_Tile)):
                for n in T.serial(N_Tile // thread_num_x):
                    for tx in T.thread_binding(0, thread_num_x, thread="threadIdx.x"):
                        for ki in T.serial(K_Tile // query_vectorize_size):
                            for v in T.vectorized(query_vectorize_size):
                                query[v] = B[by * N_Tile + (n * thread_num_x + tx),
                                             (ko * reduce_k + kr) * K_Tile +
                                             ki * query_vectorize_size + v]
                            for v in T.serial(query_vectorize_size):
                                accum_res[n] += TABLE[bx, (ko * reduce_k + kr) * K_Tile +
                                                      ki * query_vectorize_size + v, query[v]]

            for n in T.serial(N_Tile // thread_num_x):
                T.attr(
                    T.comm_reducer(lambda x, y: x + y, [T.float16(0)]),
                    "reduce_scope",
                    T.reinterpret(T.uint64(0), dtype="handle"),
                )
                T.evaluate(
                    T.tvm_thread_allreduce(
                        T.uint32(1),
                        accum_res[n],
                        True,
                        reduced_accum_res[0],
                        kr,
                        dtype="handle",
                    ))
            for n in T.serial(N_Tile // thread_num_x):
                for t in T.thread_binding(0, thread_num_x, thread="threadIdx.x"):
                    C[bx, by * N_Tile + (n * thread_num_x + t)] = reduced_accum_res[0]


@tvm.register_func(func_name="tvm_callback_cuda_postproc", override=True)
def tvm_callback_cuda_postproc(code, _):
    print(code)
    return code


mod, params = tl.lower(main_nTile_kTile_threads_reducek)
mod = tl.Profiler(mod, params, [2], tl.TensorSupplyType.Integer)

with open("debug/kernel.cu", "w") as f:
    f.write(mod.mod.imported_modules[0].get_source())

# out = mod.run_once()
cuda_table = table_fp16.cuda()
cuda_weight = weight_int2.cuda()

cuda_output = mod.func(cuda_table, cuda_weight)
print("cuda_output:", cuda_output)

# assert close

torch.testing.assert_close(ref_output, cuda_output, rtol=1e-2, atol=1e-2)
