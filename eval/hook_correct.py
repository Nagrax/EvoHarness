import math

def hook_lengths_correct(partition):
    """更清晰地计算钩长"""
    rows = len(partition)
    # 创建格子图
    diagram = []
    for i in range(rows):
        diagram.append([1] * partition[i])
    
    hooks = []
    for i in range(rows):
        row_hooks = []
        for j in range(partition[i]):
            # arm: 向右直到边缘
            arm = 0
            for k in range(j + 1, partition[i]):
                arm += 1
            
            # leg: 向下直到边缘
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

# 测试 S7 [4,3]
print("Testing S7 [4,3]:")
n = 7
part = [4,3]
hooks = hook_lengths_correct(part)
print(f"Hook lengths: {hooks}")

# 手动计算预期的钩长
# 对于 Young diagram [4,3]:
# 第一行格子: (1,1), (1,2), (1,3), (1,4)
# 第二行格子: (2,1), (2,2), (2,3)
# 钩长:
# (1,1): 向右3个，向下1个 = 3 + 1 + 1 = 5?
# 实际上钩长是：向右的格子数 + 向下的格子数 + 1
# 所以 (1,1): arm=3, leg=1, hook=5
# (1,2): arm=2, leg=1, hook=4
# (1,3): arm=1, leg=1, hook=3
# (1,4): arm=0, leg=0, hook=1 (只有自己)
# (2,1): arm=2, leg=0, hook=3
# (2,2): arm=1, leg=0, hook=2
# (2,3): arm=0, leg=0, hook=1
# 所以钩长应该是: [[5,4,3,1], [3,2,1]]
print(f"Expected hook lengths: [[5,4,3,1], [3,2,1]]")
print(f"Computed hook lengths: {hooks}")

dim = dimension_from_partition_correct(n, part)
print(f"Dimension = {n}! / product(hooks) = {dim}")
print(f"Expected dimension for S7 [4,3] = 35")

# 计算乘积
product = 1
for row in hooks:
    for hook in row:
        product *= hook
print(f"Product of hook lengths = {product}")
print(f"7! = {math.factorial(7)} = {math.factorial(7)}")
print(f"7! / product = {math.factorial(7) // product}")

# 测试 S6 [3,2,1] 应该为16
print("\nTesting S6 [3,2,1]:")
n = 6
part = [3,2,1]
dim = dimension_from_partition_correct(n, part)
print(f"Dimension = {dim} (expected 16)")

# 测试 S8 [5,3]
print("\nTesting S8 [5,3]:")
n = 8
part = [5,3]
dim = dimension_from_partition_correct(n, part)
print(f"Dimension = {dim} (expected 70)")