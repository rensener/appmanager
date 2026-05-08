"""APT Provider — 解析 /var/log/apt/history.log 获取包安装历史。"""

import glob
import re
import subprocess
from datetime import datetime

from src.providers import BaseProvider
from src.utils import dpkg_utils


HISTORY_DIR = "/var/log/apt"
HISTORY_PATTERN = "history.log*"


class AptProvider(BaseProvider):
    name = "apt"

    def __init__(self, history_dir: str = HISTORY_DIR):
        self._history_dir = history_dir
        self._transactions: list[dict] = []
        self._parent_packages: dict[str, list[str]] = {}  # 父包 → [依赖包名]
        self._all_known_packages: set[str] = set()  # history.log 中出现过的所有包
        self._depends_cache: dict[str, list[str]] = {}  # dpkg-query -W 批量获取的 Depends

    @staticmethod
    def is_available() -> bool:
        import os
        return os.path.exists("/usr/bin/dpkg")

    def fetch_packages(self) -> list[dict]:
        """解析 history.log，返回所有 APT 包（父包 + 依赖）。

        分类逻辑：
        - 用户安装：在 history.log 中被显式 apt install 命名的包
        - 系统预装：在 apt-mark showmanual 中但未被用户显式安装过的包
        - 自动依赖：其余已安装的包（is_manual=False）
        """
        self._parse_all_history_logs()
        self._preload_depends()
        installed = dpkg_utils.get_installed_packages()

        # 收集用户显式安装的包名（在命令行中直接指定的）
        # 只统计 install 操作；remove/purge/upgrade 不算
        user_installed: set[str] = set()
        for txn in self._transactions:
            if txn.get("operation") != "install":
                continue
            cmd = txn.get("command", "")
            # 跳过系统自动化脚本（特征：--force-unsafe-io, AutoInstall, unattended）
            if any(kw in cmd for kw in ("--force-unsafe-io", "AutoInstall", "unattended")):
                continue
            parent_names = self._extract_parent_name(cmd)
            txn_pkgs = txn.get("packages", [])
            # .deb 安装时命令行是文件名（如 ./Clash.Verge_2.4.7_amd64.deb），
            # 无法匹配实际包名（clash-verge），检查是否有匹配
            any_matched = any(
                self._clean_pkg_name(e["name"]) in parent_names
                for e in txn_pkgs
            )
            for entry in txn_pkgs:
                name = self._clean_pkg_name(entry["name"])
                is_auto = entry.get("is_automatic", False)
                is_parent = not is_auto and (
                    name in parent_names or not any_matched
                )
                if is_parent:
                    user_installed.add(name)
                    self._all_known_packages.add(name)

        # 从 history.log 提取包信息
        pkg_info: dict[str, dict] = {}  # name → info dict

        for txn in self._transactions:
            timestamp = txn.get("timestamp", "")
            cmd = txn.get("command", "")
            operation = txn.get("operation", "install")
            parent_names = self._extract_parent_name(cmd)

            # 只有用户 install 操作才会标记 is_manual / installed_at
            is_user_install = (
                operation == "install"
                and not any(kw in cmd for kw in ("--force-unsafe-io", "AutoInstall", "unattended"))
            )

            txn_pkgs2 = txn.get("packages", [])
            any_matched2 = any(
                self._clean_pkg_name(e["name"]) in parent_names
                for e in txn_pkgs2
            ) if is_user_install else False

            for entry in txn_pkgs2:
                name = self._clean_pkg_name(entry["name"])
                is_auto = entry.get("is_automatic", False)
                if is_user_install:
                    is_parent = not is_auto and (
                        name in parent_names or not any_matched2
                    )
                else:
                    is_parent = False

                if name not in pkg_info:
                    pkg_info[name] = {
                        "name": name,
                        "version": entry.get("version", ""),
                        "is_manual": is_parent,
                        "installed_at": timestamp if is_parent else "",
                    }
                else:
                    if is_parent:
                        pkg_info[name]["is_manual"] = True
                        if timestamp > pkg_info[name]["installed_at"]:
                            pkg_info[name]["installed_at"] = timestamp
                            pkg_info[name]["version"] = entry.get("version", "")

                if is_parent and name not in self._parent_packages:
                    self._parent_packages[name] = []

                self._all_known_packages.add(name)

        # apt-mark showmanual 中的包：区分用户安装 vs 系统预装 vs .deb 安装
        manual_set = self._get_apt_mark_manual()

        # .deb 安装：在 manual_set 中，不在 APT 仓库中（只有本地 dpkg status 记录）
        candidates = {n for n in manual_set if n in installed and n not in self._all_known_packages}
        deb_installed = set()
        if candidates:
            deb_installed = self._detect_local_only_packages(candidates)

        # 补全 .deb 包（不在任何 history.log 事务中）
        for name in deb_installed:
            self._all_known_packages.add(name)
            pkg_info[name] = {
                "name": name,
                "version": "",
                "is_manual": True,
                "installed_at": "手动安装",
            }

        # 补全当前已装但 history.log 中没有的包 → 系统预装
        for pkg_name in installed:
            if pkg_name not in self._all_known_packages:
                self._all_known_packages.add(pkg_name)
                pkg_info[pkg_name] = {
                    "name": pkg_name,
                    "version": "",
                    "is_manual": True,
                    "installed_at": "",
                }

        result = []
        for name, info in pkg_info.items():
            if name not in installed:
                continue

            if name in manual_set:
                info["is_manual"] = True
                if name not in user_installed and name not in deb_installed:
                    info["installed_at"] = ""  # 系统预装：清空时间戳

            result.append(info)

        # 用 dpkg-query -W 一次性补全所有包的版本、大小、描述
        print("  [apt] 查询包详情...", end="", flush=True)
        all_info = dpkg_utils.get_all_packages_info()
        for info in result:
            dpkg_data = all_info.get(info["name"], {})
            if not info["version"]:
                info["version"] = dpkg_data.get("version", "")
            info["installed_size"] = dpkg_data.get("size_kb", 0)
            info["description"] = dpkg_data.get("description", "")
        print(f" 完成 ({len(all_info)} 个包)")

        print()  # dpkg 进度行换行
        return result

    def fetch_package_names(self) -> set[str]:
        """快速获取当前已安装的 APT 包名（一次 dpkg 调用）。"""
        return dpkg_utils.get_installed_packages()

    def fetch_single_package(self, name: str) -> dict:
        """获取单个包的完整信息（一次 dpkg -s 调用）。

        区分用户安装 vs 系统预装 vs .deb 安装。
        """
        info = dpkg_utils.get_package_info(name)

        # 检查是否被用户显式安装过
        if not self._transactions:
            self._parse_all_history_logs()

        is_user = False
        installed_at = ""
        in_history = False
        for txn in self._transactions:
            if txn.get("operation") != "install":
                continue
            cmd = txn.get("command", "")
            if any(kw in cmd for kw in ("--force-unsafe-io", "AutoInstall", "unattended")):
                continue
            parent_names = self._extract_parent_name(cmd)
            txn_pkgs = txn.get("packages", [])
            any_matched = any(
                self._clean_pkg_name(e["name"]) in parent_names
                for e in txn_pkgs
            )
            for entry in txn_pkgs:
                pkg_name = self._clean_pkg_name(entry["name"])
                if pkg_name == name:
                    in_history = True
                    is_auto = entry.get("is_automatic", False)
                    is_parent = not is_auto and (
                        pkg_name in parent_names or not any_matched
                    )
                    if is_parent:
                        is_user = True
                        installed_at = txn.get("timestamp", "")
                    break
            if is_user:
                break

        # .deb 安装：在 apt-mark showmanual 但不在 APT 仓库中
        manual_set = self._get_apt_mark_manual()
        if name in manual_set and not in_history:
            local_only = self._detect_local_only_packages({name})
            if name in local_only:
                installed_at = "手动安装"

        return {
            "name": name,
            "version": info.get("version", ""),
            "installed_size": info.get("size_kb", 0),
            "description": info.get("description", ""),
            "is_manual": True,
            "installed_at": installed_at if (is_user or name in manual_set and not in_history) else "",
        }

    def _preload_depends(self):
        """一次 dpkg-query -W 获取所有包的 Depends，避免逐个 dpkg -s。"""
        try:
            out = subprocess.check_output(
                ["dpkg-query", "-W", "-f", "${Package}\\t${Depends}\\n"],
                stderr=subprocess.DEVNULL, text=True,
            )
            for line in out.strip().split("\n"):
                if "\t" not in line:
                    continue
                parts = line.split("\t", 1)
                name = parts[0].split(":")[0]
                dep_str = parts[1] if len(parts) > 1 else ""
                if dep_str:
                    # 解析 "pkg1 (>= ver1), pkg2, pkg3 (>= ver2)" 格式
                    dep_names = []
                    for dep_part in dep_str.split(","):
                        dn = dep_part.strip().split()[0].split(":")[0] if dep_part.strip() else ""
                        if dn:
                            dep_names.append(dn)
                    self._depends_cache[name] = dep_names
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    def fetch_dependencies(self, pkg_name: str) -> list[dict]:
        """返回某个父包的依赖列表。

        依赖来自两个方面：
        1. history.log 中和父包同一次安装的 automatic 包
        2. dpkg-query 预加载的 Depends 缓存
        """
        deps: list[dict] = []

        # 方法 1: 从 history.log 中查找同一次安装的 automatic 包
        for txn in self._transactions:
            cmd = txn.get("command", "")
            parent_names = self._extract_parent_name(cmd)
            if pkg_name not in parent_names:
                continue

            for entry in txn.get("packages", []):
                name = self._clean_pkg_name(entry["name"])
                if entry.get("is_automatic", False):
                    deps.append({
                        "name": name,
                        "version": entry.get("version", ""),
                        "is_automatic": True,
                    })

        # 方法 2: 从预先加载的 Depends 缓存补充
        for dep_name in self._depends_cache.get(pkg_name, []):
            if not any(d["name"] == dep_name for d in deps):
                deps.append({
                    "name": dep_name,
                    "version": "",
                    "is_automatic": True,
                })

        return [d for d in deps if d["name"] != pkg_name]

    # ── 内部方法 ──────────────────────────────────────

    def _parse_all_history_logs(self):
        """解析所有 history.log 文件（含轮转日志）。"""
        self._transactions = []
        pattern = f"{self._history_dir}/{HISTORY_PATTERN}"

        for fpath in sorted(glob.glob(pattern)):
            try:
                self._transactions.extend(self._parse_file(fpath))
            except (PermissionError, OSError):
                continue

    def _parse_file(self, fpath: str) -> list[dict]:
        """解析单个 history.log 文件，返回 transaction 列表。

        每个 transaction:
        {
            "timestamp": "2026-05-01T14:00:00",
            "command": "apt install fcitx5",
            "operation": "install",
            "user": "rensen",
            "packages": [
                {"name": "fcitx5:amd64", "version": "5.1.0", "is_automatic": False},
                ...
            ]
        }
        """
        transactions = []
        current_txn: dict | None = None
        install_buffer: list[str] = []  # 处理多行 Install 续行
        in_install = False

        with open(fpath, errors="replace") as f:
            for raw_line in f:
                line = raw_line.rstrip("\n")

                # Start-Date
                if line.startswith("Start-Date:"):
                    current_txn = {
                        "timestamp": self._normalize_timestamp(line[11:].strip()),
                        "command": "",
                        "operation": "install",
                        "user": "",
                        "packages": [],
                    }
                    in_install = False
                    install_buffer = []
                    continue

                if current_txn is None:
                    continue

                # Commandline
                if line.startswith("Commandline:"):
                    current_txn["command"] = line[12:].strip()
                    current_txn["operation"] = self._detect_operation(
                        current_txn["command"]
                    )
                    continue

                # Requested-By
                if line.startswith("Requested-By:"):
                    current_txn["user"] = line[13:].strip().split()[0]
                    continue

                # Install / Remove / Purge / Upgrade — 可能多行
                if line.startswith("Install:"):
                    in_install = True
                    current_txn["operation"] = "install"
                    install_buffer.append(line[8:].strip())
                    continue
                elif line.startswith("Remove:"):
                    in_install = True
                    current_txn["operation"] = "remove"
                    install_buffer.append(line[7:].strip())
                    continue
                elif line.startswith("Purge:"):
                    in_install = True
                    current_txn["operation"] = "purge"
                    install_buffer.append(line[6:].strip())
                    continue
                elif line.startswith("Upgrade:"):
                    in_install = True
                    current_txn["operation"] = "upgrade"
                    install_buffer.append(line[8:].strip())
                    continue

                # 续行 (以空格开头或反斜杠结尾的继续)
                if in_install and line and line[0] in (" ", "\t"):
                    install_buffer.append(line.strip())
                    continue

                # End-Date
                if line.startswith("End-Date:"):
                    if in_install and install_buffer:
                        full_install = "".join(install_buffer)
                        # 去掉行尾续行符
                        full_install = full_install.replace("\\", "")
                        current_txn["packages"] = self._parse_package_list(full_install)
                    if current_txn["packages"]:
                        transactions.append(current_txn)
                    current_txn = None
                    in_install = False
                    install_buffer = []
                    continue

                # Error-Date (某些版本有)
                if line.startswith("Error-Date:"):
                    current_txn = None
                    in_install = False
                    install_buffer = []

        # 处理文件末尾没有 End-Date 的情况
        if current_txn and in_install and install_buffer:
            full_install = "".join(install_buffer).replace("\\", "")
            current_txn["packages"] = self._parse_package_list(full_install)
            if current_txn["packages"]:
                transactions.append(current_txn)

        return transactions

    def _parse_package_list(self, text: str) -> list[dict]:
        """解析包列表字符串，如 'fcitx5:amd64 (5.1.0), libfcitx5core7:amd64 (5.1.0, automatic)'"""
        packages = []
        # 按逗号分割，但要注意版本号里也可能有逗号（极少见）
        pattern = re.compile(r"([^\s,]+(?::\w+)?)\s*\(([^)]+)\)")
        for match in pattern.finditer(text):
            full_name = match.group(1)
            details = match.group(2)
            is_auto = "automatic" in details.lower()
            # 版本是 details 中逗号前的部分
            version = details.split(",")[0].strip() if details else ""
            packages.append({
                "name": full_name,
                "version": version,
                "is_automatic": is_auto,
            })
        return packages

    @staticmethod
    def _clean_pkg_name(raw_name: str) -> str:
        """去掉架构后缀，如 'fcitx5:amd64' → 'fcitx5'"""
        return raw_name.split(":")[0]

    @staticmethod
    def _extract_parent_name(command: str) -> set[str]:
        """从命令行提取用户主动安装的包名。

        例如 'apt install fcitx5' → {'fcitx5'}
            'apt-get --yes install mpv vim' → {'mpv', 'vim'}
        """
        # 去掉命令前缀
        cmd = re.sub(r"^(apt|apt-get|aptitude)\s+", "", command)
        # 去掉选项（-x, --xxx）
        cmd = re.sub(r"(^|\s)-\S+", " ", cmd)
        # 去掉操作词（install, remove 等），先 strip 防止前面有空格残留
        cmd = cmd.strip()
        cmd = re.sub(r"^(install|remove|purge|upgrade|autoremove|dist-upgrade)\s*", "", cmd)

        names = set()
        for part in cmd.split():
            part = part.strip()
            if part and not part.startswith("-"):
                names.add(part.split(":")[0])
        return names

    @staticmethod
    def _detect_operation(command: str) -> str:
        cmd_lower = command.lower()
        if "purge" in cmd_lower:
            return "purge"
        if "remove" in cmd_lower or "autoremove" in cmd_lower:
            return "remove"
        if "upgrade" in cmd_lower:
            return "upgrade"
        return "install"

    @staticmethod
    def _normalize_timestamp(ts: str) -> str:
        """将 history.log 中的时间转为 ISO8601。

        输入可能是 '2026-05-07  20:31:31' （注意双空格）
        """
        ts = re.sub(r"\s+", " ", ts).strip()
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            return dt.isoformat()
        except ValueError:
            return ts

    @staticmethod
    def _detect_local_only_packages(names: set[str]) -> set[str]:
        """通过 apt-cache policy 批量检测仅有本地 .deb 来源的包。

        .deb 包的 apt-cache policy 输出只有 /var/lib/dpkg/status，
        而 APT 仓库包会有 http://... 行。
        """
        if not names:
            return set()
        try:
            out = subprocess.check_output(
                ["apt-cache", "policy"] + sorted(names),
                stderr=subprocess.DEVNULL, text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return set()

        result = set()
        current_pkg = None
        has_http = False
        for line in out.strip().split("\n"):
            if line and not line.startswith(" "):
                if current_pkg and not has_http and current_pkg != "N:":
                    result.add(current_pkg)
                current_pkg = line.rstrip(":")
                has_http = False
            elif "http://" in line:
                has_http = True
        if current_pkg and not has_http and current_pkg != "N:":
            result.add(current_pkg)
        return result

    @staticmethod
    def _get_apt_mark_manual() -> set[str]:
        """调用 apt-mark showmanual 获取手动安装的包列表。"""
        try:
            out = subprocess.check_output(
                ["apt-mark", "showmanual"], stderr=subprocess.DEVNULL, text=True
            )
            return {line.strip().split(":")[0] for line in out.strip().split("\n") if line}
        except (subprocess.CalledProcessError, FileNotFoundError):
            return set()
