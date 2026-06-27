from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def copy_fixture_tree(src_name: str, dest: Path) -> Path:
    src = FIXTURES_DIR / src_name
    target = dest / src_name
    shutil.copytree(src, target)
    return target


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def create_logs_db(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            create table logs (
                id integer primary key,
                ts integer not null,
                level text not null,
                thread_id text,
                process_uuid text,
                feedback_log_body text
            )
            """
        )
        conn.executemany(
            "insert into logs (id, ts, level, thread_id, process_uuid, feedback_log_body) values (?, ?, ?, ?, ?, ?)",
            [
                (
                    row["id"],
                    row["ts"],
                    row["level"],
                    row.get("thread_id"),
                    row.get("process_uuid"),
                    row["feedback_log_body"],
                )
                for row in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()
