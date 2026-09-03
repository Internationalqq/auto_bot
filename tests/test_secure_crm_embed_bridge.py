from __future__ import annotations

import unittest

from autobot.paths import REPO_ROOT


class SecureCrmEmbedBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_ui = (REPO_ROOT / "autobot" / "web_ui.py").read_text(encoding="utf-8")
        cls.bridge = (REPO_ROOT / "autobot" / "static" / "embed_bridge.js").read_text(encoding="utf-8")

    def test_bridge_uses_correlated_messages_and_exact_parent_origin(self) -> None:
        for message_type in (
            "autobot:crm-projects-request",
            "autobot:crm-projects-result",
            "autobot:crm-estimate-import",
            "autobot:crm-estimate-import-result",
        ):
            self.assertIn(message_type, self.bridge)
        self.assertIn("requestId", self.bridge)
        self.assertIn("event.source !== window.parent", self.bridge)
        self.assertIn("event.origin !== trustedParentOrigin", self.bridge)
        self.assertIn("window.parent.postMessage(message, trustedParentOrigin)", self.bridge)
        self.assertIn("available: Boolean(trustedParentOrigin && !originMismatch)", self.bridge)
        self.assertIn("if (data.ok !== true)", self.bridge)

    def test_sensitive_bridge_never_falls_back_to_wildcard_target(self) -> None:
        trusted_sender = self.bridge.split("function postTrusted", 1)[1].split("function newRequestId", 1)[0]
        self.assertNotIn('"*"', trusted_sender)
        self.assertIn("if (!trustedParentOrigin)", trusted_sender)
        self.assertNotIn("token", self.bridge.casefold())
        self.assertNotIn("cookie", self.bridge.casefold())

    def test_embedded_estimate_flow_reads_local_payload_then_asks_parent_to_write(self) -> None:
        embedded_branch = self.web_ui.split("if (estimateCrmEmbedded) {", 4)[4].split(
            'const resp = await fetch("/api/estimates/{{ meta.id }}/export-to-crm"', 1
        )[0]
        self.assertIn('/crm-import-payload"', embedded_branch)
        self.assertIn("X-AutoBot-Estimate-Capability", embedded_branch)
        self.assertIn("estimateCrmBridge.importEstimate", embedded_branch)
        self.assertNotIn("PMBI_CRM_PASSWORD", embedded_branch)
        self.assertNotIn("/api/crm/projects", embedded_branch)

    def test_payload_api_is_read_only_capability_scoped_and_not_cached(self) -> None:
        route = self.web_ui.split('@app.route("/api/estimates/<estimate_id>/crm-import-payload")', 1)[1].split(
            '@app.route("/api/crm/projects")', 1
        )[0]
        self.assertNotIn('methods=["POST"]', route)
        self.assertIn("X-AutoBot-Estimate-Capability", route)
        self.assertIn("_verify_estimate_import_capability", route)
        self.assertIn('fetch_site != "same-origin"', route)
        self.assertIn('response.headers["Cache-Control"] = "no-store"', route)
        for field in ('"items"', '"source"', '"label"', '"reference"'):
            self.assertIn(field, self.web_ui.split("def _build_estimate_crm_import_payload", 1)[1].split(
                "def _build_crm_project_payload", 1
            )[0])

    def test_configured_parent_origin_also_limits_frame_ancestors(self) -> None:
        self.assertIn("PMBI_CRM_PARENT_ORIGIN", self.web_ui)
        self.assertIn("frame-ancestors 'self'", self.web_ui)
        self.assertIn('if parent_origin:', self.web_ui)
        self.assertIn('<meta name="autobot-parent-origin" content="{{ crm_parent_origin|e }}" />', self.web_ui)

    def test_standalone_service_account_ui_requires_explicit_opt_in(self) -> None:
        self.assertIn("PMBI_ALLOW_LEGACY_BROWSER_CRM_EXPORT", self.web_ui)
        self.assertIn("if (!estimateCrmLegacyAllowed)", self.web_ui)
        self.assertIn("Откройте AutoBot внутри PM.bi", self.web_ui)


if __name__ == "__main__":
    unittest.main()
