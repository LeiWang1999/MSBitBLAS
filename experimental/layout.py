import tvm
import math

def _xor2x2(i,j):
    return (i + j) % 2

def _xor4x4(i, j):
    i0 = i % 2
    i1 = i // 2
    j0 = j % 2
    j1 = j // 2
    return 2 * _xor2x2(i1, j1) + _xor2x2(i0, j0)

class Layout:
    def __init__(self) -> None:
        pass

    def get(self):
        # convert to lambda to make tir script parser works correctly
        def func(*args):
            return self(*args)
        return func

    def requires_padding(self) -> bool:
        return False

    def get_stride(self) -> int:
        raise NotImplementedError

    def get_vectorize(self) -> int:
        raise NotImplementedError

class RowMajorLayout(Layout):
    def __init__(self, ldm) -> None:
        super().__init__()
        self._ldm = ldm
        self._pad = 0

    def __call__(self, offset):
        return offset

    def smem_layout_name(self):
        return "cutlass::layout::RowMajor"

    def local_layout_name(self):
        return "cutlass::layout::RowMajor"

    def requires_padding(self) -> bool:
        return True

    def set_pad(self, pad):
        self._pad = pad

    def get_stride(self) -> int:
        return self._ldm + self._pad

    def get_vectorize(self) -> int:
        return math.gcd(math.gcd(8, self.get_stride()), self._ldm)

class ColumnMajorLayout(Layout):
    def __init__(self) -> None:
        super().__init__()

    def __call__(self, offset):
        return offset

    def smem_layout_name(self):
        return "cutlass::layout::ColumnMajor"

    def local_layout_name(self):
        return "cutlass::layout::ColumnMajor"

    def requires_padding(self) -> bool:
        return True

class RowMajorVoltaTensorOpMultiplicandBCongruous(Layout):
    def __init__(self, ldm) -> None:
        super().__init__()
        self._ldm = ldm
        assert(self._ldm % 64 == 0) # 64 half = full 32 banks

    def __call__(self, offset):
        i, j = offset // self._ldm, offset % self._ldm
        vec_contiguous_idx = j // 8
        vec_strided_idx = i
        tile_contiguous_idx = vec_contiguous_idx // 8
        tile_strided_idx = vec_strided_idx // 4
        tile_contiguous_residual = vec_contiguous_idx % 8
        tile_strided_residual = vec_strided_idx % 4

        permuted_strided_within_tile = tile_contiguous_residual % 4
        permuted_contiguous_within_tile = (tile_contiguous_residual // 4) * 4 + \
            _xor4x4(tile_strided_residual, permuted_strided_within_tile)

        element_strided = permuted_strided_within_tile + tile_strided_idx * 4
        element_contiguous = j % 8 + (permuted_contiguous_within_tile + tile_contiguous_idx * 8) * 8
        return element_strided * self._ldm + element_contiguous

    def smem_layout_name(self):
        return "cutlass::layout::RowMajorVoltaTensorOpMultiplicandBCongruous<16>"

    def local_layout_name(self):
        return "cutlass::layout::RowMajor"

    def get_vectorize(self) -> int:
        return 8

    def get_stride(self) -> int:
        return self._ldm

class RowMajorVoltaTensorOpMultiplicandCongruous(Layout):
    def __init__(self, ldm) -> None:
        super().__init__()
        self._ldm = ldm
        assert(self._ldm % 64 == 0) # 64 half = full 32 banks

    def __call__(self, offset):
        i, j = offset // self._ldm, offset % self._ldm
        vec_contiguous_idx = j // 8
        vec_strided_idx = i
        tile_contiguous_idx = vec_contiguous_idx // 8
        tile_strided_idx = vec_strided_idx // 4
        tile_contiguous_residual = vec_contiguous_idx % 8
        tile_strided_residual = vec_strided_idx % 4

        permuted_strided_within_tile = tile_contiguous_residual // 2
        permuted_contiguous_within_tile = (tile_contiguous_residual % 2) * 4 + \
            _xor4x4(tile_strided_residual, permuted_strided_within_tile)

        element_strided = permuted_strided_within_tile + tile_strided_idx * 4
        element_contiguous = j % 8 + (permuted_contiguous_within_tile + tile_contiguous_idx * 8) * 8
        return element_strided * self._ldm + element_contiguous

    def smem_layout_name(self):
        return "cutlass::layout::RowMajorVoltaTensorOpMultiplicandCongruous<16>"

    def local_layout_name(self):
        return "cutlass::layout::RowMajor"

    def get_vectorize(self) -> int:
        return 8

    def get_stride(self) -> int:
        return self._ldm

class RowMajorVoltaTensorOpMultiplicandCrosswise(Layout):
    def __init__(self, mblock, kblock, mwarp) -> None:
        super().__init__()
        self._mblock = mblock
        self._kblock = kblock
        self._mwarp = mwarp
        # should contain at least 32x32 half
        assert(self._mblock % 32 == 0) # 4xfloat16 * 8
        assert(self._kblock % 32 == 0) # 4xfloat16 * 8

    # def __call__(self, offset):
    #     i, j = offset // self._kblock, offset % self._kblock
    #     vec_contiguous_idx = j // 4
    #     vec_strided_idx = i
    #     vec_strided_within_tile = vec_contiguous_idx % 8

    #     permuted_vec_contiguous = (vec_strided_idx // 16) * 16 + (vec_strided_idx & 0x3) * 4 + \
    #         (((vec_strided_idx >> 2) ^ ((vec_strided_idx & 0x10) >> 3)) & 0x3)

    #     permuted_vec_contiguous ^= ((vec_strided_within_tile >> 1) & 0x3)

    #     permuted_vec_strided = vec_contiguous_idx
    #     element_contiguous = permuted_vec_contiguous * 4 + (j % 4)

    #     offset = element_contiguous + permuted_vec_strided * (self._mblock * 4)

    #     return offset

    def __call__(self, offset):
        i, j = offset // self._kblock, offset % self._kblock
        vec_contiguous_idx = j // 4
        vec_strided_idx = i
        vec_strided_within_tile = vec_contiguous_idx % 8

        permuted_vec_contiguous = (vec_strided_idx // 16) * 16 + (vec_strided_idx % 4) * 4
        bit2 = ((vec_strided_idx % 32 // 16) + ((vec_strided_idx % 16) // 8) + (vec_strided_within_tile // 4)) % 2
        bit1 = (((vec_strided_idx % 8) // 4) + (vec_strided_within_tile % 4 // 2)) % 2
        permuted_vec_contiguous += bit2 * 2 + bit1
        # permuted_vec_contiguous = (vec_strided_idx // 16) * 16 + (vec_strided_idx & 0x3) * 4 + \
        #     (((vec_strided_idx >> 2) ^ ((vec_strided_idx & 0x10) >> 3)) & 0x3)
        # permuted_vec_contiguous ^= ((vec_strided_within_tile >> 1) & 0x3)

        offset = j % 4 + permuted_vec_contiguous * 4 + vec_contiguous_idx * self._mblock * 4

        return offset

    def smem_layout_name(self):
        return f"cutlass::layout::RowMajorVoltaTensorOpMultiplicandCrosswise<16, {self._kblock}>"

    def local_layout_name(self):
        return "cutlass::layout::RowMajor"

    def get_vectorize(self) -> int:
        return 4

    def get_stride(self) -> int:
        return self._mblock

class RowMajorTensorOpMultiplicandCongruous(Layout):
    def __init__(self, ldm) -> None:
        super().__init__()
        self._ldm = ldm
        self._access_elements = 8
        self._tile_shape = (8, 8)
        self._partition_shape = (4, 4)
        assert(self._ldm % 64 == 0)

    def __call__(self, offset):
        i, j = offset // self._ldm, offset % self._ldm
        vec_contiguous_idx = j // self._access_elements
        vec_strided_idx = i
        tile_contiguous_idx = vec_contiguous_idx // self._tile_shape[1]
        tile_contiguous_residual = vec_contiguous_idx % self._tile_shape[1]
        tile_strided_residual = vec_strided_idx % self._tile_shape[0]

        partition_contiguous_idx = tile_contiguous_residual // self._partition_shape[1]
        partition_strided_idx = tile_strided_residual // self._partition_shape[0]
        partition_contiguous_residual = tile_contiguous_residual % self._partition_shape[1]
        partition_strided_residual = tile_strided_residual % self._partition_shape[0]

        permuted_vec_contiguous_within_partition = _xor4x4(partition_contiguous_residual, partition_strided_residual)
        permuted_partition_contiguous_within_tile = _xor2x2(partition_contiguous_idx, partition_strided_idx)

        element_contiguous = self._access_elements * (
            permuted_vec_contiguous_within_partition + tile_contiguous_idx * self._tile_shape[1] + \
            permuted_partition_contiguous_within_tile * self._partition_shape[1]
        ) + j % self._access_elements

        return vec_strided_idx * self._ldm + element_contiguous

    def smem_layout_name(self):
        return f"cutlass::layout::RowMajorTensorOpMultiplicandCongruous<16, 64>"

    def local_layout_name(self):
        return "cutlass::layout::RowMajor"

    def get_vectorize(self) -> int:
        return 8

    def get_stride(self) -> int:
        return self._ldm

class RowMajorTensorOpMultiplicandCrosswise(Layout):
    def __init__(self, ldm, factor = 2) -> None:
        super().__init__()
        self._ldm = ldm
        self._kfactor = factor
        self._access_elements = 8
        self._tile_shape = (8 // self._kfactor, 8 // self._kfactor)
        self._partition_shape = (4, 4)
        assert(self._ldm % 32 == 0)

    def __call__(self, offset):
        i, j = offset // self._ldm, offset % self._ldm
        vec_contiguous_idx = j // self._access_elements
        vec_strided_idx = i // self._kfactor
        tile_contiguous_idx = vec_contiguous_idx // self._tile_shape[1]
        tile_contiguous_residual = vec_contiguous_idx % self._tile_shape[1] + (i % self._kfactor) * self._tile_shape[1]
        tile_strided_residual = vec_strided_idx % self._tile_shape[0]

        partition_contiguous_idx = tile_contiguous_residual // self._partition_shape[1]
        partition_strided_idx = tile_strided_residual // self._partition_shape[0]
        partition_contiguous_residual = tile_contiguous_residual % self._partition_shape[1]
        partition_strided_residual = tile_strided_residual % self._partition_shape[0]

        permuted_vec_contiguous_within_partition = _xor4x4(partition_contiguous_residual, partition_strided_residual)
        permuted_partition_contiguous_within_tile = _xor2x2(partition_contiguous_idx, partition_strided_idx)

        element_contiguous = self._access_elements * (
            permuted_vec_contiguous_within_partition + tile_contiguous_idx * self._tile_shape[1] * self._kfactor + \
            permuted_partition_contiguous_within_tile * self._partition_shape[1]
        ) + j % self._access_elements

        return vec_strided_idx * self._ldm * self._kfactor + element_contiguous

    def smem_layout_name(self):
        return f"cutlass::layout::RowMajorTensorOpMultiplicandCrosswise<16, 32>"

    def local_layout_name(self):
        return "cutlass::layout::RowMajor"

    def get_vectorize(self) -> int:
        return 8

    def get_stride(self) -> int:
        return self._ldm

class voltaFragmentCLayout32x32(Layout):
    def __init__(self, m, n) -> None:
        super().__init__()
        self._m = m
        self._n = n

        assert(self._m % 32 == 0)
        assert(self._n % 32 == 0)

    def _map_index_32x32(self, i, j):
        thread_id = i % 4 + ((i % 16) // 8) * 4 + ((j % 16) // 8) * 8 + (i // 16) * 16
        local_id = j % 4 + (j // 16) * 4 + ((i % 8) // 4) * 8 + ((j % 8) // 4) * 16
        return [thread_id, local_id]

    def __call__(self, i, j):
        if self._m > 32:
            i_in_block, i_block = i % 32, i // 32
        else:
            i_in_block, i_block = i, 0
        if self._n > 32:
            j_in_block, j_block = j % 32, j // 32
        else:
            j_in_block, j_block = j, 0
        thread_id , offset = self._map_index_32x32(i_in_block, j_in_block)
        local_id = offset + (i_block + j_block * (self._m // 32)) * 32
        return [thread_id, local_id]

    def get_vectorize(self) -> int:
        return 4

class FragmentCLayout8x8(Layout):
    def __init__(self, m, n) -> None:
        super().__init__()
        self._m = m
        self._n = n

        assert(self._m % 8 == 0)
        assert(self._n % 8 == 0)

    def _map_index_8x8(self, i, j):
        thread_id = i * 4 + j // 2
        local_id =  j % 2
        return [thread_id, local_id]

    def __call__(self, i, j):
        if self._m > 8:
            i_in_block, i_block = i % 8, i // 8
        else:
            i_in_block, i_block = i, 0
        if self._n > 8:
            j_in_block, j_block = j % 8, j // 8
        else:
            j_in_block, j_block = j, 0
        thread_id, offset = self._map_index_8x8(i_in_block, j_in_block)
        local_id = offset + (i_block + j_block * (self._m // 8)) * 2
        return [thread_id, local_id]

    def get_vectorize(self) -> int:
        return 2
