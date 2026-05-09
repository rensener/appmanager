"""扫描器 — 从各 Provider 拉取数据，写入数据库，支持增量更新。

============================================================
扫描器的两种模式
============================================================

1. 全量扫描（scan_all）：
   --scan 参数触发。重新解析所有 Provider 的完整输出，重写数据库。
   适用于：第一次运行、分类逻辑改变、数据库损坏修复。

2. 增量扫描（scan_incremental）：
   默认模式。只对比系统当前包名和数据库中的包名。
   新增的包 → 调 fetch_single_package() 获取详情并插入
   删除的包 → 调 delete_package_by_name() 清理
   适用：日常使用，通常 < 1 秒完成。

为什么需要两种模式？
  全量慢但完整（重新解析 history.log，分类最准确）。
  增量快但粗糙（只对比包名，已存在的包不做任何更新）。
  用户启动时默认增量，需要时手动 --scan。

============================================================
性能关键：bulk_write()
============================================================

正常 Database 操作是每行自动 commit 的。但扫描 1830 个包时，
commit 1830 次会产生大量磁盘 IO。

bulk_write() 上下文管理器把扫描过程中所有写操作合并成一个事务：
  with db.bulk_write():
      for pkg in 1830_packages:
          db.upsert_package(pkg)  # 不 commit
          db.add_dependency(...)   # 不 commit
  # ← 退出 with 时一次性 commit

这个优化让全量扫描从「几十秒」降到「2-3 秒」。
"""

from src.db.database import Database
from src.db.models import Package, Dependency, InstallHistory, HistoryPackage
from src.providers import BaseProvider


def scan_all(db: Database, providers: list[BaseProvider]) -> dict[str, int]:
    """全量扫描（--build）：重扫所有 Provider，完整更新数据库。

    流程：
      1. 遍历每个可用的 Provider
      2. 调 provider.fetch_packages() 获取所有包
      3. 用 bulk_write() 包裹，批量写入数据库
      4. 对每个父包，调 provider.fetch_dependencies() 获取依赖
      5. 依赖可能不在数据库中 → 先 upsert，再建立依赖关系
      6. 最后写入 APT 的安装历史

    Args:
        db: 数据库实例
        providers: 可用的 Provider 列表

    Returns:
        {source: count} 每个来源写入的包数量
    """
    counts: dict[str, int] = {}

    for provider in providers:
        if not provider.is_available():
            continue

        source = provider.name

        # Step 1: 获取该来源的所有包
        try:
            pkgs = provider.fetch_packages()
        except Exception as e:
            print(f"[警告] {source} 扫描失败: {e}")
            continue

        total = len(pkgs)
        print(f"[扫描] {source}: 发现 {total} 个包，正在写入数据库...")

        # Step 2: 批量事务中写入所有包
        with db.bulk_write():
            parents_count = 0
            for i, pkg_data in enumerate(pkgs):
                # 创建 Package 对象并写入数据库
                pkg = Package(
                    name=pkg_data["name"],
                    source=source,
                    version=pkg_data.get("version", ""),
                    installed_size=pkg_data.get("installed_size", 0),
                    description=pkg_data.get("description", ""),
                    is_manual=pkg_data.get("is_manual", True),
                    installed_at=pkg_data.get("installed_at", ""),
                )
                package_id = db.upsert_package(pkg)

                if pkg.is_manual:
                    parents_count += 1

                # 获取并存储依赖关系
                try:
                    deps = provider.fetch_dependencies(pkg_data["name"])
                except Exception:
                    deps = []  # 依赖获取失败不阻塞主流程

                for dep_data in deps:
                    dep_name = dep_data["name"]
                    # 检查依赖是否已在数据库中
                    existing_id = db.get_package_id(dep_name, source)
                    # APT 特殊处理：如果依赖不在数据库，用 dpkg 补充信息
                    if existing_id is None and source == "apt":
                        from src.utils import dpkg_utils
                        dpkg_info = dpkg_utils.get_package_info(dep_name)
                        dep_data.setdefault("version", dpkg_info.get("version", ""))
                        dep_data.setdefault("installed_size", dpkg_info.get("size_kb", 0))
                        dep_data.setdefault("description", dpkg_info.get("description", ""))

                    # 先写入依赖包（is_manual=False，因为它是被拉入的）
                    dep_pkg = Package(
                        name=dep_name,
                        source=source,
                        version=dep_data.get("version", ""),
                        installed_size=dep_data.get("installed_size", 0),
                        description=dep_data.get("description", ""),
                        is_manual=False,
                    )
                    child_id = db.upsert_package(dep_pkg)
                    # 建立父包→子包的依赖关系
                    db.add_dependency(
                        package_id, child_id,
                        is_automatic=dep_data.get("is_automatic", True),
                    )

                # 进度提示（同一行刷新，\r 回到行首）
                print(
                    f"\r  [{source}] 写入进度: {i + 1}/{total}  (父包: {parents_count})",
                    end="", flush=True,
                )

        print()  # 进度行换行

        # Step 3: 包全部写入后，再存储安装历史（需要包 ID 已存在）
        _store_install_history(db, provider)

        counts[source] = total
        print(f"[完成] {source}: {total} 个包, 其中 {parents_count} 个父包")

    return counts


def scan_incremental(db: Database, providers: list[BaseProvider]) -> dict[str, int]:
    """增量扫描（--scan）：快速对比系统与数据库，只处理新增/删除的包。

    核心思路：用 Python 集合运算对比差异。

    流程：
      1. 获取当前系统包名集合（dpkg --get-selections，一次调用）
      2. 获取数据库包名集合（SELECT name FROM packages WHERE source=?）
      3. 集合差集运算：
         new_names = system - db        → 新增的包
         removed_names = db - system    → 已卸载的包
      4. 对每个新包调 fetch_single_package() 获取详情
      5. 删除已卸载的包（级联删除依赖和文件记录）

    性能：APT 1830 个包，对比只需 < 0.1 秒。
    """
    counts: dict[str, int] = {}

    for provider in providers:
        if not provider.is_available():
            continue

        source = provider.name

        # Step 1: 获取当前系统包名（快速路径）
        # fetch_package_names() 只返回包名集合，不获取完整信息
        if hasattr(provider, "fetch_package_names"):
            current_names = provider.fetch_package_names()
        else:
            # 降级：Provider 没有快速路径，走完整获取
            try:
                pkgs = provider.fetch_packages()
                current_names = {p["name"] for p in pkgs}
            except Exception as e:
                print(f"[警告] {source} 获取包列表失败: {e}")
                continue

        # Step 2: 获取数据库中的包名
        db_names = db.get_package_names(source)

        # Step 3: 集合运算对比差异
        new_names = current_names - db_names       # 新增
        removed_names = db_names - current_names   # 已删除

        print(f"[扫描] {source}: 系统 {len(current_names)} 个, "
              f"数据库 {len(db_names)} 个, "
              f"新增 {len(new_names)} 个, 已删除 {len(removed_names)} 个")

        # Step 4: 处理新增的包（批量事务）
        added = 0
        with db.bulk_write():
            for name in sorted(new_names):
                # fetch_single_package() 只查一个包，比 fetch_packages() 快得多
                if hasattr(provider, "fetch_single_package"):
                    info = provider.fetch_single_package(name)
                else:
                    # 降级：Provider 不支持单包查询，走完整获取后筛选
                    try:
                        all_pkgs = provider.fetch_packages()
                        match = next((p for p in all_pkgs if p["name"] == name), None)
                        info = match if match else {"name": name}
                    except Exception:
                        info = {"name": name}

                pkg = Package(
                    name=name,
                    source=source,
                    version=info.get("version", ""),
                    installed_size=info.get("installed_size", 0),
                    description=info.get("description", ""),
                    is_manual=info.get("is_manual", True),
                    installed_at=info.get("installed_at", ""),
                )
                package_id = db.upsert_package(pkg)

                # 获取新包的依赖
                try:
                    deps = provider.fetch_dependencies(name)
                except Exception:
                    deps = []

                for dep_data in deps:
                    dep_name = dep_data["name"]
                    existing_id = db.get_package_id(dep_name, source)
                    if existing_id is None and source == "apt":
                        from src.utils import dpkg_utils
                        dpkg_info = dpkg_utils.get_package_info(dep_name)
                        dep_data.setdefault("version", dpkg_info.get("version", ""))
                        dep_data.setdefault("installed_size", dpkg_info.get("size_kb", 0))
                        dep_data.setdefault("description", dpkg_info.get("description", ""))

                    dep_pkg = Package(
                        name=dep_name,
                        source=source,
                        version=dep_data.get("version", ""),
                        installed_size=dep_data.get("installed_size", 0),
                        description=dep_data.get("description", ""),
                        is_manual=False,
                    )
                    child_id = db.upsert_package(dep_pkg)
                    db.add_dependency(
                        package_id, child_id,
                        is_automatic=dep_data.get("is_automatic", True),
                    )

                added += 1
                if added % 50 == 0:
                    print(f"  [{source}] 已处理 {added}/{len(new_names)} 个新包")

        # Step 5: 删除已卸载的包
        for name in removed_names:
            db.delete_package_by_name(name, source)

        if removed_names:
            print(f"  [{source}] 清理了 {len(removed_names)} 个已卸载的包")

        # Step 6: 更新安装历史（APT）
        _store_install_history(db, provider)

        counts[source] = len(current_names)

    return counts


def _store_install_history(db: Database, provider: BaseProvider):
    """存储 APT 的安装历史记录。

    必须在包已写入数据库之后调用，因为 history_packages 表
    需要通过 package_id 关联到 packages 表。

    为什么只有 APT 有历史记录？
      只有 APT 的 /var/log/apt/history.log 记录了完整的安装历史。
      Snap/Flatpak/Brew 只有当前状态快照，没有时间线。
      AppImage 更是连包管理器都没有。
    """
    if provider.name != "apt":
        return

    # AptProvider 在 fetch_packages() 时会解析 history.log，
    # 结果存在 _transactions 属性中
    if not hasattr(provider, "_transactions"):
        return

    transactions = getattr(provider, "_transactions", [])
    print(f"  [apt] 写入安装历史 ({len(transactions)} 条记录)...")

    stored = 0
    errors = 0
    for txn in transactions:
        # 先写入一条安装历史记录
        history = InstallHistory(
            timestamp=txn.get("timestamp", ""),
            source="apt",
            command=txn.get("command", ""),
            operation=txn.get("operation", "install"),
            user=txn.get("user", ""),
        )
        history_id = db.add_install_history(history)
        if history_id <= 0:
            errors += 1
            continue

        # 再写关联的包（主包 + 依赖）
        for entry in txn.get("packages", []):
            # 包名去掉架构后缀（history.log 中是 "fcitx5:amd64"）
            name = entry["name"].split(":")[0]
            pkg_id = db.get_package_id(name, "apt")
            if not pkg_id:
                continue  # 包不在数据库中（可能已被卸载）

            try:
                hp = HistoryPackage(
                    history_id=history_id,
                    package_id=pkg_id,
                    is_parent=not entry.get("is_automatic", False),
                    is_automatic=entry.get("is_automatic", False),
                    version=entry.get("version", ""),
                )
                db.add_history_package(hp)
                stored += 1
            except Exception as e:
                errors += 1
                # 只打印前 3 个错误，避免刷屏
                if errors <= 3:
                    print(f"  [警告] 历史记录写入失败: "
                          f"history_id={history_id}, package={name}, "
                          f"pkg_id={pkg_id}, 错误={e}")

    print(f"  [apt] 安装历史写入完成 ({stored} 条包关联, {errors} 错误)")
