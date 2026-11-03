import copy
import unittest

from bridge.cleaning import clean_request
from bridge.config import validate


class CleaningTests(unittest.TestCase):
    def test_preserves_tool_links_files_and_original_request(self):
        payload = {"model": "example", "input": [
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
            {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "run", "arguments": "{}"},
            {"type": "function_call_output", "id": "out_1", "call_id": "call_1", "output": "done"},
            {"role": "user", "content": [{"type": "input_file", "file_id": "file_1"},
                                         {"type": "input_text", "text": "你好", "id": "nested_kept"}]}],
            "tools": [{"type": "function", "name": "run", "parameters": {"type": "object"}}],
            "reasoning": {"effort": "high"}}
        original = copy.deepcopy(payload)
        cleaned, report = clean_request(payload)
        self.assertEqual(payload, original)
        self.assertEqual(report.reasoning_items, 1)
        self.assertEqual(report.item_ids, 2)
        self.assertEqual(cleaned["input"][0]["call_id"], "call_1")
        self.assertEqual(cleaned["input"][1]["call_id"], "call_1")
        self.assertEqual(cleaned["input"][2]["content"], original["input"][3]["content"])
        self.assertEqual(cleaned["tools"], payload["tools"])
        self.assertEqual(cleaned["reasoning"], payload["reasoning"])

    def test_remote_history_is_rejected_before_forwarding(self):
        for data in [{"conversation": "conv_1"}, {"previous_response_id": "resp_1"},
                     {"input": [{"type": "item_reference", "id": "item_1"}]},
                     {"input": [{"type": "compaction", "encrypted_content": "opaque"}]}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                clean_request(data)

    def test_full_history_opt_in_removes_response_link(self):
        cleaned, report = clean_request({"input": "complete history", "previous_response_id": "resp_1"},
                                       allow_response_link_removal=True)
        self.assertNotIn("previous_response_id", cleaned)
        self.assertEqual(report.response_links, 1)

    def test_invalid_inputs_are_rejected(self):
        for data in [[], None, {"input": ["text"]}, {"input": 42}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                clean_request(data)

    def test_config_rejects_credential_storage_self_proxy_and_invalid_values(self):
        for values in [{"api_key": "example"}, {"port": True}, {"timeout_seconds": float("inf")},
                       {"upstream_url": "https://user:pass@example.com/v1"},
                       {"upstream_url": "http://127.0.0.1:8787/v1"},
                       {"upstream_url": "https://example.com/v1?key=example"},
                       {"upstream_url": "https://example.com:0/v1"},
                       {"allow_response_link_removal": 1}]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                validate(values)


if __name__ == "__main__":
    unittest.main()
