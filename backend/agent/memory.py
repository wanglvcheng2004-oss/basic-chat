from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class Memory:
    id: int
    user_id: str
    scope: str
    kind: str
    content: str
    tags: str
    importance: int
    source_thread_id: str
    created_at: str
    updated_at: str
    last_used_at: str


class PersonalMemoryStore:
    """Per-user memory plus a group-chat interaction graph.

    Every recall is scoped to one user_id, so one person's memories never leak
    into another person's private context.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS persons (
                user_id      TEXT PRIMARY KEY,
                nickname     TEXT NOT NULL,
                created_at   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memories (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id          TEXT NOT NULL,
                scope            TEXT NOT NULL DEFAULT 'private',
                kind             TEXT NOT NULL DEFAULT 'fact',
                content          TEXT NOT NULL,
                tags             TEXT NOT NULL DEFAULT '',
                importance       INTEGER NOT NULL DEFAULT 3,
                source_thread_id TEXT NOT NULL DEFAULT '',
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                last_used_at     TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_memories_user_scope
                ON memories(user_id, scope, importance DESC);
            CREATE TABLE IF NOT EXISTS messages (
                id          TEXT PRIMARY KEY,
                thread_id   TEXT NOT NULL,
                author_id   TEXT NOT NULL,
                author_name TEXT NOT NULL,
                content     TEXT NOT NULL,
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_thread_time
                ON messages(thread_id, created_at);
            """
        )
        self.connection.commit()

    # ---------- persons ----------
    def add_person(self, user_id: str, nickname: str) -> None:
        self.connection.execute(
            "INSERT INTO persons(user_id, nickname, created_at) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET nickname=excluded.nickname",
            (user_id, nickname, _now()),
        )
        self.connection.commit()

    def get_person(self, user_id: str) -> dict | None:
        row = self.connection.execute("SELECT * FROM persons WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    # ---------- memory ----------
    def remember(
        self,
        user_id: str,
        content: str,
        *,
        kind: str = "fact",
        scope: str = "private",
        tags: str = "",
        importance: int = 3,
        source_thread_id: str = "",
    ) -> int:
        content = content.strip()
        if not content:
            raise ValueError("memory content is empty")
        now = _now()
        cursor = self.connection.execute(
            "INSERT INTO memories(user_id, scope, kind, content, tags, importance, "
            "source_thread_id, created_at, updated_at, last_used_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, scope, kind, content, tags, max(1, min(5, importance)), source_thread_id, now, now, ""),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def recall(
        self,
        user_id: str,
        query: str = "",
        *,
        scope: str | None = None,
        limit: int = 8,
    ) -> list[Memory]:
        sql = "SELECT * FROM memories WHERE user_id=?"
        params: list = [user_id]
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        if query.strip():
            sql += " AND (lower(content) LIKE ? OR lower(tags) LIKE ?)"
            pattern = f"%{query.strip().lower()}%"
            params.extend([pattern, pattern])
        sql += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
        params.append(max(1, min(50, limit)))
        rows = self.connection.execute(sql, params).fetchall()
        if rows:
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            self.connection.execute(
                f"UPDATE memories SET last_used_at=? WHERE id IN ({placeholders})",
                [_now(), *ids],
            )
            self.connection.commit()
        return [Memory(**dict(row)) for row in rows]

    def forget(self, user_id: str, memory_id: int) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM memories WHERE id=? AND user_id=?", (memory_id, user_id)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def list_memories(self, user_id: str, scope: str | None = None) -> list[Memory]:
        return self.recall(user_id, scope=scope, limit=50)

    # ---------- group chat graph ----------
    def record_message(
        self,
        *,
        message_id: str,
        thread_id: str,
        author_id: str,
        author_name: str,
        content: str,
        created_at: str | None = None,
        ensure_person: bool = True,
    ) -> None:
        if ensure_person:
            self.add_person(author_id, author_name)
        self.connection.execute(
            "INSERT OR REPLACE INTO messages(id, thread_id, author_id, author_name, content, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (message_id, thread_id, author_id, author_name, content, created_at or _now()),
        )
        self.connection.commit()

    def relationship_graph(self, *, window: int = 4, min_weight: int = 1) -> nx.Graph:
        """Build a co-participation graph from messages in the same thread."""
        graph = nx.Graph()
        for row in self.connection.execute("SELECT * FROM persons"):
            memories = self.connection.execute(
                "SELECT COUNT(*) AS n FROM memories WHERE user_id=?", (row["user_id"],)
            ).fetchone()["n"]
            messages = self.connection.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE author_id=?", (row["user_id"],)
            ).fetchone()["n"]
            graph.add_node(
                row["user_id"],
                nickname=row["nickname"],
                memories=memories,
                messages=messages,
            )

        threads: dict[str, list[sqlite3.Row]] = {}
        for row in self.connection.execute("SELECT * FROM messages ORDER BY thread_id, created_at, id"):
            threads.setdefault(row["thread_id"], []).append(row)

        for messages in threads.values():
            for index, current in enumerate(messages):
                for previous in messages[max(0, index - window):index]:
                    if previous["author_id"] == current["author_id"]:
                        continue
                    graph.add_edge(previous["author_id"], current["author_id"])
                    edge = graph[previous["author_id"]][current["author_id"]]
                    edge["weight"] = edge.get("weight", 0) + 1

        for u, v, data in list(graph.edges(data=True)):
            if data.get("weight", 0) < min_weight:
                graph.remove_edge(u, v)
        return graph

    def top_relationships(self, limit: int = 10) -> list[dict]:
        graph = self.relationship_graph()
        edges = sorted(graph.edges(data=True), key=lambda item: item[2].get("weight", 0), reverse=True)
        result = []
        for u, v, data in edges[:limit]:
            result.append(
                {
                    "source": graph.nodes[u]["nickname"],
                    "target": graph.nodes[v]["nickname"],
                    "weight": data.get("weight", 0),
                }
            )
        return result
