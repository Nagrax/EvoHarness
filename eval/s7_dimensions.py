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

# 计算 S7 的所有维度
print("S7 dimensions:")
n = 7
partitions = partitions_of_n(n)
for part in partitions:
    dim = dimension_from_partition_correct(n, part)
    print(f"{part}: {dim}")

# 检查总数
print(f"\nTotal irreps of S7: {len(partitions)} (should be p(7)=15)")

# 验证一些已知值
print("\nVerifying known values:")
test_cases = [
    ([7], 1),
    ([6,1], 6),
    ([5,2], 14),
    ([5,1,1], 15),
    ([4,3], 14),  # 不是35!
    ([4,2,1], 35),
    ([4,1,1,1], 20),
    ([3,3,1], 21),
    ([3,2,2], 21),
    ([3,2,1,1], 35),
    ([3,1,1,1,1], 15),
    ([2,2,2,1], 14),
    ([2,2,1,1,1], 14),
    ([2,1,1,1,1,1], 6),
    ([1,1,1,1,1,1,1], 1)
]

for part, expected in test_cases:
    computed = dimension_from_partition_correct(n, part)
    if computed == expected:
        print(f"✓ {part}: {computed}")
    else:
        print(f"✗ {part}: computed {computed}, expected {expected}")