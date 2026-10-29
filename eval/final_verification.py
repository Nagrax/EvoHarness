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
    """直接计算钩长乘积，更高效且正确"""
    rows = len(partition)
    
    # 计算每列的高度
    max_col = partition[0] if partition else 0
    col_heights = [0] * max_col
    for i in range(rows):
        for j in range(partition[i]):
            col_heights[j] += 1
    
    prod = 1
    for i in range(rows):
        row_len = partition[i]
        for j in range(row_len):
            arm = row_len - j - 1
            # 当前单元格下方的行数 = 列高度 - 当前行上方的行数 - 1
            # 当前行索引为 i，所以上方有 i 行，当前行算1行
            leg = col_heights[j] - (i + 1)
            prod *= (arm + leg + 1)
    
    return prod

def dimension_from_partition_fast(n, partition):
    prod = hook_product(partition)
    return math.factorial(n) // prod

# 测试小n值
print("Testing with small n values:")
for n_test in [3, 4, 5, 6]:
    parts = partitions_of_n_cached(n_test)
    print(f"S{n_test}: {len(parts)} partitions")
    
    # 计算所有维度并验证一致性
    dims = []
    for part in parts:
        dim = dimension_from_partition_fast(n_test, part)
        dims.append(dim)
    
    print(f"  Dimensions: {sorted(dims)}")
    print(f"  Sum of squares: {sum(d*d for d in dims)}")
    print(f"  n! = {math.factorial(n_test)}")
    if sum(d*d for d in dims) == math.factorial(n_test):
        print("  ✓ Sum of squares equals n! (good)")
    else:
        print("  ✗ Sum of squares mismatch!")

# 主计算
n = 25
threshold = 500000

partitions = partitions_of_n_cached(n)
print(f"\n\nTotal partitions of {n}: {len(partitions)}")

# 计数
count_small = 0

for i, part in enumerate(partitions):
    dim = dimension_from_partition_fast(n, part)
    if dim < threshold:
        count_small += 1
    
    if (i + 1) % 500 == 0:
        print(f"  Processed {i+1}/{len(partitions)} partitions")

print(f"\nNumber of irreps with dimension < {threshold}: {count_small}")

# 验证一些关键值
print("\nVerification of key dimensions:")
test_cases = [
    ([25], 1),
    ([24,1], 24),
    ([23,2], 275),
    ([18,7], 303600),
    ([17,8], 600875),
]

all_correct = True
for part, expected in test_cases:
    computed = dimension_from_partition_fast(n, part)
    if computed == expected:
        print(f"  ✓ {str(part):20s}: {computed:,}")
    else:
        print(f"  ✗ {str(part):20s}: computed {computed:,}, expected {expected:,}")
        all_correct = False

if all_correct:
    print("All test cases passed!")

print(f"\nFinal Answer: {count_small}")