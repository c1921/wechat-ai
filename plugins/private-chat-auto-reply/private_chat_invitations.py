"""Persist one-time codes and private-chat membership for each WeChat account."""

import re
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from hashlib import sha256


PREFIX = "WXJOIN-"
LIFETIME = 30 * 60
CONFIRMATION = "已加入私聊自动回复。请再发一条消息开始对话。"


class Invitations:
    def __init__(self, path):
        self.path = path
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reply_invitations (
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, digest TEXT UNIQUE NOT NULL,
                    expires_at INTEGER NOT NULL, revoked_at INTEGER, redeemed_by TEXT,
                    redeemed_at INTEGER, confirmation_status TEXT, confirmation_action_id TEXT
                );
                CREATE TABLE IF NOT EXISTS reply_invite_members (
                    account TEXT NOT NULL, username TEXT NOT NULL, invite_id TEXT UNIQUE NOT NULL,
                    joined_at INTEGER NOT NULL, removed_at INTEGER,
                    PRIMARY KEY (account, username)
                );
            """)
            db.execute(
                """UPDATE reply_invitations SET confirmation_status='unconfirmed'
                   WHERE confirmation_status IN ('pending', 'queued')"""
            )

    def _connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def issue(self, account):
        code = PREFIX + secrets.token_hex(16)
        invite_id = uuid.uuid4().hex
        expires = int(time.time()) + LIFETIME
        with closing(self._connect()) as db, db:
            db.execute(
                "INSERT INTO reply_invitations (id, account, digest, expires_at) VALUES (?, ?, ?, ?)",
                (invite_id, account, sha256(code.encode()).hexdigest(), expires),
            )
        return {"id": invite_id, "code": code, "expires_at": expires}

    def redeem(self, account, username, code):
        if re.fullmatch(PREFIX + r"[0-9a-f]{32}", code) is None:
            return None
        now = int(time.time())
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM reply_invite_members WHERE account=? AND username=? AND removed_at IS NULL",
                (account, username),
            ).fetchone():
                return None
            invite = db.execute(
                """SELECT id FROM reply_invitations WHERE account=? AND digest=?
                   AND expires_at>? AND revoked_at IS NULL AND redeemed_at IS NULL""",
                (account, sha256(code.encode()).hexdigest(), now),
            ).fetchone()
            if not invite:
                return None
            invite_id = invite["id"]
            db.execute(
                """UPDATE reply_invitations SET redeemed_by=?, redeemed_at=?,
                   confirmation_status='pending' WHERE id=?""",
                (username, now, invite_id),
            )
            db.execute(
                """INSERT INTO reply_invite_members (account, username, invite_id, joined_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT (account, username) DO UPDATE SET
                       invite_id=excluded.invite_id, joined_at=excluded.joined_at, removed_at=NULL""",
                (account, username, invite_id, now),
            )
            return invite_id

    def active(self, account, username):
        with closing(self._connect()) as db:
            row = db.execute(
                """SELECT invite_id FROM reply_invite_members
                   WHERE account=? AND username=? AND removed_at IS NULL""",
                (account, username),
            ).fetchone()
        return row["invite_id"] if row else None

    def confirmation(self, invite_id, status, action_id=None):
        with closing(self._connect()) as db, db:
            db.execute(
                "UPDATE reply_invitations SET confirmation_status=?, confirmation_action_id=? WHERE id=?",
                (status, action_id, invite_id),
            )

    def revoke(self, account, invite_id):
        now = int(time.time())
        with closing(self._connect()) as db, db:
            return db.execute(
                """UPDATE reply_invitations SET revoked_at=? WHERE account=? AND id=?
                   AND redeemed_at IS NULL AND revoked_at IS NULL AND expires_at>?""",
                (now, account, invite_id, now),
            ).rowcount == 1

    def remove(self, account, invite_id):
        with closing(self._connect()) as db, db:
            return db.execute(
                """UPDATE reply_invite_members SET removed_at=?
                   WHERE account=? AND invite_id=? AND removed_at IS NULL""",
                (int(time.time()), account, invite_id),
            ).rowcount == 1

    def snapshot(self, account):
        with closing(self._connect()) as db:
            invites = db.execute(
                """SELECT id, expires_at, redeemed_by, redeemed_at, confirmation_status,
                       confirmation_action_id, CASE
                       WHEN redeemed_at IS NOT NULL THEN 'used'
                       WHEN revoked_at IS NOT NULL THEN 'revoked'
                       WHEN expires_at<=? THEN 'expired' ELSE 'pending' END AS status
                   FROM reply_invitations WHERE account=?
                   ORDER BY expires_at DESC, rowid DESC LIMIT 100""",
                (int(time.time()), account),
            ).fetchall()
            contacts = db.execute(
                """SELECT m.invite_id AS id, m.username, m.joined_at, i.confirmation_status,
                          i.confirmation_action_id
                   FROM reply_invite_members m JOIN reply_invitations i ON i.id=m.invite_id
                   WHERE m.account=? AND m.removed_at IS NULL ORDER BY m.joined_at DESC""",
                (account,),
            ).fetchall()
        return {"invitations": [dict(row) for row in invites], "contacts": [dict(row) for row in contacts]}
