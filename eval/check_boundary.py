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

# 找到所有维度并排序
all_dims = []
for part in partitions:
    dim = dimension_from_partition(n, part)
    all_dims.append(dim)

all_dims.sort()

# 找到阈值后的几个维度
print(f"Dimensions around threshold {threshold}:")
for i, dim in enumerate(all_dims):
    if dim >= threshold:
        print(f"\nFirst dimension >= {threshold}:")
        print(f"  {dim:,}")
        
        # 显示前10个大于等于阈值的维度
        print(f"\nFirst 10 dimensions >= {threshold}:")
        for j in range(i, min(i+10, len(all_dims))):
            print(f"  {all_dims[j]:,}")
        
        # 显示最后10个小于阈值的维度
        print(f"\nLast 10 dimensions < {threshold}:")
        start = max(0, i-10)
        for j in range(start, i):
            print(f"  {all_dims[j]:,}")
        break

# 验证最大的小于阈值的维度
max_below = max(d for d in all_dims if d < threshold)
print(f"\nMaximum dimension < {threshold}: {max_below:,}")
print(f"Minimum dimension >= {threshold}: {min(d for d in all_dims if d >= threshold):,}")

# 检查是否有维度等于阈值
if threshold in all_dims:
    print(f"\nWARNING: There is a dimension exactly equal to {threshold}!")
else:
    print(f"\nNo dimension exactly equal to {threshold}.")