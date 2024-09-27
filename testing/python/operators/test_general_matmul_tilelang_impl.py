# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

from bitblas import tvm as tvm
import bitblas.testing
from tvm import tl
from bitblas.ops.general_matmul.tilelang.dense import matmul_blocked
import torch
import torch.backends
torch.manual_seed(0)


def assert_tl_matmul_correctness(M, N, K,
    block_M=64,
    block_N=64,
    block_K=32,
    trans_A=False,
    trans_B=True,
    dtypeAB="float16",
    dtypeC="float16",
    accum_dtype="float16",
    num_stages=2,
    threads=128,
    enable_rasterization=False):
    matmul = matmul_blocked(M, N, K,
                block_M=block_M,
                block_N=block_N,
                block_K=block_K,
                trans_A=trans_A,
                trans_B=trans_B,
                dtypeAB=dtypeAB,
                dtypeC=dtypeC,
                accum_dtype=accum_dtype,
                num_stages=num_stages,
                threads=threads,
                enable_rasterization=enable_rasterization,
    )

    mod, params = tl.lower(matmul)
    src_code = mod.imported_modules[0].get_source()

    # src_code is the generated cuda source
    assert src_code is not None

    A = torch.rand(M, K, device="cuda", dtype=getattr(torch, dtypeAB))
    B = torch.rand(N, K, device="cuda", dtype=getattr(torch, dtypeAB))
    C = torch.zeros(M, N, device="cuda", dtype=getattr(torch, accum_dtype))

    mod = tl.Profiler(mod, params, [], tl.TensorSupplyType.Integer)

    mod(A, B, C)

    latency = mod.do_bench(mod.func, warmup=25)

    # Ensure that the latency is not None
    assert latency is not None

    # Get Reference Result
    ref_c = torch.matmul(A, B.T).to(getattr(torch, accum_dtype))
    torch.testing.assert_close(C, ref_c, rtol=1e-2, atol=1e-2)


def test_matmul_blocked():
    # pipeline
    assert_tl_matmul_correctness(1024, 1024, 1024, num_stages=2)
    assert_tl_matmul_correctness(1024, 1024, 1024, num_stages=1)
    # L2 Cache
    assert_tl_matmul_correctness(1024, 1024, 1024, enable_rasterization=True)

if __name__ == "__main__":
    bitblas.testing.main()
