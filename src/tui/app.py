"""Textual TUI 主应用 — 包管理器终端界面。

============================================================
Textual 框架基础
============================================================

Textual 是一个 Python TUI 框架，概念类似 Web 前端：

  App     ≈ 浏览器窗口（应用本身）
  Screen  ≈ 页面（模态弹窗）
  Widget  ≈ HTML 元素（Tree、Input、Label 等）
  CSS     ≈ 布局和样式（dock、height、background 等）
  Binding ≈ 键盘快捷键绑定
  events  ≈ 事件系统（按键、鼠标点击等）

Widget 树（本应用的嵌套结构）：
  AppManagerApp (App)
    ├── Input       — 搜索框
    ├── ListView    — 下拉搜索结果预览
    ├── PackageTree — 主树形列表（继承 Tree）
    │     ├── APT (source node)
    │     │     ├── 用户安装 (group node)
    │     │     │     ├── fcitx5 (package node, data=Package对象)
    │     │     │     │     └── libfcitx5core7 (dependency node)
    │     │     │     └── mpv
    │     │     └── 系统预装 (group node)
    │     │           ├── bash
    │     │           └── ...
    │     ├── SNAP (source node)
    │     └── ...
    │     └── 已隐藏 (hidden node, always last)
    ├── Label       — 底部状态栏
    └── Footer      — 按键提示栏

每个树节点可以携带 data 属性，我们用它存储 Package 对象。

============================================================
事件处理机制
============================================================

Textual 的事件有「冒泡」机制：事件从当前焦点 Widget 向上传递，
直到某个父级处理了它（调用 event.stop()）。

例如：q 键的智能判断
  - 搜索框有焦点 → q 不拦截（用户可以输入 "q"）
  - 树有焦点 → q 退出程序

这是通过在 on_key() 中检查 has_focus 实现的。

============================================================
搜索的双模式设计
============================================================

模式 1 — 下拉预览（输入 ≥ 2 字时）：
  ListView 弹出前 200 个匹配结果。
  按键：↓ 进入列表 | Enter 切换到全屏 | ESC 回到搜索框

模式 2 — 全屏结果（按 Enter 后）：
  清空 Tree，把所有匹配结果显示在树中。
  按键：↑↓ 选择 | Enter 跳转到树中位置 | q/ESC 回到搜索框

双模式的存在理由：
  下拉预览快但信息少（只显示包名和来源），适合快速浏览。
  全屏结果可以展开依赖树，信息完整但渲染慢。

============================================================
选中恢复机制（_node_by_name + _select_node_by_name）
============================================================

问题：操作（隐藏、移动）后树被重建（_rebuild_tree），光标跳到顶部。
解决：
  1. 构建树时，用 _node_by_name 缓存 {包名: TreeNode}
  2. 操作前记录「锚点」包名（_get_anchor_name）
  3. 重建后调用 _select_node_by_name(anchor) 恢复位置
  4. 如果 Tree 还没完成布局，select_node 会失效 → 定时器重试 5 次

这个 retry 机制是关键：Textual 的 Tree.select_node() 需要在
layout 完成后才能生效，而 _rebuild_tree() 后 layout 是异步的。
"""

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
    """自定义包管理树控件 — 继承 Textual 的 Tree，添加左右键和右键菜单。

    为什么自定义 Tree 而不是直接用？
      需要定制行为：
        - 右键展开节点（默认 Tree 不支持）
        - 左键折叠 / 回到父节点（默认 Tree 不支持导航到父节点）
        - 右键弹出菜单（默认 Tree 不支持）

    BINDINGS 定义了控件的快捷键。show=False 表示不在 Footer 中显示。
    """

    BINDINGS = [
        Binding("right", "expand_node", "展开", show=False),
        Binding("left", "collapse_node", "折叠", show=False),
    ]

    def action_expand_node(self) -> None:
        """右键 → 展开当前节点（展开其子依赖）。"""
        node = self.cursor_node
        if node is not None and not node.is_expanded:
            node.expand()

    def action_collapse_node(self) -> None:
        """左键 → 折叠或返回父节点。

        两段式逻辑（类似文件管理器）：
          1. 如果当前节点已展开 → 折叠它
          2. 如果当前节点已折叠（或无子节点） → 跳到父节点
        """
        node = self.cursor_node
        if node is not None:
            if node.is_expanded:
                node.collapse()
            elif node.parent is not None:
                # 跳到父节点（无限往回退）
                self.select_node(node.parent)

    def on_click(self, event: events.Click) -> None:
        """鼠标点击事件 → 右键（button==3）弹出上下文菜单。

        Textual 的 Click 事件包含 button 属性：
          1 = 左键
          2 = 中键
          3 = 右键

        只有点击在包节点上（data 是 Package 对象）才弹出菜单。
        """
        if event.button == 3 and self.cursor_node is not None:
            node = self.cursor_node
            if node.data and isinstance(node.data, Package):
                event.stop()  # 阻止事件冒泡（不让其他控件处理）
                self.app.push_screen(ContextMenu(node.data, self.app))


class ContextMenu(Screen):
    """右键上下文菜单 — 用 Screen 实现模态弹窗。

    为什么用 Screen 而不是 Widget？
      Screen 可以覆盖在 App 之上，自动处理 ESC 关闭、背景遮罩。
      push_screen() 压入一个新 Screen，dismiss() 返回上一个。

    菜单项说明：
      - 详情：查看包详情（文件列表 + dpkg -s 信息）
      - 隐藏/取消隐藏：切换隐藏状态
      - 移动到 用户安装/系统预装（双击）：APT 包的分类切换
        - 双击是因为这个操作不可逆（改变了包的分组）
        - 600ms 内的两次点击才触发
      - 取消：关闭菜单，不做任何操作
    """

    def __init__(self, pkg: Package, app: 'AppManagerApp'):
        super().__init__()
        self._pkg = pkg
        self._app = app
        # 如果当前在用户安装 → 目标显示「系统预装」；反之亦然
        target = "系统预装" if pkg.installed_at else "用户安装"
        self._items = [
            ("详情", "show_detail", False),
            ("隐藏" if not pkg.hide else "取消隐藏", "toggle_hide", False),
            (f"移动到 {target}（双击）", "move_package", True),
            ("取消", "cancel", False),
        ]
        # 双击检测：记录上次点击的 (索引, 时间戳)
        self._last_click = (None, 0.0)

    def compose(self) -> ComposeResult:
        """构建菜单的 Widget 树。

        Static 是纯文本标签（不可交互），作为菜单标题。
        ListView 是菜单项列表（可键盘导航）。
        """
        yield Static(f" {self._pkg.name} [{self._pkg.source}]", id="menu-title")
        yield ListView(
            *[ListItem(Label(f" {label}")) for label, _, _ in self._items],
            id="menu-list",
        )

    def on_key(self, event: events.Key) -> None:
        """键盘操作菜单。

        ESC → 关闭菜单（dismiss）
        Enter → 执行当前选中的菜单项
        """
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
        """鼠标点击菜单项 → 执行对应操作。

        对于需要双击的项（移动），用 time.monotonic() 判断：
          同一项在 600ms 内点击两次 → 执行
          否则 → 记录本次点击，等待下次
        """
        if event.item is not None:
            idx = self.query_one("#menu-list", ListView).index
            if idx is not None and idx < len(self._items):
                _, _, need_double = self._items[idx]
                if need_double:
                    now = time.monotonic()
                    prev_idx, prev_time = self._last_click
                    if prev_idx == idx and now - prev_time < 0.6:
                        # 600ms 内双击 → 执行
                        self._execute(idx)
                    else:
                        # 第一次点击，记录时间
                        self._last_click = (idx, now)
                else:
                    # 不需要双击的项，直接执行
                    self._execute(idx)

    def _execute(self, idx: int) -> None:
        """执行菜单项对应的操作，然后关闭菜单。"""
        _, action, _ = self._items[idx]
        self.dismiss()  # 先关闭菜单（返回主界面）
        if action == "cancel":
            return
        if action == "show_detail":
            self._app._do_show_detail(self._pkg)
        elif action == "toggle_hide":
            self._app._do_toggle_hide(self._pkg)
        elif action == "move_package":
            self._app._do_move_package(self._pkg)


class AppManagerApp(App):
    """包管理器主应用。

    这是整个 TUI 的核心。继承 Textual 的 App 类，管理：
      - compose(): 构建 Widget 树
      - on_mount(): 应用启动后的初始化
      - on_key(): 全局键盘事件处理
      - action_*(): 各绑定的快捷键响应
      - _do_*(): 实际执行逻辑（被 action 和右键菜单共用）
    """

    # CSS 样式定义
    # Textual 的 CSS 类似 Web CSS 的子集。
    # 关键概念：
    #   dock: top    — 固定在顶部（类似 position: fixed）
    #   height: 1fr  — 占满剩余空间（类似 flex: 1）
    #   #id          — ID 选择器（类似 #id）
    #   Class        — 类选择器（如 ContextMenu）
    CSS = """
    #search-input {
        dock: top;              /* 搜索框固定在顶部 */
        margin-bottom: 1;
    }
    #search-input:focus {
        border: solid $accent;  /* 搜索框获焦时高亮边框 */
    }
    #search-input.search-active {
        border: solid $warning; /* 搜索有内容时黄色边框 */
    }
    #search-results {
        display: none;          /* 默认隐藏下拉列表 */
        border: solid $warning;
        background: #202020;
    }
    #search-results.active {
        display: block;         /* 有搜索结果时显示 */
        height: auto;
        max-height: 10;         /* 最多显示 10 行 */
    }
    #search-results:focus {
        border: solid $accent;
    }
    #package-tree {
        height: 1fr;            /* 占满搜索框之外的所有空间 */
    }
    #status-bar {
        height: 1;              /* 只有 1 行高 */
        background: #004080;    /* 深蓝底 */
        color: #ffffff;         /* 白字 */
        padding: 0 1;
    }
    ContextMenu {
        align: center middle;    /* 菜单居中显示 */
    }
    #menu-title {
        height: 1;
        padding: 0 1;
        background: #004080;
        color: #ffffff;
        width: 40;              /* 固定宽度 */
    }
    #menu-list {
        width: 40;
        height: auto;
        background: #202020;
        border: solid #004080;
    }
    """

    # 全局快捷键绑定
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
        self._pending_move: Package | None = None  # m/y 两步移动的暂存
        self._search_packages: list[Package] = []  # 全屏搜索结果
        self._search_preview: list[Package] = []   # 下拉预览结果
        self._node_by_name: dict[str, object] = {} # {包名: TreeNode} 缓存

    def compose(self) -> ComposeResult:
        """构建 Widget 树。Textual 在 App 启动时调用此方法。

        yield 的每个 Widget 按顺序渲染（从上到下）。
        """
        yield Input(placeholder="/ 搜索包名...  ESC 退出搜索", id="search-input")
        yield ListView(id="search-results")
        yield PackageTree("包管理器", id="package-tree")
        yield Label("加载中...", id="status-bar")
        yield Footer()  # 底部快捷键提示栏

    def on_mount(self) -> None:
        """应用启动后调用 — 初始化数据并渲染树。"""
        tree = self.query_one("#package-tree", PackageTree)
        tree.show_root = False  # 隐藏树的根节点（"包管理器"文字不显示）
        tree.focus()
        self._rebuild_tree()
        self._update_status()

    # ═══════════════════════════════════════════════════════
    # 树的重建（核心渲染逻辑）
    # ═══════════════════════════════════════════════════════

    def _rebuild_tree(self) -> None:
        """重建整个树形列表。

        这是 TUI 中最频繁调用的方法。三种模式：

        1. 全屏搜索结果模式（_search_packages 不为空 且 搜索框为空）
           → 清空树，显示所有搜索结果（可展开看依赖）

        2. 下拉预览模式（搜索框有内容 ≥ 2 字）
           → 不清树，只更新 ListView（下拉结果预览）

        3. 正常模式（_search_packages 为空 且 搜索框为空）
           → 按来源 → 分组 → 包 → 依赖 的层级构建完整树
           → 底部追加「已隐藏」节点
        """
        tree = self.query_one("#package-tree", PackageTree)
        results = self.query_one("#search-results", ListView)
        search = self.query_one("#search-input", Input).value.strip()

        # ── 模式 1: 全屏搜索结果 ──────────────────────
        if self._search_packages and not search:
            tree.clear()
            results.clear()
            results.remove_class("active")
            for p in self._search_packages:
                tree.root.add(
                    self._package_label(p, show_source=True),
                    data=p, expand=False,
                )
            self._update_status(
                f"搜索 — {len(self._search_packages)} 个结果 | ↑↓选择 Enter 跳转  ESC 退出"
            )
            tree.focus()
            return

        # ── 模式 2: 下拉预览 ──────────────────────────
        if search:
            if len(search) >= 2:
                pkgs = self.db.search_packages(search)
                count = len(pkgs)
                self._search_preview = pkgs[:200]  # 最多显示 200 条预览
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
                # 少于 2 字 → 不触发搜索，清空预览
                results.clear()
                results.remove_class("active")
                self._search_preview = []
            return

        # ── 模式 3: 正常树 ─────────────────────────────
        results.clear()
        results.remove_class("active")
        self._search_packages = []
        self._search_preview = []
        self._node_by_name = {}
        tree.clear()

        # 遍历所有来源构建树
        all_sources = ["apt", "snap", "flatpak", "brew", "appimage"]

        for src in all_sources:
            parents = self.db.get_parent_packages(src)
            src_total = self.db.get_package_count(src)
            # 来源节点：如 "APT (1830)"
            src_node = tree.root.add(
                f"{src.upper()} ({src_total})",
                expand=src in ("apt", "snap"),  # APT 和 Snap 默认展开
            )

            if not parents:
                continue

            # APT 特殊处理：按 installed_at 分成两组
            if src == "apt":
                # installed_at 非空 = 用户安装 / .deb 手动安装
                user_pkgs = [p for p in parents if p.installed_at]
                # installed_at 为空 = 系统预装
                system_pkgs = [p for p in parents if not p.installed_at]

                if user_pkgs:
                    group = src_node.add(f"用户安装 ({len(user_pkgs)})", expand=False)
                    self._add_packages(group, user_pkgs)

                if system_pkgs:
                    group = src_node.add(f"系统预装 ({len(system_pkgs)})", expand=False)
                    self._add_packages(group, system_pkgs)
            else:
                # 其他来源没有分组，直接列出所有父包
                self._add_packages(src_node, parents)

        # 底部追加「已隐藏」节点（始终展开以便查看）
        hidden = self.db.get_hidden_packages()
        if hidden:
            hidden_node = tree.root.add(f"已隐藏 ({len(hidden)})", expand=True)
            for p in hidden:
                hn = hidden_node.add(
                    self._package_label(p, show_source=True),
                    data=p, expand=False,
                )
                self._node_by_name[p.name] = hn

        self._update_status()

    # ═══════════════════════════════════════════════════════
    # 键盘事件处理
    # ═══════════════════════════════════════════════════════

    def on_key(self, event: events.Key) -> None:
        """全局按键处理器。

        Textual 的事件传递链：
          Widget.on_key() → 冒泡 → App.on_key()
          如果中间任何地方调了 event.stop()，停止冒泡。

        所以这里处理的是「没有被 Widget 消费的按键」。
        """
        search_input = self.query_one("#search-input", Input)
        results = self.query_one("#search-results", ListView)
        tree = self.query_one("#package-tree", PackageTree)

        # ── q 键退出（智能判断） ──────────────────────
        if event.key == "q":
            # 搜索框有焦点 → 不拦截，让用户输入 "q"
            if search_input.has_focus:
                return

            event.stop()

            # 全屏搜索结果模式 → 按 q 回到搜索框
            if self._search_packages and not search_input.value.strip():
                self._search_packages = []
                search_input.focus()
                return

            # 搜索框有内容但没焦点 → 清除搜索，恢复正常树
            if search_input.value.strip():
                search_input.value = ""
                search_input.remove_class("search-active")
                self._search_packages = []
                self._search_preview = []
                self._rebuild_tree()
                tree.focus()
                return

            # 正常模式 → 退出程序
            self.exit()
            return

        # ── 全屏结果模式 → Enter 跳转到树中 ──────────
        if self._search_packages and not search_input.value.strip() and tree.has_focus:
            if event.key == "enter":
                event.stop()
                pkg = self._get_selected_pkg()
                if pkg:
                    target_name = pkg.name
                    # 清空搜索结果，重建正常树
                    self._search_packages = []
                    self._rebuild_tree()
                    # 跳转到目标包的位置
                    self._select_node_by_name(tree, target_name)
                    tree.focus()
                return

        # ── 搜索框有焦点 + 下拉有结果 ──────────────────
        if search_input.has_focus and self._search_preview:
            if event.key == "down":
                # ↓ → 进入下拉列表
                event.stop()
                results.focus()
                return
            if event.key == "enter":
                # Enter → 切换到全屏结果模式
                event.stop()
                self._show_full_results(search_input.value.strip())
                return

        # ── 下拉列表有焦点 ────────────────────────────
        if results.has_focus and self._search_preview:
            if event.key == "enter":
                # Enter → 选中当前项并跳转到树中
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
                # ESC → 回到搜索框
                event.stop()
                search_input.focus()
                return

    # ═══════════════════════════════════════════════════════
    # 工具方法
    # ═══════════════════════════════════════════════════════

    def _show_full_results(self, search: str) -> None:
        """切换到全屏搜索结果模式。

        清空搜索框，把所有匹配结果显示在树中（替代正常树）。
        用户可以浏览结果并展开依赖。
        """
        pkgs = self.db.search_packages(search)
        self._search_packages = pkgs
        self._search_preview = []
        search_input = self.query_one("#search-input", Input)
        search_input.value = ""
        search_input.remove_class("search-active")
        self._rebuild_tree()

    def _select_node_by_name(self, tree: PackageTree, name: str, retries: int = 5) -> None:
        """选中指定包名的树节点（用于操作后恢复光标位置）。

        问题：_rebuild_tree() 后，Textual 的 Tree 可能还没完成内部布局。
             此时 select_node() 静默失败（cursor_node 不会改变）。
        解决：用 set_timer 重试，每 50ms 检查一次，
             直到 cursor_node == 目标节点（成功）或重试用完（放弃）。

        Args:
            tree: PackageTree 实例
            name: 要跳转到的包名
            retries: 剩余重试次数（内部递归用）
        """
        node = self._node_by_name.get(name)
        if node is None:
            return

        # 先展开目标节点的所有祖先（否则 select_node 可能失效）
        parent = node.parent
        while parent is not None:
            if not parent.is_expanded:
                parent.expand()
            parent = parent.parent

        tree.select_node(node)
        tree.scroll_to_node(node)

        # 检查选中是否成功
        if tree.cursor_node is not node and retries > 0:
            # 未成功 → 50ms 后重试（set_timer 延迟执行 lambda）
            self.set_timer(
                0.05,
                lambda: self._select_node_by_name(tree, name, retries - 1),
            )

    def _get_anchor_name(self, tree: PackageTree) -> str | None:
        """获取光标当前位置的「锚点」包名。

        用于操作前记录位置，操作后恢复到附近节点。
        返回当前选中包的下一个包名（如果当前是最后一个，返回上一个）。
        这样隐藏/移动后光标会移到相邻的包，而不是回到顶部。
        """
        # 收集树中所有 Package 节点的名称（按深度优先顺序）
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
            # 优先返回下一个包名
            if idx + 1 < len(all_names):
                return all_names[idx + 1]
            # 当前是最后一个 → 返回上一个
            if idx > 0:
                return all_names[idx - 1]
        except ValueError:
            pass
        return None

    def _add_packages(self, parent, packages: list[Package]) -> None:
        """向树节点添加一组包（含依赖树）。

        流程：
          1. 获取所有包的依赖关系
          2. 调用 build_trees() 组装成树形结构
          3. 递归添加每个 PackageNode 到树中
        """
        # 批量获取依赖关系
        all_deps: dict[int, list[tuple[Package, bool]]] = {}
        for pkg in packages:
            if pkg.id:
                all_deps[pkg.id] = self.db.get_dependencies(pkg.id)

        trees = build_trees(packages, all_deps)
        for node in trees:
            self._add_node(parent, node)

    def _add_node(self, parent, node: PackageNode) -> None:
        """递归添加一个 PackageNode 到树中。

        同时将包名 → TreeNode 的映射存入 _node_by_name 缓存，
        用于后续的 _select_node_by_name 定位。
        """
        label = self._package_label(node.package, node.is_shared)
        tree_node = parent.add(label, data=node.package, expand=False)
        # 缓存映射（对于共享依赖，后面的覆盖前面的，但影响不大）
        self._node_by_name[node.package.name] = tree_node
        for child in node.children:
            self._add_node(tree_node, child)

    def _package_label(self, pkg: Package, is_shared: bool = False, show_source: bool = False) -> str:
        """生成包节点的显示文本。

        格式：
          正常模式:  "fcitx5  12.3 MB"
          搜索模式:  "fcitx5  [apt]  12.3 MB"
          共享依赖:  "libc6  1.2 MB (共享依赖)"
        """
        size = format_size(pkg.installed_size)
        if show_source:
            label = f"{pkg.name}  [{pkg.source}]  {size}" if size else f"{pkg.name}  [{pkg.source}]"
        else:
            label = f"{pkg.name}  {size}" if size else pkg.name
        if is_shared:
            label = f"{label} (共享依赖)"
        return label

    def _notify(self, message: str, **kwargs) -> None:
        """显示通知 — 先清除旧通知再显示新的。

        包装 self.notify()（Textual 的内置通知方法），
        防止多个通知堆积在屏幕上。
        """
        self.clear_notifications()
        self.notify(message, **kwargs)

    def _update_status(self, extra: str = "") -> None:
        """更新底部状态栏。"""
        status = self.query_one("#status-bar", Label)
        if extra:
            status.update(extra)
            return
        total = self.db.get_package_count()
        status.update(
            f"共 {total} 个包 | ↑↓选择 →展开 ←折叠 s详情 m移动 h隐藏 /搜索"
        )

    def _get_selected_pkg(self) -> Package | None:
        """获取当前树中选中的 Package 对象。"""
        tree = self.query_one("#package-tree", PackageTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return None
        # 确保 data 是 Package 对象（分组节点没有 data）
        return node.data if isinstance(node.data, Package) else None

    # ═══════════════════════════════════════════════════════
    # 用户操作（action_* 由 BINDINGS 触发 / _do_* 被右键菜单复用）
    # ═══════════════════════════════════════════════════════

    def action_focus_search(self) -> None:
        """/ 键 → 聚焦搜索框。"""
        self.query_one("#search-input", Input).focus()

    def action_clear_search(self) -> None:
        """ESC 键 → 退出搜索模式。

        三种情况：
          1. 全屏结果 → 回到搜索框
          2. 下拉预览 → 清空搜索
          3. 正常模式 → 已经是正常模式，不做任何事
        """
        search = self.query_one("#search-input", Input)
        if self._search_packages and not search.value.strip():
            self._search_packages = []
            search.focus()
            return
        search.value = ""
        search.remove_class("search-active")
        self._search_packages = []
        self._search_preview = []
        self._rebuild_tree()
        self.query_one("#package-tree", PackageTree).focus()

    def action_show_detail(self) -> None:
        """s 键 → 查看当前选中包的详情。"""
        pkg = self._get_selected_pkg()
        if pkg is None:
            return
        self._do_show_detail(pkg)

    def _do_show_detail(self, pkg: Package) -> None:
        """显示包详情通知。

        对 APT 包：额外调用 dpkg -s 获取更详细的信息：
          - 完整 Description（可能多行）
          - Depends（依赖列表）
          - Recommends（推荐列表）
          - Homepage（项目主页）

        对非 APT 包：只显示数据库中的基本信息。

        文件列表从 package_files 表获取（扫描时已存入）。
        """
        deps = ""
        desc = pkg.description
        homepage = ""

        # APT 包：用 dpkg -s 获取完整元数据
        if pkg.source == "apt":
            try:
                out = subprocess.check_output(
                    ["dpkg", "-s", pkg.name], stderr=subprocess.DEVNULL, text=True,
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
                        # dpkg -s 的描述是多行的，后续行以空格开头
                        desc_lines.append(stripped)
                    elif in_desc and not line.startswith(" "):
                        # 遇到非空格开头 → 描述结束
                        in_desc = False
                if desc_lines:
                    desc = " ".join(desc_lines)
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass

        # 文件列表（最多显示 30 个）
        files = self.db.get_package_files(pkg.id) if pkg.id else []
        file_list = "\n".join(
            files[:30]
        ) if files else "(文件列表未载入)"

        # 用 Textual 的 notify 显示信息（弹窗形式）
        self._notify(
            f"{pkg.name} [{pkg.source}]\n"
            f"版本: {pkg.version}\n"
            f"大小: {format_size(pkg.installed_size)}\n"
            f"描述: {desc}"
            f"{homepage}{deps}\n\n"
            f"安装文件:\n{file_list}",
            timeout=60,  # 60 秒后自动消失
        )

    def action_toggle_hide(self) -> None:
        """h 键 → 隐藏/取消隐藏当前包。"""
        pkg = self._get_selected_pkg()
        if pkg is None or pkg.id is None:
            return
        self._do_toggle_hide(pkg)

    def _do_toggle_hide(self, pkg: Package) -> None:
        """执行隐藏/取消隐藏操作。

        隐藏后重建树，光标移到相邻包（保持位置感）。
        """
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
        """m 键 → 标记当前包为「待移动」状态，等待 y 确认。

        两步操作设计（m → y）：
          移动是一个不可逆的操作（改变包的分组），
          所以用两步确认防止误触。
        """
        pkg = self._get_selected_pkg()
        if pkg is None or pkg.source != "apt" or pkg.id is None:
            return  # 只支持 APT 包移动
        self._pending_move = pkg
        target = "系统预装" if pkg.installed_at else "用户安装"
        self._notify(f"将 {pkg.name} 移到「{target}」?  按 y 确认", timeout=10)

    def _do_move_package(self, pkg: Package) -> None:
        """执行移动操作（右键菜单直接触发，无需 y 确认）。

        移动原理：
          改变 installed_at 字段来切换分组：
            - 移到系统预装：installed_at = ""（空 = 系统预装）
            - 移到用户安装：installed_at = "手动移动"（非空 = 用户安装）
        """
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
        """y 键 → 确认移动（配合 action_move_package 使用）。"""
        if self._pending_move is None:
            return
        pkg = self._pending_move
        self._pending_move = None
        self._do_move_package(pkg)

    # ═══════════════════════════════════════════════════════
    # 输入事件
    # ═══════════════════════════════════════════════════════

    def on_input_changed(self, event: Input.Changed) -> None:
        """搜索框内容变化事件。

        每次输入都重建树/更新预览（实时搜索）。
        输入 ≥ 2 字 → 触发下拉预览
        清空输入 → 恢复正常树
        """
        search = self.query_one("#search-input", Input)
        if event.value:
            search.add_class("search-active")  # 黄色边框
            self._search_packages = []  # 退出全屏模式
        else:
            search.remove_class("search-active")
        self._rebuild_tree()
