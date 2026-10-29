import math

def hook_lengths_correct(partition):
    rows = len(partition)
    hooks = []
    for i in range(rows):
        row_hooks = []
        for j in range(partition[i]):
            arm = 0
            for k in range(j + 1, partition[i]):
                arm += 1
            
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

def dimension_from_partition_correct(n, partition):
    hooks = hook_lengths_correct(partition)
    product = 1
    for row in hooks:
        for hook in row:
            product *= hook
    return math.factorial(n) // product

n = 25

# 计算一些特定分拆的维度
test_partitions = [
    [25],  # 平凡表示
    [24,1],  # 标准表示
    [23,2],
    [18,7],  # 我们列表中最大的小于500000的
    [17,8],  # 可能刚好超过500000
    [13,12],  # 更平衡的分拆
    [9,8,8],  # 另一个分拆
    [5,5,5,5,5],  # 自共轭，最平衡之一
]

print("Dimensions of selected partitions for S25:")
for part in test_partitions:
    dim = dimension_from_partition_correct(n, part)
    print(f"{str(part):20s}: {dim:,}")

# 计算最大维度（应该是非常平衡的分拆）
print("\nChecking maximum dimension...")
# 尝试一些可能产生大维度的分拆
candidate_parts = [
    [5,5,5,5,5],
    [6,5,5,4,3,2],
    [7,6,5,4,3],
    [8,7,6,4],
    [9,8,7,1],
]

max_dim = 0
max_part = None
for part in candidate_parts:
    dim = dimension_from_partition_correct(n, part)
    if dim > max_dim:
        max_dim = dim
        max_part = part
    print(f"{str(part):20s}: {dim:,}")

print(f"\nMaximum among these: {max_part} with dimension {max_dim:,}")

# 与我们之前计算的最大维度比较
print(f"\nPreviously computed maximum dimension: 547,591,590,000")
print(f"Ratio: {max_dim/547591590000:.6f}")