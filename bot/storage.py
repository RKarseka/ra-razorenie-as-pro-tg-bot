"""SQLite storage for check-in places."""

from __future__ import annotations

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
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS settings("
            "  key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS admins("
            "  venue TEXT NOT NULL COLLATE NOCASE,"
            "  tg_id INTEGER NOT NULL,"
            "  created REAL NOT NULL DEFAULT 0,"
            "  PRIMARY KEY(venue, tg_id))"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS invites("
            "  token TEXT PRIMARY KEY,"
            "  venue TEXT NOT NULL COLLATE NOCASE,"
            "  created_by INTEGER NOT NULL,"
            "  created REAL NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS requests("
            "  id TEXT PRIMARY KEY,"
            "  venue TEXT NOT NULL COLLATE NOCASE,"
            "  service TEXT NOT NULL DEFAULT '',"
            "  owner INTEGER NOT NULL,"
            "  state TEXT NOT NULL,"
            "  updated REAL NOT NULL)"
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

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.db.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)",
            (key, value),
        )
        self.db.commit()

    def jitter_on(self) -> bool:
        return self.get_setting("jitter", "1") != "0"

    def set_name(self, tg_id: int, alias: str) -> None:
        self.set_setting(f"name:{tg_id}", alias)

    def name_of(self, tg_id: int) -> str:
        return self.get_setting(f"name:{tg_id}")

    # ---------- venue admins ----------

    def add_admin(self, venue: str, tg_id: int) -> None:
        import time
        self.db.execute(
            "INSERT OR IGNORE INTO admins(venue, tg_id, created) VALUES(?,?,?)",
            (venue, tg_id, time.time()),
        )
        self.db.commit()

    def del_admin(self, venue: str, tg_id: int) -> None:
        self.db.execute(
            "DELETE FROM admins WHERE venue=? AND tg_id=?", (venue, tg_id))
        self.db.commit()

    def admins_for(self, venue: str) -> list[int]:
        return [r[0] for r in self.db.execute(
            "SELECT tg_id FROM admins WHERE venue=?", (venue,))]

    def venues_of(self, tg_id: int) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT venue FROM admins WHERE tg_id=? ORDER BY venue", (tg_id,))]

    def is_admin(self, tg_id: int) -> bool:
        return bool(self.venues_of(tg_id))

    def all_admin_ids(self) -> list[int]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT tg_id FROM admins")]

    # ---------- invites ----------

    def add_invite(self, token: str, venue: str, created_by: int) -> None:
        import time
        self.db.execute(
            "INSERT INTO invites(token, venue, created_by, created) "
            "VALUES(?,?,?,?)", (token, venue, created_by, time.time()))
        self.db.commit()

    def take_invite(self, token: str):
        row = self.db.execute(
            "SELECT venue, created_by FROM invites WHERE token=?",
            (token,)).fetchone()
        if row:
            self.db.execute("DELETE FROM invites WHERE token=?", (token,))
            self.db.commit()
        return row  # (venue, created_by) or None

    # ---------- visit requests ----------

    def put_request(self, req_id: str, venue: str, service: str,
                    owner: int, state: str) -> None:
        import time
        self.db.execute(
            "INSERT OR REPLACE INTO requests(id, venue, service, owner, "
            "state, updated) VALUES(?,?,?,?,?,?)",
            (req_id, venue, service, owner, state, time.time()))
        self.db.commit()

    def get_request(self, req_id: str):
        r = self.db.execute(
            "SELECT id, venue, service, owner, state FROM requests "
            "WHERE id=?", (req_id,)).fetchone()
        return ({"id": r[0], "venue": r[1], "service": r[2],
                 "owner": r[3], "state": r[4]} if r else None)

    def set_request_state(self, req_id: str, state: str) -> None:
        import time
        self.db.execute(
            "UPDATE requests SET state=?, updated=? WHERE id=?",
            (state, time.time(), req_id))
        self.db.commit()

    def close_open_requests(self, venue: str | None = None) -> None:
        """Close every open request — optionally only one venue's."""
        import time
        if venue is None:
            self.db.execute(
                "UPDATE requests SET state='closed', updated=? "
                "WHERE state IN ('waiting','ready','sent','expired','failed')",
                (time.time(),))
        else:
            self.db.execute(
                "UPDATE requests SET state='closed', updated=? "
                "WHERE venue=? AND state IN ('waiting','ready','sent','expired','failed')",
                (time.time(), venue))
        self.db.commit()

    def current_request(self):
        """The single globally-active request, if any."""
        r = self.db.execute(
            "SELECT id, venue, service, owner, state FROM requests "
            "WHERE state IN ('waiting','ready','sent','expired','failed') "
            "ORDER BY updated DESC LIMIT 1").fetchone()
        return ({"id": r[0], "venue": r[1], "service": r[2],
                 "owner": r[3], "state": r[4]} if r else None)

    def open_requests_for_admin(self, tg_id: int):
        rows = self.db.execute(
            "SELECT r.id, r.venue, r.service, r.owner, r.state "
            "FROM requests r JOIN admins a ON a.venue=r.venue "
            "WHERE a.tg_id=? AND r.state IN ('waiting','ready','sent','expired','failed') "
            "ORDER BY r.updated DESC", (tg_id,)).fetchall()
        return [{"id": r[0], "venue": r[1], "service": r[2],
                 "owner": r[3], "state": r[4]} for r in rows]
