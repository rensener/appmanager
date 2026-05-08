"""核心匹配器 — 将平铺的包列表组装成依赖树。"""

from dataclasses import dataclass, field
from src.db.models import Package


@dataclass
class PackageNode:
    """树中的一个节点：一个包及其子依赖。"""
    package: Package
    children: list["PackageNode"] = field(default_factory=list)
    is_shared: bool = False  # 是否被多个父包共享
    shared_by: list[str] = field(default_factory=list)  # 哪些父包共享了它
    is_automatic: bool = False  # 是否是自动拉入的依赖

    @property
    def child_count(self) -> int:
        return len(self.children)

    @property
    def total_size_kb(self) -> int:
        """返回此包及其所有独有依赖的总大小。"""
        return self.package.installed_size + sum(
            c.package.installed_size for c in self.children
        )


def build_trees(
    parents: list[Package],
    dependencies: dict[int, list[tuple[Package, bool]]],
) -> list[PackageNode]:
    """将父包列表和依赖映射组装成树形结构。

    Args:
        parents: 父包列表
        dependencies: {parent_id: [(child_package, is_automatic), ...]}

    Returns:
        PackageNode 列表，每个节点包含其子依赖
    """
    # 统计每个子包被多少个父包共享
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

    trees: list[PackageNode] = []
    for parent in parents:
        node = PackageNode(package=parent)

        if parent.id and parent.id in dependencies:
            for child, is_auto in dependencies[parent.id]:
                child_node = PackageNode(package=child, is_automatic=is_auto)
                # 标记共享依赖
                if child.id and child.id in child_parents:
                    sharers = child_parents[child.id]
                    # 过滤掉自己
                    other_sharers = [s for s in sharers if s != parent.name]
                    if other_sharers:
                        child_node.is_shared = True
                        child_node.shared_by = other_sharers
                node.children.append(child_node)

        trees.append(node)

    return trees


def find_unique_dependencies(node: PackageNode) -> list[PackageNode]:
    """找出此节点独有的依赖（不被其他父包共享的）。"""
    return [c for c in node.children if not c.is_shared]


def find_shared_dependencies(node: PackageNode) -> list[PackageNode]:
    """找出此节点被共享的依赖。"""
    return [c for c in node.children if c.is_shared]
