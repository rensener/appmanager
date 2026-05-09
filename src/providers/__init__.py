"""Provider 基类 — 定义所有包来源必须实现的接口。

============================================================
Provider 模式（策略模式）
============================================================

Provider 模式的核心思想：「面向接口编程，而非面向实现」。

这个文件定义了 BaseProvider 抽象类，所有包管理器都必须实现它。
上层代码（scanner.py、TUI）只需要和 BaseProvider 交互，
不需要知道具体是 APT 还是 Snap 还是 Flatpak。

用抽象基类（ABC）的好处：
  1. 强制子类实现所有 @abstractmethod —— 忘了实现会直接报错，不会悄悄出 bug
  2. 文档化接口约定 —— 看这个文件就知道 Provider 需要什么方法
  3. isinstance(p, BaseProvider) 可以统一判断

为什么 fetch_packages() 返回 list[dict] 而不是 list[Package]？
  解耦：Provider 层不应该知道数据库 model 的存在。
  它只负责「从系统获取数据」，返回纯字典。
  数据转换（dict → Package → SQL）是 scanner.py 的职责。

新增一个包来源的步骤：
  1. 新建 providers/xxx.py，继承 BaseProvider
  2. 实现 is_available()、fetch_packages()、fetch_dependencies()
  3. 在 main.py 中注册
  4. 搞定。scanner.py 和 tui/app.py 完全不用改。
"""

from abc import ABC, abstractmethod


class BaseProvider(ABC):
    """每种包管理器实现这个抽象类。

    必须实现的三个部分：
      name              — 类属性，来源名称字符串
      is_available()    — 静态方法，检查命令是否存在于系统
      fetch_packages()  — 获取所有包
      fetch_dependencies() — 获取某个包的依赖
    """

    # 子类必须覆盖这个类属性，如 name = "apt"
    name: str = ""

    @staticmethod
    @abstractmethod
    def is_available() -> bool:
        """检查这个包管理器是否在系统上可用。

        通常用 shutil.which("命令名") 判断可执行文件是否存在。
        如 shutil.which("snap") 返回 "/usr/bin/snap" 表示已安装。

        AppImage 特殊：不需要命令，始终返回 True。

        注意：这是 @staticmethod（静态方法），不需要实例就能调用。
        这样 scanner 在创建 Provider 实例前就能判断可用性。
        """
        ...

    @abstractmethod
    def fetch_packages(self) -> list[dict]:
        """获取所有已安装包的基本信息。

        返回统一格式的字典列表。每个字典的约定字段：
          name: str           — 包名，去掉架构后缀（如 "fcitx5" 而非 "fcitx5:amd64"）
          version: str        — 版本号
          installed_size: int — 安装大小（KB）
          description: str    — 简短描述
          is_manual: bool     — True=父包，False=依赖（没有父子概念的全填 True）
          installed_at: str   — 安装时间 ISO8601（没有的全填 ""）

        对于没有父子概念的管理器（snap/appimage），is_manual 全部为 True。
        """
        ...

    @abstractmethod
    def fetch_dependencies(self, pkg_name: str) -> list[dict]:
        """获取某个包的依赖列表。

        返回统一格式的字典列表。每个字典的约定字段：
          name: str           — 依赖包名
          version: str        — 版本号
          is_automatic: bool  — True=自动拉入的依赖

        对于没有依赖的管理器（snap/appimage），返回空列表 []。
        """
        ...
