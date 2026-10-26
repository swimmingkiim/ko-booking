"""Transactional originals, normalized datasets, and collection observations."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .common import canonical, now


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Archive:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS versions (
                    id INTEGER PRIMARY KEY, source_key TEXT NOT NULL,
                    raw_sha256 TEXT NOT NULL, normalized_sha256 TEXT NOT NULL,
                    raw BLOB NOT NULL, normalized TEXT NOT NULL, metadata TEXT NOT NULL,
                    UNIQUE(source_key, raw_sha256, normalized_sha256)
                );
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY, version_id INTEGER NOT NULL REFERENCES versions(id),
                    checked_at TEXT NOT NULL, state TEXT NOT NULL, metadata TEXT NOT NULL
                );
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def save(self, key, raw, normalized, metadata):
        serialized = canonical(normalized)
        raw_hash = digest(raw)
        normalized_hash = digest(serialized.encode())
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("""
                SELECT v.* FROM observations o JOIN versions v ON v.id=o.version_id
                WHERE v.source_key=? ORDER BY o.id DESC LIMIT 1
            """, (key,)).fetchone()
            state = "new" if previous is None else (
                "changed" if previous["normalized_sha256"] != normalized_hash else (
                    "raw_changed" if previous["raw_sha256"] != raw_hash else "unchanged"
                )
            )
            db.execute("""INSERT OR IGNORE INTO versions
                (source_key,raw_sha256,normalized_sha256,raw,normalized,metadata)
                VALUES (?,?,?,?,?,?)""",
                (key, raw_hash, normalized_hash, raw, serialized, canonical(metadata)))
            version_id = db.execute("""SELECT id FROM versions WHERE source_key=?
                AND raw_sha256=? AND normalized_sha256=?""",
                (key, raw_hash, normalized_hash)).fetchone()[0]
            db.execute("""INSERT INTO observations
                (version_id,checked_at,state,metadata) VALUES (?,?,?,?)""",
                (version_id, now(), state, canonical(metadata)))
            return {"source_key": key, "version_id": version_id, "state": state,
                    "raw_sha256": raw_hash, "normalized_sha256": normalized_hash}

    def list_observations(self):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute("""
                SELECT o.id,o.checked_at,o.state,v.id AS version_id,v.source_key,
                       v.raw_sha256,v.normalized_sha256
                FROM observations o JOIN versions v ON v.id=o.version_id ORDER BY o.id
            """)]

    def export(self, version_id, destination):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown version ID")
        destination = Path(destination)
        # A fresh directory avoids silently overwriting an earlier review artifact.
        destination.mkdir(parents=True, exist_ok=False)
        metadata = json.loads(row["metadata"])
        (destination / ("original" + metadata["extension"])).write_bytes(row["raw"])
        (destination / "normalized.json").write_text(row["normalized"] + "\n", encoding="utf-8")
        metadata.update({k: row[k] for k in ("id", "source_key", "raw_sha256", "normalized_sha256")})
        (destination / "metadata.json").write_text(canonical(metadata) + "\n", encoding="utf-8")
