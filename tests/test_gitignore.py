"""The secret-file rules in .gitignore must actually match.

Git has no trailing comments: `config/api_keys.json   # note` is ONE pattern that
includes the note, so it matches nothing and the file shows up as committable.
That is how API keys get published, so this is checked directly.
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MUST_BE_IGNORED = [
    "config/api_keys.json",
    "config/certs/dashboard.key",
    "config/whatsapp_web/session",
    "config/gmail_token.json",
    "config/gmail_work_token.json",
    "config/product_sales/drafts.json",
    "config/trading/journal.jsonl",
    "memory/long_term.json",
    "memory/mission_control.lock",
    ".env",
]


class GitignoreTests(unittest.TestCase):
    def test_no_rule_carries_a_trailing_comment(self):
        for number, line in enumerate((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#") and not line.startswith("#"):
                self.fail(f".gitignore line {number} is indented text, which git reads as a pattern")
            if not line.startswith("#"):
                self.assertIsNone(re.search(r"\S\s+#", line),
                                  f".gitignore line {number} has a trailing comment, so the rule matches nothing")

    @unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "needs a git checkout")
    def test_secret_paths_are_really_ignored(self):
        for path in MUST_BE_IGNORED:
            result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
            self.assertEqual(result.returncode, 0, f"{path} is NOT ignored and could be committed")


if __name__ == "__main__":
    unittest.main()
