"""数据结构定义 — 对应数据库中的 5 张表。

本文件使用 Python 的 @dataclass（数据类）来定义数据结构。
dataclass 会自动生成 __init__、__repr__、__eq__ 等方法，
让我们可以专注于「这个结构体里有什么数据」，不用写样板代码。

重点概念：
  @dataclass 是 Python 3.7 引入的装饰器，标记一个类为「纯数据容器」。
  对比普通类：普通类需要手动写 __init__(self, name, source, ...)，
  dataclass 只要声明字段及类型，__init__ 自动生成。

为什么有些字段有默认值？
  这些是在创建对象时可以省略的字段。比如刚解析出来的包可能没有 version，
  后续通过 dpkg -s 补充，所以给空字符串 "" 作为默认值。
"""

from dataclasses import dataclass, field


@dataclass
class Package:
    """一个软件包，可以来自任意来源（apt / snap / flatpak / brew / appimage）。

    这是整个系统最核心的数据结构。TUI 中的每一行、树中的每个节点，
    最终都对应一个 Package 实例。

    字段说明：
      id            — 数据库主键。创建新对象时还不知道（None），插入后数据库分配。
      name          — 包名，如 "fcitx5"、"firefox"
      source        — 来源，决定在哪个来源节点下显示
      version       — 版本号字符串
      installed_size — 已安装大小，单位 KB（1024 字节）
      description   — 简短描述
      is_manual     — True=父包（用户主动安装），False=自动依赖
      hide          — True=用户手动隐藏了此项
      category      — 用户自定义标签（预留功能，当前未使用）
      installed_at  — 安装时间，ISO8601 格式。
                       区分三类 APT 包的关键字段：
                         • "2026-05-01T14:00:00" → 用户安装（有时间戳）
                         • ""                    → 系统预装
                         • "手动安装"            → .deb 文件安装
    """
    name: str
    source: str
    version: str = ""
    installed_size: int = 0
    description: str = ""
    is_manual: bool = True
    hide: bool = False
    category: str = ""
    installed_at: str = ""
    # id 放在最后，因为它是可选的（None 表示还未入库）
    id: int | None = None


@dataclass
class Dependency:
    """父子包之间的依赖关系。

    例如：fcitx5（父包）依赖 libfcitx5core7（子包）。
    这条记录就存储了这种关系。

    字段说明：
      parent_id    — 父包在 packages 表中的 id
      child_id     — 子包（依赖）在 packages 表中的 id
      is_automatic — True=自动拉入的依赖（如 apt 的 automatic 标记），
                     False=显式安装的依赖
    """
    parent_id: int
    child_id: int
    is_automatic: bool = True
    id: int | None = None


@dataclass
class PackageFile:
    """包安装的文件路径。

    例如 fcitx5 包安装了 /usr/bin/fcitx5、/usr/share/fcitx5/... 等文件。
    通过 dpkg -L <包名> 获取，存储在 package_files 表中。

    为什么存这些？
      在包详情页展示「这个包到底装了哪些文件」。
      不存储的话，每次查看详情都要重新调 dpkg -L。
    """
    package_id: int
    file_path: str
    id: int | None = None


@dataclass
class InstallHistory:
    """一次安装/卸载操作的记录。

    APT 的 /var/log/apt/history.log 记录了每次操作：
      谁在什么时候执行了什么命令，装/卸了哪些包。

    例如：
      2026-05-01 14:00, rensen, apt install fcitx5, install

    字段说明：
      timestamp — 操作时间，ISO8601
      source    — "apt"（目前仅 APT 有历史记录）
      command   — 完整命令行，如 "apt install fcitx5"
      operation — "install" / "remove" / "purge" / "upgrade"
      user      — 执行者用户名
    """
    timestamp: str
    source: str
    command: str = ""
    operation: str = "install"
    user: str = ""
    id: int | None = None


@dataclass
class HistoryPackage:
    """安装记录中涉及的包。

    一条 InstallHistory 可能涉及多个包（主包 + 依赖），
    这个表把「历史记录」和「包」关联起来。

    例如：
      一次 apt install fcitx5 产生了：
        - history_packages(history_id=1, package_id=fcitx5, is_parent=True)
        - history_packages(history_id=1, package_id=libfcitx5core7, is_parent=False)

    字段说明：
      history_id   — 对应 install_history 表中的记录
      package_id   — 对应 packages 表中的包
      is_parent    — True=主包，False=依赖
      is_automatic — True=自动拉入
      version      — 当时安装的版本（可能与当前版本不同）
    """
    history_id: int
    package_id: int
    is_parent: bool = False
    is_automatic: bool = True
    version: str = ""
    id: int | None = None
