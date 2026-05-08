"""Flatpak Provider — 调用 flatpak list 获取已安装的 flatpak 应用和运行时。"""

import subprocess
from src.providers import BaseProvider


class FlatpakProvider(BaseProvider):
    name = "flatpak"

    @staticmethod
    def is_available() -> bool:
        import shutil
        return shutil.which("flatpak") is not None

    def fetch_packages(self) -> list[dict]:
        apps = self._list_flatpak("--app")
        runtimes = self._list_flatpak("--runtime")

        packages = []

        for name, version, size_bytes, desc in apps:
            packages.append({
                "name": name,
                "version": version,
                "installed_size": size_bytes // 1024,
                "description": desc or "[flatpak]",
                "is_manual": True,
                "installed_at": "",
            })

        for name, version, size_bytes, desc in runtimes:
            packages.append({
                "name": name,
                "version": version,
                "installed_size": size_bytes // 1024,
                "description": desc or "[flatpak runtime]",
                "is_manual": False,  # 运行时通常是自动安装的
                "installed_at": "",
            })

        return packages

    def fetch_dependencies(self, pkg_name: str) -> list[dict]:
        """返回 flatpak 应用的运行时依赖。"""
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
        """执行 flatpak list，返回 [(name, version, size, description), ...]"""
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
            name = parts[0] if len(parts) > 0 else ""
            version = parts[1] if len(parts) > 1 else ""
            try:
                size_bytes = int(parts[2]) if len(parts) > 2 and parts[2] else 0
            except ValueError:
                size_bytes = 0
            desc = parts[3] if len(parts) > 3 else ""
            results.append((name, version, size_bytes, desc))

        return results
