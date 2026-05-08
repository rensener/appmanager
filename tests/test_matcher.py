"""Step 3 验证脚本 — 测试依赖树组装逻辑。"""
import sys
sys.path.insert(0, "/home/rensen/projects/app_manager")

from src.core.matcher import (
    build_trees, find_unique_dependencies, find_shared_dependencies, PackageNode
)
from src.db.models import Package

# 构造测试数据：fcitx5 → libfcitx5core7, mpv → libmpv2
fcitx5 = Package(id=1, name="fcitx5", source="apt", is_manual=True, installed_size=600)
libcore = Package(id=2, name="libfcitx5core7", source="apt", is_manual=False, installed_size=500)
mpv = Package(id=3, name="mpv", source="apt", is_manual=True, installed_size=1000)
libmpv = Package(id=4, name="libmpv2", source="apt", is_manual=False, installed_size=800)

parents = [fcitx5, mpv]
deps = {
    1: [(libcore, True)],   # fcitx5 依赖 libfcitx5core7
    3: [(libmpv, True)],    # mpv 依赖 libmpv2
}

trees = build_trees(parents, deps)
assert len(trees) == 2
print(f"[OK] 构建 {len(trees)} 棵树")

# 检查 fcitx5 的依赖
fcitx5_node = trees[0]
assert fcitx5_node.package.name == "fcitx5"
assert len(fcitx5_node.children) == 1
assert fcitx5_node.children[0].package.name == "libfcitx5core7"
print(f"[OK] fcitx5 有 {len(fcitx5_node.children)} 个依赖")

# 检查独有依赖
unique = find_unique_dependencies(fcitx5_node)
assert len(unique) == 1
print(f"[OK] fcitx5 独有依赖: {[c.package.name for c in unique]}")

# 测试共享依赖
# fcitx5 → libfcitx5core7, fcitx5-rime → libfcitx5core7
fcitx5_rime = Package(id=5, name="fcitx5-rime", source="apt", is_manual=True, installed_size=200)
parents2 = [fcitx5, fcitx5_rime]
deps2 = {
    1: [(libcore, True)],  # fcitx5 → libcore
    5: [(libcore, True)],  # fcitx5-rime → libcore (共享!)
}
trees2 = build_trees(parents2, deps2)
assert len(trees2) == 2

# libcore 应该被标记为共享
shared_found = False
for node in trees2:
    for child in node.children:
        if child.package.name == "libfcitx5core7" and child.is_shared:
            shared_found = True
            print(f"[OK] libfcitx5core7 被标记为共享，分享者: {child.shared_by}")
assert shared_found, "libfcitx5core7 应该被标记为共享依赖"

# 测试独有/共享分离
for node in trees2:
    unique_deps = find_unique_dependencies(node)
    shared_deps = find_shared_dependencies(node)
    print(f"  {node.package.name}: 独有={len(unique_deps)}, 共享={len(shared_deps)}")

# 测试 total_size_kb
# fcitx5: 600 + 500(libcore) = 1100
assert fcitx5_node.total_size_kb == 1100
print(f"[OK] total_size_kb: {fcitx5_node.total_size_kb} KB")

# 测试空依赖
empty_node = PackageNode(package=fcitx5)
assert empty_node.child_count == 0
assert empty_node.total_size_kb == 600
print("[OK] 空依赖节点")

print("\n=== 所有 Step 3 测试通过 ===")
