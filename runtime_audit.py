"""On-demand owner diagnostics: numeric resource facts, never credentials."""

import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import db


def numeric_file(path):
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def memory_summary(root=Path("/sys/fs/cgroup")):
    allowed = {
        "anon",
        "file",
        "kernel",
        "sock",
        "slab",
        "file_dirty",
        "inactive_file",
        "active_file",
    }
    result = {"current_bytes": numeric_file(root / "memory.current")}
    try:
        for line in (root / "memory.stat").read_text().splitlines():
            key, value = line.split()
            if key in allowed:
                result[key + "_bytes"] = int(value)
    except (OSError, ValueError):
        pass
    return result


def processes(root=Path("/proc")):
    rows = []
    for directory in root.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            values = dict(
                line.split(":", 1)
                for line in (directory / "status").read_text().splitlines()
                if ":" in line
            )
            rss = int(values.get("VmRSS", "0 kB").split()[0]) * 1024
            rows.append(
                {
                    "pid": int(directory.name),
                    "parent_pid": int(values.get("PPid", "0")),
                    "resident_bytes": rss,
                    "threads": int(values.get("Threads", "0")),
                }
            )
        except (OSError, ValueError):
            continue
    return sorted(rows, key=lambda row: row["resident_bytes"], reverse=True)


def storage_summary(database, *, limit=20000):
    groups = {}
    count = 0
    truncated = False
    for directory, subdirs, files in os.walk(database.parent, followlinks=False):
        subdirs[:] = [
            name for name in subdirs if not Path(directory, name).is_symlink()
        ]
        for name in files:
            path = Path(directory, name)
            if path.is_symlink():
                continue
            count += 1
            if count > limit:
                truncated = True
                break
            try:
                size = path.stat().st_size
            except OSError:
                continue
            category = (
                "database"
                if path == database
                else (
                    "database_wal"
                    if name == database.name + "-wal"
                    else (
                        "backups"
                        if "backups" in path.relative_to(database.parent).parts[:-1]
                        else "other"
                    )
                )
            )
            group = groups.setdefault(category, {"bytes": 0, "files": 0})
            group["bytes"] += size
            group["files"] += 1
        if truncated:
            break
    return {"groups": groups, "truncated": truncated}


def snapshot():
    database = Path(db.DB_PATH).resolve()
    with closing(db.get_db_connection()) as conn:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        sizes = []
        # dbstat traverses metadata without reading media BLOB contents.
        try:
            sizes = [
                dict(zip(("table", "bytes", "pages"), row))
                for row in conn.execute(
                    "SELECT name,SUM(pgsize),COUNT(*) FROM dbstat GROUP BY name ORDER BY SUM(pgsize) DESC LIMIT 15"
                )
            ]
        except sqlite3.DatabaseError:
            sizes = []
    return {
        "checked_at": time.time(),
        "memory": memory_summary(),
        "processes": processes(),
        "storage": storage_summary(database),
        "database": {
            "bytes": page_count * page_size,
            "free_bytes": free_pages * page_size,
            "tables": len(tables),
            "largest_tables": sizes,
        },
    }
