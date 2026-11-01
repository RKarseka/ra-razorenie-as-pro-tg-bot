"""SQLite storage for check-in places."""

import sqlite3
from dataclasses import dataclass


@dataclass
class Place:
    name: str
    lat: float
    lon: float


class PlaceStore:
    def __init__(self, db_path: str):
        self.db = sqlite3.connect(db_path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS places("
            "  name TEXT PRIMARY KEY COLLATE NOCASE,"
            "  lat REAL NOT NULL,"
            "  lon REAL NOT NULL)"
        )
        self.db.commit()

    def add(self, name: str, lat: float, lon: float) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO places(name, lat, lon) VALUES(?,?,?)",
            (name, lat, lon),
        )
        self.db.commit()

    def remove(self, name: str) -> bool:
        cur = self.db.execute("DELETE FROM places WHERE name=?", (name,))
        self.db.commit()
        return cur.rowcount > 0

    def get(self, name: str) -> Place | None:
        row = self.db.execute(
            "SELECT name, lat, lon FROM places WHERE name=?", (name,)
        ).fetchone()
        return Place(*row) if row else None

    def list(self) -> list[Place]:
        return [
            Place(*r)
            for r in self.db.execute("SELECT name, lat, lon FROM places ORDER BY name")
        ]
