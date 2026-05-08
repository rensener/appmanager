"""AppImage Provider — 扫描文件系统查找 .AppImage 文件。"""

import os
from src.providers import BaseProvider

# 常见的 AppImage 存放目录
SEARCH_DIRS = [
    os.path.expanduser("~/Applications"),
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/bin"),
    "/opt",
]

# 排除的目录（系统目录，不太可能放 AppImage）
EXCLUDE_DIRS = {
    "/opt/containerd",
    "/opt/google",
    "/opt/microsoft",
}


class AppImageProvider(BaseProvider):
    name = "appimage"

    @staticmethod
    def is_available() -> bool:
        # 不需要特殊命令，总是"可用"（但可能找不到任何包）
        return True

    def fetch_packages(self) -> list[dict]:
        packages = []
        seen = set()  # 去重（可能存在符号链接）

        search_dirs = [d for d in SEARCH_DIRS if os.path.isdir(d)]

        for search_dir in search_dirs:
            try:
                for root, dirs, files in os.walk(search_dir):
                    # 跳过排除目录
                    if root in EXCLUDE_DIRS:
                        dirs.clear()
                        continue

                    for filename in files:
                        if not filename.endswith(".AppImage"):
                            continue

                        filepath = os.path.join(root, filename)
                        realpath = os.path.realpath(filepath)
                        if realpath in seen:
                            continue
                        seen.add(realpath)

                        try:
                            stat = os.stat(filepath)
                            size_kb = stat.st_size // 1024
                        except OSError:
                            size_kb = 0

                        # 从文件名提取应用名（去掉 .AppImage 后缀）
                        app_name = filename.replace(".AppImage", "")

                        packages.append({
                            "name": app_name,
                            "version": "",
                            "installed_size": size_kb,
                            "description": f"[AppImage] {filepath}",
                            "is_manual": True,
                            "installed_at": "",
                        })
            except PermissionError:
                continue

        return packages

    def fetch_dependencies(self, pkg_name: str) -> list[dict]:
        # AppImage 自包含，没有依赖
        return []
