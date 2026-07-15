from datetime import datetime

import pymysql
import pymysql.cursors
import os


def get_db():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST"),
        port=int(os.getenv("MYSQL_PORT")),
        user=os.getenv("MYSQL_USER"),
        password=os.getenv("MYSQL_PASSWORD"),
        database=os.getenv("MYSQL_DATABASE"),
        cursorclass=pymysql.cursors.DictCursor,
    )


def init_db():
    conn = get_db()
    with conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS issues (
                    id          INT AUTO_INCREMENT PRIMARY KEY,
                    title       VARCHAR(255) NOT NULL,
                    description TEXT,
                    status      VARCHAR(50)  DEFAULT 'open',
                    repo_name   VARCHAR(255),
                    repo_url    VARCHAR(500),
                    created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Migrate existing tables that predate these columns (IF NOT EXISTS not
            # supported on older MySQL; catch duplicate-column error instead)
            for ddl in [
                "ALTER TABLE issues ADD COLUMN repo_name VARCHAR(255)",
                "ALTER TABLE issues ADD COLUMN repo_url  VARCHAR(500)",
                "ALTER TABLE issues ADD COLUMN repo_path VARCHAR(500)",
            ]:
                try:
                    cursor.execute(ddl)
                except pymysql.err.OperationalError as e:
                    if e.args[0] != 1060:  # 1060 = Duplicate column name
                        raise
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    id         INT AUTO_INCREMENT PRIMARY KEY,
                    agent_id   VARCHAR(255) NOT NULL UNIQUE,
                    status     VARCHAR(50)  DEFAULT 'active',
                    created_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
                                           ON UPDATE CURRENT_TIMESTAMP
                )
                """
            )
        conn.commit()


def serialize(row: dict) -> dict:
    return {
        k: v.isoformat() if isinstance(v, datetime) else v
        for k, v in row.items()
    }


def upsert_session(agent_id: str, status: str) -> None:
    conn = get_db()
    with conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_sessions (agent_id, status)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE status = VALUES(status)
                """,
                (agent_id, status),
            )
        conn.commit()
