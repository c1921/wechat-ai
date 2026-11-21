import importlib.util
import sys
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from wechat_invite_admission import PROBE_OPERATION, sign


class StubErosGenerator:
    def __init__(self, genome_id, jwt_secret, caller_secret):
        self.genome_id = genome_id
        self.generate = AsyncMock(return_value="reply")


def load_bridge():
    path = Path(__file__).resolve().parents[1] / "eros" / "bridge.py"
    spec = importlib.util.spec_from_file_location("wechat_eros_bridge", path)
    bridge = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "eros_provider": SimpleNamespace(ErosGenerator=StubErosGenerator),
        "llm_provider": SimpleNamespace(ProviderError=RuntimeError),
    }):
        spec.loader.exec_module(bridge)
    return bridge


class ErosBridgeTest(unittest.TestCase):
    first = "a" * 64
    second = "b" * 64

    def setUp(self):
        self.bridge = load_bridge()
        self.namespace = uuid.UUID("5d7d66c7-b732-48a7-9dbf-5e5d35559637")
        self.config = {
            "genome_id": "shared-persona",
            "conversation_namespace": str(self.namespace),
            "allowed_conversation": self.first,
        }
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        credentials = Path(directory.name)
        (credentials / "eros_jwt").write_text("test", encoding="utf-8")
        (credentials / "eros_caller").write_text("test", encoding="utf-8")
        self.generator = self.bridge.WeChatGenerator(self.config, credentials)

    def request(self, conversation):
        return self.bridge.handle(
            {"op": "generate", "conversation": conversation, "message_id": "1", "text": "hello"},
            self.config, self.generator,
        )

    def test_legacy_allowlist_and_identity_still_work(self):
        response = self.request(self.first)
        self.assertTrue(response["ok"])
        self.assertEqual(response["reply"], "reply")
        self.assertEqual(response["replies"], ["reply"])
        self.assertEqual(self.generator.identity(self.first), uuid.uuid5(self.namespace, "wechat-aimee:" + self.first))
        with self.assertRaisesRegex(ValueError, "invalid_turn"):
            self.request(self.second)
        self.generator.generate.assert_awaited_once()

    def test_multiple_conversations_share_genome_and_keep_distinct_eros_users(self):
        self.config["allowed_conversations"] = [self.first, self.second]
        self.assertTrue(self.request(self.first)["ok"])
        self.assertTrue(self.request(self.second)["ok"])
        self.assertEqual(self.generator.genome_id, "shared-persona")
        self.assertNotEqual(self.generator.identity(self.first), self.generator.identity(self.second))
        with self.assertRaisesRegex(ValueError, "invalid_turn"):
            self.request("c" * 64)
        self.assertEqual(self.generator.generate.await_count, 2)

    def test_explicit_list_controls_access_and_invalid_lists_fail_closed(self):
        self.config["allowed_conversations"] = [self.second]
        with self.assertRaisesRegex(ValueError, "invalid_turn"):
            self.request(self.first)
        for invalid in (self.first, [self.first, "malformed"], None):
            with self.subTest(invalid=invalid):
                self.config["allowed_conversations"] = invalid
                with self.assertRaisesRegex(ValueError, "invalid_conversation_allowlist"):
                    self.request(self.first)
        self.generator.generate.assert_not_awaited()

    def test_signed_admission_binds_the_turn_and_requires_the_key(self):
        key = bytes(range(32))
        request = {"op": "generate", "conversation": self.second, "message_id": "7", "text": "hello"}
        with patch("wechat_invite_admission.time.time", return_value=1000):
            request["invite_admission"] = sign(key, request)
            self.assertTrue(self.bridge.handle(request, self.config, self.generator, key)["ok"])
            for altered in ({"text": "other"}, {"message_id": "8"}, {"conversation": "c" * 64}):
                with self.assertRaisesRegex(ValueError, "invalid_turn"):
                    self.bridge.handle({**request, **altered}, self.config, self.generator, key)
            with self.assertRaisesRegex(ValueError, "invalid_turn"):
                self.bridge.handle(request, self.config, self.generator)
            probe = {**request, "op": PROBE_OPERATION, "text": ""}
            probe["invite_admission"] = sign(key, probe)
            self.assertTrue(self.bridge.handle(probe, self.config, self.generator, key)["ok"])
        with patch("wechat_invite_admission.time.time", return_value=1120):
            with self.assertRaisesRegex(ValueError, "invalid_turn"):
                self.bridge.handle(request, self.config, self.generator, key)
        self.generator.generate.assert_awaited_once()

    def test_multi_bubble_replies_return_segments_and_joined_fallback(self):
        self.generator.generate_replies = AsyncMock(return_value=["第一条", "第二条", "第三条"])
        response = self.request(self.first)
        self.assertEqual(response, {
            "ok": True,
            "reply": "第一条\n\n第二条\n\n第三条",
            "replies": ["第一条", "第二条", "第三条"],
        })
        self.generator.generate_replies.assert_awaited_once()

    def test_multi_bubble_replies_have_no_segment_cap(self):
        bubbles = [f"第{i}条" for i in range(12)]
        self.generator.generate_replies = AsyncMock(return_value=bubbles)
        self.assertEqual(self.request(self.first)["replies"], bubbles)

    def test_invalid_multi_bubble_replies_fail_closed(self):
        for invalid in ([], ["ok", ""], ["a", 1], "not-a-list"):
            with self.subTest(invalid=invalid):
                self.generator.generate_replies = AsyncMock(return_value=invalid)
                with self.assertRaisesRegex(RuntimeError, "invalid_reply"):
                    self.request(self.first)
