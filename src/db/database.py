"""SQLite 数据库管理 — 建表、CRUD 操作。

本文件是数据层的核心，封装了所有与 SQLite 的交互。
设计目标：让调用方不需要写一行 SQL，只需要调用 Python 方法。

============================================================
关键概念
============================================================

1. WAL 模式（Write-Ahead Logging）
   PRAGMA journal_mode=WAL 让 SQLite 使用 WAL 而非默认的 DELETE 模式。
   好处：读写操作不会互相阻塞。TUI 在读取数据时，扫描器可以同时写入。
   代价：WAL 文件（apps.db-wal）会多占一点磁盘空间。

2. 外键约束
   PRAGMA foreign_keys=ON 显式开启外键（SQLite 默认关闭！）。
   这让 ON DELETE CASCADE 生效：删除包时，自动清理关联的依赖、文件记录。

3. bulk_write（批量写入模式）
   正常情况下，每次 _cursor() 结束后都会 commit。
   在 bulk_write 上下文中，_cursor() 不 commit，等到 bulk_write 结束时
   一次性提交。这对扫描性能至关重要——1830 个包一次 commit 比 1830 次快几十倍。

   with db.bulk_write():    # ← 开始批量模式
       for pkg in pkgs:     #   这里面的所有操作...
           db.upsert_package(pkg)  #   ...都不单独 commit
           db.add_dependency(...)  #
   # ← 退出 bulk_write 时，一次性 commit 所有操作

4. upsert（INSERT OR UPDATE）
   upsert_package() 使用 SQL 的 ON CONFLICT ... DO UPDATE 语法。
   如果 (name, source) 组合已存在 → 更新；否则 → 插入。
   更新的 CASE 逻辑特别重要：只在有新数据时才覆盖旧值（见方法注释）。
"""

import sqlite3
import os
from contextlib import contextmanager
from src.db.models import Package, Dependency, PackageFile, InstallHistory, HistoryPackage


class Database:
    """SQLite 数据库封装。

    管理连接生命周期、提供所有 CRUD 方法。
    所有公开方法都是线程安全的——每次调用都创建新的 cursor。

    用法：
        db = Database("~/.local/share/app-manager/apps.db", "data/schema.sql")
        pkgs = db.get_all_packages("apt")
        db.close()
    """

    def __init__(self, db_path: str, schema_path: str | None = None):
        """
        Args:
            db_path: 数据库文件路径。目录不存在会自动创建。
            schema_path: 建表 SQL 文件路径。传 None 则跳过建表（假设库已存在）。
        """
        self.db_path = db_path
        # 确保目录存在（如 ~/.local/share/app-manager/）
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        # 连接数据库，启用 WAL 模式和外键
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # bulk_write 模式标记（见类文档说明）
        self._bulk = False
        # 首次运行时建表
        if schema_path:
            self._init_schema(schema_path)

    def _init_schema(self, schema_path: str):
        """读取 schema.sql 并执行所有建表语句。"""
        with open(schema_path) as f:
            self._conn.executescript(f.read())
        self._conn.commit()

    @contextmanager
    def _cursor(self):
        """获取数据库 cursor 的上下文管理器。

        核心逻辑：
          - 正常模式（self._bulk=False）：退出时自动 commit
          - 批量模式（self._bulk=True）：不 commit，由 bulk_write() 统一提交

        这样扫描器可以在 bulk_write 中放心调用多个方法，
        而 TUI 也能安全地逐条修改而不需要关心事务。

        使用 @contextmanager + yield 实现上下文管理：
          函数中 yield 之前是 __enter__，之后是 __exit__。
        """
        cur = self._conn.cursor()
        try:
            yield cur
            # 关键：批量模式下跳过 commit，留给 bulk_write 统一提交
            if not self._bulk:
                self._conn.commit()
        finally:
            # finally 保证即使发生异常也关闭 cursor，防止资源泄露
            cur.close()

    @contextmanager
    def bulk_write(self):
        """批量写入模式：所有操作在一个事务中提交。

        用法：
            with db.bulk_write():
                for pkg in many_packages:
                    db.upsert_package(pkg)   # 不 commit
                    db.add_dependency(...)   # 不 commit
            # 退出 with 时一次性 commit

        实现原理：
          设置 self._bulk = True → _cursor() 检测到 → 跳过 commit
          with 块结束时 → commit 一次
          finally 中恢复 self._bulk = False（即使发生异常也恢复）
        """
        self._bulk = True
        try:
            yield
            self._conn.commit()
        finally:
            self._bulk = False

    def close(self):
        """关闭数据库连接。程序退出前调用。"""
        self._conn.close()

    # ═══════════════════════════════════════════════════════
    # Package（包）相关操作
    # ═══════════════════════════════════════════════════════

    def upsert_package(self, pkg: Package) -> int:
        """
        插入或更新包，返回包的 id。

        「upsert」= UPDATE + INSERT，是数据库领域常见术语。

        这是整个系统最重要的写操作方法。扫描器每发现一个包就调用一次。
        SQL 中的 ON CONFLICT(name, source) 表示当 name+source 唯一键冲突时，
        不报错，而是执行 DO UPDATE 后面的更新。

        CASE 逻辑说明（SQL 中的 CASE WHEN ... THEN ... ELSE ... END）：
          version:
            excluded.version 是「准备插入的新值」。
            如果新值不为空 → 用新值覆盖；否则 → 保留旧值。
            这防止空字符串覆盖已存在的版本号。

          installed_size:
            同理，新值 > 0 才覆盖。dpkg-query 有时返回 0。

          description:
            同上，新描述不为空才覆盖。

          is_manual:
            直接覆盖。因为扫描时的分类逻辑可能变化（如之前误判为系统预装的
            包，这次被正确识别为用户安装）。

          installed_at:
            同上，保留非空值。特别保护「手动移动」这个标记不被清空。
        """
        with self._cursor() as cur:
            cur.execute(
                """INSERT INTO packages (name, source, version, installed_size,
                   description, is_manual, hide, category, installed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name, source) DO UPDATE SET
                   version=CASE WHEN excluded.version != '' THEN excluded.version ELSE packages.version END,
                   installed_size=CASE WHEN excluded.installed_size > 0 THEN excluded.installed_size ELSE packages.installed_size END,
                   description=CASE WHEN excluded.description != '' THEN excluded.description ELSE packages.description END,
                   is_manual=excluded.is_manual,
                   installed_at=CASE WHEN excluded.installed_at != '' THEN excluded.installed_at ELSE packages.installed_at END""",
                (
                    pkg.name, pkg.source, pkg.version, pkg.installed_size,
                    pkg.description, pkg.is_manual, pkg.hide, pkg.category,
                    pkg.installed_at,
                ),
            )
            # upsert 后 lastrowid 可能不准，统一用 SELECT 查一次
            pkg_id = self._get_package_id(cur, pkg.name, pkg.source)
            return pkg_id if pkg_id else 0

    def _get_package_id(self, cur, name: str, source: str) -> int | None:
        """内部方法：根据 name+source 查找包 id。
        接受 cur 参数（而非自己创建 cursor），方便在 upsert 等操作中复用。
        """
        cur.execute(
            "SELECT id FROM packages WHERE name=? AND source=?", (name, source)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def get_package_id(self, name: str, source: str) -> int | None:
        """公开方法：根据 name+source 查找包 id。"""
        with self._cursor() as cur:
            return self._get_package_id(cur, name, source)

    def get_package(self, package_id: int) -> Package | None:
        """根据 id 获取单个包的完整信息。"""
        with self._cursor() as cur:
            cur.execute("SELECT * FROM packages WHERE id=?", (package_id,))
            row = cur.fetchone()
            return self._row_to_package(row) if row else None

    def get_all_packages(self, source: str | None = None) -> list[Package]:
        """获取所有包（按 name 排序）。

        Args:
            source: 传 "apt" 则只查 APT 包；传 None 则查所有来源。
        """
        with self._cursor() as cur:
            if source:
                cur.execute(
                    "SELECT * FROM packages WHERE source=? ORDER BY name", (source,)
                )
            else:
                cur.execute("SELECT * FROM packages ORDER BY source, name")
            return [self._row_to_package(r) for r in cur.fetchall()]

    def get_parent_packages(self, source: str | None = None) -> list[Package]:
        """获取所有父包（is_manual=True 且未被隐藏的包）。

        这是 TUI 构建树的主要数据源。
        过滤条件：
          - is_manual=1：只要父包（用户主动安装的顶层应用）
          - hide=0：被用户手动隐藏的不显示在主列表中
        """
        with self._cursor() as cur:
            if source:
                cur.execute(
                    """SELECT * FROM packages
                       WHERE is_manual=1 AND hide=0 AND source=?
                       ORDER BY name""",
                    (source,),
                )
            else:
                cur.execute(
                    """SELECT * FROM packages
                       WHERE is_manual=1 AND hide=0
                       ORDER BY source, name"""
                )
            return [self._row_to_package(r) for r in cur.fetchall()]

    def set_package_installed_at(self, package_id: int, installed_at: str):
        """修改包的 installed_at 字段。
        用于「移动到用户安装/系统预装」功能：
          - 移到系统预装：installed_at = ""
          - 移到用户安装：installed_at = "手动移动"
        """
        with self._cursor() as cur:
            cur.execute(
                "UPDATE packages SET installed_at=? WHERE id=?",
                (installed_at, package_id),
            )

    def set_package_hidden(self, package_id: int, hide: bool):
        """设置包的隐藏状态。True=隐藏，False=显示。"""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE packages SET hide=? WHERE id=?", (hide, package_id)
            )

    def get_hidden_packages(self) -> list[Package]:
        """获取所有被隐藏的包。显示在树的最底部的「已隐藏」节点下。"""
        with self._cursor() as cur:
            cur.execute("SELECT * FROM packages WHERE hide=1 ORDER BY name")
            return [self._row_to_package(r) for r in cur.fetchall()]

    def search_packages(self, query: str) -> list[Package]:
        """按包名模糊搜索。
        使用 SQL LIKE '%query%'，匹配包含关键词的所有包。
        注意：LIKE 默认不区分大小写（取决于 SQLite 的 NOCASE 设置）。
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM packages WHERE name LIKE ? ORDER BY name",
                (f"%{query}%",),
            )
            return [self._row_to_package(r) for r in cur.fetchall()]

    # ═══════════════════════════════════════════════════════
    # Dependencies（依赖关系）相关操作
    # ═══════════════════════════════════════════════════════

    def add_dependency(self, parent_id: int, child_id: int, is_automatic: bool = True):
        """记录一个依赖关系：parent_id 依赖 child_id。

        INSERT OR IGNORE — 如果 (parent_id, child_id) 组合已存在，跳过。
        这很重要，因为多次扫描可能遇到相同的依赖关系。
        """
        with self._cursor() as cur:
            cur.execute(
                """INSERT OR IGNORE INTO dependencies (parent_id, child_id, is_automatic)
                   VALUES (?, ?, ?)""",
                (parent_id, child_id, is_automatic),
            )

    def get_dependencies(self, package_id: int) -> list[tuple[Package, bool]]:
        """获取某个包的所有依赖。

        JOIN 查询：连表获取子包的完整信息（packages 表的所有列）+
        is_automatic 标记（dependencies 表）。

        返回 [(子包, is_automatic), ...]。
        例如 fcitx5 返回：
          [(Package("libfcitx5core7", ...), True),
           (Package("fcitx5-data", ...), True)]
        """
        with self._cursor() as cur:
            cur.execute(
                """SELECT p.*, d.is_automatic FROM packages p
                   JOIN dependencies d ON p.id = d.child_id
                   WHERE d.parent_id=?
                   ORDER BY p.name""",
                (package_id,),
            )
            result = []
            for row in cur.fetchall():
                # row[:10] 是 packages 的 10 列，row[10] 是 is_automatic
                pkg = self._row_to_package(row[:10])
                is_auto = bool(row[10])
                result.append((pkg, is_auto))
            return result

    def get_parents(self, package_id: int) -> list[Package]:
        """查找依赖这个包的所有父包。

        用于识别「共享依赖」：如果一个子包被多个父包依赖，
        那它是共享的，卸载任一父包时不应该删除它。

        例如 libfcitx5core7 可能被 fcitx5 和 fcitx5-config-qt 同时依赖。
        """
        with self._cursor() as cur:
            cur.execute(
                """SELECT p.* FROM packages p
                   JOIN dependencies d ON p.id = d.parent_id
                   WHERE d.child_id=?""",
                (package_id,),
            )
            return [self._row_to_package(r) for r in cur.fetchall()]

    # ═══════════════════════════════════════════════════════
    # Package Files（包文件路径）相关操作
    # ═══════════════════════════════════════════════════════

    def set_package_files(self, package_id: int, files: list[str]):
        """替换包的文件夹联（先删后插）。

        为什么是「替换」而不是「追加」？
          包的安装文件不会变（同一版本），重扫时应该是完整的列表。
          先删后插保证数据一致性。
        """
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM package_files WHERE package_id=?", (package_id,)
            )
            # executemany：一次执行多条 INSERT，比循环 execute 快
            cur.executemany(
                "INSERT INTO package_files (package_id, file_path) VALUES (?, ?)",
                [(package_id, fp) for fp in files],
            )

    def get_package_files(self, package_id: int) -> list[str]:
        """获取包的安装文件路径列表（用于详情页展示）。"""
        with self._cursor() as cur:
            cur.execute(
                "SELECT file_path FROM package_files WHERE package_id=? ORDER BY file_path",
                (package_id,),
            )
            return [r[0] for r in cur.fetchall()]

    # ═══════════════════════════════════════════════════════
    # Install History（安装历史）相关操作
    # ═══════════════════════════════════════════════════════

    def add_install_history(self, h: InstallHistory) -> int:
        """添加一条安装历史记录，返回数据库分配的 id。

        先去重：用 (timestamp, command, source) 唯一键检查是否已存在。
        如果已存在（上次扫描已写入），返回已有 id，不重复插入。
        这保证了多次扫描不会产生重复历史记录。
        """
        with self._cursor() as cur:
            # 先查是否存在
            cur.execute(
                """SELECT id FROM install_history
                   WHERE timestamp=? AND command=? AND source=?""",
                (h.timestamp, h.command, h.source),
            )
            row = cur.fetchone()
            if row:
                return row[0]  # 已存在，返回已有 id

            cur.execute(
                """INSERT INTO install_history
                   (timestamp, source, command, operation, user)
                   VALUES (?, ?, ?, ?, ?)""",
                (h.timestamp, h.source, h.command, h.operation, h.user),
            )
            # lastrowid 返回最后 INSERT 的行 id
            return cur.lastrowid or 0

    def add_history_package(self, hp: HistoryPackage):
        """关联一个包到安装历史记录。INSERT OR IGNORE 防重复。"""
        with self._cursor() as cur:
            cur.execute(
                """INSERT OR IGNORE INTO history_packages
                   (history_id, package_id, is_parent, is_automatic, version)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    hp.history_id, hp.package_id, hp.is_parent,
                    hp.is_automatic, hp.version,
                ),
            )

    def get_last_history_timestamp(self, source: str) -> str | None:
        """获取某个来源最后一条历史记录的时间戳。
        用于增量扫描：判断上次扫描后是否有新操作。
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT MAX(timestamp) FROM install_history WHERE source=?",
                (source,),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None

    # ═══════════════════════════════════════════════════════
    # Source management（来源级别操作）
    # ═══════════════════════════════════════════════════════

    def get_package_names(self, source: str) -> set[str]:
        """获取某个来源的所有包名（只查 name 列，不加载所有字段）。

        这是增量扫描的核心方法。只拿包名（字符串集合），
        与当前系统用 Python 集合运算对比，找出新增/删除的包。
        比 get_all_packages() 快得多——不构造 Package 对象，只返回字符串。
        """
        with self._cursor() as cur:
            cur.execute("SELECT name FROM packages WHERE source=?", (source,))
            return {r[0] for r in cur.fetchall()}

    def delete_package_by_name(self, name: str, source: str):
        """删除一个包及其关联的所有数据（依赖、文件、历史记录）。

        级联删除顺序（重要！）：
          1. history_packages（历史关联）
          2. package_files（文件列表）
          3. dependencies（作为父包或子包的依赖关系）
          4. packages（包本身）

        必须先删子表再删主表，否则外键约束会报错。
        虽然建表时设了 ON DELETE CASCADE，但显式删除更可控。
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT id FROM packages WHERE name=? AND source=?",
                (name, source),
            )
            row = cur.fetchone()
            if not row:
                return  # 包不存在，无需删除
            pkg_id = row[0]

            # 按依赖顺序清理（子表 → 主表）
            cur.execute("DELETE FROM history_packages WHERE package_id=?", (pkg_id,))
            cur.execute("DELETE FROM package_files WHERE package_id=?", (pkg_id,))
            # 这个包可能既是父包又是子包，所以两个方向都要清理
            cur.execute(
                "DELETE FROM dependencies WHERE parent_id=? OR child_id=?",
                (pkg_id, pkg_id),
            )
            cur.execute("DELETE FROM packages WHERE id=?", (pkg_id,))

    def clear_source_data(self, source: str):
        """清除某个来源的所有数据（全量重扫前调用）。

        与 delete_package_by_name 同理，先删子表后删主表。
        使用子查询 IN (SELECT id FROM packages WHERE source=?) 批量定位。
        """
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM history_packages WHERE package_id IN "
                "(SELECT id FROM packages WHERE source=?)",
                (source,),
            )
            cur.execute(
                "DELETE FROM install_history WHERE source=?", (source,),
            )
            cur.execute(
                "DELETE FROM package_files WHERE package_id IN "
                "(SELECT id FROM packages WHERE source=?)",
                (source,),
            )
            cur.execute(
                "DELETE FROM dependencies WHERE "
                "parent_id IN (SELECT id FROM packages WHERE source=?) "
                "OR child_id IN (SELECT id FROM packages WHERE source=?)",
                (source, source),
            )
            cur.execute("DELETE FROM packages WHERE source=?", (source,))

    def get_package_count(self, source: str | None = None) -> int:
        """获取包总数。source 参数可选，用于按来源过滤。"""
        with self._cursor() as cur:
            if source:
                cur.execute("SELECT COUNT(*) FROM packages WHERE source=?", (source,))
            else:
                cur.execute("SELECT COUNT(*) FROM packages")
            return cur.fetchone()[0]

    # ═══════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════

    @staticmethod
    def _row_to_package(row: tuple) -> Package:
        """将数据库行（tuple）转为 Package 对象。

        数据库列的编号（与 schema.sql 中 CREATE TABLE 顺序一致）：
          0: id
          1: name
          2: source
          3: version
          4: installed_size
          5: description
          6: is_manual (SQLite 存 0/1，转 bool)
          7: hide (SQLite 存 0/1，转 bool)
          8: category
          9: installed_at

        注意：SQLite 不区分 BOOLEAN，实际存储 0 和 1。用 bool() 转换。
        """
        return Package(
            id=row[0],
            name=row[1],
            source=row[2],
            version=row[3] or "",          # None → ""
            installed_size=row[4] or 0,     # None → 0
            description=row[5] or "",       # None → ""
            is_manual=bool(row[6]),
            hide=bool(row[7]),
            category=row[8] or "",          # None → ""
            installed_at=row[9] or "",      # None → ""
        )
