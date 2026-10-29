import math

def partitions_of_n(n):
    result = []
    
    def generate_partitions(target, max_part, current):
        if target == 0:
            result.append(current.copy())
            return
        if max_part == 0:
            return
        
        if max_part <= target:
            current.append(max_part)
            generate_partitions(target - max_part, max_part, current)
            current.pop()
        
        generate_partitions(target, max_part - 1, current)
    
    generate_partitions(n, n, [])
    return result

def hook_lengths(partition):
    rows = len(partition)
    hooks = []
    for i, row_len in enumerate(partition):
        row_hooks = []
        for j in range(row_len):
            arm = row_len - j - 1
            leg = 0
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
    hooks = hook_lengths(partition)
    product = 1
    for row in hooks:
        for hook in row:
            product *= hook
    return math.factorial(n) // product

n = 25
threshold = 500000
partitions = partitions_of_n(n)

# 找到阈值附近的分拆
near_threshold = []
for part in partitions:
    dim = dimension_from_partition(n, part)
    if abs(dim - threshold) < threshold * 0.1:  # 在阈值的 ±10% 内
        near_threshold.append((part, dim))

print(f"Partitions with dimension near {threshold}:")
near_threshold.sort(key=lambda x: x[1])
for part, dim in near_threshold:
    if dim < threshold:
        print(f"  {str(part):30s} : {dim:,} (< threshold)")
    else:
        print(f"  {str(part):30s} : {dim:,} (>= threshold)")

# 检查边界情况
print(f"\nChecking boundary cases:")
boundary_cases = []
for part in partitions:
    dim = dimension_from_partition(n, part)
    if 490000 <= dim <= 510000:
        boundary_cases.append((part, dim))

boundary_cases.sort(key=lambda x: x[1])
for part, dim in boundary_cases:
    status = "<" if dim < threshold else ">="
    print(f"  {str(part):30s} : {dim:,} ({status} threshold)")

# 统计精确值
print(f"\nPrecise count:")
below = sum(1 for part in partitions if dimension_from_partition(n, part) < threshold)
print(f"Dimensions < {threshold}: {below}")
at_or_above = sum(1 for part in partitions if dimension_from_partition(n, part) >= threshold)
print(f"Dimensions >= {threshold}: {at_or_above}")

# 验证一些已知的维度计算
print(f"\nVerifying some dimensions with known formulas:")
# 平凡表示
print(f"Trivial [25]: {dimension_from_partition(n, [25])}")
# 符号表示  
print(f"Sign [1^25]: {dimension_from_partition(n, [1]*25)}")
# 标准表示
print(f"Standard [24,1]: {dimension_from_partition(n, [24,1])} (should be {n-1})")
# 第二个最小的非平凡表示
print(f"[23,2]: {dimension_from_partition(n, [23,2])}")
# 检查 [18,7] 的维度是否正确（我们列表中的最大值）
print(f"[18,7]: {dimension_from_partition(n, [18,7])}")