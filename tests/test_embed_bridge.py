from __future__ import annotations

import unittest
from pathlib import Path

from autobot.paths import REPO_ROOT


class EmbedBridgeTests(unittest.TestCase):
    def test_embedded_pages_report_scroll_state_to_crm(self) -> None:
        script = (REPO_ROOT / "autobot" / "static" / "embed_bridge.js").read_text(encoding="utf-8")

        self.assertIn('type: "autobot:scroll"', script)
        self.assertIn("scrollTop > 24", script)
        self.assertIn('window.addEventListener("scroll"', script)

        for template_name in ("tenders.html", "tender_detail.html", "source_file_preview.html"):
            with self.subTest(template=template_name):
                template = (REPO_ROOT / "autobot" / "templates" / template_name).read_text(encoding="utf-8")
                self.assertIn("embed_bridge.js", template)


if __name__ == "__main__":
    unittest.main()
