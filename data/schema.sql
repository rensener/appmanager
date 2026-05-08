CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('apt', 'snap', 'flatpak', 'brew', 'appimage')),
    version TEXT,
    installed_size INTEGER DEFAULT 0,
    description TEXT DEFAULT '',
    is_manual BOOLEAN DEFAULT 1,
    hide BOOLEAN DEFAULT 0,
    category TEXT DEFAULT '',
    installed_at TEXT DEFAULT '',
    UNIQUE(name, source)
);

CREATE TABLE IF NOT EXISTS dependencies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id INTEGER NOT NULL,
    child_id INTEGER NOT NULL,
    is_automatic BOOLEAN DEFAULT 1,
    FOREIGN KEY (parent_id) REFERENCES packages(id) ON DELETE CASCADE,
    FOREIGN KEY (child_id) REFERENCES packages(id) ON DELETE CASCADE,
    UNIQUE(parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS package_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS install_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    source TEXT NOT NULL,
    command TEXT DEFAULT '',
    operation TEXT DEFAULT 'install',
    user TEXT DEFAULT '',
    UNIQUE(timestamp, command, source)
);

CREATE TABLE IF NOT EXISTS history_packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    history_id INTEGER NOT NULL,
    package_id INTEGER NOT NULL,
    is_parent BOOLEAN DEFAULT 0,
    is_automatic BOOLEAN DEFAULT 1,
    version TEXT DEFAULT '',
    FOREIGN KEY (history_id) REFERENCES install_history(id) ON DELETE CASCADE,
    FOREIGN KEY (package_id) REFERENCES packages(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_packages_source ON packages(source);
CREATE INDEX IF NOT EXISTS idx_packages_name ON packages(name);
CREATE INDEX IF NOT EXISTS idx_dependencies_parent ON dependencies(parent_id);
CREATE INDEX IF NOT EXISTS idx_dependencies_child ON dependencies(child_id);
CREATE INDEX IF NOT EXISTS idx_package_files_package ON package_files(package_id);
CREATE INDEX IF NOT EXISTS idx_history_source ON install_history(source);
