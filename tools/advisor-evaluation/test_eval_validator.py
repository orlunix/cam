"""Tests for evaluation integrity, NOT tests of Advisor intelligence."""
import copy
import json
from pathlib import Path
import unittest
from eval_advisor import parse_reply, validate


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.case = json.loads(Path(__file__).with_name("cases.json").read_text())[0]
        self.reply = {"test_id": "n", "decision": "intervene", "evidence_ids": ["e1"],
                      "reason": "decode incomplete", "next_action": "Implement decode and run acceptance tests"}

    def test_valid_contract(self):
        self.assertEqual(validate(self.reply, self.case), [])

    def test_wrong_decision_rejected(self):
        self.reply["decision"] = "complete"
        self.assertTrue(validate(self.reply, self.case))

    def test_invented_evidence_rejected(self):
        self.reply["evidence_ids"] = ["not-in-input"]
        self.assertTrue(validate(self.reply, self.case))

    def test_empty_intervention_rejected(self):
        self.reply["next_action"] = ""
        self.assertTrue(validate(self.reply, self.case))

    def test_noisy_continue_rejected(self):
        case = copy.deepcopy(self.case)
        case["expected"] = self.reply["decision"] = "continue"
        self.assertTrue(validate(self.reply, case))

    def test_missing_reply_is_not_success(self):
        with self.assertRaises(ValueError):
            parse_reply("MSG_ID=deadbeef", "n")

    def test_wrong_thread_is_not_success(self):
        with self.assertRaises(ValueError):
            parse_reply(json.dumps(self.reply), "different-test")

    def test_duplicate_replies_rejected(self):
        with self.assertRaises(ValueError):
            parse_reply(json.dumps(self.reply) * 2, "n")

    def test_json_with_nested_content_parses(self):
        self.reply["extra"] = {"text": "nested"}
        self.assertEqual(parse_reply("reply: " + json.dumps(self.reply), "n"), self.reply)


if __name__ == "__main__":
    unittest.main(verbosity=2)
