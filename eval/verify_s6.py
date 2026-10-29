import math

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

# 已知的 S6 维度（从文献或已知结果）
# S6 的分拆和维度：
# [6]: 1
# [5,1]: 5
# [4,2]: 9
# [4,1,1]: 10
# [3,3]: 5
# [3,2,1]: 16
# [3,1,1,1]: 10
# [2,2,2]: 5
# [2,2,1,1]: 9
# [2,1,1,1,1]: 5
# [1,1,1,1,1,1]: 1

print("Verifying S6 dimensions:")
n = 6
test_cases = [
    ([6], 1),
    ([5,1], 5),
    ([4,2], 9),
    ([4,1,1], 10),
    ([3,3], 5),
    ([3,2,1], 16),
    ([3,1,1,1], 10),
    ([2,2,2], 5),
    ([2,2,1,1], 9),
    ([2,1,1,1,1], 5),
    ([1,1,1,1,1,1], 1)
]

all_correct = True
for part, expected in test_cases:
    computed = dimension_from_partition(n, part)
    if computed == expected:
        print(f"✓ {part}: {computed}")
    else:
        print(f"✗ {part}: computed {computed}, expected {expected}")
        all_correct = False

if all_correct:
    print("\nAll S6 tests passed!")
else:
    print("\nSome S6 tests failed!")

# 测试 S7 的一个已知维度
print("\nVerifying S7 [4,3] dimension (should be 35):")
n = 7
dim = dimension_from_partition(n, [4,3])
print(f"[4,3]: {dim} (expected 35)")

# 测试 S8 [5,3] 维度
print("\nVerifying S8 [5,3] dimension (should be 70):")
n = 8
dim = dimension_from_partition(n, [5,3])
print(f"[5,3]: {dim} (expected 70)")

# 验证钩长公式的替代计算
print("\nAlternative verification for S25 [18,7]:")
n = 25
part = [18,7]
hooks = hook_lengths(part)
print(f"Hook lengths for {part}:")
for i, row in enumerate(hooks):
    print(f"  Row {i}: {row}")

# 手动计算钩长乘积
product = 1
for row in hooks:
    for hook in row:
        product *= hook

print(f"Product of hook lengths: {product}")
print(f"25! = {math.factorial(n)}")
print(f"Dimension = 25! / product = {math.factorial(n) // product}")