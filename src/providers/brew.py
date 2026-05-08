"""Homebrew Provider — 调用 brew list 获取已安装的 formula。"""

import subprocess
from src.providers import BaseProvider


class BrewProvider(BaseProvider):
    name = "brew"

    @staticmethod
    def is_available() -> bool:
        import shutil
        return shutil.which("brew") is not None

    def fetch_packages(self) -> list[dict]:
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

            # 获取版本和大小信息
            version, size_kb = self._get_brew_info(name)

            packages.append({
                "name": name,
                "version": version,
                "installed_size": size_kb,
                "description": "[brew]",
                "is_manual": True,
                "installed_at": "",
            })

        return packages

    def fetch_dependencies(self, pkg_name: str) -> list[dict]:
        """通过 brew deps 获取依赖列表。"""
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
        """获取 brew 包的版本和大小。"""
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
                version = entry.get("installed", [{}])[0].get("version", "")
                size_kb = entry.get("installed", [{}])[0].get("size", 0) // 1024
        except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
            pass
        return version, size_kb
