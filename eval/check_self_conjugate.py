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

def is_self_conjugate(partition):
    """检查分拆是否是自共轭的"""
    rows = len(partition)
    if rows == 0:
        return True
    
    # 计算共轭
    conjugate = []
    for j in range(partition[0]):
        count = 0
        for i in range(rows):
            if j < partition[i]:
                count += 1
        conjugate.append(count)
    
    return partition == conjugate

n = 25
threshold = 500000
partitions = partitions_of_n(n)

# 统计自共轭分拆
self_conjugate_parts = []
for part in partitions:
    if is_self_conjugate(part):
        self_conjugate_parts.append(part)

print(f"Self-conjugate partitions of {n}: {len(self_conjugate_parts)}")
for part in self_conjugate_parts:
    print(f"  {part}")

# 检查哪些自共轭分拆的维度小于阈值
print(f"\nSelf-conjugate partitions with dimension < {threshold}:")
count_self_conjugate_small = 0
for part in self_conjugate_parts:
    dim = dimension_from_partition_correct(n, part)
    if dim < threshold:
        print(f"  {part}: {dim}")
        count_self_conjugate_small += 1

print(f"\nTotal self-conjugate partitions with dimension < {threshold}: {count_self_conjugate_small}")

# 验证总计数
total_small = 0
dimension_counts = {}
for part in partitions:
    dim = dimension_from_partition_correct(n, part)
    if dim < threshold:
        total_small += 1
        dimension_counts[dim] = dimension_counts.get(dim, 0) + 1

print(f"\nTotal partitions with dimension < {threshold}: {total_small}")
print(f"Number of unique dimensions: {len(dimension_counts)}")

# 检查每个维度出现的次数
print("\nDimension frequencies:")
for dim, count in sorted(dimension_counts.items()):
    print(f"  {dim:,}: appears {count} time(s)")

# 验证：对于非自共轭分拆，维度应该出现2次
non_self_conjugate_dims = {}
for part in partitions:
    if not is_self_conjugate(part):
        dim = dimension_from_partition_correct(n, part)
        if dim < threshold:
            non_self_conjugate_dims[dim] = non_self_conjugate_dims.get(dim, 0) + 1

print("\nChecking non-self-conjugate dimension frequencies:")
all_even = True
for dim, count in non_self_conjugate_dims.items():
    if count % 2 != 0:
        print(f"  WARNING: Dimension {dim:,} appears {count} times (odd!)")
        all_even = False

if all_even:
    print("  All non-self-conjugate dimensions appear an even number of times (good)")