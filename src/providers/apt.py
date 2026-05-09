"""APT Provider — 解析 /var/log/apt/history.log 获取包安装历史。

============================================================
APT 是数据最丰富的来源，也是最复杂的 Provider。
============================================================

与其他包管理器（snap list 就一行输出）不同，APT 有完整的安装历史日志。
这让我们能区分三类包：

  ① 用户安装 — 用户显式敲过 apt install xxx
     • 在 history.log 中被命名，且不是 automatic
     • installed_at 有时间戳
     • 显示在「用户安装」分组

  ② 系统预装 — 装机时就在的包（bash、coreutils 等）
     • 在 apt-mark showmanual 中，但从未出现在 history.log
     • installed_at 为空字符串 ""
     • 显示在「系统预装」分组

  ③ .deb 手动安装 — 用户下载 .deb 文件用 dpkg -i 安装
     • 在 apt-mark showmanual 中，不在 APT 仓库中
     • 通过 apt-cache policy 检测：没有 http:// 源的就是本地包
     • installed_at = "手动安装"
     • 显示在「用户安装」分组

数据来源有三个：
  1. /var/log/apt/history.log* — 安装历史（含轮转日志）
  2. dpkg-query / dpkg -s  — 包的属性（版本、大小、描述）
  3. apt-mark showmanual    — 手动安装的包列表
  4. apt-cache policy       — 区分 .deb 本地包和 APT 仓库包

依赖解析结合两个来源：
  1. history.log — 同一次安装中的 automatic 标记包
  2. dpkg-query 预加载的 Depends 字段 — 包的元数据依赖
"""

import glob
import re
import subprocess
from datetime import datetime

from src.providers import BaseProvider
from src.utils import dpkg_utils


# APT 历史日志的目录和文件名模式
# history.log 是当前日志，history.log.1.gz 等是轮转的旧日志
HISTORY_DIR = "/var/log/apt"
HISTORY_PATTERN = "history.log*"


class AptProvider(BaseProvider):
    """APT 包管理器 Provider。

    内部状态：
      _transactions   — 从 history.log 解析出的所有操作记录
      _parent_packages — {父包名: [子包名列表]}，从 history.log 提取
      _all_known_packages — history.log 中出现过的所有包名
      _depends_cache   — {包名: [依赖包名列表]}，从 dpkg-query -W 批量获取

    这些状态在首次调用时延迟加载，后续调用复用。
    """

    name = "apt"

    def __init__(self, history_dir: str = HISTORY_DIR):
        self._history_dir = history_dir
        self._transactions: list[dict] = []        # 所有解析出的操作记录
        self._parent_packages: dict[str, list[str]] = {}  # 父包 → 依赖包名
        self._all_known_packages: set[str] = set() # history.log 中出现过的包
        self._depends_cache: dict[str, list[str]] = {}    # dpkg-query 批量 Depends

    @staticmethod
    def is_available() -> bool:
        """APT 可用性 = dpkg 命令存在。所有 Debian/Ubuntu 系统都有。"""
        import os
        return os.path.exists("/usr/bin/dpkg")

    def fetch_packages(self) -> list[dict]:
        """解析 history.log，返回所有 APT 包（父包 + 依赖）。

        这是 APT Provider 最核心的方法。流程如下：

        1. 解析所有 history.log 文件（含轮转的旧日志）
        2. 用 dpkg-query -W 一次性预加载所有包的 Depends 字段
        3. 用 dpkg --get-selections 获取当前已安装的包名
        4. 遍历 history.log 中的操作记录，标记用户安装 vs 自动依赖
        5. 通过 apt-mark showmanual + apt-cache policy 区分系统预装和 .deb 安装
        6. 用 dpkg-query -W 一次性补全所有包的版本、大小、描述

        返回格式：
          [{
              "name": "fcitx5",
              "version": "5.1.0",
              "installed_size": 12345,
              "description": "Fcitx5 input method framework",
              "is_manual": True,
              "installed_at": "2026-05-01T14:00:00"
          }, ...]
        """
        # Step 1: 解析所有 history.log 文件
        self._parse_all_history_logs()
        # Step 2: 一次性预加载所有包的 Depends 字段（dpkg-query -W）
        self._preload_depends()
        # Step 3: 获取当前系统已安装的包名集合
        installed = dpkg_utils.get_installed_packages()

        # ═══════════════════════════════════════════════════════
        # Step 4: 收集「用户显式安装」的包名
        # ═══════════════════════════════════════════════════════
        # 遍历所有 history.log 记录，找出用户在命令行中直接指定的包名。
        # 判断逻辑：
        #   - 操作类型必须是 "install"（remove/purge/upgrade 不算）
        #   - 命令行不包含自动化脚本的特征关键词
        #   - 包在命令行中被显式指定（名字匹配）且不是 automatic 标记
        #   - .deb 安装的 fallback：如果命令行是 ./xxx.deb 文件名，
        #     无法匹配任何实际包名，则所有非 auto 包都算父包

        user_installed: set[str] = set()
        for txn in self._transactions:
            if txn.get("operation") != "install":
                continue
            cmd = txn.get("command", "")
            # 跳过系统自动化脚本（特征关键词）
            # --force-unsafe-io: 系统升级时的内部参数
            # AutoInstall: unattended-upgrades 的标记
            # unattended: 无人值守安全更新
            if any(kw in cmd for kw in ("--force-unsafe-io", "AutoInstall", "unattended")):
                continue
            parent_names = self._extract_parent_name(cmd)
            txn_pkgs = txn.get("packages", [])

            # .deb 安装的 fallback：
            # 命令行可能是 "./Clash.Verge_2.4.7_amd64.deb"，
            # 实际包名是 "clash-verge"，永远匹配不上。
            # any_matched 检查是否至少有一个包名匹配了命令行。
            any_matched = any(
                self._clean_pkg_name(e["name"]) in parent_names
                for e in txn_pkgs
            )

            for entry in txn_pkgs:
                name = self._clean_pkg_name(entry["name"])
                is_auto = entry.get("is_automatic", False)
                # 父包 = 非 automatic +（名字在命令行中 或 没有匹配项时的 fallback）
                is_parent = not is_auto and (
                    name in parent_names or not any_matched
                )
                if is_parent:
                    user_installed.add(name)
                    self._all_known_packages.add(name)

        # ═══════════════════════════════════════════════════════
        # Step 5: 从 history.log 提取所有包的基本信息
        # ═══════════════════════════════════════════════════════
        # pkg_info 是中间数据结构：{包名: {name, version, is_manual, installed_at}}
        # 同一个包可能出现在多个 transaction 中（升级、重装），
        # 取最后一次用户安装的时间戳和版本。

        pkg_info: dict[str, dict] = {}  # name → info dict

        for txn in self._transactions:
            timestamp = txn.get("timestamp", "")
            cmd = txn.get("command", "")
            operation = txn.get("operation", "install")
            parent_names = self._extract_parent_name(cmd)

            # 只有用户 install 操作才能标记 is_manual / installed_at
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
                    # 父包判断：非 auto +（名字匹配 或 fallback）
                    is_parent = not is_auto and (
                        name in parent_names or not any_matched2
                    )
                else:
                    is_parent = False

                # 初始化或更新包信息
                if name not in pkg_info:
                    pkg_info[name] = {
                        "name": name,
                        "version": entry.get("version", ""),
                        "is_manual": is_parent,
                        "installed_at": timestamp if is_parent else "",
                    }
                else:
                    # 已存在：如果本次是父包，更新为最新信息
                    if is_parent:
                        pkg_info[name]["is_manual"] = True
                        if timestamp > pkg_info[name]["installed_at"]:
                            pkg_info[name]["installed_at"] = timestamp
                            pkg_info[name]["version"] = entry.get("version", "")

                # 记录父包的子包关系（用于 fetch_dependencies）
                if is_parent and name not in self._parent_packages:
                    self._parent_packages[name] = []

                self._all_known_packages.add(name)

        # ═══════════════════════════════════════════════════════
        # Step 6: apt-mark showmanual → 区分系统预装 vs .deb 安装
        # ═══════════════════════════════════════════════════════
        manual_set = self._get_apt_mark_manual()

        # 候选 .deb 包：在 manual_set 中，已安装，但在 history.log 中没有记录
        candidates = {
            n for n in manual_set
            if n in installed and n not in self._all_known_packages
        }
        deb_installed = set()
        if candidates:
            # apt-cache policy 批量检测：
            # 输出有 http:// → APT 仓库包（系统预装）
            # 输出只有 /var/lib/dpkg/status → 本地 .deb 包
            deb_installed = self._detect_local_only_packages(candidates)

        # 补全 .deb 包信息（不在任何 history.log 事务中）
        for name in deb_installed:
            self._all_known_packages.add(name)
            pkg_info[name] = {
                "name": name,
                "version": "",
                "is_manual": True,
                "installed_at": "手动安装",
            }

        # 补全系统预装包（当前已装但 history.log 中没有 → 装机时就在的）
        for pkg_name in installed:
            if pkg_name not in self._all_known_packages:
                self._all_known_packages.add(pkg_name)
                pkg_info[pkg_name] = {
                    "name": pkg_name,
                    "version": "",
                    "is_manual": True,
                    "installed_at": "",  # 空字符串 = 系统预装
                }

        # ═══════════════════════════════════════════════════════
        # Step 7: 组装结果 + 用 dpkg 补全信息
        # ═══════════════════════════════════════════════════════

        result = []
        for name, info in pkg_info.items():
            # 只保留当前已安装的包
            if name not in installed:
                continue

            # 所有在 apt-mark showmanual 中的包都是 is_manual=True
            if name in manual_set:
                info["is_manual"] = True
                # 系统预装：是 manual 但不是用户安装，也不是 .deb 安装
                if name not in user_installed and name not in deb_installed:
                    info["installed_at"] = ""  # 清空时间戳 = 系统预装

            result.append(info)

        # 用 dpkg-query -W 一次性补全所有包的版本、大小、描述
        # 这是关键的性能优化：一次调用替代逐个 dpkg -s
        print("  [apt] 查询包详情...", end="", flush=True)
        all_info = dpkg_utils.get_all_packages_info()
        for info in result:
            dpkg_data = all_info.get(info["name"], {})
            # 只在没有版本信息时才用 dpkg 数据补充
            if not info["version"]:
                info["version"] = dpkg_data.get("version", "")
            info["installed_size"] = dpkg_data.get("size_kb", 0)
            info["description"] = dpkg_data.get("description", "")
        print(f" 完成 ({len(all_info)} 个包)")

        print()  # dpkg 进度行换行
        return result

    def fetch_package_names(self) -> set[str]:
        """快速获取当前已安装的 APT 包名。

        用于增量扫描，只需要包名集合来与数据库对比。
        一次 dpkg --get-selections < 0.1 秒。
        """
        return dpkg_utils.get_installed_packages()

    def fetch_single_package(self, name: str) -> dict:
        """获取单个包的完整信息。

        用于增量扫描：发现新包时，不需要重新解析所有 history.log，
        只需要查这一个包的信息。但为了判断分类，需要先解析 history.log。

        分类逻辑与 fetch_packages() 一致（详见上方注释）。
        """
        info = dpkg_utils.get_package_info(name)

        # 延迟加载 history.log（只在需要时解析）
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

        # .deb 安装检测
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
        """一次 dpkg-query -W 获取所有包的 Depends 字段。

        性能关键：用一次子进程调用替代逐个 dpkg -s。
        解析格式：${Package}\t${Depends}\n
        例如：
          fcitx5    libfcitx5core7 (>= 5.1.0), libc6 (>= 2.34), ...
        解析后存入 _depends_cache：{"fcitx5": ["libfcitx5core7", "libc6", ...]}
        """
        try:
            out = subprocess.check_output(
                ["dpkg-query", "-W", "-f", "${Package}\\t${Depends}\\n"],
                stderr=subprocess.DEVNULL, text=True,
            )
            for line in out.strip().split("\n"):
                if "\t" not in line:
                    continue
                parts = line.split("\t", 1)
                # 去掉架构后缀（如 "libc6:amd64" → "libc6"）
                name = parts[0].split(":")[0]
                dep_str = parts[1] if len(parts) > 1 else ""
                if dep_str:
                    # 解析 "pkg1 (>= ver1), pkg2, pkg3 (>= ver2)" 格式
                    # 我们只需要包名，不需要版本约束
                    dep_names = []
                    for dep_part in dep_str.split(","):
                        # 取空格分割的第一段作为包名，去掉架构后缀
                        dn = dep_part.strip().split()[0].split(":")[0] if dep_part.strip() else ""
                        if dn:
                            dep_names.append(dn)
                    self._depends_cache[name] = dep_names
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

    def fetch_dependencies(self, pkg_name: str) -> list[dict]:
        """返回某个父包的依赖列表。

        依赖来自两个方面，合并去重：

        1. history.log 中同一次安装的 automatic 包
           这是「实际拉入的依赖」——APT 在这个事务中自动选择了它们。
           例如：apt install fcitx5 → fcitx5 (非auto) + libfcitx5core7 (auto)

        2. dpkg-query 预加载的 Depends 缓存
           这是「声明的依赖」——包的元数据中说它需要哪些包。
           用于补充 history.log 中遗漏的依赖（比如系统预装包的依赖）。
        """
        deps: list[dict] = []

        # 方法 1: 从 history.log 中查找同一次安装的 automatic 包
        for txn in self._transactions:
            cmd = txn.get("command", "")
            parent_names = self._extract_parent_name(cmd)
            # 筛选包含目标包的操作
            if pkg_name not in parent_names:
                continue

            for entry in txn.get("packages", []):
                name = self._clean_pkg_name(entry["name"])
                # 只取 automatic 标记的（自动拉入的依赖）
                if entry.get("is_automatic", False):
                    deps.append({
                        "name": name,
                        "version": entry.get("version", ""),
                        "is_automatic": True,
                    })

        # 方法 2: 从 Depends 字段补充 history.log 中遗漏的依赖
        for dep_name in self._depends_cache.get(pkg_name, []):
            if not any(d["name"] == dep_name for d in deps):
                deps.append({
                    "name": dep_name,
                    "version": "",
                    "is_automatic": True,
                })

        # 过滤自引用（极少数情况包声称依赖自己）
        return [d for d in deps if d["name"] != pkg_name]

    # ═══════════════════════════════════════════════════════
    # 内部方法 — history.log 解析
    # ═══════════════════════════════════════════════════════

    def _parse_all_history_logs(self):
        """解析所有 history.log 文件（含轮转的旧日志）。

        history.log 的轮转机制：
          history.log     ← 当前日志
          history.log.1.gz ← 上一次轮转（已压缩）
          history.log.2.gz ← 更早的

        我们只解析非压缩文件（glob 默认不匹配 .gz），
        因为压缩文件需要 zcat 额外处理，且通常已经很旧。
        """
        self._transactions = []
        pattern = f"{self._history_dir}/{HISTORY_PATTERN}"

        # glob.glob 按文件名排序，保证时间顺序
        for fpath in sorted(glob.glob(pattern)):
            try:
                self._transactions.extend(self._parse_file(fpath))
            except (PermissionError, OSError):
                continue

    def _parse_file(self, fpath: str) -> list[dict]:
        """解析单个 history.log 文件，返回 transaction 列表。

        APT history.log 格式示例：

          Start-Date: 2026-05-07  20:31:31
          Commandline: apt install fcitx5
          Requested-By: rensen (1000)
          Install: fcitx5:amd64 (5.1.0), libfcitx5core7:amd64 (5.1.0, automatic)
          End-Date: 2026-05-07  20:31:45

        注意事项：
          - Install/Remove/Purge/Upgrade 行可能跨多行（续行符 \\）
          - Error-Date 表示操作失败，应跳过
          - 文件末尾可能没有 End-Date（未正常关闭的日志）

        每个 transaction 的结构：
          {
              "timestamp": "2026-05-07T20:31:31",   # ISO8601
              "command": "apt install fcitx5",
              "operation": "install",
              "user": "rensen",
              "packages": [
                  {"name": "fcitx5:amd64", "version": "5.1.0", "is_automatic": False},
                  {"name": "libfcitx5core7:amd64", "version": "5.1.0", "is_automatic": True},
              ]
          }
        """
        transactions = []
        current_txn: dict | None = None   # 当前正在解析的 transaction
        install_buffer: list[str] = []    # 多行 Install 续行的缓冲
        in_install = False                # 是否正在解析 Install 行

        with open(fpath, errors="replace") as f:
            for raw_line in f:
                line = raw_line.rstrip("\n")

                # ── Start-Date: 新 transaction 开始 ──
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

                # ── Commandline: 用户执行的命令 ──
                if line.startswith("Commandline:"):
                    current_txn["command"] = line[12:].strip()
                    current_txn["operation"] = self._detect_operation(
                        current_txn["command"]
                    )
                    continue

                # ── Requested-By: 执行者 ──
                if line.startswith("Requested-By:"):
                    # 格式: "rensen (1000)"，取第一个空格前的用户名
                    current_txn["user"] = line[13:].strip().split()[0]
                    continue

                # ── Install / Remove / Purge / Upgrade — 可能多行 ──
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

                # ── 续行（以空格或 Tab 开头） ──
                if in_install and line and line[0] in (" ", "\t"):
                    install_buffer.append(line.strip())
                    continue

                # ── End-Date: 当前 transaction 结束 ──
                if line.startswith("End-Date:"):
                    if in_install and install_buffer:
                        full_install = "".join(install_buffer)
                        # 去掉续行符（反斜杠）
                        full_install = full_install.replace("\\", "")
                        current_txn["packages"] = self._parse_package_list(full_install)
                    # 只有包含包列表的 transaction 才保留
                    if current_txn["packages"]:
                        transactions.append(current_txn)
                    current_txn = None
                    in_install = False
                    install_buffer = []
                    continue

                # ── Error-Date: 操作失败，丢弃 ──
                if line.startswith("Error-Date:"):
                    current_txn = None
                    in_install = False
                    install_buffer = []

        # 处理文件末尾没有 End-Date 的情况（日志未正常关闭）
        if current_txn and in_install and install_buffer:
            full_install = "".join(install_buffer).replace("\\", "")
            current_txn["packages"] = self._parse_package_list(full_install)
            if current_txn["packages"]:
                transactions.append(current_txn)

        return transactions

    def _parse_package_list(self, text: str) -> list[dict]:
        """解析包列表字符串。

        输入：
          "fcitx5:amd64 (5.1.0), libfcitx5core7:amd64 (5.1.0, automatic)"

        输出：
          [
            {"name": "fcitx5:amd64", "version": "5.1.0", "is_automatic": False},
            {"name": "libfcitx5core7:amd64", "version": "5.1.0", "is_automatic": True},
          ]

        使用正则表达式匹配「包名 (版本信息)」的模式。
        版本信息中如果有 "automatic" 字样，标记为自动依赖。
        """
        packages = []
        # 正则说明：
        #   ([^\s,]+(?::\w+)?)  — 包名，可能带架构后缀（如 :amd64）
        #   \s*\(([^)]+)\)      — 括号内的版本和标记
        pattern = re.compile(r"([^\s,]+(?::\w+)?)\s*\(([^)]+)\)")
        for match in pattern.finditer(text):
            full_name = match.group(1)      # "fcitx5:amd64"
            details = match.group(2)         # "5.1.0, automatic" 或 "5.1.0"
            is_auto = "automatic" in details.lower()
            # 版本是 details 中逗号前的部分
            version = details.split(",")[0].strip() if details else ""
            packages.append({
                "name": full_name,
                "version": version,
                "is_automatic": is_auto,
            })
        return packages

    # ═══════════════════════════════════════════════════════
    # 内部方法 — 工具函数
    # ═══════════════════════════════════════════════════════

    @staticmethod
    def _clean_pkg_name(raw_name: str) -> str:
        """去掉架构后缀。

        例如：'fcitx5:amd64' → 'fcitx5'
              'libc6:i386'  → 'libc6'

        APT 在 history.log 中会标注包的架构（:amd64, :i386, :all），
        但我们内部统一用纯包名（与 dpkg 的 Package 字段对齐）。
        """
        return raw_name.split(":")[0]

    @staticmethod
    def _extract_parent_name(command: str) -> set[str]:
        """从命令行提取用户主动指定的包名。

        这是判断「用户安装了哪些包」的关键函数。

        处理步骤：
          1. 去掉命令前缀（apt、apt-get、aptitude）
          2. 去掉选项（-y, --yes 等）
          3. 去掉操作词（install, remove 等）
          4. 剩余的空格分隔词就是用户指定的包名

        输入示例：
          "apt-get --yes install mpv vim"    → {"mpv", "vim"}
          "apt install ./package.deb"        → {"package.deb"}  (文件名，非包名)
          "apt install fcitx5"               → {"fcitx5"}
        """
        # 去掉命令前缀
        cmd = re.sub(r"^(apt|apt-get|aptitude)\s+", "", command)
        # 去掉选项（以 - 开头的词）
        cmd = re.sub(r"(^|\s)-\S+", " ", cmd)
        # 去掉操作词
        cmd = cmd.strip()
        cmd = re.sub(r"^(install|remove|purge|upgrade|autoremove|dist-upgrade)\s*", "", cmd)

        names = set()
        for part in cmd.split():
            part = part.strip()
            # 排除残留的选项标记（如单独的 -）
            if part and not part.startswith("-"):
                # 去掉可能的架构后缀
                names.add(part.split(":")[0])
        return names

    @staticmethod
    def _detect_operation(command: str) -> str:
        """从命令行判断操作类型。

        优先级：purge > remove > upgrade > install
        （purge 命令行中通常也包含 remove，所以先检查）
        """
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
        """将 history.log 中的时间转为 ISO8601 格式。

        输入："2026-05-07  20:31:31"（注意中间可能有两个空格）
        输出："2026-05-07T20:31:31"

        处理步骤：
          1. 合并多余空格
          2. 用 datetime.strptime 解析并转 ISO8601
        """
        ts = re.sub(r"\s+", " ", ts).strip()
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            return dt.isoformat()
        except ValueError:
            # 解析失败：返回原始字符串（至少不会丢失数据）
            return ts

    @staticmethod
    def _detect_local_only_packages(names: set[str]) -> set[str]:
        """通过 apt-cache policy 批量检测仅有本地 .deb 来源的包。

        原理：
          正常 APT 仓库包的 policy 输出包含 http://... 的仓库地址。
          .deb 手动安装的包只有 /var/lib/dpkg/status（本地 dpkg 数据库）。
          通过检查是否包含 "http://" 来区分。

        示例输出差异：
          # APT 仓库包
          fcitx5:
            Installed: 5.1.0
            Candidate: 5.1.0
            Version table:
           *** 5.1.0 500
                  500 http://archive.ubuntu.com/ubuntu noble/main amd64 Packages  ← 有 http

          # .deb 本地包
          clash-verge:
            Installed: 2.4.7
            Candidate: (none)
            Version table:
           *** 2.4.7 100
                  100 /var/lib/dpkg/status  ← 只有本地状态

        用 apt-cache policy 批量调用（一次子进程传多个包名），高效。
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
                # 新包的开始（非缩进行）
                # 保存上一个包的判断结果
                if current_pkg and not has_http and current_pkg != "N:":
                    result.add(current_pkg)
                current_pkg = line.rstrip(":")
                has_http = False
            elif "http://" in line:
                # 发现有远程仓库 → 这是 APT 仓库包
                has_http = True
        # 最后一个包的判断
        if current_pkg and not has_http and current_pkg != "N:":
            result.add(current_pkg)
        return result

    @staticmethod
    def _get_apt_mark_manual() -> set[str]:
        """调用 apt-mark showmanual 获取手动安装的包列表。

        apt-mark showmanual 列出所有被标记为「手动安装」的包。
        这包括：
          - 用户显式 apt install 的包
          - 系统预装的包
          - .deb 手动安装的包

        这是一个静态快照，不区分来源。我们用它和 history.log 交叉对比来分类。
        """
        try:
            out = subprocess.check_output(
                ["apt-mark", "showmanual"], stderr=subprocess.DEVNULL, text=True
            )
            # 去掉架构后缀，去重
            return {line.strip().split(":")[0] for line in out.strip().split("\n") if line}
        except (subprocess.CalledProcessError, FileNotFoundError):
            return set()
