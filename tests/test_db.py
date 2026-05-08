"""Step 1 验证脚本 — 测试数据库层是否正常工作。"""
import sys
import tempfile
import os

sys.path.insert(0, "/home/rensen/projects/app_manager")

from src.db.database import Database
from src.db.models import Package, Dependency, PackageFile

schema = "/home/rensen/projects/app_manager/data/schema.sql"

with tempfile.TemporaryDirectory() as tmpdir:
    db_path = os.path.join(tmpdir, "test.db")
    db = Database(db_path, schema)

    # 测试 1: 插入包
    p1 = Package(name="fcitx5", source="apt", version="5.1.0",
                 installed_size=12000, is_manual=True, installed_at="2026-05-01")
    p2 = Package(name="libfcitx5core7", source="apt", version="5.1.0",
                 installed_size=500, is_manual=False, installed_at="2026-05-01")
    p3 = Package(name="firefox", source="snap", version="125.0",
                 installed_size=290000, is_manual=True)

    id1 = db.upsert_package(p1)
    id2 = db.upsert_package(p2)
    id3 = db.upsert_package(p3)
    print(f"[OK] 插入 3 个包，id: {id1}, {id2}, {id3}")

    # 测试 2: 查询所有包
    all_pkgs = db.get_all_packages()
    assert len(all_pkgs) == 3, f"期望 3 个包，得到 {len(all_pkgs)}"
    print(f"[OK] 查询所有包: {len(all_pkgs)} 个")

    # 测试 3: 按来源查询
    apt_pkgs = db.get_all_packages(source="apt")
    snap_pkgs = db.get_all_packages(source="snap")
    assert len(apt_pkgs) == 2
    assert len(snap_pkgs) == 1
    print(f"[OK] APT: {len(apt_pkgs)} 个, Snap: {len(snap_pkgs)} 个")

    # 测试 4: 父包查询
    parents = db.get_parent_packages()
    assert len(parents) == 2  # fcitx5 + firefox
    print(f"[OK] 父包: {len(parents)} 个")

    # 测试 5: upsert 更新
    p1_updated = Package(name="fcitx5", source="apt", version="5.1.1",
                         installed_size=12500, is_manual=True)
    id1b = db.upsert_package(p1_updated)
    assert id1b == id1
    pkg = db.get_package(id1)
    assert pkg.version == "5.1.1"
    print(f"[OK] upsert 更新版本: {pkg.version}")

    # 测试 6: 依赖关系
    db.add_dependency(id1, id2, is_automatic=True)
    deps = db.get_dependencies(id1)
    assert len(deps) == 1
    assert deps[0][0].name == "libfcitx5core7"
    assert deps[0][1] == True  # is_automatic
    print(f"[OK] 依赖关系: {deps[0][0].name} (auto={deps[0][1]})")

    # 测试 7: 隐藏
    db.set_package_hidden(id1, True)
    parents_after = db.get_parent_packages()
    assert len(parents_after) == 1  # 只剩 firefox
    hidden = db.get_hidden_packages()
    assert len(hidden) == 1
    print(f"[OK] 隐藏后父包: {len(parents_after)} 个, 已隐藏: {len(hidden)} 个")

    # 测试 8: 搜索
    results = db.search_packages("fcitx")
    assert len(results) == 2  # fcitx5 + libfcitx5core7
    print(f"[OK] 搜索 'fcitx': {len(results)} 个结果")

    # 测试 9: 清除来源
    db.clear_source_data("snap")
    assert db.get_package_count("snap") == 0
    assert db.get_package_count() == 2  # 只剩 apt 的两个
    print(f"[OK] 清除 snap 后还剩 {db.get_package_count()} 个包")

    # 测试 10: 计数
    assert db.get_package_count("apt") == 2
    print(f"[OK] APT 包数: {db.get_package_count('apt')}")

    db.close()
    print("\n=== 所有测试通过 ===")
