"""Homebrew Provider — 调用 brew list 获取已安装的 formula。

============================================================
Homebrew 的特点
============================================================

Homebrew（简称 brew）是 macOS 上的包管理器，也有 Linux 版本（Linuxbrew）。
与 APT 类似的有父子依赖概念：
  - brew list --formula → 已安装的 formula 列表
  - brew deps --installed <name> → 某个 formula 的依赖
  - brew info --json <name> → 获取版本和大小

注意：Homebrew 还支持 cask（GUI 应用的二进制分发），
当前只处理 formula（命令行工具和库）。

为什么每个包都单独调 brew info --json？
  因为 brew list 只输出包名，不含版本和大小。
  目前没有类似 dpkg-query -W 的批量查询方式。
  如果包数量多，这可能是性能瓶颈。后续可优化为 brew info --json --formula 批量查询。
"""

import subprocess
from src.providers import BaseProvider


class BrewProvider(BaseProvider):
    """Homebrew 包管理器 Provider。"""

    name = "brew"

    @staticmethod
    def is_available() -> bool:
        """检查 brew 命令是否可执行。"""
        import shutil
        return shutil.which("brew") is not None

    def fetch_packages(self) -> list[dict]:
        """获取所有已安装的 brew formula。

        brew list --formula 只输出包名（每行一个），不含其他信息。
        所以对每个包再调 brew info --json 获取版本和大小。
        """
        try:
            out = subprocess.check_output(
                ["brew", "list", "--formula"],
                stderr=subprocess.DEVNULL, text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []

        packages = []
        for line in out.strip().split("\n"):
            name = line.strip()
            if not name:
                continue

            version, size_kb = self._get_brew_info(name)

            packages.append({
                "name": name,
                "version": version,
                "installed_size": size_kb,
                "description": "[brew]",
                "is_manual": True,  # brew list 列出的都是手动安装的
                "installed_at": "",
            })

        return packages

    def fetch_dependencies(self, pkg_name: str) -> list[dict]:
        """通过 brew deps --installed 获取已安装的依赖。

        brew deps <name> 列出所有依赖（包括未安装的），
        --installed 只显示已安装的依赖。
        输出每行一个包名。
        """
        deps = []
        try:
            out = subprocess.check_output(
                ["brew", "deps", "--installed", pkg_name],
                stderr=subprocess.DEVNULL, text=True,
            )
            for line in out.strip().split("\n"):
                name = line.strip()
                if name:
                    version, size_kb = self._get_brew_info(name)
                    deps.append({
                        "name": name,
                        "version": version,
                        "is_automatic": True,
                        "installed_size": size_kb,
                    })
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
        return deps

    def _get_brew_info(self, name: str) -> tuple[str, int]:
        """获取 brew 包的版本和大小。

        brew info --json 返回 JSON 数组，每个元素包含：
          - installed[].version — 已安装的版本
          - installed[].size — 已安装的大小（字节）

        返回 (version, size_kb)
        """
        version = ""
        size_kb = 0
        try:
            out = subprocess.check_output(
                ["brew", "info", "--json", name],
                stderr=subprocess.DEVNULL, text=True,
            )
            import json
            data = json.loads(out)
            if data:
                entry = data[0]
                # installed 是一个数组，可能有多个版本
                installed_list = entry.get("installed", [])
                if installed_list:
                    version = installed_list[0].get("version", "")
                    # brew 的 size 单位是字节
                    size_kb = installed_list[0].get("size", 0) // 1024
        except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
            pass
        return version, size_kb
