"""Provider 基类 — 定义所有包来源必须实现的接口。"""

from abc import ABC, abstractmethod


class BaseProvider(ABC):
    """每种包管理器实现这个抽象类。"""

    # 子类必须设置
    name: str = ""

    @staticmethod
    @abstractmethod
    def is_available() -> bool:
        """检查这个包管理器是否在系统上可用。"""
        ...

    @abstractmethod
    def fetch_packages(self) -> list[dict]:
        """获取所有包的基本信息列表。

        每个 dict 的字段：
        - name: str
        - version: str
        - installed_size: int (KB)
        - description: str
        - is_manual: bool (是否用户主动安装的父包)
        - installed_at: str (ISO8601)

        返回的列表中，每个包都应该有这些字段。
        对于没有父子概念的管理器（snap/appimage），is_manual 全部为 True。
        """
        ...

    @abstractmethod
    def fetch_dependencies(self, pkg_name: str) -> list[dict]:
        """获取某个包的依赖列表。

        每个 dict 的字段：
        - name: str
        - version: str
        - is_automatic: bool

        对于没有依赖的管理器（snap/appimage），返回空列表。
        """
        ...
