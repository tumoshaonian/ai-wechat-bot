"""Enterprise WeChat payload conversion tests."""

import unittest

from wechat_agent.adapters.wecom_payload import InvalidWeComPayload, parse_text_message


class WeComPayloadTests(unittest.TestCase):
    def test_parses_single_chat(self) -> None:
        parsed = parse_text_message(
            {
                "body": {
                    "msgid": "msg-1",
                    "chattype": "single",
                    "from": {"userid": "owner"},
                    "text": {"content": "  hello  "},
                }
            }
        )

        self.assertEqual("owner", parsed.chat_id)
        self.assertEqual("hello", parsed.content)
        self.assertEqual("wecom:single:owner", parsed.session_id)

    def test_parses_group_chat(self) -> None:
        parsed = parse_text_message(
            {
                "body": {
                    "msgid": "msg-2",
                    "chatid": "room-1",
                    "chattype": "group",
                    "from": {"userid": "owner"},
                    "text": {"content": "status"},
                }
            }
        )

        self.assertTrue(parsed.is_group)
        self.assertEqual("wecom:group:room-1", parsed.session_id)

    def test_rejects_missing_content(self) -> None:
        with self.assertRaises(InvalidWeComPayload):
            parse_text_message(
                {
                    "body": {
                        "from": {"userid": "owner"},
                        "text": {"content": ""},
                    }
                }
            )
