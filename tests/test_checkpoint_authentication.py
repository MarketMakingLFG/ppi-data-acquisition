from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import private_checkpoint_store as store  # noqa: E402


class CheckpointAuthenticationTests(unittest.TestCase):
    def test_auth_tag_is_deterministic_and_domain_separated(self) -> None:
        raw = b'{"status":"r11_private_checkpoint_material"}\n'
        first = store.checkpoint_auth_tag(raw, "private-token-a")
        second = store.checkpoint_auth_tag(raw, "private-token-a")
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, store.checkpoint_auth_tag(b"x" + raw, "private-token-a"))

    def test_wrong_credential_cannot_authenticate_checkpoint(self) -> None:
        raw = b'{"workflow_run_id":123,"head_sha":"abc"}\n'
        expected = store.checkpoint_auth_tag(raw, "private-token-a")
        forged = store.checkpoint_auth_tag(raw, "private-token-b")
        self.assertNotEqual(expected, forged)

    def test_asset_name_requires_digest_and_authentication_tag(self) -> None:
        digest = "a" * 64
        auth_tag = "b" * 64
        valid = f"ppi-r11-checkpoint-123-2-{digest}-{auth_tag}.json"
        self.assertIsNotNone(store.ASSET_RE.fullmatch(valid))
        self.assertIsNone(store.ASSET_RE.fullmatch(f"ppi-r11-checkpoint-123-2-{digest}.json"))
        self.assertIsNone(store.ASSET_RE.fullmatch(f"ppi-r11-checkpoint-123-2-{digest}-{'c' * 63}.json"))


if __name__ == "__main__":
    unittest.main()
