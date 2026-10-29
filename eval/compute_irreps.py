import math
from collections import defaultdict
from functools import lru_cache

# 钩长公式：对于 Young diagram，dimension = n! / product of hook lengths
# 我们先实现生成所有分拆的函数

def partitions_of_n(n):
    """生成 n 的所有整数分拆"""
    result = []
    
    def generate_partitions(target, max_part, current):
        if target == 0:
            result.append(current.copy())
            return
        if max_part == 0:
            return
        
        # 包含 max_part
        if max_part <= target:
            current.append(max_part)
            generate_partitions(target - max_part, max_part, current)
            current.pop()
        
        # 不包含 max_part
        generate_partitions(target, max_part - 1, current)
    
    generate_partitions(n, n, [])
    return result

def hook_lengths(partition):
    """计算 Young diagram 的钩长"""
    # partition 是分拆，例如 [5, 3, 1]
    rows = len(partition)
    # 创建钩长表格
    hooks = []
    for i, row_len in enumerate(partition):
        row_hooks = []
        for j in range(row_len):
            # 计算钩长：向右走到边缘 + 向下走到边缘 - 1
            arm = row_len - j - 1  # 向右
            leg = 0  # 向下
            for k in range(i + 1, rows):
                if j < partition[k]:
                    leg += 1
                else:
                    break
            hook = arm + leg + 1
            row_hooks.append(hook)
        hooks.append(row_hooks)
    return hooks

def dimension_from_partition(n, partition):
    """计算分拆对应的表示的维度"""
    hooks = hook_lengths(partition)
    # 计算所有钩长的乘积
    product = 1
    for row in hooks:
        for hook in row:
            product *= hook
    
    # dimension = n! / product of hook lengths
    return math.factorial(n) // product

def count_irreps_below_threshold(n, threshold):
    """统计维度小于阈值的不可约表示的数量"""
    partitions = partitions_of_n(n)
    count = 0
    dims = []
    
    for i, part in enumerate(partitions):
        dim = dimension_from_partition(n, part)
        dims.append(dim)
        if dim < threshold:
            count += 1
        # 进度跟踪
        if (i + 1) % 100 == 0:
            print(f"Processed {i+1}/{len(partitions)} partitions")
    
    return count, dims

# 测试小n值以验证算法
print("Testing with n=5...")
partitions_5 = partitions_of_n(5)
print(f"Number of partitions of 5: {len(partitions_5)}")

for part in partitions_5:
    dim = dimension_from_partition(5, part)
    print(f"Partition {part}: dimension = {dim}")

# 验证：S5应该有7个不可约表示（分拆数）
print(f"\nS5 has {len(partitions_5)} irreps")

# 现在计算n=25
print("\n\nCalculating for n=25...")
n = 25
threshold = 500000

count, dims = count_irreps_below_threshold(n, threshold)
print(f"\nNumber of irreps of S_{n} with dimension < {threshold}: {count}")

# 输出一些统计信息
dims.sort()
print(f"\nDimension statistics:")
print(f"Min dimension: {min(dims)}")
print(f"Max dimension: {max(dims)}")
print(f"Number of irreps total: {len(dims)}")