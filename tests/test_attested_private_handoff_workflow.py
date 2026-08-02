from __future__ import annotations
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/collect-r11-public-evidence.yml"
PUBLISHER = ROOT / "src/publish_prepared_private_handoff.py"

class AttestedPrivateHandoffWorkflowTests(unittest.TestCase):
    def test_attestation_precedes_publication(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        prepare = "python src/publish_prepared_private_handoff.py prepare"
        attest = "uses: actions/attest@59d89421af93a897026c735860bf21b6eb4f7b26"
        publish = "python src/publish_prepared_private_handoff.py publish"
        for value in ("id-token: write", "attestations: write", "artifact-metadata: write", prepare, attest, publish):
            self.assertIn(value, text)
        self.assertLess(text.index(prepare), text.index(attest))
        self.assertLess(text.index(attest), text.index(publish))
        self.assertNotIn("python src/publish_private_handoff.py \\", text)

    def test_digest_and_attestation_are_required(self) -> None:
        source = PUBLISHER.read_text(encoding="utf-8")
        self.assertIn("prepared archive digest changed after attestation", source)
        self.assertIn("GitHub attestation ID is missing", source)
        self.assertIn("GitHub attestation URL is invalid", source)
        self.assertIn('"attestation_verified_before_publication": True', source)

if __name__ == "__main__":
    unittest.main()
