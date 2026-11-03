"""SQLite storage for check-in places."""

import sqlite3
from dataclasses import dataclass


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    address: str = ""


class PlaceStore:
    def __init__(self, db_path: str):
        self.db = sqlite3.connect(db_path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS places("
            "  name TEXT PRIMARY KEY COLLATE NOCASE,"
            "  lat REAL NOT NULL,"
            "  lon REAL NOT NULL,"
            "  address TEXT NOT NULL DEFAULT '')"
        )
        try:
            self.db.execute("ALTER TABLE places ADD COLUMN address TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        self.db.commit()

    def add(self, name: str, lat: float, lon: float, address: str = "") -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO places(name, lat, lon, address) VALUES(?,?,?,?)",
            (name, lat, lon, address),
        )
        self.db.commit()

    def remove(self, name: str) -> bool:
        cur = self.db.execute("DELETE FROM places WHERE name=?", (name,))
        self.db.commit()
        return cur.rowcount > 0

    def get(self, name: str) -> Place | None:
        row = self.db.execute(
            "SELECT name, lat, lon, address FROM places WHERE name=?", (name,)
        ).fetchone()
        return Place(*row) if row else None

    def list(self) -> list[Place]:
        return [
            Place(*r)
            for r in self.db.execute("SELECT name, lat, lon, address FROM places ORDER BY name")
        ]
