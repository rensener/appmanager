"""核心匹配器 — 将平铺的包列表组装成依赖树。

============================================================
树形结构的构建
============================================================

数据库中依赖关系是「平铺」的：
  fcitx5 → libfcitx5core7
  fcitx5 → fcitx5-data
  mpv → libmpv2
  mpv → libavcodec60

本模块把它们组装成树形结构：
  PackageNode(package=fcitx5)
    ├── PackageNode(package=libfcitx5core7)
    └── PackageNode(package=fcitx5-data)
  PackageNode(package=mpv)
    ├── PackageNode(package=libmpv2)
    └── PackageNode(package=libavcodec60)

TUI 按这个树来渲染节点，每层递归展开。

============================================================
共享依赖的标记
============================================================

如果一个子包被多个父包依赖，它是「共享依赖」。
例如 libc6 可能被 fcitx5 和 mpv 同时依赖。

共享依赖在 TUI 中显示 "(共享依赖)" 标签。
卸载父包时不应该删除共享依赖（还有其他包在用）。
"""

from dataclasses import dataclass, field
from src.db.models import Package


@dataclass
class PackageNode:
    """树中的一个节点：包含一个包及其所有子依赖。

    这是一个递归数据结构：
      - 每个节点有一个 Package 对象
      - children 是子依赖的 PackageNode 列表（可能为空）
      - 子依赖的 children 又可以包含更多层

    对比：普通树节点 vs PackageNode
      普通树节点只有 text（显示文本）
      PackageNode 额外存储了 is_shared、shared_by 等语义信息，
      供 TUI 在渲染时使用（如显示共享标记、计算总大小）。
    """
    package: Package                         # 这个节点对应的包
    children: list["PackageNode"] = field(default_factory=list)  # 子依赖列表
    is_shared: bool = False                  # 是否为共享依赖（被多个父包依赖）
    shared_by: list[str] = field(default_factory=list)  # 被哪些父包共享
    is_automatic: bool = False               # 是否为自动拉入的依赖

    @property
    def child_count(self) -> int:
        """子依赖数量。"""
        return len(self.children)

    @property
    def total_size_kb(self) -> int:
        """返回此包及其所有独有依赖的总大小。

        只计入直接子依赖（一层），不包括共享依赖的大小。
        （共享依赖的大小不应算在任何单一父包头上。）
        """
        return self.package.installed_size + sum(
            c.package.installed_size for c in self.children
        )


def build_trees(
    parents: list[Package],
    dependencies: dict[int, list[tuple[Package, bool]]],
) -> list[PackageNode]:
    """将父包列表和依赖映射组装成树形结构。

    这是从「数据库平铺数据」到「树形展示结构」的核心转换函数。

    算法：
      1. 遍历所有父包的所有依赖，统计哪些子包被多个父包共享
      2. 对每个父包创建 PackageNode
      3. 遍历该父包的依赖，创建子节点
      4. 标记共享依赖（子包被多个父包依赖）

    Args:
        parents: 父包列表（is_manual=True 的包）
        dependencies: {parent_id: [(child_package, is_automatic), ...]}

    Returns:
        PackageNode 列表，每个节点是一棵完整的依赖树

    示例：
      输入：
        parents = [Package("fcitx5"), Package("mpv")]
        dependencies = {
          fcitx5.id: [(Package("libfcitx5core7"), True), (Package("libc6"), True)],
          mpv.id:     [(Package("libmpv2"), True),     (Package("libc6"), True)],
        }
      输出：
        [
          PackageNode(fcitx5, children=[
            PackageNode(libfcitx5core7),
            PackageNode(libc6, is_shared=True, shared_by=["mpv"]),
          ]),
          PackageNode(mpv, children=[
            PackageNode(libmpv2),
            PackageNode(libc6, is_shared=True, shared_by=["fcitx5"]),
          ]),
        ]
    """
    # Step 1: 统计每个子包被哪些父包依赖（用于共享检测）
    # {child_id: [parent_name1, parent_name2, ...]}
    child_parents: dict[int, list[str]] = {}
    for parent in parents:
        if parent.id is None:
            continue
        for child, _ in dependencies.get(parent.id, []):
            if child.id is None:
                continue
            if child.id not in child_parents:
                child_parents[child.id] = []
            child_parents[child.id].append(parent.name)

    # Step 2: 构建每棵依赖树
    trees: list[PackageNode] = []
    for parent in parents:
        node = PackageNode(package=parent)

        if parent.id and parent.id in dependencies:
            for child, is_auto in dependencies[parent.id]:
                child_node = PackageNode(package=child, is_automatic=is_auto)

                # 检查是否为共享依赖
                if child.id and child.id in child_parents:
                    sharers = child_parents[child.id]
                    # 过滤掉当前父包（只关心「其他」父包）
                    other_sharers = [s for s in sharers if s != parent.name]
                    if other_sharers:
                        child_node.is_shared = True
                        child_node.shared_by = other_sharers

                node.children.append(child_node)

        trees.append(node)

    return trees


def find_unique_dependencies(node: PackageNode) -> list[PackageNode]:
    """找出此节点独有的依赖（不被其他父包共享的）。

    独有依赖意味着：卸载这个父包时，这些依赖也可以安全卸载。
    共享依赖则需要保留（其他包还在用）。
    """
    return [c for c in node.children if not c.is_shared]


def find_shared_dependencies(node: PackageNode) -> list[PackageNode]:
    """找出此节点被共享的依赖（被多个父包同时依赖的）。"""
    return [c for c in node.children if c.is_shared]
