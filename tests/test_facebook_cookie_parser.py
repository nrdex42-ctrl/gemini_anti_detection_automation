import json
import unittest

from facebook_cookie_parser import parse_account_cookie_payload, parse_cookie_payload


class FacebookCookieParserTests(unittest.TestCase):
    def test_parse_raw_cookie_header_extracts_c_user(self):
        parsed = parse_account_cookie_payload("datr=abc; c_user=123; xs=session", "auto")

        self.assertEqual(parsed.account_id, "123")
        self.assertIn("c_user=123", parsed.cookie_header)
        self.assertIn("xs=session", parsed.cookie_header)

    def test_parse_cookie_json_export_object(self):
        payload = json.dumps(
            {
                "cookies": [
                    {"name": "datr", "value": "abc"},
                    {"name": "c_user", "value": "456", "domain": ".facebook.com"},
                    {"name": "xs", "value": "session"},
                ]
            }
        )

        parsed = parse_account_cookie_payload(payload)

        self.assertEqual(parsed.account_id, "456")
        self.assertEqual(parsed.cookie_header, "datr=abc; c_user=456; xs=session")

    def test_parse_cookie_json_array_preserves_optional_fields(self):
        cookies = parse_cookie_payload(
            json.dumps(
                [
                    {
                        "name": "c_user",
                        "value": "789",
                        "expirationDate": 1780000000,
                        "httpOnly": True,
                        "sameSite": "None",
                    }
                ]
            )
        )

        self.assertEqual(cookies[0]["name"], "c_user")
        self.assertEqual(cookies[0]["expirationDate"], 1780000000)
        self.assertIs(cookies[0]["httpOnly"], True)
        self.assertEqual(cookies[0]["sameSite"], "None")


if __name__ == "__main__":
    unittest.main()
