"""SQLite storage for check-in tokens and pending submissions.

The bot used to keep a check-in in ``context.user_data``, so a restart in the
middle of one lost it. A guest filling in the self check-in form is worse: the
link has to survive a redeploy, and a submission waiting for approval must not
evaporate. Both live here instead.

Plain stdlib sqlite3 behind ``asyncio.to_thread`` - the volumes involved are a
handful of rows per day, so a connection per call is cheaper than an extra
dependency.
"""
import asyncio
import json
import logging
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Statuses a submission moves through.
STATUS_PENDING = "pending"    # guest confirmed, waiting for the owner
STATUS_APPLIED = "applied"    # written to Rentlio
STATUS_REJECTED = "rejected"  # owner declined
STATUS_FAILED = "failed"      # owner approved but Rentlio refused

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkin_tokens (
    token           TEXT PRIMARY KEY,
    reservation_id  TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    revoked_at      INTEGER,
    opened_at       INTEGER,
    submitted_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tokens_reservation
    ON checkin_tokens(reservation_id);

CREATE TABLE IF NOT EXISTS submissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT,
    reservation_id  TEXT NOT NULL,
    guests_json     TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'form',
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      INTEGER NOT NULL,
    decided_at      INTEGER,
    decided_by      INTEGER,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_submissions_status
    ON submissions(status);
"""


class Store:
    """Everything that has to outlive a container restart."""

    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        self._ready = False

    # ---------- plumbing ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        # WAL keeps the web request and the Telegram handler from blocking
        # each other on the same file.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_sync(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    async def init(self) -> None:
        if self._ready:
            return
        await asyncio.to_thread(self._init_sync)
        self._ready = True
        logger.info(f"Store ready at {self.db_path}")

    # ---------- tokens ----------

    def _create_token_sync(self, reservation_id: str, ttl_days: int) -> str:
        now = int(time.time())
        with self._connect() as conn:
            # Tapping "send link" twice should hand out the same link, not
            # invalidate the one already sitting in the guest's chat.
            row = conn.execute(
                """
                SELECT token FROM checkin_tokens
                 WHERE reservation_id = ?
                   AND revoked_at IS NULL
                   AND submitted_at IS NULL
                   AND expires_at > ?
                 ORDER BY created_at DESC
                 LIMIT 1
                """,
                (str(reservation_id), now),
            ).fetchone()
            if row:
                return row["token"]

            token = secrets.token_urlsafe(16)
            conn.execute(
                """
                INSERT INTO checkin_tokens
                       (token, reservation_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (token, str(reservation_id), now, now + ttl_days * 86400),
            )
            return token

    async def create_token(self, reservation_id: str, ttl_days: int = 30) -> str:
        """Return a usable check-in token for this reservation, reusing one if live."""
        return await asyncio.to_thread(self._create_token_sync, reservation_id, ttl_days)

    def _get_token_sync(self, token: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM checkin_tokens WHERE token = ?", (token,)
            ).fetchone()

    async def get_token(self, token: str) -> Optional[dict]:
        """Raw token row, whether or not it is still valid."""
        row = await asyncio.to_thread(self._get_token_sync, token)
        return dict(row) if row else None

    async def valid_token(self, token: str) -> Optional[dict]:
        """Token row, or None if it does not exist, is revoked, or has expired.

        An already-submitted token stays valid: a guest who mistyped a name
        needs to be able to reopen the link and send a correction.
        """
        row = await self.get_token(token)
        if not row:
            return None
        if row["revoked_at"] is not None:
            logger.info(f"Token {token[:6]}... was revoked")
            return None
        if row["expires_at"] <= int(time.time()):
            logger.info(f"Token {token[:6]}... expired")
            return None
        return row

    def _touch_token_sync(self, token: str, column: str) -> None:
        with self._connect() as conn:
            conn.execute(
                f"UPDATE checkin_tokens SET {column} = ? WHERE token = ? "
                f"AND {column} IS NULL",
                (int(time.time()), token),
            )

    async def mark_opened(self, token: str) -> None:
        await asyncio.to_thread(self._touch_token_sync, token, "opened_at")

    async def mark_submitted(self, token: str) -> None:
        await asyncio.to_thread(self._touch_token_sync, token, "submitted_at")

    def _revoke_sync(self, reservation_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE checkin_tokens SET revoked_at = ?
                 WHERE reservation_id = ? AND revoked_at IS NULL
                """,
                (int(time.time()), str(reservation_id)),
            )
            return cur.rowcount

    async def revoke_tokens(self, reservation_id: str) -> int:
        """Kill every live link for a reservation. Returns how many."""
        return await asyncio.to_thread(self._revoke_sync, reservation_id)

    # ---------- submissions ----------

    def _create_submission_sync(
        self, token: Optional[str], reservation_id: str, guests: list, source: str
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO submissions
                       (token, reservation_id, guests_json, source, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    token,
                    str(reservation_id),
                    json.dumps(guests, ensure_ascii=False),
                    source,
                    STATUS_PENDING,
                    int(time.time()),
                ),
            )
            return cur.lastrowid

    async def create_submission(
        self,
        reservation_id: str,
        guests: list,
        token: Optional[str] = None,
        source: str = "form",
    ) -> int:
        """Park a guest's confirmed data until the owner approves it."""
        return await asyncio.to_thread(
            self._create_submission_sync, token, reservation_id, guests, source
        )

    def _get_submission_sync(self, submission_id: int) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM submissions WHERE id = ?", (submission_id,)
            ).fetchone()

    async def get_submission(self, submission_id: int) -> Optional[dict]:
        row = await asyncio.to_thread(self._get_submission_sync, submission_id)
        if not row:
            return None
        data = dict(row)
        data["guests"] = json.loads(data["guests_json"])
        return data

    def _claim_sync(self, submission_id: int, status: str, by: Optional[int],
                    note: Optional[str]) -> bool:
        with self._connect() as conn:
            # Only a pending submission can be decided, so a double tap on the
            # Telegram button cannot write the same guests to Rentlio twice.
            cur = conn.execute(
                """
                UPDATE submissions
                   SET status = ?, decided_at = ?, decided_by = ?, note = ?
                 WHERE id = ? AND status = ?
                """,
                (status, int(time.time()), by, note, submission_id, STATUS_PENDING),
            )
            return cur.rowcount == 1

    async def claim_submission(
        self,
        submission_id: int,
        status: str,
        decided_by: Optional[int] = None,
        note: Optional[str] = None,
    ) -> bool:
        """Move a pending submission to a decided state.

        Returns False if somebody already decided it - the caller should then
        say so rather than acting again.
        """
        return await asyncio.to_thread(
            self._claim_sync, submission_id, status, decided_by, note
        )

    def _set_status_sync(self, submission_id: int, status: str, note: Optional[str]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE submissions SET status = ?, note = ? WHERE id = ?",
                (status, note, submission_id),
            )

    async def set_status(
        self, submission_id: int, status: str, note: Optional[str] = None
    ) -> None:
        """Record the outcome of an already-claimed submission."""
        await asyncio.to_thread(self._set_status_sync, submission_id, status, note)

    def _pending_sync(self) -> list:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM submissions WHERE status = ?
                 ORDER BY created_at ASC
                """,
                (STATUS_PENDING,),
            ).fetchall()

    async def pending_submissions(self) -> list:
        rows = await asyncio.to_thread(self._pending_sync)
        out = []
        for row in rows:
            data = dict(row)
            data["guests"] = json.loads(data["guests_json"])
            out.append(data)
        return out

    def _latest_for_reservation_sync(self, reservation_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT * FROM submissions WHERE reservation_id = ?
                 ORDER BY created_at DESC LIMIT 1
                """,
                (str(reservation_id),),
            ).fetchone()

    async def latest_submission(self, reservation_id: str) -> Optional[dict]:
        row = await asyncio.to_thread(self._latest_for_reservation_sync, reservation_id)
        return dict(row) if row else None
