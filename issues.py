"""
Plain CRUD functions for issues. No HTTP — safe to call from routes or agents.
"""

from db import get_db, serialize


def list_issues() -> list[dict]:
    conn = get_db()
    with conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM issues ORDER BY created_at DESC")
            return [serialize(r) for r in cursor.fetchall()]


def get_issue(issue_id: int) -> dict | None:
    conn = get_db()
    with conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM issues WHERE id = %s", (issue_id,))
            row = cursor.fetchone()
    return serialize(row) if row else None


def create_issue(
    title: str,
    description: str = "",
    status: str = "open",
    repo_name: str | None = None,
    repo_url: str | None = None,
    repo_path: str | None = None,
) -> dict:
    conn = get_db()
    with conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO issues (title, description, status, repo_name, repo_url, repo_path)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (title, description, status, repo_name, repo_url, repo_path),
            )
            conn.commit()
            cursor.execute("SELECT * FROM issues WHERE id = %s", (cursor.lastrowid,))
            return serialize(cursor.fetchone())


def update_issue(issue_id: int, **fields) -> dict | None:
    """Update any subset of (title, description, status, repo_name, repo_url).
    Returns the updated issue, or None if not found."""
    allowed = {"title", "description", "status", "repo_name", "repo_url", "repo_path"}
    fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not fields:
        return get_issue(issue_id)

    set_clause = ", ".join(f"{k} = %s" for k in fields)
    conn = get_db()
    with conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"UPDATE issues SET {set_clause} WHERE id = %s",
                (*fields.values(), issue_id),
            )
            if cursor.rowcount == 0:
                return None
            conn.commit()
            cursor.execute("SELECT * FROM issues WHERE id = %s", (issue_id,))
            return serialize(cursor.fetchone())


def delete_issue(issue_id: int) -> bool:
    """Delete an issue. Returns True if deleted, False if not found."""
    conn = get_db()
    with conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM issues WHERE id = %s", (issue_id,))
            deleted = cursor.rowcount > 0
        conn.commit()
    return deleted
