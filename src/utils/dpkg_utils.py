"""封装 dpkg 命令调用 — 获取包大小、文件列表、描述。

============================================================
性能设计关键
============================================================

本文件的核心优化原则：「批量优于逐个」。

对比：
  逐个 dpkg -s：1830 个包 → 1830 次子进程 → ~30 秒
  一次 dpkg-query -W：全部包 → 1 次子进程 → ~0.5 秒
  快了约 60 倍！

每次 subprocess.check_output() 都会：
  1. fork() 一个新进程
  2. exec() 新程序的二进制
  3. 等待子进程结束
  4. 收集输出

这些开销加起来约 10-50ms 一次。1830 次就是 18-90 秒。
合并成一次调用，只需要 1 次这些开销。

所以：
  - get_all_packages_info() 替代逐个 get_package_info()
  - _preload_depends()（在 apt.py 中）用 dpkg-query -W 替代逐个 dpkg -s
  - get_installed_packages() 用 dpkg --get-selections 替代 dpkg -l

============================================================
dpkg 命令速查
============================================================

dpkg --get-selections  → 所有已安装包名（快速，只输出名称+状态）
dpkg-query -W -f "..."  → 自定义格式输出（批量获取指定字段）
dpkg -s <包名>          → 单个包的完整状态（慢，有完整元数据）
dpkg -L <包名>          → 包安装的文件路径列表
"""

import subprocess
import os


def get_all_packages_info() -> dict[str, dict]:
    """
    一次 dpkg-query -W 调用获取所有已安装包的版本、大小、描述。

    这是整个项目最重要的性能优化之一。

    dpkg-query -W 的 -f 参数使用自定义格式字符串：
      ${Package}        → 包名
      ${Version}        → 版本号
      ${Installed-Size} → 已安装大小（KB）
      ${Description}    → 简短描述（单行）
      \\t               → Tab 分隔符
      \\n               → 换行符

    输出示例（每行一个包）：
      fcitx5\t5.1.0\t12345\tFcitx5 input method framework
      bash\t5.2.21\t1845\tGNU Bourne Again SHell

    返回格式：
      {"fcitx5": {"version": "5.1.0", "size_kb": 12345, "description": "..."}, ...}
    """
    result: dict[str, dict] = {}
    try:
        out = subprocess.check_output(
            ["dpkg-query", "-W", "-f",
             "${Package}\\t${Version}\\t${Installed-Size}\\t${Description}\\n"],
            stderr=subprocess.DEVNULL, text=True,
        )
        for line in out.strip().split("\n"):
            if "\t" not in line:
                continue
            parts = line.split("\t", 3)  # 最多分割 3 次（保留 Description 中可能的 Tab）
            if len(parts) < 4:
                continue
            # 去掉架构后缀（:amd64）
            name = parts[0].split(":")[0]
            result[name] = {
                "version": parts[1],
                # Installed-Size 是数字字符串，如 "12345"
                "size_kb": int(parts[2]) if parts[2].isdigit() else 0,
                "description": parts[3],
            }
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return result


def get_package_info(pkg_name: str) -> dict:
    """通过 dpkg -s 获取单个包的详细信息。

    返回: {"size_kb": int, "description": str, "version": str}

    注意：这是「逐个查询」，只在新包加入或依赖补充时用到。
    主扫描流程用 get_all_packages_info() 批量查询。
    """
    result = {"size_kb": 0, "description": "", "version": ""}
    try:
        out = subprocess.check_output(
            ["dpkg", "-s", pkg_name], stderr=subprocess.DEVNULL, text=True
        )
        # 逐行解析 dpkg -s 的输出
        for line in out.split("\n"):
            if line.startswith("Installed-Size:"):
                # "Installed-Size: 12345" → 12345
                result["size_kb"] = int(line.split(":", 1)[1].strip())
            elif line.startswith("Description:"):
                # "Description: Fcitx5 input method framework"
                # 注意：只取第一行。多行描述需要特殊处理（详见 tui/app.py 中
                # 的 _do_show_detail 方法）
                result["description"] = line.split(":", 1)[1].strip()
            elif line.startswith("Version:"):
                result["version"] = line.split(":", 1)[1].strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return result


def get_package_files(pkg_name: str) -> list[str]:
    """通过 dpkg -L 获取包安装的文件路径列表。

    例如 fcitx5 返回：
      ["/usr/bin/fcitx5", "/usr/share/fcitx5/...", ...]

    注意：
      - 有些包不包含文件（如元包），dpkg -L 输出
        "Package does not contain any files"
      - 返回的路径可能包含目录，这里只保留文件（os.path.isfile）
      - dpkg -L 不要求包已被安装，列出的路径是「如果安装会有什么文件」
    """
    try:
        out = subprocess.check_output(
            ["dpkg", "-L", pkg_name], stderr=subprocess.DEVNULL, text=True
        )
        lines = [l for l in out.strip().split("\n") if l]
        # 检查「包不包含文件」的特殊情况
        if len(lines) == 1 and "does not contain" in lines[0]:
            return []
        # 只保留实际存在的文件（过滤目录和已删除的文件）
        return [l for l in lines if os.path.isfile(l)]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def get_installed_packages() -> set[str]:
    """通过 dpkg --get-selections 获取所有已安装的包名。

    这是最快的获取包名方式。
    输出格式：
      bash            install
      fcitx5          install
      libfcitx5core7  install
      ...
      <包名>\t<状态>

    只保留状态为 "install" 的包（排除 "deinstall" 等）。
    """
    try:
        out = subprocess.check_output(
            ["dpkg", "--get-selections"], stderr=subprocess.DEVNULL, text=True
        )
        pkgs = set()
        for line in out.strip().split("\n"):
            if "\t" in line:
                name, status = line.split("\t", 1)
                if "install" in status:
                    # 去掉架构后缀（:amd64 等）
                    pkgs.add(name.split(":")[0])
        return pkgs
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()
