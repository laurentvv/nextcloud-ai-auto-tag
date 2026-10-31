import json
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    user TEXT NOT NULL,
    fileid INTEGER NOT NULL,
    path TEXT NOT NULL,
    mimetype TEXT,
    etag TEXT,
    size INTEGER,
    status TEXT NOT NULL DEFAULT 'pending',
    model_ver TEXT,
    error TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user, fileid)
);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
CREATE TABLE IF NOT EXISTS scores (
    user TEXT NOT NULL,
    fileid INTEGER NOT NULL,
    tag TEXT NOT NULL,
    score REAL NOT NULL,
    PRIMARY KEY (user, fileid, tag)
);
CREATE TABLE IF NOT EXISTS proposals (
    user TEXT NOT NULL,
    fileid INTEGER NOT NULL,
    tag TEXT NOT NULL,
    score REAL NOT NULL,
    PRIMARY KEY (user, fileid, tag)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    mode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    user TEXT NOT NULL,
    fileid INTEGER NOT NULL,
    tag TEXT NOT NULL,
    tag_id INTEGER,
    created_at REAL NOT NULL,
    removed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_assignments_run ON assignments(run_id);
CREATE TABLE IF NOT EXISTS hashes (
    user TEXT NOT NULL,
    fileid INTEGER NOT NULL,
    kind TEXT NOT NULL,
    dhash INTEGER NOT NULL,
    sharpness REAL,
    pixels INTEGER,
    duration REAL,
    taken_at TEXT,
    PRIMARY KEY (user, fileid)
);
"""

# Colonnes ajoutées après coup : ajoutées aux bases existantes par State._migrate.
MIGRATIONS = {
    "files": {
        "reasons": "TEXT",  # raisons de « à supprimer », séparées par des virgules
        "keeper": "INTEGER",  # fileid gardé quand la raison est « doublon »
        "applied": "TEXT",  # JSON des tags voulus lors de la dernière application
        "run_id": "INTEGER",  # run qui a traité le fichier
    },
    "runs": {"finished_at": "REAL", "stats": "TEXT"},
}

STATUSES = ("pending", "done", "error", "skipped")
FILE_COLS = (
    "user, fileid, path, mimetype, etag, size, status, model_ver, error,"
    " reasons, keeper, applied, run_id"
)
ASSIGN_COLS = "run_id, user, fileid, tag, tag_id, created_at, removed_at"


@dataclass
class FileRow:
    user: str
    fileid: int
    path: str
    mimetype: str | None
    etag: str | None
    size: int | None
    status: str
    model_ver: str | None
    error: str | None
    reasons: str | None = None
    keeper: int | None = None
    applied: str | None = None
    run_id: int | None = None

    @property
    def reason_list(self) -> list[str]:
        return [r for r in (self.reasons or "").split(",") if r]


@dataclass
class RunRow:
    id: int
    started_at: float
    mode: str
    finished_at: float | None
    stats: dict


@dataclass
class Assignment:
    run_id: int
    user: str
    fileid: int
    tag: str
    tag_id: int | None
    created_at: float
    removed_at: float | None


@dataclass
class HashRow:
    user: str
    fileid: int
    kind: str
    dhash: int
    sharpness: float | None
    pixels: int | None
    duration: float | None
    taken_at: str | None
    folder: str = ""


class State:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __del__(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    def upsert_file(
        self,
        user: str,
        fileid: int,
        path: str,
        mimetype: str | None,
        etag: str | None,
        size: int | None,
        commit: bool = True,
    ) -> str:
        """Retourne 'new', 'changed' ou 'unchanged'. Un etag modifié remet le fichier en pending."""
        now = time.time()
        row = self.db.execute(
            "SELECT etag FROM files WHERE user=? AND fileid=?", (user, fileid)
        ).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO files (user, fileid, path, mimetype, etag, size, status, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                (user, fileid, path, mimetype, etag, size, now),
            )
            result = "new"
        elif row["etag"] != etag:
            self.db.execute(
                "UPDATE files SET path=?, mimetype=?, etag=?, size=?, status='pending',"
                " error=NULL, updated_at=? WHERE user=? AND fileid=?",
                (path, mimetype, etag, size, now, user, fileid),
            )
            result = "changed"
        else:
            self.db.execute(
                "UPDATE files SET path=?, mimetype=?, size=? WHERE user=? AND fileid=?",
                (path, mimetype, size, user, fileid),
            )
            result = "unchanged"
        if commit:
            self.db.commit()
        return result

    def get_file(self, user: str, fileid: int) -> FileRow | None:
        row = self.db.execute(
            f"SELECT {FILE_COLS} FROM files WHERE user=? AND fileid=?", (user, fileid)
        ).fetchone()
        return FileRow(**row) if row else None

    def next_pending(
        self,
        limit: int = 100,
        user: str | None = None,
        path_prefix: str | None = None,
        exclude_paths: Iterable[str] = (),
    ) -> list[FileRow]:
        return self.list_files(user, path_prefix, ("pending",), limit, exclude_paths=exclude_paths)

    def list_files(
        self,
        user: str | None = None,
        path_prefix: str | None = None,
        statuses: Iterable[str] | None = None,
        limit: int | None = None,
        run_id: int | None = None,
        exclude_paths: Iterable[str] = (),
    ) -> list[FileRow]:
        sql, args = f"SELECT {FILE_COLS} FROM files WHERE 1=1", []
        if user:
            sql += " AND user=?"
            args.append(user)
        if path_prefix:
            prefix = "/" + path_prefix.strip("/")
            sql += " AND (path=? OR substr(path, 1, ?)=?)"
            args += [prefix, len(prefix) + 1, prefix + "/"]
        for excluded in exclude_paths:
            prefix = "/" + excluded.strip("/")
            sql += " AND NOT (path=? OR substr(path, 1, ?)=?)"
            args += [prefix, len(prefix) + 1, prefix + "/"]
        if statuses:
            statuses = tuple(statuses)
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            args += statuses
        if run_id is not None:
            sql += " AND run_id=?"
            args.append(run_id)
        sql += " ORDER BY user, fileid"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        return [FileRow(**r) for r in self.db.execute(sql, args)]

    def requeue(self, user: str | None = None, path_prefix: str | None = None) -> int:
        """Remet en pending les fichiers déjà traités, en erreur ou ignorés (--reprocess)."""
        rows = self.list_files(user, path_prefix, ("done", "error", "skipped"))
        self.db.executemany(
            "UPDATE files SET status='pending', error=NULL, updated_at=? WHERE user=? AND fileid=?",
            [(time.time(), r.user, r.fileid) for r in rows],
        )
        self.db.commit()
        return len(rows)

    def _set_status(
        self, user: str, fileid: int, status: str, model_ver: str | None, error: str | None
    ) -> None:
        self.db.execute(
            "UPDATE files SET status=?, model_ver=COALESCE(?, model_ver), error=?, updated_at=?"
            " WHERE user=? AND fileid=?",
            (status, model_ver, error, time.time(), user, fileid),
        )
        self.db.commit()

    def mark_done(self, user: str, fileid: int, model_ver: str | None = None) -> None:
        self._set_status(user, fileid, "done", model_ver, None)

    def mark_error(self, user: str, fileid: int, error: str) -> None:
        self._set_status(user, fileid, "error", None, error)

    def mark_skipped(self, user: str, fileid: int, reason: str | None = None) -> None:
        self._set_status(user, fileid, "skipped", None, reason)

    def counts(self, user: str | None = None) -> dict[str, int]:
        sql, args = "SELECT status, COUNT(*) n FROM files", []
        if user:
            sql += " WHERE user=?"
            args.append(user)
        sql += " GROUP BY status"
        out = dict.fromkeys(STATUSES, 0)
        out.update({r["status"]: r["n"] for r in self.db.execute(sql, args)})
        return out

    def record_scores(self, user: str, fileid: int, scores: dict[str, float]) -> None:
        self.db.execute("DELETE FROM scores WHERE user=? AND fileid=?", (user, fileid))
        self.db.executemany(
            "INSERT INTO scores (user, fileid, tag, score) VALUES (?, ?, ?, ?)",
            [(user, fileid, tag, s) for tag, s in scores.items()],
        )
        self.db.commit()

    def get_scores(self, user: str, fileid: int) -> dict[str, float]:
        rows = self.db.execute(
            "SELECT tag, score FROM scores WHERE user=? AND fileid=?", (user, fileid)
        )
        return {r["tag"]: r["score"] for r in rows}

    def record_proposals(self, user: str, fileid: int, tags: dict[str, float]) -> None:
        """Tags Nextcloud proposés (hors « à supprimer », qui découle des raisons)."""
        self.db.execute("DELETE FROM proposals WHERE user=? AND fileid=?", (user, fileid))
        self.db.executemany(
            "INSERT INTO proposals (user, fileid, tag, score) VALUES (?, ?, ?, ?)",
            [(user, fileid, tag, s) for tag, s in tags.items()],
        )
        self.db.commit()

    def get_proposals(self, user: str, fileid: int) -> dict[str, float]:
        rows = self.db.execute(
            "SELECT tag, score FROM proposals WHERE user=? AND fileid=? ORDER BY score DESC, tag",
            (user, fileid),
        )
        return {r["tag"]: r["score"] for r in rows}

    def set_result(
        self, user: str, fileid: int, reasons: list[str], run_id: int, keeper: int | None = None
    ) -> None:
        self.db.execute(
            "UPDATE files SET reasons=?, keeper=?, run_id=? WHERE user=? AND fileid=?",
            (",".join(reasons) or None, keeper, run_id, user, fileid),
        )
        self.db.commit()

    def apply_duplicates(self, user: str, dup_reason: str, flags: dict[int, int]) -> None:
        """Remplace les marques de doublon d'un compte : fileid signalé -> fileid gardé."""
        for f in self.list_files(user, statuses=("done",)):
            reasons = [r for r in f.reason_list if r != dup_reason]
            keeper = None
            if f.fileid in flags:
                reasons.append(dup_reason)
                keeper = flags[f.fileid]
            if reasons != f.reason_list or keeper != f.keeper:
                self.db.execute(
                    "UPDATE files SET reasons=?, keeper=? WHERE user=? AND fileid=?",
                    (",".join(reasons) or None, keeper, user, f.fileid),
                )
        self.db.commit()

    def set_applied(self, user: str, fileid: int, wanted: list[str]) -> None:
        self.db.execute(
            "UPDATE files SET applied=? WHERE user=? AND fileid=?",
            (json.dumps(sorted(wanted), ensure_ascii=False), user, fileid),
        )
        self.db.commit()

    def clear_applied(self, user: str, fileid: int) -> None:
        self.db.execute("UPDATE files SET applied=NULL WHERE user=? AND fileid=?", (user, fileid))
        self.db.commit()

    def start_run(self, mode: str) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (started_at, mode) VALUES (?, ?)", (time.time(), mode)
        )
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, stats: dict) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, stats=? WHERE id=?",
            (time.time(), json.dumps(stats, ensure_ascii=False), run_id),
        )
        self.db.commit()

    def recent_runs(self, limit: int = 5) -> list[RunRow]:
        rows = self.db.execute(
            "SELECT id, started_at, mode, finished_at, stats FROM runs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            RunRow(
                r["id"],
                r["started_at"],
                r["mode"],
                r["finished_at"],
                json.loads(r["stats"] or "{}"),
            )
            for r in rows
        ]

    def all_assignments(
        self, run_id: int | None = None, user: str | None = None, only_active: bool = True
    ) -> list[Assignment]:
        sql, args = f"SELECT {ASSIGN_COLS} FROM assignments WHERE 1=1", []
        if run_id is not None:
            sql += " AND run_id=?"
            args.append(run_id)
        if user:
            sql += " AND user=?"
            args.append(user)
        if only_active:
            sql += " AND removed_at IS NULL"
        return [Assignment(**r) for r in self.db.execute(sql + " ORDER BY rowid", args)]

    def last_run_id(self) -> int | None:
        return self.db.execute("SELECT MAX(id) m FROM runs").fetchone()["m"]

    def journal_assignment(
        self, run_id: int, user: str, fileid: int, tag: str, tag_id: int | None = None
    ) -> None:
        """Uniquement pour une affectation posée par l'outil, jamais une préexistante."""
        self.db.execute(
            "INSERT INTO assignments (run_id, user, fileid, tag, tag_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, user, fileid, tag, tag_id, time.time()),
        )
        self.db.commit()

    def assignments_for_run(self, run_id: int, only_active: bool = True) -> list[Assignment]:
        sql = f"SELECT {ASSIGN_COLS} FROM assignments WHERE run_id=?"
        if only_active:
            sql += " AND removed_at IS NULL"
        return [Assignment(**r) for r in self.db.execute(sql + " ORDER BY rowid", (run_id,))]

    def active_assignments(self, user: str, fileid: int) -> list[Assignment]:
        rows = self.db.execute(
            f"SELECT {ASSIGN_COLS} FROM assignments"
            " WHERE user=? AND fileid=? AND removed_at IS NULL",
            (user, fileid),
        )
        return [Assignment(**r) for r in rows]

    def mark_removed(self, assignments: Iterable[Assignment]) -> None:
        now = time.time()
        self.db.executemany(
            "UPDATE assignments SET removed_at=? WHERE run_id=? AND user=? AND fileid=?"
            " AND tag=? AND removed_at IS NULL",
            [(now, a.run_id, a.user, a.fileid, a.tag) for a in assignments],
        )
        self.db.commit()

    def upsert_hash(
        self,
        user: str,
        fileid: int,
        kind: str,
        dhash: int,
        sharpness: float | None = None,
        pixels: int | None = None,
        duration: float | None = None,
        taken_at: str | None = None,
    ) -> None:
        """dhash 64 bits non signé ; SQLite stocke des entiers signés 64 bits."""
        signed = dhash - (1 << 64) if dhash >= 1 << 63 else dhash
        self.db.execute(
            "INSERT OR REPLACE INTO hashes"
            " (user, fileid, kind, dhash, sharpness, pixels, duration, taken_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user, fileid, kind, signed, sharpness, pixels, duration, taken_at),
        )
        self.db.commit()

    def get_hashes(self, user: str | None = None) -> list[HashRow]:
        """Empreintes, avec le dossier du fichier (via files) pour les prises sans date."""
        sql = (
            "SELECT h.user, h.fileid, h.kind, h.dhash, h.sharpness, h.pixels, h.duration,"
            " h.taken_at, f.path FROM hashes h LEFT JOIN files f"
            " ON f.user=h.user AND f.fileid=h.fileid"
        )
        args: list = []
        if user:
            sql += " WHERE h.user=?"
            args.append(user)
        rows = []
        for r in self.db.execute(sql + " ORDER BY h.user, h.fileid", args):
            folder = str(PurePosixPath(r["path"].replace("\\", "/")).parent) if r["path"] else ""
            rows.append(
                HashRow(
                    r["user"], r["fileid"], r["kind"], r["dhash"] & ((1 << 64) - 1),
                    r["sharpness"], r["pixels"], r["duration"], r["taken_at"], folder,
                )
            )  # fmt: skip
        return rows
