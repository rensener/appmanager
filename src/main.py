"""App Manager 入口 — 扫描包管理器 → 启动 TUI。

============================================================
程序启动流程
============================================================

  main()
    │
    ├─ 1. 解析命令行参数（--scan = 全量扫描）
    │
    ├─ 2. 初始化数据库
    │      Database(data_dir/apps.db, data/schema.sql)
    │      如果 apps.db 不存在 → 自动创建 + 建表
    │
    ├─ 3. 创建所有 Provider 实例
    │      [AptProvider(), SnapProvider(), FlatpakProvider(), BrewProvider(), AppImageProvider()]
    │
    ├─ 4. 检测可用性 → 过滤掉未安装的
    │      如 Flatpak 没装 → 跳过，不影响其他
    │
    ├─ 5. 选择扫描策略
    │      --scan → scan_all()  全量重扫
    │      db 为空 → scan_all()  首次运行自动全量
    │      其他 → scan_incremental()  增量对比（默认）
    │
    ├─ 6. 启动 TUI
    │      AppManagerApp(db).run()
    │      这是一个阻塞调用，直到用户按 q 退出
    │
    └─ 7. 关闭数据库连接

============================================================
为什么把 sys.path 调整放在最前面？
============================================================

项目结构是 src/ 作为包根目录。当作为脚本直接运行时：
  python src/main.py
Python 的 sys.path 可能不包含 src/ 的父目录。
所以先手动插入，确保 from src.xxx import yyy 能正确解析。
"""

import sys
import os

# 确保 src/ 的父目录在 Python 路径中
# 这样不管从哪里运行 main.py，import 都不会出错
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    """应用主入口。

    命令行参数：
      --scan    全量重扫（重新解析所有来源的完整数据）
      无参数     增量扫描（默认，只对比新增/删除的包）
    """
    full_scan = "--scan" in sys.argv

    # 延迟导入：在路径修正后导入项目内模块
    from src.db.database import Database
    from src.core.scanner import scan_all, scan_incremental
    from src.providers.apt import AptProvider
    from src.providers.snap import SnapProvider
    from src.providers.flatpak import FlatpakProvider
    from src.providers.brew import BrewProvider
    from src.providers.appimage import AppImageProvider
    from src.tui.app import AppManagerApp

    # 数据库路径：~/.local/share/app-manager/apps.db
    # 遵循 XDG 规范，用户数据放在 ~/.local/share/
    data_dir = os.path.expanduser("~/.local/share/app-manager")
    db_path = os.path.join(data_dir, "apps.db")
    # schema.sql 路径：项目根目录下的 data/ 目录
    schema_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "schema.sql"
    )

    print("初始化数据库...")
    db = Database(db_path, schema_path)

    # 创建所有 Provider 实例（不管是否可用，后续再过滤）
    providers = [
        AptProvider(),
        SnapProvider(),
        FlatpakProvider(),
        BrewProvider(),
        AppImageProvider(),
    ]

    # 检测可用的包管理器并过滤
    print("检测可用的包管理器...")
    available = [p for p in providers if p.is_available()]
    print(f"发现: {', '.join(p.name for p in available)}")

    # 获取当前数据库中的包数量
    total = db.get_package_count()

    # 决定扫描策略
    if full_scan:
        print("全量扫描模式...")
        counts = scan_all(db, available)
    elif total == 0:
        # 数据库为空 → 首次运行，自动全量扫描
        print("数据库为空，首次运行自动执行全量扫描...")
        counts = scan_all(db, available)
    else:
        # 已有数据 → 快速增量扫描
        print(f"数据库已有 {total} 个包，执行增量扫描...")
        counts = scan_incremental(db, available)

    # 扫描结果提示
    if not counts:
        print("未找到任何包。请确认已安装支持的包管理器。")
        print("继续启动 TUI（将显示空列表）...")

    print(f"\n启动终端界面...")
    # run() 是 Textual 的阻塞主循环，直到用户退出才返回
    app = AppManagerApp(db)
    app.run()

    # 退出前关闭数据库连接
    db.close()


if __name__ == "__main__":
    main()
