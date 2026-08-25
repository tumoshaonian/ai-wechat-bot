import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wechat_agent.adapters.wecom_channel import WeComStreamResponder


class FakeClient:
    def __init__(self) -> None:
        self.uploads: list[tuple[bytes, str, str]] = []
        self.sent: list[tuple[str, str, str]] = []

    async def reply_stream(self, *_args) -> None:
        return None

    async def upload_media(self, data: bytes, *, type: str, filename: str):
        self.uploads.append((data, type, filename))
        return {"media_id": "media-1"}

    async def send_media_message(self, chat_id: str, media_type: str, media_id: str):
        self.sent.append((chat_id, media_type, media_id))


class WeComStreamResponderTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploads_and_sends_local_file(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.docx"
            path.write_bytes(b"document")
            client = FakeClient()
            responder = WeComStreamResponder(client, {}, "stream-1", "owner")

            await responder.send_file(path)

            self.assertEqual([(b"document", "file", "report.docx")], client.uploads)
            self.assertEqual([("owner", "file", "media-1")], client.sent)

    async def test_rejects_empty_file_before_upload(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "empty.txt"
            path.touch()
            client = FakeClient()
            responder = WeComStreamResponder(client, {}, "stream-1", "owner")

            with self.assertRaisesRegex(ValueError, "empty file"):
                await responder.send_file(path)

            self.assertEqual([], client.uploads)


if __name__ == "__main__":
    unittest.main()
