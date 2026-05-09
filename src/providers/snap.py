"""Snap Provider — 调用 snap list 获取已安装的 snap 包。

============================================================
Snap 的特点
============================================================

Snap 是 Canonical 的沙盒包格式，每个 snap 自包含所有依赖。
所以不需要处理依赖关系，fetch_dependencies() 直接返回空列表。

与 APT 的巨大差异：
  - 没有安装历史（只有一个当前状态快照）
  - 没有依赖（自包含）
  - 所有包统一视为用户安装（is_manual=True）
  - 大小字段不包含在 snap list 默认输出中

snap list 默认输出：
  Name      Version        Rev    Tracking       Publisher   Notes
  firefox   131.0.2-1      5374   latest/stable  mozilla     -
  snapd     2.66.1         23567  latest/stable  canonical   snapd
"""

import subprocess
from src.providers import BaseProvider


class SnapProvider(BaseProvider):
    """Snap 包管理器 Provider。

    实现非常简单：调一次 snap list，解析输出，返回结果。
    """

    name = "snap"

    @staticmethod
    def is_available() -> bool:
        """检查 snap 命令是否可执行。"""
        import shutil
        return shutil.which("snap") is not None

    def fetch_packages(self) -> list[dict]:
        """调用 snap list 获取所有已安装的 snap 包。

        snap list 输出格式：
          Name      Version        Rev    Tracking       Publisher   Notes
          firefox   131.0.2-1      5374   latest/stable  mozilla     -
          snapd     2.66.1         23567  latest/stable  canonical   snapd

        默认输出不包括 Size 列。可以用 --columns 指定，但为了兼容性，
        这里用默认输出，大小填 0。
        """
        try:
            out = subprocess.check_output(
                ["snap", "list"], stderr=subprocess.DEVNULL, text=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []

        packages = []
        # split("\n")[1:] 跳过标题行（Name Version Rev ...）
        for line in out.strip().split("\n")[1:]:
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) < 3:
                continue

            name = parts[0]
            version = parts[1]
            # Notes 列在最后（可能包含空格），如 "canonical,classic"
            notes = " ".join(parts[5:]) if len(parts) > 5 else ""

            packages.append({
                "name": name,
                "version": version,
                "installed_size": 0,  # snap list 默认不输出大小
                "description": f"[snap] {notes}" if notes else "[snap]",
                "is_manual": True,     # snap 都是用户主动装的
                "installed_at": "",
            })

        return packages

    def fetch_dependencies(self, pkg_name: str) -> list[dict]:
        """Snap 包自包含，没有系统级依赖。"""
        return []
