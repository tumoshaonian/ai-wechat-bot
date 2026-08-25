import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wechat_agent.domain import AgentReply
from wechat_agent.file_requests import DesktopFileRequestResolver


class DesktopFileRequestResolverTests(unittest.TestCase):
    def test_resolves_natural_language_document_request_and_prefers_docx(self) -> None:
        with TemporaryDirectory() as temporary:
            desktop = Path(temporary)
            docx = desktop / "LeapMind暑假开发计划.docx"
            docx.write_bytes(b"docx")
            (desktop / "LeapMind暑假开发计划.pdf").write_bytes(b"pdf")
            resolver = DesktopFileRequestResolver(desktop)

            reply = resolver.resolve("给我发送电脑桌面的LeapMind暑假开发计划文档")

            self.assertEqual(
                AgentReply("已在电脑桌面找到文件，准备发送：LeapMind暑假开发计划.docx", (docx.resolve(),)),
                reply,
            )

    def test_resolves_reordered_send_phrase(self) -> None:
        with TemporaryDirectory() as temporary:
            desktop = Path(temporary)
            report = desktop / "周报.xlsx"
            report.write_bytes(b"xlsx")
            resolver = DesktopFileRequestResolver(desktop)

            reply = resolver.resolve("请把桌面上的周报.xlsx发给我")

            self.assertIsInstance(reply, AgentReply)
            self.assertEqual((report.resolve(),), reply.files)  # type: ignore[union-attr]

    def test_unrelated_message_is_left_for_unified_agent(self) -> None:
        resolver = DesktopFileRequestResolver(Path("D:/does-not-matter"))

        self.assertIsNone(resolver.resolve("什么是计算机网络"))
        self.assertIsNone(resolver.resolve("帮我修改桌面的文档"))

    def test_reports_missing_desktop_file_without_calling_model(self) -> None:
        with TemporaryDirectory() as temporary:
            resolver = DesktopFileRequestResolver(Path(temporary))

            reply = resolver.resolve("给我发送电脑桌面的不存在文档")

            self.assertIn("没有在电脑桌面找到", str(reply))


if __name__ == "__main__":
    unittest.main()
