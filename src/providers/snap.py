"""Snap Provider — 调用 snap list 获取已安装的 snap 包。"""

import subprocess
from src.providers import BaseProvider


class SnapProvider(BaseProvider):
    name = "snap"

    @staticmethod
    def is_available() -> bool:
        import shutil
        return shutil.which("snap") is not None

    def fetch_packages(self) -> list[dict]:
        try:
            out = subprocess.check_output(
                ["snap", "list"], stderr=subprocess.DEVNULL, text=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []

        packages = []
        for line in out.strip().split("\n")[1:]:  # 跳过标题行
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) < 3:
                continue

            name = parts[0]
            version = parts[1]
            notes = " ".join(parts[5:]) if len(parts) > 5 else ""

            # snap 的 Size 列不在默认输出中，用 snap info 查询太慢
            # 先设为 0，后续可以懒加载
            installed_size = 0

            packages.append({
                "name": name,
                "version": version,
                "installed_size": installed_size,
                "description": f"[snap] {notes}" if notes else "[snap]",
                "is_manual": True,  # snap 都是用户主动装的
                "installed_at": "",
            })

        return packages

    def fetch_dependencies(self, pkg_name: str) -> list[dict]:
        # snap 包自包含，没有依赖
        return []
