"""App Manager 入口 — 扫描包管理器 → 启动 TUI。

用法:
  appmanager          默认增量扫描（秒级）
  appmanager --scan   全量重扫，完整更新所有包信息
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    full_scan = "--scan" in sys.argv

    from src.db.database import Database
    from src.core.scanner import scan_all, scan_incremental
    from src.providers.apt import AptProvider
    from src.providers.snap import SnapProvider
    from src.providers.flatpak import FlatpakProvider
    from src.providers.brew import BrewProvider
    from src.providers.appimage import AppImageProvider
    from src.tui.app import AppManagerApp

    # 数据库路径
    data_dir = os.path.expanduser("~/.local/share/app-manager")
    db_path = os.path.join(data_dir, "apps.db")
    schema_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "schema.sql"
    )

    print("初始化数据库...")
    db = Database(db_path, schema_path)

    # 创建各 Provider
    providers = [
        AptProvider(),
        SnapProvider(),
        FlatpakProvider(),
        BrewProvider(),
        AppImageProvider(),
    ]

    print("检测可用的包管理器...")
    available = [p for p in providers if p.is_available()]
    print(f"发现: {', '.join(p.name for p in available)}")

    total = db.get_package_count()

    if full_scan:
        print("全量扫描模式...")
        counts = scan_all(db, available)
    elif total == 0:
        print("数据库为空，首次运行自动执行全量扫描...")
        counts = scan_all(db, available)
    else:
        print(f"数据库已有 {total} 个包，执行增量扫描...")
        counts = scan_incremental(db, available)

    if not counts:
        print("未找到任何包。请确认已安装支持的包管理器。")
        print("继续启动 TUI（将显示空列表）...")

    print(f"\n启动终端界面...")
    app = AppManagerApp(db)
    app.run()

    db.close()


if __name__ == "__main__":
    main()
