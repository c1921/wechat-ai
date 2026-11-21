import asyncio
import logging
import sqlite3
import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from private_chat_auto_reply import PrivateChatAutoReplyPlugin, request_reply
from private_chat_invitations import CONFIRMATION
from wechat_invite_admission import CREDENTIAL_NAME, PROBE_OPERATION, verify
from wechat_ai_bot.weixin.message_classes import TextMessage


class PrivateChatAutoReplyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.contact = SimpleNamespace(username="allowed-id", display_name="Allowed", is_chatroom=False)
        self.contacts = {self.contact.username: self.contact}
        self.actions = []
        self.config = {
            "enabled": True,
            "contact_username": self.contact.username,
            "ssh_host": "example.invalid",
            "state_directory": self.directory,
        }
        self.bot = SimpleNamespace(
            logger=logging.getLogger(__name__),
            config={"plugins": {"private-chat-auto-reply": self.config}},
            database_service=SimpleNamespace(
                is_available=True,
                get_contact_by_username=self.contacts.get,
                get_contact_by_display_name=lambda name: [
                    contact for contact in self.contacts.values() if contact.display_name == name
                ],
            ),
            rpa_task_queue=None,
            rpa_service=SimpleNamespace(track_action=Mock()),
            processor_service=SimpleNamespace(add_rpa_actions=self.handle_actions),
        )
        self.plugin = PrivateChatAutoReplyPlugin(self.bot)

    def handle_actions(self, actions):
        self.actions.extend(actions)
        for action in actions:
            action.result_queue.put({"ok": True, "verified": True})

    def context(self, message_id=1, **overrides):
        message = Mock(spec=TextMessage)
        message.to_text.return_value = f"test {message_id}"
        message.contact = self.contact
        message.room = None
        message.is_self = False
        message.server_id = message_id
        message.user_info = SimpleNamespace(account="test-account")
        for name, value in overrides.items():
            setattr(message, name, value)
        return SimpleNamespace(get_message=lambda: message)

    async def test_scope(self):
        with patch("private_chat_auto_reply.request_reply", new_callable=AsyncMock) as generate:
            await self.plugin.handle_message(self.context(is_self=True))
            await self.plugin.handle_message(self.context(room=object()))
            await self.plugin.handle_message(self.context(server_id=0))
            await self.plugin.handle_message(self.context(contact=SimpleNamespace(
                username="other-id", is_chatroom=False,
            )))
            await self.plugin.handle_message(SimpleNamespace(get_message=lambda: object()))
            self.bot.database_service.get_contact_by_display_name = lambda name: [
                self.contact, SimpleNamespace(display_name=name, username="other-id")
            ]
            await self.plugin.handle_message(self.context())
            self.bot.database_service = None
            await self.plugin.handle_message(self.context())
            generate.assert_not_awaited()
            self.assertEqual(self.actions, [])

    async def test_serial_delivery_and_persistent_dedup(self):
        async def generate(config, turn):
            await asyncio.sleep(0.01)
            return {"reply": turn["text"]}

        with patch("private_chat_auto_reply.request_reply", side_effect=generate) as request:
            await asyncio.gather(
                self.plugin.handle_message(self.context(11)),
                self.plugin.handle_message(self.context(12)),
                self.plugin.handle_message(self.context(11)),
            )
            await PrivateChatAutoReplyPlugin(self.bot).handle_message(self.context(11))
            self.assertEqual(request.await_count, 2)
        self.assertEqual([action.content for action in self.actions], ["test 11", "test 12"])
        self.assertTrue(all(action.target_username == self.contact.username for action in self.actions))

    async def test_generation_timeout_does_not_retry(self):
        with patch("private_chat_auto_reply.request_reply", side_effect=TimeoutError) as generate:
            await self.plugin.handle_message(self.context(21))
            await self.plugin.handle_message(self.context(21))
            self.assertEqual(generate.await_count, 1)
        self.assertEqual(self.actions, [])
        with closing(sqlite3.connect(self.plugin.state_file)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM turns").fetchone(), ("generation_failed",))

    async def test_unconfirmed_send_does_not_retry(self):
        self.bot.processor_service.add_rpa_actions = self.actions.extend
        with patch("private_chat_auto_reply.SEND_TIMEOUT", 0.02), patch(
            "private_chat_auto_reply.request_reply", new_callable=AsyncMock, return_value={"reply": "test"}
        ) as generate:
            await self.plugin.handle_message(self.context(31))
            await PrivateChatAutoReplyPlugin(self.bot).handle_message(self.context(31))
            self.assertEqual(generate.await_count, 1)
        self.assertEqual(len(self.actions), 1)
        with closing(sqlite3.connect(self.plugin.state_file)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM turns").fetchone(), ("send_unconfirmed",))

    async def test_multi_bubble_delivery_sends_each_segment_in_order(self):
        with patch(
            "private_chat_auto_reply.request_reply",
            new_callable=AsyncMock,
            return_value={"ok": True, "reply": "第一条\n\n第二条\n\n第三条", "replies": ["第一条", "第二条", "第三条"]},
        ):
            await self.plugin.handle_message(self.context(35))
        self.assertEqual([action.content for action in self.actions], ["第一条", "第二条", "第三条"])
        with closing(sqlite3.connect(self.plugin.state_file)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM turns").fetchone(), ("executed",))

    async def test_multi_bubble_failure_stops_remaining_bubbles_without_retry(self):
        def send(actions):
            action = actions[0]
            self.actions.append(action)
            if len(self.actions) == 2:
                action.result_queue.put({"ok": False, "verified": False})
            else:
                action.result_queue.put({"ok": True, "verified": True})

        self.bot.processor_service.add_rpa_actions = send
        with patch(
            "private_chat_auto_reply.request_reply",
            new_callable=AsyncMock,
            return_value={"ok": True, "replies": ["第一条", "第二条", "第三条"]},
        ) as generate:
            await self.plugin.handle_message(self.context(36))
            await self.plugin.handle_message(self.context(36))
            self.assertEqual(generate.await_count, 1)
        self.assertEqual([action.content for action in self.actions], ["第一条", "第二条"])
        with closing(sqlite3.connect(self.plugin.state_file)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM turns").fetchone(), ("send_failed",))

    async def test_multi_bubble_delivery_has_no_segment_cap(self):
        bubbles = [f"第{i}条" for i in range(12)]
        with patch(
            "private_chat_auto_reply.request_reply",
            new_callable=AsyncMock,
            return_value={"ok": True, "reply": "\n\n".join(bubbles), "replies": bubbles},
        ):
            await self.plugin.handle_message(self.context(34))
        self.assertEqual([action.content for action in self.actions], bubbles)

    async def test_malformed_multi_bubble_replies_are_discarded(self):
        for invalid in ([], ["ok", "  "], [1], "not-a-list"):
            self.actions.clear()
            with self.subTest(invalid=invalid), patch(
                "private_chat_auto_reply.request_reply",
                new_callable=AsyncMock,
                return_value={"ok": True, "reply": "fallback", "replies": invalid},
            ):
                await self.plugin.handle_message(self.context(37 + len(str(invalid))))
            self.assertEqual(self.actions, [])

    async def test_migration_keeps_the_conversation_id_and_existing_dedup(self):
        conversation = sha256(b"wechat-aimee\0test-account\0allowed-id").hexdigest()
        with closing(sqlite3.connect(self.plugin.state_file)) as connection, connection:
            connection.execute(
                "INSERT INTO turns VALUES (?, '41', 'executed', NULL)", (conversation,),
            )
        self.config["conversation_namespace"] = "wechat-aimee"
        self.config["contact_usernames"] = [self.contact.username, "second-id"]
        migrated = PrivateChatAutoReplyPlugin(self.bot)
        with patch("private_chat_auto_reply.request_reply", return_value={"reply": "new"}) as generate:
            await migrated.handle_message(self.context(41))
            generate.assert_not_awaited()
            await migrated.handle_message(self.context(42))
            generate.assert_awaited_once()
            self.assertEqual(generate.await_args.args[1]["conversation"], conversation)
        self.assertEqual([action.content for action in self.actions], ["new"])

    async def test_multiple_contacts_share_backend_with_isolated_conversations_and_dedup(self):
        second = SimpleNamespace(username="second-id", display_name="Second", is_chatroom=False)
        self.contacts[second.username] = second
        self.config["contact_usernames"] = [self.contact.username, second.username]
        self.plugin.reload_plugin_config()
        histories = {}

        async def generate(config, turn):
            self.assertIs(config, self.plugin.plugin_config)
            messages = histories.setdefault(turn["conversation"], [])
            messages.append(turn["text"])
            return {"reply": " / ".join(messages)}

        with patch("private_chat_auto_reply.request_reply", side_effect=generate) as request:
            await self.plugin.handle_message(self.context(51, to_text=lambda: "first"))
            await self.plugin.handle_message(self.context(51, contact=second, to_text=lambda: "second"))
            await self.plugin.handle_message(self.context(52, to_text=lambda: "follow-up"))
            await PrivateChatAutoReplyPlugin(self.bot).handle_message(self.context(51, contact=second))
            self.assertEqual(request.await_count, 3)

        self.assertEqual(
            [(action.target_username, action.content) for action in self.actions],
            [(self.contact.username, "first"), (second.username, "second"),
             (self.contact.username, "first / follow-up")],
        )
        self.assertEqual(len(histories), 2)

    async def test_contact_allowlist_replaces_legacy_contact_when_provided(self):
        self.config["contact_usernames"] = []
        self.plugin.reload_plugin_config()
        with patch("private_chat_auto_reply.request_reply") as generate:
            await self.plugin.handle_message(self.context())
        generate.assert_not_awaited()
        self.assertEqual(self.actions, [])

    async def test_slow_contact_does_not_block_another_contacts_reply(self):
        second = SimpleNamespace(username="second-id", display_name="Second", is_chatroom=False)
        self.contacts[second.username] = second
        self.config["contact_usernames"] = [self.contact.username, second.username]
        self.plugin.reload_plugin_config()
        started = asyncio.Event()
        release = asyncio.Event()

        async def generate(config, turn):
            if turn["text"] == "test 61":
                started.set()
                await release.wait()
            return {"reply": turn["text"]}

        with patch("private_chat_auto_reply.request_reply", side_effect=generate):
            pending = asyncio.create_task(self.plugin.handle_message(self.context(61)))
            try:
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.wait_for(self.plugin.handle_message(self.context(62, contact=second)), 1)
                self.assertEqual([action.target_username for action in self.actions], [second.username])
            finally:
                release.set()
                await pending

    async def test_ssh_uses_configured_identity_and_checks_the_host(self):
        self.config.update(ssh_user="custom-bridge", ssh_directory=self.directory)
        self.plugin.reload_plugin_config()
        process = SimpleNamespace(
            communicate=AsyncMock(return_value=(b'{"ok":true,"reply":"hello"}\n', None)),
            returncode=0,
        )
        with patch("private_chat_auto_reply.asyncio.create_subprocess_exec", return_value=process) as start:
            response = await request_reply(self.plugin.plugin_config, {"op": "generate"})
        self.assertEqual(response["reply"], "hello")
        arguments = start.await_args.args
        self.assertEqual(arguments[-2:], ("--", "custom-bridge@example.invalid"))
        self.assertIn(str(self.directory / "id_ed25519"), arguments)
        self.assertIn(f"UserKnownHostsFile={self.directory / 'known_hosts'}", arguments)
        self.assertIn("StrictHostKeyChecking=yes", arguments)

    async def test_invite_admits_once_and_isolates_two_contacts(self):
        first = SimpleNamespace(username="invite-one", display_name="One", is_chatroom=False)
        second = SimpleNamespace(username="invite-two", display_name="Two", is_chatroom=False)
        self.contacts.update({first.username: first, second.username: second})
        self.bot.user_info = SimpleNamespace(account="test-account")
        self.config.update(invitations_enabled=True, ssh_directory=self.directory)
        key = bytes(range(32))
        (self.directory / CREDENTIAL_NAME).write_text(key.hex(), encoding="ascii")
        plugin = PrivateChatAutoReplyPlugin(self.bot)
        histories = {}

        def send(actions):
            action = actions[0]
            if action.target_username == first.username and action.content == CONFIRMATION:
                self.actions.append(action)
                action.result_queue.put({"ok": False, "send_attempted": False})
            else:
                self.handle_actions(actions)

        self.bot.processor_service.add_rpa_actions = send

        async def eros(config, payload, **kwargs):
            self.assertTrue(verify(key, payload))
            if payload["op"] == PROBE_OPERATION:
                return {"ok": True}
            self.assertNotIn("WXJOIN-", payload["text"])
            turns = histories.setdefault(payload["conversation"], [])
            turns.append(payload["text"])
            return {"reply": " / ".join(turns)}

        with patch("private_chat_auto_reply.request_reply", side_effect=eros):
            invite = await plugin.issue_invitation()
            for message in (
                self.context(70, contact=first, to_text=lambda: invite["code"], room=object()),
                self.context(71, contact=first, to_text=lambda: invite["code"], is_self=True),
                self.context(72, to_text=lambda: invite["code"]),
            ):
                await plugin.handle_message(message)
            self.assertEqual(plugin.invitation_state()["invitations"][0]["status"], "pending")
            await plugin.handle_message(self.context(73, contact=first, to_text=lambda: invite["code"]))
            self.assertEqual(plugin.invitation_state()["contacts"][0]["confirmation_status"], "not_sent")
            await plugin.handle_message(self.context(74, contact=second, to_text=lambda: invite["code"]))
            second_invite = await plugin.issue_invitation()
            await plugin.handle_message(self.context(75, contact=second, to_text=lambda: second_invite["code"]))
            await plugin.handle_message(self.context(80, contact=first, to_text=lambda: "first memory"))
            await plugin.handle_message(self.context(80, contact=second, to_text=lambda: "second memory"))
            self.assertTrue(plugin.remove_invited_contact(invite["id"]))
            await plugin.handle_message(self.context(81, contact=first))

        self.assertEqual([(action.target_username, action.content) for action in self.actions], [
            (first.username, CONFIRMATION), (second.username, CONFIRMATION),
            (first.username, "first memory"), (second.username, "second memory"),
        ])
        self.assertEqual(len(histories), 2)
        state = plugin.invitation_state()
        self.assertEqual([row["username"] for row in state["contacts"]], [second.username])
        self.assertEqual(state["contacts"][0]["confirmation_status"], "verified")
        self.assertNotIn(invite["code"], repr(state))
