import math
from collections import defaultdict

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
print(f"Total partitions: {len(partitions)}")

# 按维度范围统计
ranges = [
    (0, 100),
    (100, 1000),
    (1000, 10000),
    (10000, 100000),
    (100000, threshold),
    (threshold, threshold * 10),
    (threshold * 10, float('inf'))
]

counts = {r: 0 for r in ranges}
small_count = 0
all_dims = []

for i, part in enumerate(partitions):
    dim = dimension_from_partition(n, part)
    all_dims.append(dim)
    
    if dim < threshold:
        small_count += 1
    
    for r_min, r_max in ranges:
        if r_min <= dim < r_max:
            counts[(r_min, r_max)] += 1
            break
    
    if (i + 1) % 500 == 0:
        print(f"Processed {i+1}/{len(partitions)}")

print(f"\nIrreps with dimension < {threshold}: {small_count}")
print("\nDimension distribution:")
for (r_min, r_max), cnt in counts.items():
    if r_max == float('inf'):
        print(f"{r_min}+: {cnt}")
    else:
        print(f"{r_min}-{r_max-1}: {cnt}")

# 找出最小的几个维度
all_dims.sort()
print(f"\nSmallest 10 dimensions:")
for i in range(min(10, len(all_dims))):
    print(f"{i+1}: {all_dims[i]}")

print(f"\nLargest 10 dimensions:")
for i in range(min(10, len(all_dims))):
    print(f"{i+1}: {all_dims[-i-1]}")