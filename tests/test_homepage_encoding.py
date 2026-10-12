import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import main


class HomepageEncodingTests(unittest.TestCase):
    def test_homepage_reads_utf8_html_on_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            static_dir = Path(tmp) / "static"
            static_dir.mkdir()
            (static_dir / "index.html").write_text(
                "<html><body>Paper trading — café</body></html>",
                encoding="utf-8",
            )
            with patch.object(main, "ROOT", Path(tmp)):
                html = main.home()

        self.assertIn("Paper trading — café", html)


if __name__ == "__main__":
    unittest.main()
