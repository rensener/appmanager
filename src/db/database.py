"""SQLite 数据库管理 — 建表、CRUD 操作。"""

import sqlite3
import os
from contextlib import contextmanager
from src.db.models import Package, Dependency, PackageFile, InstallHistory, HistoryPackage


class Database:
    def __init__(self, db_path: str, schema_path: str | None = None):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._bulk = False
        if schema_path:
            self._init_schema(schema_path)

    def _init_schema(self, schema_path: str):
        with open(schema_path) as f:
            self._conn.executescript(f.read())
        self._conn.commit()

    @contextmanager
    def _cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
            if not self._bulk:
                self._conn.commit()
        finally:
            cur.close()

    @contextmanager
    def bulk_write(self):
        """批量写入模式：所有操作在一个事务中提交。"""
        self._bulk = True
        try:
            yield
            self._conn.commit()
        finally:
            self._bulk = False

    def close(self):
        self._conn.close()

    # ── Package ──────────────────────────────────────

    def upsert_package(self, pkg: Package) -> int:
        """插入或更新包，返回包的 id。"""
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
                (pkg.name, pkg.source, pkg.version, pkg.installed_size,
                 pkg.description, pkg.is_manual, pkg.hide, pkg.category,
                 pkg.installed_at),
            )
            pkg_id = self._get_package_id(cur, pkg.name, pkg.source)
            return pkg_id if pkg_id else 0

    def _get_package_id(self, cur, name: str, source: str) -> int | None:
        cur.execute(
            "SELECT id FROM packages WHERE name=? AND source=?", (name, source)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def get_package_id(self, name: str, source: str) -> int | None:
        with self._cursor() as cur:
            return self._get_package_id(cur, name, source)

    def get_package(self, package_id: int) -> Package | None:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM packages WHERE id=?", (package_id,))
            row = cur.fetchone()
            return self._row_to_package(row) if row else None

    def get_all_packages(self, source: str | None = None) -> list[Package]:
        with self._cursor() as cur:
            if source:
                cur.execute(
                    "SELECT * FROM packages WHERE source=? ORDER BY name", (source,)
                )
            else:
                cur.execute("SELECT * FROM packages ORDER BY source, name")
            return [self._row_to_package(r) for r in cur.fetchall()]

    def get_parent_packages(self, source: str | None = None) -> list[Package]:
        """获取所有父包（is_manual=True 且未被隐藏）。"""
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
        with self._cursor() as cur:
            cur.execute(
                "UPDATE packages SET installed_at=? WHERE id=?", (installed_at, package_id)
            )

    def set_package_hidden(self, package_id: int, hide: bool):
        with self._cursor() as cur:
            cur.execute(
                "UPDATE packages SET hide=? WHERE id=?", (hide, package_id)
            )

    def get_hidden_packages(self) -> list[Package]:
        with self._cursor() as cur:
            cur.execute("SELECT * FROM packages WHERE hide=1 ORDER BY name")
            return [self._row_to_package(r) for r in cur.fetchall()]

    def search_packages(self, query: str) -> list[Package]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT * FROM packages WHERE name LIKE ? ORDER BY name",
                (f"%{query}%",),
            )
            return [self._row_to_package(r) for r in cur.fetchall()]

    # ── Dependencies ─────────────────────────────────

    def add_dependency(self, parent_id: int, child_id: int, is_automatic: bool = True):
        with self._cursor() as cur:
            cur.execute(
                """INSERT OR IGNORE INTO dependencies (parent_id, child_id, is_automatic)
                   VALUES (?, ?, ?)""",
                (parent_id, child_id, is_automatic),
            )

    def get_dependencies(self, package_id: int) -> list[tuple[Package, bool]]:
        """返回 (子包, is_automatic) 的列表。"""
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
                pkg = self._row_to_package(row[:10])
                is_auto = bool(row[10])
                result.append((pkg, is_auto))
            return result

    def get_parents(self, package_id: int) -> list[Package]:
        """查找依赖这个包的所有父包（用于识别共享依赖）。"""
        with self._cursor() as cur:
            cur.execute(
                """SELECT p.* FROM packages p
                   JOIN dependencies d ON p.id = d.parent_id
                   WHERE d.child_id=?""",
                (package_id,),
            )
            return [self._row_to_package(r) for r in cur.fetchall()]

    # ── Package Files ────────────────────────────────

    def set_package_files(self, package_id: int, files: list[str]):
        """替换包的文件夹联（先删后插）。"""
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM package_files WHERE package_id=?", (package_id,)
            )
            cur.executemany(
                "INSERT INTO package_files (package_id, file_path) VALUES (?, ?)",
                [(package_id, fp) for fp in files],
            )

    def get_package_files(self, package_id: int) -> list[str]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT file_path FROM package_files WHERE package_id=? ORDER BY file_path",
                (package_id,),
            )
            return [r[0] for r in cur.fetchall()]

    # ── Install History ──────────────────────────────

    def add_install_history(self, h: InstallHistory) -> int:
        """添加安装历史记录，返回 id。如果已存在则返回已有的 id。"""
        with self._cursor() as cur:
            # 先查是否存在（避免 INSERT OR IGNORE 的 lastrowid 陷阱）
            cur.execute(
                """SELECT id FROM install_history
                   WHERE timestamp=? AND command=? AND source=?""",
                (h.timestamp, h.command, h.source),
            )
            row = cur.fetchone()
            if row:
                return row[0]

            # 不存在则插入
            cur.execute(
                """INSERT INTO install_history
                   (timestamp, source, command, operation, user)
                   VALUES (?, ?, ?, ?, ?)""",
                (h.timestamp, h.source, h.command, h.operation, h.user),
            )
            return cur.lastrowid or 0

    def add_history_package(self, hp: HistoryPackage):
        with self._cursor() as cur:
            cur.execute(
                """INSERT OR IGNORE INTO history_packages
                   (history_id, package_id, is_parent, is_automatic, version)
                   VALUES (?, ?, ?, ?, ?)""",
                (hp.history_id, hp.package_id, hp.is_parent, hp.is_automatic, hp.version),
            )

    def get_last_history_timestamp(self, source: str) -> str | None:
        with self._cursor() as cur:
            cur.execute(
                "SELECT MAX(timestamp) FROM install_history WHERE source=?",
                (source,),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None

    def get_package_names(self, source: str) -> set[str]:
        """获取某个来源的所有包名（快速，仅查 name 列）。"""
        with self._cursor() as cur:
            cur.execute("SELECT name FROM packages WHERE source=?", (source,))
            return {r[0] for r in cur.fetchall()}

    def delete_package_by_name(self, name: str, source: str):
        """删除一个包及其关联的依赖、文件、历史记录。"""
        with self._cursor() as cur:
            cur.execute("SELECT id FROM packages WHERE name=? AND source=?", (name, source))
            row = cur.fetchone()
            if not row:
                return
            pkg_id = row[0]
            cur.execute("DELETE FROM history_packages WHERE package_id=?", (pkg_id,))
            cur.execute("DELETE FROM package_files WHERE package_id=?", (pkg_id,))
            cur.execute("DELETE FROM dependencies WHERE parent_id=? OR child_id=?", (pkg_id, pkg_id))
            cur.execute("DELETE FROM packages WHERE id=?", (pkg_id,))

    # ── Source management ────────────────────────────

    def clear_source_data(self, source: str):
        """清除某个来源的所有数据（包、依赖、文件、历史）。"""
        with self._cursor() as cur:
            cur.execute("DELETE FROM history_packages WHERE package_id IN (SELECT id FROM packages WHERE source=?)", (source,))
            cur.execute("DELETE FROM install_history WHERE source=?", (source,))
            cur.execute("DELETE FROM package_files WHERE package_id IN (SELECT id FROM packages WHERE source=?)", (source,))
            cur.execute("DELETE FROM dependencies WHERE parent_id IN (SELECT id FROM packages WHERE source=?) OR child_id IN (SELECT id FROM packages WHERE source=?)", (source, source))
            cur.execute("DELETE FROM packages WHERE source=?", (source,))

    # ── Helpers ──────────────────────────────────────

    @staticmethod
    def _row_to_package(row: tuple) -> Package:
        return Package(
            id=row[0],
            name=row[1],
            source=row[2],
            version=row[3] or "",
            installed_size=row[4] or 0,
            description=row[5] or "",
            is_manual=bool(row[6]),
            hide=bool(row[7]),
            category=row[8] or "",
            installed_at=row[9] or "",
        )

    def get_package_count(self, source: str | None = None) -> int:
        with self._cursor() as cur:
            if source:
                cur.execute("SELECT COUNT(*) FROM packages WHERE source=?", (source,))
            else:
                cur.execute("SELECT COUNT(*) FROM packages")
            return cur.fetchone()[0]
