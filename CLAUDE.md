# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Activate virtual environment (always first)
source .venv/bin/activate

# Run the app (default: incremental scan)
python src/main.py

# Run the app with full scan (rebuilds all data)
python src/main.py --scan

# Run individual test files
python tests/test_db.py
python tests/test_matcher.py
python tests/test_apt_parser.py

# The global wrapper is at ~/.local/bin/appmanager
```

## Architecture

**Provider pattern** — each package source is an independent module implementing `BaseProvider` (`src/providers/__init__.py`). Adding a new source means creating one file; existing code stays untouched.

**Scan flow** (`src/core/scanner.py`):
- **Incremental** (default, <1s): compares system package names against DB via `dpkg --get-selections` → only processes new/removed packages
- **Full** (`--scan`): re-parses all sources, rewrites everything. Use when classification logic changes.
- Both wrap writes in `db.bulk_write()` — a single transaction instead of per-row commits.

**APT classification** (`src/providers/apt.py`) — packages fall into three categories:
- **User-installed**: explicitly named in `apt install` commands in history.log (not automatic)
- **System pre-installed**: in `apt-mark showmanual` but never user-typed → `installed_at=""` 
- **.deb-installed**: in `apt-mark showmanual` but never appears in any history.log → `installed_at="手动安装"`

The `is_manual` field alone isn't enough; `installed_at` is the discriminator between user and system packages.

**APT dependency resolution** uses two sources combined:
1. history.log — packages marked `automatic` in the same transaction as the parent
2. `dpkg-query -W -f '${Package}\t${Depends}\n'` — batch preloaded in `_preload_depends()`

**Database** (`src/db/database.py`): SQLite with WAL mode. The `upsert_package()` ON CONFLICT DO UPDATE uses CASE logic to only overwrite version/size/description when the new value is non-empty. `bulk_write()` context manager suppresses per-operation commits and commits once at the end — critical for scan performance.

**TUI** (`src/tui/app.py`): Textual framework. `PackageTree` is a Tree subclass with custom left/right arrow bindings for expand/collapse. Tree structure is `source → group (用户安装/系统预装) → package → deps`, with hidden packages as a bottom node. Key bindings: `s` detail, `m`/`y` move between user/system, `h` hide, `ctrl+h` toggle hidden visibility, `/` search, `q` quit (or exit search).

## Key performance decisions

- `dpkg_utils.get_all_packages_info()`: one `dpkg-query -W` call for all packages (vs. 1830 individual `dpkg -s` calls)
- `AptProvider._preload_depends()`: one `dpkg-query -W` for all Depends fields (vs. per-package `dpkg -s`)
- `AptProvider.fetch_package_names()`: `dpkg --get-selections` for incremental scan (no parsing needed)
- Scanner writes use `bulk_write()` — all packages in one transaction
