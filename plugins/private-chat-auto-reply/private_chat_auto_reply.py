"""Generate and send automatic text replies for configured WeChat private chats."""

import asyncio
import json
import os
import secrets
import sqlite3
from collections import defaultdict
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from queue import Empty, Queue
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, model_validator
from private_chat_invitations import CONFIRMATION, PREFIX, Invitations
from wechat_invite_admission import CREDENTIAL_NAME, PROBE_OPERATION, read_key, sign
from wechat_ai_bot.plugins.core.plugin_interface import Plugin
from wechat_ai_bot.rpa.action_handlers import SendTextMessageAction
from wechat_ai_bot.weixin.message_classes import TextMessage


REQUEST_LIMIT = 131072
REPLY_TIMEOUT = 95
SEND_TIMEOUT = 45


class PrivateChatAutoReplyConfig(BaseModel):
    enabled: bool = False
    invitations_enabled: bool = False
    contact_usernames: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]] = Field(
        default_factory=list,
    )
    ssh_host: str
    ssh_user: str = "reply-bridge"
    ssh_directory: Path = Path("/run/private-chat-auto-reply-ssh")
    state_directory: Path = Path("/config/private-chat-auto-reply")
    conversation_namespace: str = "wechat-private-chat-auto-reply"

    @model_validator(mode="before")
    @classmethod
    def read_legacy_contact(cls, values):
        if isinstance(values, dict) and "contact_usernames" not in values:
            legacy = values.get("contact_username")
            if legacy is not None and legacy != "":
                return {**values, "contact_usernames": [legacy]}
        return values


class GenerationError(Exception):
    """Generation failed without a usable reply."""


def extract_replies(response: dict) -> list[str]:
    if "replies" in response:
        raw = response["replies"]
        if not isinstance(raw, list) or not raw:
            return []
        if any(not isinstance(item, str) or not item.strip() for item in raw):
            return []
        return [item.strip() for item in raw]
    reply = response.get("reply")
    if isinstance(reply, str) and reply.strip():
        return [reply.strip()]
    return []


async def request_reply(config: PrivateChatAutoReplyConfig, payload: dict, *, timeout=REPLY_TIMEOUT) -> dict:
    request = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
    if len(request) > REQUEST_LIMIT:
        raise GenerationError("request_too_large")

    process = await asyncio.create_subprocess_exec(
        "/usr/bin/ssh", "-F", "/dev/null", "-T",
        "-i", str(config.ssh_directory / "id_ed25519"),
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", f"UserKnownHostsFile={config.ssh_directory / 'known_hosts'}",
        "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
        "--", f"{config.ssh_user}@{config.ssh_host}",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(request), timeout)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        raise

    if process.returncode != 0 or not output or len(output) > REQUEST_LIMIT:
        raise GenerationError("ssh_or_protocol_error")
    try:
        response = json.loads(output)
    except (ValueError, UnicodeError) as error:
        raise GenerationError("invalid_response") from error
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise GenerationError("engine_request_failed")
    return response


class PrivateChatAutoReplyPlugin(Plugin):
    def __init__(self, bot):
        super().__init__(bot)
        self.turn_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        directory = self.plugin_config.state_directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_file = directory / "turns.sqlite3"
        fd = os.open(self.state_file, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        with closing(sqlite3.connect(self.state_file)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    conversation TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    action_id TEXT,
                    PRIMARY KEY (conversation, message_id)
                )
                """
            )
        self.invitations = Invitations(self.state_file) if self.plugin_config.invitations_enabled else None

    @classmethod
    def get_plugin_config_schema(cls):
        return PrivateChatAutoReplyConfig

    def get_priority(self):
        return 0

    def get_plugin_name(self):
        return "private-chat-auto-reply"

    def get_plugin_description(self):
        return "Automatic text replies for configured private WeChat contacts with separate conversations"

    def _claim(self, conversation: str, message_id: str) -> bool:
        with closing(sqlite3.connect(self.state_file)) as connection, connection:
            result = connection.execute(
                "INSERT OR IGNORE INTO turns VALUES (?, ?, 'claimed', NULL)",
                (conversation, message_id),
            )
            return result.rowcount == 1

    def _record(self, conversation: str, message_id: str, status: str, action_id=None):
        with closing(sqlite3.connect(self.state_file)) as connection, connection:
            connection.execute(
                "UPDATE turns SET status = ?, action_id = ? WHERE conversation = ? AND message_id = ?",
                (status, action_id, conversation, message_id),
            )
        self.logger.info("Private chat auto-reply status=%s action=%s", status, action_id or "-")

    def _target(self, username: str) -> str | None:
        database = self.bot.database_service
        if database is None or not database.is_available:
            return None
        contact = database.get_contact_by_username(username)
        if not contact or contact.is_chatroom or not contact.display_name:
            return None
        matches = database.get_contact_by_display_name(contact.display_name) or []
        usernames = {match.username for match in matches if match.display_name == contact.display_name}
        return contact.display_name if usernames == {username} else None

    def _invitation_account(self):
        if self.invitations is None or not self.plugin_config.enabled or not self.plugin_config.invitations_enabled:
            return ""
        return self.bot.user_info.account or ""

    def invitation_state(self):
        account = self._invitation_account()
        state = self.invitations.snapshot(account) if account else {"invitations": [], "contacts": []}
        for contact in state["contacts"]:
            contact["display_name"] = self._target(contact["username"]) or contact["username"]
        return {"available": bool(account), **state}

    async def issue_invitation(self):
        account = self._invitation_account()
        if not account:
            return None
        config = self.plugin_config
        payload = {"op": PROBE_OPERATION, "conversation": secrets.token_hex(32), "message_id": "1", "text": ""}
        try:
            payload["invite_admission"] = sign(read_key(config.ssh_directory / CREDENTIAL_NAME), payload)
            await request_reply(config, payload, timeout=10)
        except (GenerationError, TimeoutError, OSError, ValueError):
            return None
        if account != self._invitation_account() or config is not self.plugin_config:
            return None
        return self.invitations.issue(account)

    def revoke_invitation(self, invite_id):
        account = self._invitation_account()
        return bool(account and self.invitations.revoke(account, invite_id))

    def remove_invited_contact(self, membership_id):
        account = self._invitation_account()
        return bool(account and self.invitations.remove(account, membership_id))

    def _membership(self, account, username):
        if username in self.plugin_config.contact_usernames:
            return "static"
        if account == self._invitation_account():
            return self.invitations.active(account, username)
        return None

    async def _redeem_invitation(self, account, username, code):
        if account != self._invitation_account() or self._membership(account, username):
            return
        invite_id = self.invitations.redeem(account, username, code)
        if invite_id is None:
            return
        target = self._target(username)
        if not target or self.bot.rpa_service is None:
            self.invitations.confirmation(invite_id, "not_sent")
            return
        results = Queue(maxsize=1)
        action = SendTextMessageAction(
            target_username=username, target=target, content=CONFIRMATION, is_chatroom=False, result_queue=results,
        )
        self.invitations.confirmation(invite_id, "queued", action.action_id)
        try:
            self.bot.rpa_service.track_action(action)
            self.add_rpa_action(action)
            result = await asyncio.to_thread(results.get, True, SEND_TIMEOUT)
        except Empty:
            status = "unconfirmed"
        except Exception:
            self.logger.exception("Invitation confirmation could not be verified")
            status = "unconfirmed"
        else:
            if result.get("ok") and result.get("verified"):
                status = "verified"
            else:
                status = "failed" if result.get("send_attempted") else "not_sent"
        self.invitations.confirmation(invite_id, status, action.action_id)

    async def handle_message(self, context):
        message = context.get_message()
        config = self.plugin_config
        if (
            not config.enabled or not isinstance(message, TextMessage)
            or message.room is not None or not message.contact
            or message.contact.is_chatroom
            or message.is_self or not message.server_id
        ):
            return
        contact_username = message.contact.username
        text = message.to_text()
        account = message.user_info.account
        if not account or not contact_username or not isinstance(text, str) or not text.strip():
            return

        conversation = sha256(
            f"{config.conversation_namespace}\0{account}\0{contact_username}".encode()
        ).hexdigest()
        message_id = str(message.server_id)
        code = text.strip()
        if code.startswith(PREFIX):
            if account == self._invitation_account():
                async with self.turn_locks[conversation]:
                    await self._redeem_invitation(account, contact_username, code)
            return
        if not self._membership(account, contact_username):
            return
        async with self.turn_locks[conversation]:
            membership = self._membership(account, contact_username)
            if not membership or not self._target(contact_username):
                return
            try:
                payload = {
                    "op": "generate", "conversation": conversation,
                    "message_id": message_id, "text": text,
                }
                if membership != "static":
                    payload["invite_admission"] = sign(read_key(config.ssh_directory / CREDENTIAL_NAME), payload)
                if not self._claim(conversation, message_id):
                    return
                response = await request_reply(config, payload)
            except (GenerationError, TimeoutError, OSError, ValueError) as error:
                self._record(conversation, message_id, "generation_failed")
                self.logger.warning("Private chat auto-reply generation stopped (%s)", type(error).__name__)
                return

            replies = extract_replies(response)
            target = self._target(contact_username)
            if (not replies or not target
                    or self._membership(account, contact_username) != membership):
                self._record(conversation, message_id, "reply_discarded")
                return

            last_action_id = None
            for index, bubble in enumerate(replies):
                if index > 0:
                    target = self._target(contact_username)
                    if not target or self._membership(account, contact_username) != membership:
                        self._record(conversation, message_id, "send_failed", last_action_id)
                        return
                results = Queue(maxsize=1)
                action = SendTextMessageAction(
                    target_username=contact_username, target=target,
                    content=bubble, is_chatroom=False, result_queue=results,
                )
                self._record(conversation, message_id, "queued", action.action_id)
                self.bot.rpa_service.track_action(action)
                self.add_rpa_action(action)
                try:
                    result = await asyncio.to_thread(results.get, True, SEND_TIMEOUT)
                except Empty:
                    self._record(conversation, message_id, "send_unconfirmed", action.action_id)
                    return
                if not (result.get("ok") and result.get("verified")):
                    self._record(conversation, message_id, "send_failed", action.action_id)
                    return
                last_action_id = action.action_id
            self._record(conversation, message_id, "executed", last_action_id)
