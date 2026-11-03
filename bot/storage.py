"""SQLite storage for check-in places."""

import json
import sqlite3
from dataclasses import dataclass, field


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    address: str = ""
    services: list[str] = field(default_factory=list)


class PlaceStore:
    def __init__(self, db_path: str):
        self.db = sqlite3.connect(db_path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS places("
            "  name TEXT PRIMARY KEY COLLATE NOCASE,"
            "  lat REAL NOT NULL,"
            "  lon REAL NOT NULL,"
            "  address TEXT NOT NULL DEFAULT '',"
            "  services TEXT NOT NULL DEFAULT '[]')"
        )
        for col in ("address TEXT NOT NULL DEFAULT ''",
                    "services TEXT NOT NULL DEFAULT '[]'"):
            try:
                self.db.execute(f"ALTER TABLE places ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        self.db.commit()

    def add(self, name: str, lat: float, lon: float, address: str = "",
            services: list[str] | None = None) -> None:
        prev = self.get(name)
        svc = services if services is not None else (prev.services if prev else [])
        self.db.execute(
            "INSERT OR REPLACE INTO places(name, lat, lon, address, services) "
            "VALUES(?,?,?,?,?)",
            (name, lat, lon, address, json.dumps(svc, ensure_ascii=False)),
        )
        self.db.commit()

    def remove(self, name: str) -> bool:
        cur = self.db.execute("DELETE FROM places WHERE name=?", (name,))
        self.db.commit()
        return cur.rowcount > 0

    @staticmethod
    def _row(r) -> Place:
        return Place(r[0], r[1], r[2], r[3], json.loads(r[4] or "[]"))

    def get(self, name: str) -> Place | None:
        row = self.db.execute(
            "SELECT name, lat, lon, address, services FROM places WHERE name=?",
            (name,),
        ).fetchone()
        return self._row(row) if row else None

    def list(self) -> list[Place]:
        return [
            self._row(r)
            for r in self.db.execute(
                "SELECT name, lat, lon, address, services FROM places ORDER BY name"
            )
        ]
