import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"

# Words a visitor should never see. Checked against the text of the page and the strings in its scripts.
BANNED = ("gpu", "manifest", "bedrock", "lambda", "diffusion", "token", "instance", "s3", "aws", "dynamodb",
          "panorama", "bhk", "latency", "endpoint", "payload")


def visible_strings():
    """Every quoted or template string in the scripts plus the HTML text, with code-only lines removed."""
    texts = []
    html = (SITE / "index.html").read_text(encoding="utf-8")
    texts.append(re.sub(r"<(script|style)\b.*?</\1>|<[^>]+>", " ", html, flags=re.S))
    for path in sorted((SITE / "js").glob("*.js")):
        source = path.read_text(encoding="utf-8")
        source = re.sub(r"^\s*(import|export)\b.*$", "", source, flags=re.M)   # module plumbing
        source = re.sub(r"//.*$", "", source, flags=re.M)                       # comments
        for match in re.finditer(r"""(["'`])((?:\\.|(?!\1).)*)\1""", source, flags=re.S):
            text = match.group(2)
            if re.search(r"\s", text) and re.search(r"[A-Za-z]{3}", text):      # looks like words, not an identifier
                texts.append(re.sub(r"\$\{[^}]*\}|<[^>]+>", " ", text))
    return "\n".join(texts)


class SiteTests(unittest.TestCase):
    def test_every_file_the_page_loads_exists(self):
        html = (SITE / "index.html").read_text(encoding="utf-8")
        for ref in re.findall(r'(?:src|href)="([^"#:]+?)"', html):
            if ref.startswith(("http", "data:", "//")) or ref == "config.js":  # config.js is written at deploy time
                continue
            self.assertTrue((SITE / ref).exists(), ref)

    def test_scripts_parse(self):
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        for path in sorted((SITE / "js").glob("*.js")):
            result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{path.name}: {result.stderr}")

    def test_every_import_points_at_a_real_file_or_a_pinned_library(self):
        html = (SITE / "index.html").read_text(encoding="utf-8")
        for path in sorted((SITE / "js").glob("*.js")):
            for target in re.findall(r"from '([^']+)'", path.read_text(encoding="utf-8")):
                if target.startswith("."):
                    self.assertTrue((path.parent / target).exists(), f"{path.name} imports {target}")
                else:
                    self.assertIn(f'"{target}"', html, f"{path.name} imports {target}, which the page does not map")

    def test_libraries_are_pinned_to_exact_versions(self):
        html = (SITE / "index.html").read_text(encoding="utf-8")
        for url in re.findall(r"https://cdn\.jsdelivr\.net/npm/([^\"]+)", html):
            self.assertRegex(url, r"@\d+\.\d+\.\d+/", url)

    def test_no_technical_words_reach_visitors(self):
        text = visible_strings().lower()
        for word in BANNED:
            hits = re.findall(rf"[^\n]{{0,30}}\b{re.escape(word)}\b[^\n]{{0,30}}", text)
            self.assertEqual(hits, [], f"'{word}' appears in visitor-facing text: {hits[:2]}")

    def test_the_stages_a_visitor_sees(self):
        studio = (SITE / "js" / "studio.js").read_text(encoding="utf-8")
        for stage in ("Planning your home", "Getting the design studio ready", "Adding the finishing touches"):
            self.assertIn(stage, studio)
        self.assertIn("designing(room)", studio)  # one "Designing the ..." step per room

    def test_api_address_comes_from_deploy_time_config_not_hard_coded(self):
        api = (SITE / "js" / "api.js").read_text(encoding="utf-8")
        self.assertIn("window.STUDIO_API", api)
        self.assertNotIn("execute-api", api)
        self.assertFalse((SITE / "config.js").exists(), "config.js is generated at deploy time and must not be committed")

    def test_the_invitation_code_is_never_hard_coded(self):
        for path in SITE.rglob("*"):
            if path.is_file():
                self.assertNotRegex(path.read_text(encoding="utf-8"), r"demo-code|open-sesame", str(path))


if __name__ == "__main__":
    unittest.main()
