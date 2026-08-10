"""本地存储重置脚本。

用于清空 Browser Agent 的 SQLite 元数据和 Qdrant collection。
默认会二次确认；只有传入 --yes 或 --dry-run 时才跳过交互确认。
"""

import argparse
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv
from qdrant_client import QdrantClient


BACKEND_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_ROOT / "data" / "browser_agent.sqlite3"
ENV_PATH = BACKEND_ROOT / "config" / ".env"

SQLITE_PROJECT_TABLES = [
    "chat_plan_events",
    "chat_plan_steps",
    "chat_plan_revisions",
    "chat_plans",
    "memory_extraction_jobs",
    "memory_items",
    "chat_summaries",
    "chat_turn_summaries",
    "chat_messages",
    "chat_turns",
    "chat_pages",
    "page_snapshots",
    "pages",
    "chats",
]


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="Reset local Browser Agent storage: SQLite metadata and Qdrant vectors."
    )
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned actions without deleting anything.")
    parser.add_argument("--sqlite-only", action="store_true", help="Only reset the local SQLite database.")
    parser.add_argument("--qdrant-only", action="store_true", help="Only reset the Qdrant collections.")
    parser.add_argument("--skip-init", action="store_true", help="Do not recreate the empty SQLite schema.")
    parser.add_argument(
        "--sqlite-strategy",
        choices=("auto", "delete", "truncate"),
        default="auto",
        help="SQLite reset strategy. auto deletes the file first, then falls back to clearing tables if locked.",
    )
    return parser.parse_args()


def confirm(args: argparse.Namespace, reset_sqlite: bool, reset_qdrant: bool, collection_names: list[str]) -> None:
    """在真正删除本地数据前展示影响范围并要求确认。"""
    print("This will reset local Browser Agent storage.")
    if reset_sqlite:
        print(f"- SQLite: {DB_PATH}")
        print(f"- SQLite project tables: {', '.join(SQLITE_PROJECT_TABLES)}")
    if reset_qdrant:
        print(f"- Qdrant collections: {', '.join(collection_names)}")
    print("- Config files and API keys will not be touched.")
    if reset_sqlite:
        print(f"- SQLite strategy: {args.sqlite_strategy}")

    if args.dry_run or args.yes:
        return

    answer = input("Type RESET to continue: ").strip()
    if answer != "RESET":
        print("Cancelled.")
        raise SystemExit(1)


def init_sqlite_schema() -> None:
    """删除或清空后重新创建空 SQLite 表结构。"""
    sys.path.insert(0, str(BACKEND_ROOT))
    from storage.db import db

    db.init_db()
    db.close()
    print(f"Initialized empty SQLite schema: {DB_PATH}")


def reset_sqlite_by_delete(args: argparse.Namespace) -> bool:
    """优先通过删除 SQLite 文件完成重置；文件被锁时可回退到清表。"""
    sqlite_files = [
        DB_PATH,
        DB_PATH.with_name(f"{DB_PATH.name}-wal"),
        DB_PATH.with_name(f"{DB_PATH.name}-shm"),
    ]

    for path in sqlite_files:
        if args.dry_run:
            print(f"[dry-run] remove {path}")
            continue
        if not path.exists():
            print(f"Skipped missing file: {path}")
            continue
        try:
            path.unlink()
            print(f"Removed: {path}")
        except PermissionError:
            if args.sqlite_strategy == "auto":
                print(f"SQLite file is locked, falling back to table cleanup: {path}")
                return False
            raise RuntimeError("SQLite file is locked. Stop the backend process and retry.")

    return True


def quote_identifier(value: str) -> str:
    """安全引用 SQLite 标识符，避免表名里的双引号破坏 SQL。"""
    return '"' + value.replace('"', '""') + '"'


def reset_sqlite_by_truncate(args: argparse.Namespace) -> None:
    """在数据库文件无法删除时，逐表清空项目表。"""
    if args.dry_run:
        print(f"[dry-run] clear SQLite project tables in {DB_PATH}: {', '.join(SQLITE_PROJECT_TABLES)}")
        return
    if not DB_PATH.exists():
        print(f"Skipped missing SQLite database: {DB_PATH}")
        return

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA busy_timeout = 10000")
        existing_table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        table_names = [table_name for table_name in SQLITE_PROJECT_TABLES if table_name in existing_table_names]
        missing_table_names = [
            table_name for table_name in SQLITE_PROJECT_TABLES if table_name not in existing_table_names
        ]
        extra_table_names = sorted(existing_table_names - set(SQLITE_PROJECT_TABLES))
        with conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            for table_name in table_names:
                conn.execute(f"DELETE FROM {quote_identifier(table_name)}")
        try:
            conn.execute("VACUUM")
        except sqlite3.OperationalError as exc:
            print(f"Skipped VACUUM: {exc}")
        conn.close()
    except sqlite3.OperationalError as exc:
        raise RuntimeError("SQLite database is locked by another process. Close the holder and retry.") from exc

    print(f"Cleared SQLite tables: {', '.join(table_names) if table_names else '(none)'}")
    if missing_table_names:
        print(f"Skipped missing project tables: {', '.join(missing_table_names)}")
    if extra_table_names:
        print(f"Left non-project tables untouched: {', '.join(extra_table_names)}")


def reset_sqlite(args: argparse.Namespace) -> None:
    """按参数选择 SQLite 重置策略，并按需重建 schema。"""
    if args.sqlite_strategy == "truncate":
        reset_sqlite_by_truncate(args)
    else:
        deleted = reset_sqlite_by_delete(args)
        if not deleted:
            reset_sqlite_by_truncate(args)

    if not args.skip_init and not args.dry_run:
        init_sqlite_schema()


def reset_qdrant(args: argparse.Namespace, qdrant_url: str, api_key: str | None, collection_names: list[str]) -> None:
    """删除配置中的 Qdrant collection。"""
    if not qdrant_url:
        print("Skipped Qdrant: QDRANT_URL is not configured.")
        return

    if args.dry_run:
        print(f"[dry-run] delete Qdrant collections: {', '.join(collection_names)}")
        return

    client = QdrantClient(url=qdrant_url, api_key=api_key)
    existing_collection_names = {item.name for item in client.get_collections().collections}
    for collection_name in collection_names:
        if collection_name not in existing_collection_names:
            print(f"Skipped missing Qdrant collection: {collection_name}")
            continue

        client.delete_collection(collection_name=collection_name)
        print(f"Deleted Qdrant collection: {collection_name}")


def main() -> None:
    """脚本入口，协调 SQLite 与 Qdrant 两类存储的重置。"""
    args = parse_args()
    if args.sqlite_only and args.qdrant_only:
        raise SystemExit("--sqlite-only and --qdrant-only cannot be used together.")

    load_dotenv(dotenv_path=ENV_PATH)

    import os

    qdrant_url = os.getenv("QDRANT_URL", "")
    qdrant_api_key = os.getenv("QDRANT_API_KEY") or None
    qdrant_collections = [
        os.getenv("QDRANT_COLLECTION", "browser_agent_chunks"),
        os.getenv("QDRANT_MEMORY_COLLECTION", "browser_agent_memories"),
    ]
    qdrant_collections = list(dict.fromkeys(collection for collection in qdrant_collections if collection))

    reset_sqlite_enabled = not args.qdrant_only
    reset_qdrant_enabled = not args.sqlite_only

    confirm(args, reset_sqlite_enabled, reset_qdrant_enabled, qdrant_collections)

    if reset_sqlite_enabled:
        reset_sqlite(args)
    if reset_qdrant_enabled:
        reset_qdrant(args, qdrant_url, qdrant_api_key, qdrant_collections)

    print("Done.")


if __name__ == "__main__":
    main()
