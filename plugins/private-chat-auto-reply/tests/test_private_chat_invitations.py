import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from private_chat_invitations import Invitations, LIFETIME


class InvitationsTest(unittest.TestCase):
    def test_issue_revoke_expiry_and_atomic_single_use(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "turns.sqlite3"
            store = Invitations(path)
            with patch("private_chat_invitations.time.time", return_value=1000):
                expired = store.issue("account")
                revoked = store.issue("account")
                self.assertTrue(store.revoke("account", revoked["id"]))
                self.assertIsNone(store.redeem("account", "other", revoked["code"]))
            with patch("private_chat_invitations.time.time", return_value=1000 + LIFETIME):
                self.assertIsNone(store.redeem("account", "other", expired["code"]))
                active = store.issue("account")
            with patch("private_chat_invitations.time.time", return_value=1000 + LIFETIME + 1):
                self.assertIsNone(store.redeem("another-account", "other", active["code"]))
                with ThreadPoolExecutor(max_workers=2) as executor:
                    winners = list(executor.map(
                        lambda name: store.redeem("account", name, active["code"]), ("one", "two"),
                    ))
            self.assertCountEqual(winners, [active["id"], None])
            restarted = Invitations(path)
            state = restarted.snapshot("account")
            self.assertEqual(len(state["contacts"]), 1)
            self.assertEqual(state["contacts"][0]["confirmation_status"], "unconfirmed")
            self.assertTrue(restarted.remove("account", active["id"]))
            self.assertEqual(restarted.snapshot("account")["contacts"], [])
