"""Flatpak Provider — 调用 flatpak list 获取已安装的 flatpak 应用和运行时。

============================================================
Flatpak 的依赖概念
============================================================

Flatpak 有两类东西：
  - 应用（--app）：用户装的软件，如 org.mozilla.firefox
  - 运行时（--runtime）：应用依赖的基础环境，如 org.gnome.Platform

关系：应用依赖运行时（类似 APT 的父包依赖子包）。
在树形展示中：应用是父节点，运行时是其子节点（依赖）。

识别方式：
  - flatpak list --app      → 应用列表（is_manual=True）
  - flatpak list --runtime  → 运行时列表（is_manual=False）
  - flatpak info <应用名>   → 查看该应用依赖哪个 Runtime

所有应用和运行时都统一存储为 Package（只通过 is_manual 区分）。
"""

import subprocess
from src.providers import BaseProvider


class FlatpakProvider(BaseProvider):
    """Flatpak 包管理器 Provider。"""

    name = "flatpak"

    @staticmethod
    def is_available() -> bool:
        """检查 flatpak 命令是否可执行。"""
        import shutil
        return shutil.which("flatpak") is not None

    def fetch_packages(self) -> list[dict]:
        """获取所有 flatpak 应用和运行时。

        分别调用 flatpak list --app 和 flatpak list --runtime，
        用 --columns 指定我们需要的字段（name,version,size,description）。
        Tab 作为列分隔符，避免包名中的空格干扰解析。
        """
        apps = self._list_flatpak("--app")
        runtimes = self._list_flatpak("--runtime")

        packages = []

        # 应用 → is_manual=True（用户主动安装的顶层应用）
        for name, version, size_bytes, desc in apps:
            packages.append({
                "name": name,
                "version": version,
                "installed_size": size_bytes // 1024,  # 字节 → KB
                "description": desc or "[flatpak]",
                "is_manual": True,
                "installed_at": "",
            })

        # 运行时 → is_manual=False（通常是自动拉入的）
        for name, version, size_bytes, desc in runtimes:
            packages.append({
                "name": name,
                "version": version,
                "installed_size": size_bytes // 1024,
                "description": desc or "[flatpak runtime]",
                "is_manual": False,
                "installed_at": "",
            })

        return packages

    def fetch_dependencies(self, pkg_name: str) -> list[dict]:
        """返回 flatpak 应用的运行时依赖。

        通过 flatpak info 获取，解析 "Runtime:" 行。
        例如：
          flatpak info org.mozilla.firefox
          ...
          Runtime: org.gnome.Platform/x86_64/46
          ...

        返回格式：
          [{"name": "org.gnome.Platform/x86_64/46", "version": "", "is_automatic": True}]
        """
        deps = []
        try:
            out = subprocess.check_output(
                ["flatpak", "info", pkg_name],
                stderr=subprocess.DEVNULL, text=True,
            )
            for line in out.split("\n"):
                if line.startswith("Runtime:"):
                    runtime = line.split(":", 1)[1].strip()
                    if runtime:
                        deps.append({
                            "name": runtime,
                            "version": "",
                            "is_automatic": True,
                        })
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        return deps

    def _list_flatpak(self, kind: str) -> list[tuple]:
        """执行 flatpak list，返回 [(name, version, size, description), ...]。

        Args:
            kind: "--app" 或 "--runtime"

        --columns=name,version,size,description 指定输出列，Tab 分隔。
        避免默认输出的空格对齐格式带来的解析困难。
        """
        try:
            out = subprocess.check_output(
                ["flatpak", "list", kind, "--columns=name,version,size,description"],
                stderr=subprocess.DEVNULL, text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []

        results = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            name = parts[0]
            version = parts[1]
            # size 字段可能是空字符串（某些版本不输出）
            try:
                size_bytes = int(parts[2]) if parts[2] else 0
            except ValueError:
                size_bytes = 0
            desc = parts[3]
            results.append((name, version, size_bytes, desc))

        return results
