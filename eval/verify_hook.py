import math

def hook_lengths(partition):
    """计算 Young diagram 的钩长"""
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

# 测试已知值
# S3: 分拆数 = 3
# [3]: dim = 1
# [2,1]: dim = 2  
# [1,1,1]: dim = 1

print("Testing S3:")
partitions = [[3], [2,1], [1,1,1]]
for p in partitions:
    dim = dimension_from_partition(3, p)
    print(f"{p}: {dim}")

print("\nTesting S4:")
# S4: 分拆数 = 5
# [4]: 1
# [3,1]: 3
# [2,2]: 2
# [2,1,1]: 3
# [1,1,1,1]: 1
partitions = [[4], [3,1], [2,2], [2,1,1], [1,1,1,1]]
for p in partitions:
    dim = dimension_from_partition(4, p)
    print(f"{p}: {dim}")

print("\nTesting S5 - 标准表示 [4,1]:")
dim = dimension_from_partition(5, [4,1])
print(f"[4,1]: {dim} (should be 4)")

print("\nTesting S5 - [3,2]:")
dim = dimension_from_partition(5, [3,2])
print(f"[3,2]: {dim} (should be 5)")