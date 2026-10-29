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

small_partitions = []
for part in partitions:
    dim = dimension_from_partition(n, part)
    if dim < threshold:
        small_partitions.append((part, dim))

# 按维度排序
small_partitions.sort(key=lambda x: x[1])

print(f"Partitions with dimension < {threshold}: {len(small_partitions)}")
print("\nList of partitions and their dimensions:")
for i, (part, dim) in enumerate(small_partitions):
    print(f"{i+1:3d}. {str(part):20s} : {dim:,}")

# 验证总和
print(f"\nVerification: Counting unique dimensions...")
unique_dims = set()
for part, dim in small_partitions:
    unique_dims.add(dim)

print(f"Number of unique dimensions: {len(unique_dims)}")
print("Unique dimensions (sorted):")
for dim in sorted(unique_dims):
    print(f"  {dim:,}")

# 检查对称性：每个分拆应该有其共轭
print("\nChecking conjugate partitions...")
conjugate_pairs = []
for part, dim in small_partitions[:10]:  # 只检查前10个
    # 计算共轭分拆
    rows = len(part)
    if rows == 0:
        continue
    
    conjugate = []
    for j in range(part[0]):
        count = 0
        for i in range(rows):
            if j < part[i]:
                count += 1
        conjugate.append(count)
    
    # 查找共轭分拆
    found = False
    for p2, d2 in small_partitions:
        if p2 == conjugate:
            if dim == d2:
                print(f"✓ {part} <-> {conjugate} : {dim}")
            else:
                print(f"✗ {part} <-> {conjugate} : {dim} vs {d2}")
            found = True
            break
    
    if not found:
        print(f"✗ {part} : conjugate {conjugate} not found in small partitions")