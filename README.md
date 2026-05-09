# App Manager — 终端里的包管理仪表盘

一个用 Python + Textual 写的 TUI（终端用户界面）工具，把 APT、Snap、Flatpak、Homebrew、AppImage 五类软件聚合到同一个界面里，树形展示「应用 → 依赖」的层级关系。

## 目录

- [快速开始](#快速开始)
- [界面一览](#界面一览)
- [设计思路（教学重点）](#设计思路教学重点)
  - [为什么用 Provider 模式？](#为什么用-provider-模式)
  - [数据流：从系统命令到 SQLite](#数据流从系统命令到-sqlite)
  - [APT 的三类包：用户装、系统预装、.deb 手动装](#apt-的三类包用户装系统预装deb-手动装)
  - [扫描策略：全量 vs 增量](#扫描策略全量-vs-增量)
  - [TUI 是如何工作的](#tui-是如何工作的)
  - [关键性能优化](#关键性能优化)
- [项目结构](#项目结构)
- [优缺点](#优缺点)
- [扩展指南](#扩展指南)

## 快速开始

```bash
# 1. 克隆
git clone git@github.com:rensener/appmanager.git
cd appmanager

# 2. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖（只有一个：textual）
pip install -r requirements.txt

# 4. 运行
python src/main.py             # 增量扫描（推荐，< 1 秒）
python src/main.py --scan      # 全量扫描（重刷所有数据）
```

需要 Python 3.10+。数据库自动创建在 `~/.local/share/app-manager/apps.db`。

## 界面一览

```
┌──────────────────────────────────────────────────────────┐
│ 来源: [全部 ▼]  [ 🔍 搜索...                    ] [退出] │
├──────────────────────────────────────────────────────────┤
│  📦 fcitx5              [apt]   12.3 MB   [▶ 展开] [🗑] │
│  📦 mpv                 [apt]    3.4 MB   [▶ 展开] [🗑] │
│  📦 firefox             [snap] 283.5 MB             [🗑] │
│  📦 OBS Studio         [flatpak]  1.2 GB            [🗑] │
│  📦 MyTool.AppImage    [appimg]  45.0 MB            [🗑] │
│  ⚙ 系统预装 (42)       [apt]             [▶ 展开] [隐藏] │
│    ├── bash                                                │
│    ├── coreutils                                           │
│    └── ...                                                 │
├──────────────────────────────────────────────────────────┤
│ 共 87 个应用 | ↑↓ 选择 | → 展开 | Enter 详情 | d 卸载 | h 隐藏 │
└──────────────────────────────────────────────────────────┘
```

## 设计思路（教学重点）

### 为什么用 Provider 模式？

Linux 上的包管理器五花八门：APT 用 dpkg 数据库 + history.log，Snap 用 `snap list`，Flatpak 用 `flatpak list`，Brew 用 `brew list`，AppImage 甚至没有包管理器——只是文件系统上的 `.AppImage` 文件。

如果每种来源都写一套逻辑混在一起，代码很快就会变成一锅粥。所以采用了 **Provider 模式**（也叫「策略模式」）：每种来源是一个独立的类，都实现同样的接口，核心代码只和接口打交道，不关心具体实现。

```python
# 核心接口（src/providers/__init__.py）
class BaseProvider(ABC):
    name: str                              # "apt" / "snap" / "flatpak" ...

    @staticmethod
    def is_available() -> bool: ...        # 这个包管理器在系统上存在吗？

    def fetch_packages(self) -> list[dict]: ...     # 获取所有包

    def fetch_dependencies(self, name: str) -> list[dict]: ...  # 获取依赖
```

**关键收益**：新增一个来源（比如 pip、cargo、npm），只需新建一个文件，不改任何现有代码。这就是「对扩展开放，对修改关闭」。

### 数据流：从系统命令到 SQLite

整条数据链是这样的：

```
系统命令输出                     Provider.parse()              数据库
─────────────                   ───────────────               ──────
dpkg --get-selections  ───→  {name, version, size, ...}  ───→ packages 表
snap list              ───→  {name, version, size, ...}  ───→ packages 表
brew list              ───→  {name, version, size, ...}  ───→ packages 表
...
                              ↓
                         dependencies 表（父子关系）
                         install_history 表（安装时间线）
```

所有 Provider 返回统一格式的 `list[dict]`，scanner 把它们写入 SQLite。这样 TUI 层只需要读数据库，完全不碰系统命令。

**为什么用 SQLite 而不是 JSON 文件？**
1. 支持 SQL 查询（搜索、过滤、排序）
2. 事务写入（不会写到一半崩溃损坏数据）
3. WAL 模式（读写不互斥）
4. 零配置，Python 内置

### APT 的三类包：用户装、系统预装、.deb 手动装

这是整个项目最精妙的设计。APT 安装的包不能简单地分为「用户装的」和「自动依赖」，实际有三种情况：

**① 用户安装**（`installed_at` 有时间戳）
- 你主动敲过 `apt install xxx`，记录在 `/var/log/apt/history.log` 里
- 在命令行中显式指定，且不是自动标记（automatic）
- 显示在「用户安装」分组

**② 系统预装**（`installed_at` 为空字符串）
- 装机时就在的包（如 bash、coreutils），在 `apt-mark showmanual` 里能找到
- 但从未在 history.log 中出现过
- 显示在「系统预装」分组

**③ .deb 手动安装**（`installed_at` = `"手动安装"`）
- 你下载了 `.deb` 文件，用 `dpkg -i` 或 `apt install ./xxx.deb` 安装
- 在 `apt-mark showmanual` 里，但不在 APT 仓库中（只有本地 dpkg 记录）
- 通过 `apt-cache policy` 判断：如果输出没有 `http://` 行，就是纯本地包
- 显示在「用户安装」分组

**分类流程图**：
```
                       ┌── 在 history.log 中？ ──→ 被显式命名？ ──→ 用户安装
                       │                              └→ 自动依赖 → 不显示为父包
apt-mark showmanual ──┤
   （所有手动包）       │
                       └── 不在 history.log ──→ apt-cache policy 有 http？
                                                      │
                                               有 → 系统预装
                                               无 → .deb 手动安装
```

`is_manual` 字段只表示「是否父包」，真正区分用户/系统的是 `installed_at` 字段。

### 扫描策略：全量 vs 增量

**增量扫描**（默认，`main.py` 不带参数）：
```
dpkg --get-selections（一次调用，< 0.1 秒）
    ↓
与数据库中的包名对比（内存操作）
    ↓
只处理新增的和已删除的包
```

**全量扫描**（`--scan`）：
```
重新解析 history.log + 重新调用所有 Provider
    ↓
清除旧数据，全量写入数据库
```

默认用增量，因为系统包不会频繁变。只有重新分类逻辑改变时才需要 `--scan`。

### TUI 是如何工作的

TUI 基于 [Textual](https://textual.textualize.io/) 框架。你需要理解几个核心概念：

**Widget 树**（类似 HTML DOM）：
```
AppManagerApp (App)
  ├── Input (搜索框)
  ├── ListView (下拉搜索结果预览)
  ├── PackageTree (主树形列表，继承自 Tree)
  │     ├── "APT (1830)" ← 来源节点
  │     │     ├── "用户安装 (387)" ← 分组节点
  │     │     │     ├── fcitx5 ← 包节点（data=Package 对象）
  │     │     │     │     └── libfcitx5core7 ← 依赖节点
  │     │     │     └── mpv
  │     │     └── "系统预装 (1443)"
  │     │           ├── bash
  │     │           └── ...
  │     ├── "SNAP (8)"
  │     └── ...
  │     └── "已隐藏 (3)" ← 隐藏节点始终在最下面
  ├── Label (底部状态栏)
  └── Footer (按键提示栏)
```

**事件处理**：Textual 有「冒泡」机制——按键事件从当前焦点 Widget 向上传递，谁处理了谁调用 `event.stop()`。这就是为什么 `q` 键在搜索框里可以正常输入，在树里却能退出程序。

**搜索的双模式设计**：
1. **下拉预览**（输入 ≥ 2 字时）：`ListView` 弹出前 200 个结果，按 `↓` 进入列表选择
2. **全屏结果**（按 Enter）：清空 Tree，把所有匹配结果显示在树中，可展开看依赖

**`_node_by_name` 缓存**：一个 `{包名: TreeNode}` 的字典，在构建树时填充。用于操作后（隐藏、移动）精准定位回原来的位置，避免光标跳到树顶。

### 关键性能优化

| 优化 | 之前 | 之后 |
|------|------|------|
| 包信息查询 | 逐个 `dpkg -s`（1830 次子进程） | 一次 `dpkg-query -W` |
| 依赖查询 | 逐个 `dpkg -s` | 一次 `dpkg-query -W` 预加载 Depends 字段 |
| 数据库写入 | 逐行提交（每行一次 fsync） | `bulk_write()` 单事务提交 |
| 增量对比 | 重新解析所有日志 | `dpkg --get-selections` + 内存集合对比 |
| APT 包名列表 | 解析 history.log | `dpkg --get-selections` |

这些都是「批量优于逐个」原则的体现——每次启动子进程都有 fork/exec 开销，合并成一次调用可以快几十倍。

## 项目结构

```
app_manager/
├── requirements.txt            # 唯一外部依赖：textual
├── data/schema.sql             # 5 张表的建表 SQL
├── src/
│   ├── main.py                 # 入口：初始化 DB → 扫描 → 启动 TUI
│   ├── db/
│   │   ├── models.py           # 5 个 dataclass：Package, Dependency, ...
│   │   └── database.py         # SQLite CRUD（含 upsert、bulk_write）
│   ├── providers/
│   │   ├── __init__.py         # BaseProvider 抽象类
│   │   ├── apt.py              # APT：解析 history.log + dpkg 查询
│   │   ├── snap.py             # Snap：snap list
│   │   ├── flatpak.py          # Flatpak：flatpak list
│   │   ├── brew.py             # Homebrew：brew list + brew deps
│   │   └── appimage.py         # AppImage：文件系统扫描
│   ├── core/
│   │   ├── scanner.py          # 全量/增量扫描，数据入库
│   │   └── matcher.py          # 平铺包列表 → 依赖树
│   ├── tui/
│   │   └── app.py              # Textual 主应用（Widget、事件、菜单）
│   └── utils/
│       ├── dpkg_utils.py       # dpkg 命令封装（批量查询优化）
│       └── format_utils.py     # 大小格式化（KB/MB/GB）
└── tests/
    ├── test_db.py
    ├── test_matcher.py
    ├── test_apt_parser.py
    └── fixtures/sample_history.log
```

## 优缺点

**优点：**
- **多源统一**：一个界面看 APT、Snap、Flatpak、Brew、AppImage，不用分别敲命令
- **依赖可视化**：树形展开每个应用的依赖，一眼看出「这个包拉了哪些东西进来」
- **APT 三类区分**：用户安装 / 系统预装 / .deb 手动装，清理系统时有参考价值
- **增量扫描极快**：默认 < 1 秒，一次 `dpkg --get-selections` 替代逐个查询
- **安装历史可追溯**：从 `history.log` 解析出谁在什么时候装了/卸了什么
- **纯 Python + SQLite**：无外部服务，数据库约 2MB，完全离线可用
- **Provider 架构易扩展**：新增包来源只需一个文件，现有代码不用改
- **键盘优先**：搜索、展开、隐藏、卸载全程键盘操作

**局限：**
- APT 之外的来源（Snap/Flatpak/Brew/AppImage）只有当前快照，没有安装历史和依赖详情
- 卸载目前仅 APT 可用，其他来源待接
- 部分老旧终端（不支持 256 色）上 Textual 渲染可能不完整
- 卸载操作需要 sudo 权限

## 扩展指南

新增包来源只需要 3 步，以「pip 包」为例：

**1. 新建 `src/providers/pip.py`**：
```python
class PipProvider(BaseProvider):
    name = "pip"

    @staticmethod
    def is_available():
        return shutil.which("pip") is not None

    def fetch_packages(self) -> list[dict]:
        # 调用 pip list --format=json，解析后返回统一格式
        ...

    def fetch_dependencies(self, name: str) -> list[dict]:
        # 调用 pip show <name>，解析 Requires 字段
        ...
```

**2. 在 `data/schema.sql` 的 CHECK 约束里加上 `'pip'`**

**3. 在 `src/main.py` 里注册**：
```python
from src.providers.pip import PipProvider
providers = [AptProvider(), ..., PipProvider()]
```

完成。Provider 接口保证了核心代码完全不需要改动。
