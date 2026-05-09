"""AppImage Provider — 扫描文件系统查找 .AppImage 文件。

============================================================
AppImage 的特殊之处
============================================================

AppImage 是一种 Linux 可移植应用格式。与其他包管理器不同：
  - 没有包管理器命令（不需要 apt/snap/flatpak 等）
  - 就是一个独立的可执行文件，后缀 .AppImage
  - 用户手动下载，放在某个目录里
  - 自包含（没有系统级依赖）
  - 没有版本号（从文件名推断，但不一定准确）
  - 没有安装时间

所以这个 Provider 就是「文件系统扫描」：在常见目录中找 .AppImage 文件。

扫描目录（SEARCH_DIRS）：
  ~/Applications — 用户手动创建的应用目录
  ~/.local/bin   — 用户本地 bin 目录
  ~/bin          — 旧式的用户 bin 目录
  /opt            — 系统级可选软件目录（排除已知系统子目录）

去重机制：
  可能存在符号链接指向同一个文件，用 os.path.realpath() 解析真实路径，
  用 seen 集合记录已处理过的文件。
"""

import os
from src.providers import BaseProvider

# 常见 AppImage 存放目录
SEARCH_DIRS = [
    os.path.expanduser("~/Applications"),
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/bin"),
    "/opt",
]

# 排除的系统目录（不太可能放 AppImage）
EXCLUDE_DIRS = {
    "/opt/containerd",
    "/opt/google",
    "/opt/microsoft",
}


class AppImageProvider(BaseProvider):
    """AppImage Provider — 通过文件系统扫描发现 .AppImage 文件。"""

    name = "appimage"

    @staticmethod
    def is_available() -> bool:
        """AppImage 不需要任何特殊命令，始终「可用」。

        即使找不到任何 .AppImage 文件，也只是返回空列表，不会报错。
        """
        return True

    def fetch_packages(self) -> list[dict]:
        """扫描所有 SEARCH_DIRS，找出 .AppImage 文件。

        对每个目录递归搜索（os.walk），遇到 EXCLUDE_DIRS 跳过。
        用 os.path.realpath 解析符号链接，seen 集合去重。
        """
        packages = []
        seen = set()  # 已处理的真实路径（防止符号链接重复）

        # 过滤掉不存在的目录
        search_dirs = [d for d in SEARCH_DIRS if os.path.isdir(d)]

        for search_dir in search_dirs:
            try:
                # os.walk 递归遍历目录树
                # root: 当前目录路径
                # dirs: 当前目录下的子目录列表
                # files: 当前目录下的文件列表
                for root, dirs, files in os.walk(search_dir):
                    # 跳过排除目录（通过清空 dirs 阻止 os.walk 进入）
                    if root in EXCLUDE_DIRS:
                        dirs.clear()
                        continue

                    for filename in files:
                        if not filename.endswith(".AppImage"):
                            continue

                        filepath = os.path.join(root, filename)
                        # realpath 解析符号链接，获取真实文件路径
                        realpath = os.path.realpath(filepath)
                        if realpath in seen:
                            continue  # 符号链接指向已处理的文件
                        seen.add(realpath)

                        # 获取文件大小
                        try:
                            stat = os.stat(filepath)
                            size_kb = stat.st_size // 1024  # 字节 → KB
                        except OSError:
                            size_kb = 0

                        # 从文件名提取应用名（去掉 .AppImage 后缀）
                        # 例如 "MyTool-2.1-x86_64.AppImage" → "MyTool-2.1-x86_64"
                        app_name = filename.replace(".AppImage", "")

                        packages.append({
                            "name": app_name,
                            "version": "",  # AppImage 没有版本号
                            "installed_size": size_kb,
                            "description": f"[AppImage] {filepath}",
                            "is_manual": True,  # 手动下载的都是用户主动获取的
                            "installed_at": "",
                        })
            except PermissionError:
                # 无权限访问的目录跳过（如 /opt 下的某些系统目录）
                continue

        return packages

    def fetch_dependencies(self, pkg_name: str) -> list[dict]:
        """AppImage 自包含所有依赖。"""
        return []
