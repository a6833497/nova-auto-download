import io
import json
from pathlib import Path
import tempfile
import unittest

from linky_export import (ExportValidationError, export_request_body,
    request_export_url, validate_streamer_export)


HEADER = "date,sid,Total income\n"


class LinkyExportTest(unittest.TestCase):
    def test_official_request_body_uses_numeric_contract(self):
        self.assertEqual({"begin": 20260810, "end": 20260810,
            "req_type": 0, "sid": None}, export_request_body("20260810"))

    def test_authenticated_request_sends_official_body_without_secret_in_result(self):
        with tempfile.TemporaryDirectory() as root:
            tokens = Path(root) / "tokens.json"
            tokens.write_text(json.dumps({"guilds": {"G": {
                "oauth_token": "token", "oauth_token_secret": "secret"}}}))
            seen = {}
            def opener(request, timeout):
                seen["request"] = request
                seen["timeout"] = timeout
                return io.BytesIO(b'{"data":{"url":"https://bucket.aliyuncs.com/export.csv"}}')
            value = request_export_url("G", "20260810", tokens_path=str(tokens),
                urlopen=opener)
            self.assertEqual("https://bucket.aliyuncs.com/export.csv", value)
            self.assertEqual({"begin": 20260810, "end": 20260810,
                "req_type": 0, "sid": None}, json.loads(seen["request"].data))
            self.assertEqual("POST", seen["request"].method)

    def test_untrusted_download_host_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            tokens = Path(root) / "tokens.json"
            tokens.write_text(json.dumps({"guilds": {"G": {
                "oauth_token": "token", "oauth_token_secret": "secret"}}}))
            def opener(_request, timeout):
                self.assertEqual(30, timeout)
                return io.BytesIO(b'{"url":"https://127.0.0.1/export.csv"}')
            with self.assertRaises(ExportValidationError) as caught:
                request_export_url("G", "20260810", tokens_path=str(tokens),
                    urlopen=opener)
            self.assertEqual("UNTRUSTED_DOWNLOAD_URL", caught.exception.code)

    def test_complete_file_returns_publishable_rows(self):
        raw = (HEADER + "2026/08/10,1,5\n2026/08/10,2,7\nTotal,,12\n").encode()
        rows, evidence = validate_streamer_export(raw, business_date="20260810",
            expected_row_count=2, expected_amount=12)
        self.assertEqual(2, len(rows))
        self.assertEqual("PASSED", evidence.status)
        self.assertEqual("12", evidence.detail_amount)

    def test_upstream_cap_is_rejected_before_rows_are_returned(self):
        raw = (HEADER + "2026/08/10,1,5\n2026/08/10,2,6\nTotal,,12\n").encode()
        with self.assertRaises(ExportValidationError) as caught:
            validate_streamer_export(raw, business_date="20260810",
                expected_row_count=3, expected_amount=12)
        self.assertEqual("TRUNCATED_OR_EXTRA_ROWS", caught.exception.code)
        self.assertEqual(2, caught.exception.evidence.detail_row_count)
        self.assertEqual("REJECTED", caught.exception.evidence.status)

    def test_duplicate_sid_is_rejected(self):
        raw = (HEADER + "2026/08/10,1,5\n2026/08/10,1,7\nTotal,,12\n").encode()
        with self.assertRaises(ExportValidationError) as caught:
            validate_streamer_export(raw, business_date="20260810",
                expected_row_count=2, expected_amount=12)
        self.assertEqual("DUPLICATE_SID", caught.exception.code)

    def test_detail_and_summary_mismatches_are_distinct(self):
        detail_bad = (HEADER + "2026/08/10,1,5\nTotal,,6\n").encode()
        with self.assertRaises(ExportValidationError) as caught:
            validate_streamer_export(detail_bad, business_date="20260810",
                expected_row_count=1, expected_amount=6)
        self.assertEqual("DETAIL_MISMATCH", caught.exception.code)
        summary_bad = (HEADER + "2026/08/10,1,5\nTotal,,5\n").encode()
        with self.assertRaises(ExportValidationError) as caught:
            validate_streamer_export(summary_bad, business_date="20260810",
                expected_row_count=1, expected_amount=6)
        self.assertEqual("SUMMARY_MISMATCH", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
