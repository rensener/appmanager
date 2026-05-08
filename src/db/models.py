"""数据结构定义 — 对应数据库中的 5 张表。"""

from dataclasses import dataclass, field


@dataclass
class Package:
    """一个软件包，来自任意来源（apt/snap/flatpak/brew/appimage）。"""
    name: str
    source: str
    version: str = ""
    installed_size: int = 0
    description: str = ""
    is_manual: bool = True
    hide: bool = False
    category: str = ""
    installed_at: str = ""
    id: int | None = None


@dataclass
class Dependency:
    """父子包之间的依赖关系。"""
    parent_id: int
    child_id: int
    is_automatic: bool = True
    id: int | None = None


@dataclass
class PackageFile:
    """包安装的文件路径。"""
    package_id: int
    file_path: str
    id: int | None = None


@dataclass
class InstallHistory:
    """一次安装操作的记录。"""
    timestamp: str
    source: str
    command: str = ""
    operation: str = "install"
    user: str = ""
    id: int | None = None


@dataclass
class HistoryPackage:
    """安装记录中涉及的包。"""
    history_id: int
    package_id: int
    is_parent: bool = False
    is_automatic: bool = True
    version: str = ""
    id: int | None = None
