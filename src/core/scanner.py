"""扫描器 — 从各 Provider 拉取数据，写入数据库，支持增量更新。"""

from src.db.database import Database
from src.db.models import Package, Dependency, InstallHistory, HistoryPackage
from src.providers import BaseProvider


def scan_all(db: Database, providers: list[BaseProvider]) -> dict[str, int]:
    """全量扫描（--build）：重扫所有 Provider，完整更新数据库。"""
    counts: dict[str, int] = {}

    for provider in providers:
        if not provider.is_available():
            continue

        source = provider.name

        try:
            pkgs = provider.fetch_packages()
        except Exception as e:
            print(f"[警告] {source} 扫描失败: {e}")
            continue

        total = len(pkgs)
        print(f"[扫描] {source}: 发现 {total} 个包，正在写入数据库...")

        # Step 1: 插入/更新所有包（批量事务）
        with db.bulk_write():
            parents_count = 0
            for i, pkg_data in enumerate(pkgs):
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

                # 获取并存储依赖
                try:
                    deps = provider.fetch_dependencies(pkg_data["name"])
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

                # 进度：同一行刷新
                print(f"\r  [{source}] 写入进度: {i + 1}/{total}  (父包: {parents_count})", end="", flush=True)

        print()  # 进度行换行

        # Step 2: 包全部写入后，再存储安装历史（需要包 ID 已存在）
        _store_install_history(db, provider)

        counts[source] = total
        print(f"[完成] {source}: {total} 个包, 其中 {parents_count} 个父包")

    return counts


def scan_incremental(db: Database, providers: list[BaseProvider]) -> dict[str, int]:
    """增量扫描（--scan）：快速对比系统与数据库，只处理新增/删除的包。

    速度来源：APT 用 dpkg --get-selections（一次调用）代替逐个 dpkg -s（1830 次调用）。
    """
    counts: dict[str, int] = {}

    for provider in providers:
        if not provider.is_available():
            continue

        source = provider.name

        # Step 1: 获取当前系统包名（快速路径）
        if hasattr(provider, "fetch_package_names"):
            current_names = provider.fetch_package_names()
        else:
            try:
                pkgs = provider.fetch_packages()
                current_names = {p["name"] for p in pkgs}
            except Exception as e:
                print(f"[警告] {source} 获取包列表失败: {e}")
                continue

        # Step 2: 获取数据库中的包名
        db_names = db.get_package_names(source)

        # Step 3: 对比
        new_names = current_names - db_names
        removed_names = db_names - current_names

        print(f"[扫描] {source}: 系统 {len(current_names)} 个, 数据库 {len(db_names)} 个, "
              f"新增 {len(new_names)} 个, 已删除 {len(removed_names)} 个")

        # Step 4: 处理新增的包（批量事务）
        added = 0
        with db.bulk_write():
            for name in sorted(new_names):
                if hasattr(provider, "fetch_single_package"):
                    info = provider.fetch_single_package(name)
                else:
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
    """存储 APT 的安装历史（必须在包已写入数据库后调用）。"""
    if provider.name != "apt":
        return

    if not hasattr(provider, "_transactions"):
        return

    transactions = getattr(provider, "_transactions", [])
    print(f"  [apt] 写入安装历史 ({len(transactions)} 条记录)...")

    stored = 0
    errors = 0
    for txn in transactions:
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

        for entry in txn.get("packages", []):
            name = entry["name"].split(":")[0]
            pkg_id = db.get_package_id(name, "apt")
            if not pkg_id:
                continue

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
                if errors <= 3:
                    print(f"  [警告] 历史记录写入失败: history_id={history_id}, package={name}, pkg_id={pkg_id}, 错误={e}")

    print(f"  [apt] 安装历史写入完成 ({stored} 条包关联, {errors} 错误)")
