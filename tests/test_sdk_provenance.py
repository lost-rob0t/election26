"""Packaging receipt fixtures; no real dependencies or fabricated install logs."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from election26.config import PolicyError
from election26.normalize import SDK_COMMIT, verify_sdk_source


RECEIPT = {
    "format": "election26-nix-source-v1",
    "source_url": "github:lost-rob0t/starintel-doc",
    "source_rev": SDK_COMMIT,
    # Synthetic metadata fixture only, never a source lock or build hash.
    "source_nar_hash": "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
}


def distribution(direct=None, receipt=None, location="/nix/store/synthetic-fixture/lib/python3.12/site-packages"):
    data = {"direct_url.json": direct, "election26_nix_source.json": receipt}
    return SimpleNamespace(read_text=data.get, locate_file=lambda _: Path(location))


class SDKProvenanceTests(unittest.TestCase):
    def test_exact_pip_vcs_commit_still_accepted(self):
        verify_sdk_source(distribution(direct=json.dumps({"vcs_info": {"commit_id": SDK_COMMIT}}), location="/private/venv/lib"))

    def test_locked_nix_receipt_has_a_separate_path(self):
        verify_sdk_source(distribution(receipt=json.dumps(RECEIPT)))

    def test_wrong_pip_pin_cannot_fall_back_to_nix_receipt(self):
        with self.assertRaises(PolicyError):
            verify_sdk_source(distribution(direct=json.dumps({"vcs_info": {"commit_id": "wrong"}}), receipt=json.dumps(RECEIPT)))

    def test_nix_receipt_rejected_outside_store(self):
        for location in ("/private/venv/lib", "/nix/store-not-really/fixture"):
            with self.subTest(location=location), self.assertRaises(PolicyError):
                verify_sdk_source(distribution(receipt=json.dumps(RECEIPT), location=location))

    def test_nix_pin_repository_format_and_hash_are_required(self):
        for key, value in (("source_rev", "wrong"), ("source_url", "github:other/repo"),
                           ("format", "unknown"), ("source_nar_hash", "not-a-hash"),
                           ("source_nar_hash", None)):
            with self.subTest(key=key, value=value), self.assertRaises(PolicyError):
                verify_sdk_source(distribution(receipt=json.dumps({**RECEIPT, key: value})))
        for receipt in ({key: value for key, value in RECEIPT.items() if key != "source_rev"},
                        {**RECEIPT, "unexpected": "value"}, {}):
            with self.assertRaises(PolicyError):
                verify_sdk_source(distribution(receipt=json.dumps(receipt)))

    def test_malformed_metadata_fails_closed(self):
        for value in ("broken", "[]", "null", '{"vcs_info":null}', '{}'):
            with self.subTest(value=value), self.assertRaises(PolicyError):
                verify_sdk_source(distribution(direct=value))
        for value in (None, "broken", "[]", "null"):
            with self.subTest(value=value), self.assertRaises(PolicyError):
                verify_sdk_source(distribution(receipt=value))
