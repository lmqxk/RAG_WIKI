"""SQLite → PostgreSQL 数据迁移工具。

用法：
  python scripts/migrate_db.py export          # 导出 SQLite 数据到 JSON
  python scripts/migrate_db.py import          # 从 JSON 导入到 PostgreSQL
  python scripts/migrate_db.py check           # 校验数据完整性
"""

import json
import sqlite3
import sys
from pathlib import Path

# 当直接运行时，将 backend/src 加入 PYTHONPATH
BACKEND_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(BACKEND_SRC))

from backend.config import Settings, get_settings  # noqa: E402


def get_sqlite_connection() -> sqlite3.Connection:
    settings = get_settings()
    conn = sqlite3.connect(str(settings.sqlite_path))
    conn.row_factory = sqlite3.Row
    return conn


def export_data(output_dir: Path) -> None:
    """导出 SQLite 全部数据到 JSON 文件，每个表一个文件。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    conn = get_sqlite_connection()
    tables = [
        "organizations",
        "users",
        "organization_members",
        "documents",
        "jobs",
        "chunks",
        "audit_logs",
    ]

    for table in tables:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
        data = [dict(row) for row in rows]
        output_path = output_dir / f"{table}.json"
        output_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"  {table}: {len(data)} 行 → {output_path}")

    # 导出 chunks_fts 数据（FTS5 内容表）
    try:
        fts_rows = conn.execute("SELECT * FROM chunks_fts_content").fetchall()
        if fts_rows:
            fts_data = [dict(row) for row in fts_rows]
            output_path = output_dir / "chunks_fts_content.json"
            output_path.write_text(
                json.dumps(fts_data, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            print(f"  chunks_fts_content: {len(fts_data)} 行 → {output_path}")
    except sqlite3.OperationalError:
        pass  # FTS5 内容表可能不存在

    conn.close()
    print(f"\n导出完成：{len(tables)} 个表 → {output_dir}")


def import_data(input_dir: Path) -> None:
    """从 JSON 文件导入数据到 PostgreSQL。"""
    settings = get_settings()
    if not settings.database_url or settings.database_url.startswith("sqlite"):
        print("错误：请设置 RAG_DATABASE_URL 为 PostgreSQL 连接字符串")
        sys.exit(1)

    from backend.db import Database
    from backend.models import create_engine_from_settings

    engine = create_engine_from_settings(settings.database_url)
    db = Database(settings.sqlite_path, database_url=settings.database_url)
    db.initialize()

    from sqlalchemy import text

    tables = [
        "organizations",
        "users",
        "organization_members",
        "documents",
        "jobs",
        "chunks",
        "audit_logs",
    ]

    with engine.connect() as conn:
        for table in tables:
            json_path = input_dir / f"{table}.json"
            if not json_path.exists():
                print(f"  跳过 {table}：文件不存在")
                continue
            data = json.loads(json_path.read_text(encoding="utf-8"))
            if not data:
                print(f"  {table}: 0 行（空）")
                continue

            # 批量插入
            columns = list(data[0].keys())
            placeholders = ", ".join(f":{col}" for col in columns)
            col_list = ", ".join(columns)
            count = 0
            for row in data:
                try:
                    conn.execute(
                        text(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"),  # noqa: S608
                        row,
                    )
                    count += 1
                except Exception as exc:
                    print(f"  {table}: 跳过行 {row.get('id', '?')}: {exc}")
            conn.commit()
            print(f"  {table}: {count}/{len(data)} 行导入")

    print("\n导入完成")


def check_integrity() -> None:
    """校验数据完整性。"""
    settings = get_settings()
    conn = get_sqlite_connection()

    checks = [
        ("组织", "SELECT COUNT(*) FROM organizations"),
        ("用户", "SELECT COUNT(*) FROM users"),
        ("成员关系", "SELECT COUNT(*) FROM organization_members"),
        ("文档", "SELECT COUNT(*) FROM documents"),
        ("任务", "SELECT COUNT(*) FROM jobs"),
        ("文本块", "SELECT COUNT(*) FROM chunks"),
        ("审计日志", "SELECT COUNT(*) FROM audit_logs"),
        ("文档状态统计",
         "SELECT status, COUNT(*) FROM documents GROUP BY status"),
        ("孤立文档（无归属组织）",
         "SELECT COUNT(*) FROM documents WHERE organization_id NOT IN (SELECT id FROM organizations)"),
        ("孤立文本块（文档不存在）",
         "SELECT COUNT(*) FROM chunks WHERE document_id NOT IN (SELECT id FROM documents)"),
    ]

    for label, query in checks:
        result = conn.execute(query).fetchall()
        if len(result) == 1 and len(result[0]) == 1:
            print(f"  {label}: {result[0][0]}")
        else:
            for row in result:
                print(f"  {label}: {dict(row)}")

    conn.close()


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]
    base_dir = Path(__file__).resolve().parents[1]
    export_dir = base_dir / "storage" / "migration-export"

    if command == "export":
        export_data(export_dir)
    elif command == "import":
        import_data(export_dir)
    elif command == "check":
        check_integrity()
    else:
        print(f"未知命令: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()