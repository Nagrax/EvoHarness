import math
from functools import lru_cache

@lru_cache(maxsize=None)
def partitions_of_n_cached(n):
    """使用动态规划生成分拆"""
    def generate_parts(n, max_part):
        if n == 0:
            return [[]]
        if n < 0 or max_part == 0:
            return []
        
        result = []
        # 包含 max_part
        for part in generate_parts(n - max_part, max_part):
            result.append([max_part] + part)
        
        # 不包含 max_part
        result.extend(generate_parts(n, max_part - 1))
        return result
    
    return generate_parts(n, n)

def hook_product(partition):
    """直接计算钩长乘积，更高效"""
    # 将分拆转换为 Young diagram 的钩长乘积
    # 使用公式: product of hook lengths = product over cells of (arm + leg + 1)
    rows = len(partition)
    prod = 1
    
    # 预计算每列的长度
    col_heights = [0] * partition[0] if partition else []
    for i in range(rows):
        for j in range(partition[i]):
            col_heights[j] = rows - i
    
    for i in range(rows):
        row_len = partition[i]
        for j in range(row_len):
            arm = row_len - j - 1
            leg = col_heights[j] - (rows - i)  # 当前列下方还有多少行
            prod *= (arm + leg + 1)
    
    return prod

def dimension_from_partition_fast(n, partition):
    prod = hook_product(partition)
    return math.factorial(n) // prod

# 主计算
n = 25
threshold = 500000

partitions = partitions_of_n_cached(n)
print(f"Total partitions of {n}: {len(partitions)}")

# 计数
count_small = 0
small_dims = []

for i, part in enumerate(partitions):
    dim = dimension_from_partition_fast(n, part)
    if dim < threshold:
        count_small += 1
        small_dims.append(dim)
    
    if (i + 1) % 500 == 0:
        print(f"  Processed {i+1}/{len(partitions)} partitions")

print(f"\nNumber of irreps with dimension < {threshold}: {count_small}")

# 验证一些值
print("\nVerification of some dimensions:")
test_parts = [
    [25],
    [24,1],
    [23,2],
    [18,7],
    [17,8],
]

for part in test_parts:
    dim = dimension_from_partition_fast(n, part)
    status = "<" if dim < threshold else ">="
    print(f"  {str(part):20s}: {dim:,} ({status} {threshold})")

# 验证总数
print(f"\nCross-check: p(25) = 1958")
print(f"Our partition count: {len(partitions)}")
if len(partitions) == 1958:
    print("✓ Partition count matches known value")
else:
    print(f"✗ Partition count mismatch: expected 1958, got {len(partitions)}")

# 最终答案
print(f"\nFinal Answer: {count_small}")