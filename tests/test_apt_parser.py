"""Step 2 验证脚本 — 测试 APT history.log 解析器。"""
import sys
import tempfile
import os

sys.path.insert(0, "/home/rensen/projects/app_manager")

from src.providers.apt import AptProvider

fixtures_dir = "/home/rensen/projects/app_manager/tests/fixtures"

# 创建 provider，指向 fixtures 目录
provider = AptProvider(history_dir=fixtures_dir)

# 测试：解析 history.log
provider._parse_all_history_logs()
print(f"[OK] 解析到 {len(provider._transactions)} 个事务")

# 验证 3 个事务
assert len(provider._transactions) == 3, f"期望 3 个事务，实际 {len(provider._transactions)}"

# 验证第一个事务：ubuntu-desktop
txn1 = provider._transactions[0]
print(f"\n事务 1: {txn1['command']}")
print(f"  时间: {txn1['timestamp']}")
print(f"  包数: {len(txn1['packages'])}")
assert txn1["operation"] == "install"
assert len(txn1["packages"]) == 3  # ubuntu-desktop + 2 dependencies

# 检查 parent 识别
parent_names = provider._extract_parent_name(txn1["command"])
assert "ubuntu-desktop" in parent_names
print(f"  父包名: {parent_names}")

# 检查 automatic 标记
for p in txn1["packages"]:
    name = provider._clean_pkg_name(p["name"])
    if name == "ubuntu-desktop":
        assert p["is_automatic"] == False, "ubuntu-desktop 不应标记为 automatic"
    elif name in ("ubuntu-minimal", "alsa-base"):
        assert p["is_automatic"] == True, f"{name} 应该标记为 automatic"
print(f"  [OK] automatic 标记正确")

# 验证第二个事务：fcitx5
txn2 = provider._transactions[1]
print(f"\n事务 2: {txn2['command']}")
print(f"  用户: {txn2['user']}")
assert txn2["user"] == "renson"
assert len(txn2["packages"]) == 2

# 验证第三个事务：mpv
txn3 = provider._transactions[2]
print(f"\n事务 3: {txn3['command']}")
assert provider._extract_parent_name(txn3["command"]) == {"mpv"}

# 测试 _clean_pkg_name
assert provider._clean_pkg_name("fcitx5:amd64") == "fcitx5"
assert provider._clean_pkg_name("mpv") == "mpv"
print("\n[OK] _clean_pkg_name 正确")

# 测试 _normalize_timestamp
ts = provider._normalize_timestamp("2026-05-07  20:31:31")
assert ts == "2026-05-07T20:31:31", f"时间格式错误: {ts}"
print(f"[OK] 时间格式化: {ts}")

# 测试 _detect_operation
assert provider._detect_operation("apt install fcitx5") == "install"
assert provider._detect_operation("apt purge synaptic") == "purge"
assert provider._detect_operation("apt autoremove") == "remove"
print("[OK] 操作类型检测正确")

print("\n=== 所有 Step 2 测试通过 ===")
