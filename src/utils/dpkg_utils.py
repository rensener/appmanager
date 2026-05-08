"""封装 dpkg 命令调用 — 获取包大小、文件列表、描述。"""

import subprocess
import os


def get_all_packages_info() -> dict[str, dict]:
    """一次 dpkg-query 调用获取所有已安装包的版本、大小、描述。

    比逐个 dpkg -s 快 30 倍（1 次 vs 1830 次子进程）。
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
            parts = line.split("\t", 3)
            if len(parts) < 4:
                continue
            name = parts[0].split(":")[0]
            result[name] = {
                "version": parts[1],
                "size_kb": int(parts[2]) if parts[2].isdigit() else 0,
                "description": parts[3],
            }
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return result


def get_package_info(pkg_name: str) -> dict:
    """通过 dpkg -s 获取包的大小和描述。

    返回: {"size_kb": int, "description": str, "version": str}
    """
    result = {"size_kb": 0, "description": "", "version": ""}
    try:
        out = subprocess.check_output(
            ["dpkg", "-s", pkg_name], stderr=subprocess.DEVNULL, text=True
        )
        for line in out.split("\n"):
            if line.startswith("Installed-Size:"):
                result["size_kb"] = int(line.split(":", 1)[1].strip())
            elif line.startswith("Description:"):
                result["description"] = line.split(":", 1)[1].strip()
            elif line.startswith("Version:"):
                result["version"] = line.split(":", 1)[1].strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return result


def get_package_files(pkg_name: str) -> list[str]:
    """通过 dpkg -L 获取包安装的文件路径列表。"""
    try:
        out = subprocess.check_output(
            ["dpkg", "-L", pkg_name], stderr=subprocess.DEVNULL, text=True
        )
        lines = [l for l in out.strip().split("\n") if l]
        # dpkg -L 可能返回 "Package does not contain any files"
        if len(lines) == 1 and "does not contain" in lines[0]:
            return []
        return [l for l in lines if os.path.isfile(l)]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def get_installed_packages() -> set[str]:
    """通过 dpkg --get-selections 获取所有已安装的包名。"""
    try:
        out = subprocess.check_output(
            ["dpkg", "--get-selections"], stderr=subprocess.DEVNULL, text=True
        )
        pkgs = set()
        for line in out.strip().split("\n"):
            if "\t" in line:
                name, status = line.split("\t", 1)
                if "install" in status:
                    pkgs.add(name.split(":")[0])
        return pkgs
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()
