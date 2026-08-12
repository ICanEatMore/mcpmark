from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from src.mcp_services.playwright.playwright_state_manager import (
    PlaywrightStateManager,
)


class PlaywrightNativeConfigTests(TestCase):
    @patch(
        "src.mcp_services.playwright.playwright_state_manager.find_playwright_browser_executable"
    )
    def test_bundled_browser_path_is_exposed_to_mcp(self, resolve) -> None:
        resolve.return_value = Path("/cache/playwright/chrome")

        manager = PlaywrightStateManager(viewport_width=1440, viewport_height=900)
        config = manager.get_service_config_for_agent()

        self.assertEqual(config["browser_executable_path"], "/cache/playwright/chrome")
        self.assertEqual(config["viewport_width"], 1440)
        self.assertEqual(config["viewport_height"], 900)
