"""Textual TUI 主应用 — 包管理器终端界面。"""

import subprocess
import time

from textual import events
from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Input, Tree, Label, ListView, ListItem, Static
from textual.binding import Binding

from src.db.database import Database
from src.db.models import Package
from src.core.matcher import build_trees, PackageNode
from src.utils.format_utils import format_size


class PackageTree(Tree):
    """自定义 Tree：加左右键展开/折叠。"""

    BINDINGS = [
        Binding("right", "expand_node", "展开", show=False),
        Binding("left", "collapse_node", "折叠", show=False),
    ]

    def action_expand_node(self) -> None:
        node = self.cursor_node
        if node is not None and not node.is_expanded:
            node.expand()

    def action_collapse_node(self) -> None:
        node = self.cursor_node
        if node is not None:
            if node.is_expanded:
                node.collapse()
            elif node.parent is not None:
                self.select_node(node.parent)

    def on_click(self, event: events.Click) -> None:
        """右键弹出菜单。"""
        if event.button == 3 and self.cursor_node is not None:
            node = self.cursor_node
            if node.data and isinstance(node.data, Package):
                event.stop()
                self.app.push_screen(ContextMenu(node.data, self.app))


class ContextMenu(Screen):
    """右键菜单 — 详情 / 隐藏 / 移动（双击）"""

    def __init__(self, pkg: Package, app: 'AppManagerApp'):
        super().__init__()
        self._pkg = pkg
        self._app = app
        target = "系统预装" if pkg.installed_at else "用户安装"
        self._items = [
            ("详情", "show_detail", False),
            ("隐藏" if not pkg.hide else "取消隐藏", "toggle_hide", False),
            (f"移动到 {target}（双击）", "move_package", True),
            ("取消", "cancel", False),
        ]
        self._last_click = (None, 0.0)  # (idx, timestamp)

    def compose(self) -> ComposeResult:
        yield Static(f" {self._pkg.name} [{self._pkg.source}]", id="menu-title")
        yield ListView(
            *[ListItem(Label(f" {label}")) for label, _, _ in self._items],
            id="menu-list",
        )

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss()
            return
        if event.key == "enter":
            event.stop()
            idx = self.query_one("#menu-list", ListView).index
            if idx is not None and idx < len(self._items):
                self._execute(idx)
            return

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item is not None:
            idx = self.query_one("#menu-list", ListView).index
            if idx is not None and idx < len(self._items):
                _, _, need_double = self._items[idx]
                if need_double:
                    now = time.monotonic()
                    prev_idx, prev_time = self._last_click
                    if prev_idx == idx and now - prev_time < 0.6:
                        self._execute(idx)
                    else:
                        self._last_click = (idx, now)
                else:
                    self._execute(idx)

    def _execute(self, idx: int) -> None:
        _, action, _ = self._items[idx]
        self.dismiss()
        if action == "cancel":
            return
        if action == "show_detail":
            self._app._do_show_detail(self._pkg)
        elif action == "toggle_hide":
            self._app._do_toggle_hide(self._pkg)
        elif action == "move_package":
            self._app._do_move_package(self._pkg)


class AppManagerApp(App):
    """包管理器主应用。"""

    CSS = """
    #search-input {
        dock: top;
        margin-bottom: 1;
    }
    #search-input:focus {
        border: solid $accent;
    }
    #search-input.search-active {
        border: solid $warning;
    }
    #search-results {
        display: none;
        border: solid $warning;
        background: #202020;
    }
    #search-results.active {
        display: block;
        height: auto;
        max-height: 10;
    }
    #search-results:focus {
        border: solid $accent;
    }
    #package-tree {
        height: 1fr;
    }
    #status-bar {
        height: 1;
        background: #004080;
        color: #ffffff;
        padding: 0 1;
    }
    ContextMenu {
        align: center middle;
    }
    #menu-title {
        height: 1;
        padding: 0 1;
        background: #004080;
        color: #ffffff;
        width: 40;
    }
    #menu-list {
        width: 40;
        height: auto;
        background: #202020;
        border: solid #004080;
    }
    """

    BINDINGS = [
        Binding("slash", "focus_search", "搜索"),
        Binding("s", "show_detail", "详情"),
        Binding("m", "move_package", "移动至 用户/系统"),
        Binding("y", "confirm_move", "确认移动"),
        Binding("h", "toggle_hide", "隐藏/取消隐藏"),
        Binding("escape", "clear_search", "退出搜索"),
    ]

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._pending_move: Package | None = None
        self._search_packages: list[Package] = []
        self._search_preview: list[Package] = []
        self._node_by_name: dict[str, object] = {}

    def compose(self) -> ComposeResult:
        yield Input(placeholder="/ 搜索包名...  ESC 退出搜索", id="search-input")
        yield ListView(id="search-results")
        yield PackageTree("包管理器", id="package-tree")
        yield Label("加载中...", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one("#package-tree", PackageTree)
        tree.show_root = False
        tree.focus()
        self._rebuild_tree()
        self._update_status()

    def _rebuild_tree(self) -> None:
        tree = self.query_one("#package-tree", PackageTree)
        results = self.query_one("#search-results", ListView)
        search = self.query_one("#search-input", Input).value.strip()

        # ── 全屏搜索结果模式 ──────────────────────
        if self._search_packages and not search:
            tree.clear()
            results.clear()
            results.remove_class("active")
            for p in self._search_packages:
                tree.root.add(self._package_label(p, show_source=True), data=p, expand=False)
            self._update_status(
                f"搜索 — {len(self._search_packages)} 个结果 | ↑↓选择 Enter 跳转  ESC 退出"
            )
            tree.focus()
            return

        # ── 下拉预览模式 ──────────────────────────
        if search:
            if len(search) >= 2:
                pkgs = self.db.search_packages(search)
                count = len(pkgs)
                self._search_preview = pkgs[:200]
                results.clear()
                for p in self._search_preview:
                    results.append(ListItem(
                        Label(self._package_label(p, show_source=True))
                    ))
                results.add_class("active")
                if count > 200:
                    self._update_status(
                        f"搜索「{search}」— {count} 个结果（预览前 200）| ↓选择 Enter 全屏  ESC 退出"
                    )
                else:
                    self._update_status(
                        f"搜索「{search}」— {count} 个结果 | ↓选择 Enter 全屏  ESC 退出"
                    )
            else:
                results.clear()
                results.remove_class("active")
                self._search_preview = []
            return

        # ── 正常模式 ──────────────────────────────
        results.clear()
        results.remove_class("active")
        self._search_packages = []
        self._search_preview = []
        self._node_by_name = {}
        tree.clear()

        all_sources = ["apt", "snap", "flatpak", "brew", "appimage"]

        for src in all_sources:
            parents = self.db.get_parent_packages(src)
            src_total = self.db.get_package_count(src)
            src_node = tree.root.add(f"{src.upper()} ({src_total})", expand=src in ("apt", "snap"))

            if not parents:
                continue

            if src == "apt":
                user_pkgs = [p for p in parents if p.installed_at]
                system_pkgs = [p for p in parents if not p.installed_at]

                if user_pkgs:
                    group = src_node.add(f"用户安装 ({len(user_pkgs)})", expand=False)
                    self._add_packages(group, user_pkgs)

                if system_pkgs:
                    group = src_node.add(f"系统预装 ({len(system_pkgs)})", expand=False)
                    self._add_packages(group, system_pkgs)
            else:
                self._add_packages(src_node, parents)

        hidden = self.db.get_hidden_packages()
        if hidden:
            hidden_node = tree.root.add(f"已隐藏 ({len(hidden)})", expand=True)
            for p in hidden:
                hn = hidden_node.add(self._package_label(p, show_source=True), data=p, expand=False)
                self._node_by_name[p.name] = hn

        self._update_status()
        # 焦点由调用方在 _rebuild_tree 之后处理

    # ── Key handling ───────────────────────────────

    def on_key(self, event: events.Key) -> None:
        search_input = self.query_one("#search-input", Input)
        results = self.query_one("#search-results", ListView)
        tree = self.query_one("#package-tree", PackageTree)

        # q 退出：搜索框有焦点时不拦截，交给 Input 处理
        if event.key == "q":
            if search_input.has_focus:
                return  # 让 q 输入到搜索框
            event.stop()
            if self._search_packages and not search_input.value.strip():
                # 全屏结果 → 回到搜索框
                self._search_packages = []
                search_input.focus()
                return
            if search_input.value.strip():
                # 搜索框有内容但没有焦点 → 清除搜索
                search_input.value = ""
                search_input.remove_class("search-active")
                self._search_packages = []
                self._search_preview = []
                self._rebuild_tree()
                tree.focus()
                return
            self.exit()
            return

        # 全屏搜索结果模式 → Enter 跳转
        if self._search_packages and not search_input.value.strip() and tree.has_focus:
            if event.key == "enter":
                event.stop()
                pkg = self._get_selected_pkg()
                if pkg:
                    target_name = pkg.name
                    self._search_packages = []
                    self._rebuild_tree()
                    self._select_node_by_name(tree, target_name)
                    tree.focus()
                return

        # 搜索框有焦点 + 下拉有结果
        if search_input.has_focus and self._search_preview:
            if event.key == "down":
                event.stop()
                results.focus()
                return
            if event.key == "enter":
                event.stop()
                self._show_full_results(search_input.value.strip())
                return

        # 下拉列表有焦点
        if results.has_focus and self._search_preview:
            if event.key == "enter":
                event.stop()
                idx = results.index
                if idx is not None and idx < len(self._search_preview):
                    pkg = self._search_preview[idx]
                    target_name = pkg.name
                    search_input.value = ""
                    search_input.remove_class("search-active")
                    self._search_preview = []
                    self._rebuild_tree()
                    self._select_node_by_name(tree, target_name)
                    tree.focus()
                return
            if event.key == "escape":
                event.stop()
                search_input.focus()
                return

    def _show_full_results(self, search: str) -> None:
        """显示全屏搜索结果（在树中）。"""
        pkgs = self.db.search_packages(search)
        self._search_packages = pkgs
        self._search_preview = []
        search_input = self.query_one("#search-input", Input)
        search_input.value = ""
        search_input.remove_class("search-active")
        self._rebuild_tree()

    def _select_node_by_name(self, tree: PackageTree, name: str, retries: int = 5) -> None:
        """选中指定包名的树节点。可能因树尚未布局而失败，会自动重试。"""
        node = self._node_by_name.get(name)
        if node is None:
            return
        parent = node.parent
        while parent is not None:
            if not parent.is_expanded:
                parent.expand()
            parent = parent.parent
        tree.select_node(node)
        tree.scroll_to_node(node)
        # 检查是否选中成功：cursor_node 应该是目标节点
        if tree.cursor_node is not node and retries > 0:
            self.set_timer(0.05, lambda: self._select_node_by_name(tree, name, retries - 1))

    def _get_anchor_name(self, tree: PackageTree) -> str | None:
        all_names = []
        def collect(node):
            if node.data and isinstance(node.data, Package):
                all_names.append(node.data.name)
            for child in node.children:
                collect(child)
        for child in tree.root.children:
            collect(child)

        current = self._get_selected_pkg()
        if current is None:
            return None

        current_name = current.name
        try:
            idx = all_names.index(current_name)
            if idx + 1 < len(all_names):
                return all_names[idx + 1]
            if idx > 0:
                return all_names[idx - 1]
        except ValueError:
            pass
        return None

    def _add_packages(self, parent, packages: list[Package]) -> None:
        all_deps: dict[int, list[tuple[Package, bool]]] = {}
        for pkg in packages:
            if pkg.id:
                all_deps[pkg.id] = self.db.get_dependencies(pkg.id)

        trees = build_trees(packages, all_deps)
        for node in trees:
            self._add_node(parent, node)

    def _add_node(self, parent, node: PackageNode) -> None:
        label = self._package_label(node.package, node.is_shared)
        tree_node = parent.add(label, data=node.package, expand=False)
        self._node_by_name[node.package.name] = tree_node
        for child in node.children:
            self._add_node(tree_node, child)

    def _package_label(self, pkg: Package, is_shared: bool = False, show_source: bool = False) -> str:
        size = format_size(pkg.installed_size)
        if show_source:
            label = f"{pkg.name}  [{pkg.source}]  {size}" if size else f"{pkg.name}  [{pkg.source}]"
        else:
            label = f"{pkg.name}  {size}" if size else pkg.name
        if is_shared:
            label = f"{label} (共享依赖)"
        return label

    def _notify(self, message: str, **kwargs) -> None:
        self.clear_notifications()
        self.notify(message, **kwargs)

    def _update_status(self, extra: str = "") -> None:
        status = self.query_one("#status-bar", Label)
        if extra:
            status.update(extra)
            return
        total = self.db.get_package_count()
        status.update(
            f"共 {total} 个包 | ↑↓选择 →展开 ←折叠 s详情 m移动 h隐藏 /搜索"
        )

    def _get_selected_pkg(self) -> Package | None:
        tree = self.query_one("#package-tree", PackageTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return None
        return node.data if isinstance(node.data, Package) else None

    # ── Actions ─────────────────────────────────────

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_clear_search(self) -> None:
        search = self.query_one("#search-input", Input)
        # 全屏结果模式 → 回到搜索框
        if self._search_packages and not search.value.strip():
            self._search_packages = []
            search.focus()
            return
        # 下拉模式 → 清除搜索
        search.value = ""
        search.remove_class("search-active")
        self._search_packages = []
        self._search_preview = []
        self._rebuild_tree()
        self.query_one("#package-tree", PackageTree).focus()

    def action_show_detail(self) -> None:
        pkg = self._get_selected_pkg()
        if pkg is None:
            return
        self._do_show_detail(pkg)

    def _do_show_detail(self, pkg: Package) -> None:
        # APT 包用 dpkg -s 获取完整描述、依赖等信息
        deps = ""
        desc = pkg.description
        homepage = ""
        if pkg.source == "apt":
            try:
                out = subprocess.check_output(
                    ["dpkg", "-s", pkg.name], stderr=subprocess.DEVNULL, text=True
                )
                in_desc = False
                desc_lines = []
                for line in out.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("Depends:"):
                        deps += f"\n{stripped}"
                    elif stripped.startswith("Recommends:"):
                        deps += f"\n{stripped}"
                    elif stripped.startswith("Homepage:"):
                        homepage = f"\n{stripped}"
                    elif stripped.startswith("Description:"):
                        in_desc = True
                        desc_lines.append(stripped.split(":", 1)[1].strip())
                    elif in_desc and line.startswith(" ") and stripped:
                        desc_lines.append(stripped)
                    elif in_desc and not line.startswith(" "):
                        in_desc = False
                if desc_lines:
                    desc = " ".join(desc_lines)
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass

        files = self.db.get_package_files(pkg.id) if pkg.id else []
        file_list = "\n".join(files[:30]) if files else "(文件列表未载入)"
        self._notify(
            f"{pkg.name} [{pkg.source}]\n版本: {pkg.version}\n"
            f"大小: {format_size(pkg.installed_size)}\n描述: {desc}"
            f"{homepage}{deps}\n\n"
            f"安装文件:\n{file_list}",
            timeout=60,
        )

    def action_toggle_hide(self) -> None:
        pkg = self._get_selected_pkg()
        if pkg is None or pkg.id is None:
            return
        self._do_toggle_hide(pkg)

    def _do_toggle_hide(self, pkg: Package) -> None:
        if pkg.id is None:
            return
        new_hide = not pkg.hide
        self.db.set_package_hidden(pkg.id, new_hide)
        self._notify(f"{'隐藏' if new_hide else '取消隐藏'} {pkg.name}")
        tree = self.query_one("#package-tree", PackageTree)
        anchor = self._get_anchor_name(tree)
        self._rebuild_tree()
        if anchor:
            self._select_node_by_name(tree, anchor)

    def action_move_package(self) -> None:
        pkg = self._get_selected_pkg()
        if pkg is None or pkg.source != "apt" or pkg.id is None:
            return
        self._pending_move = pkg
        target = "系统预装" if pkg.installed_at else "用户安装"
        self._notify(f"将 {pkg.name} 移到「{target}」?  按 y 确认", timeout=10)

    def _do_move_package(self, pkg: Package) -> None:
        """右键菜单直接移动（无需 y 确认）。"""
        if pkg.id is None or pkg.source != "apt":
            return
        new_val = "" if pkg.installed_at else "手动移动"
        self.db.set_package_installed_at(pkg.id, new_val)
        target = "系统预装" if new_val == "" else "用户安装"
        self._notify(f"{pkg.name} → {target}")
        tree = self.query_one("#package-tree", PackageTree)
        anchor = self._get_anchor_name(tree)
        self._rebuild_tree()
        if anchor:
            self._select_node_by_name(tree, anchor)

    def action_confirm_move(self) -> None:
        if self._pending_move is None:
            return
        pkg = self._pending_move
        self._pending_move = None
        self._do_move_package(pkg)

    # ── Events ──────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        search = self.query_one("#search-input", Input)
        if event.value:
            search.add_class("search-active")
            self._search_packages = []  # 退出全屏模式
        else:
            search.remove_class("search-active")
        self._rebuild_tree()
