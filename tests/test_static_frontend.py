import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"


class StaticFrontendTests(unittest.TestCase):
    def test_index_contains_core_controls_for_current_game_mechanics(self):
        html = INDEX.read_text(encoding="utf-8")
        required_snippets = [
            "id=\"btn-new-request\"",
            "id=\"outgoing-list\"",
            "id=\"incoming-list\"",
            "Flag invalid",
            "flag_invalid_request",
            "score-chip",
            "requestScaleLabel",
            "point_multiplier",
            "sequence_aa_length",
            "screen-end",
            "safeColor",
            "rel=\"icon\" type=\"image/png\" href=\"/static/favicon.png\"",
            "id=\"game-duration\"",
            "game_duration_minutes",
            "sound_effect",
            "playSound('notification')",
            "playSound('game_over')",
            "/sounds/correct_sound.mp3",
            "/sounds/wrong_sound.mp3",
            "/sounds/notfication_sound.mp3",
            "/sounds/game_over_sound.mp3",
            "addEventListener('click'",
        ]
        for snippet in required_snippets:
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, html)

        obsolete_snippets = [
            "End window",
            "end_window_minutes",
            "id=\"end-min\"",
            "id=\"end-max\"",
            "Number of players",
            "id=\"expected-players\"",
            "expected_players:",
            "colors auto-assigned",
            "More teams",
            "assigned randomly",
            "DNA shown in app",
            "Use the DNA shown",
        ]
        for snippet in obsolete_snippets:
            with self.subTest(obsolete=snippet):
                self.assertNotIn(snippet, html)

    def test_dynamic_request_controls_do_not_use_inline_click_handlers(self):
        html = INDEX.read_text(encoding="utf-8")
        script = html[html.index("<script>") : html.index("</script>")]
        forbidden = [
            "onclick=\"confirmReq",
            "onclick=\"rejectReq",
            "onclick=\"openDecrypt",
            "onclick=\"flagInvalid",
        ]
        for snippet in forbidden:
            with self.subTest(snippet=snippet):
                self.assertNotIn(snippet, script)

    def test_embedded_javascript_parses_when_node_is_available(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")

        html = INDEX.read_text(encoding="utf-8")
        start = html.index("<script>") + len("<script>")
        end = html.index("</script>", start)
        script = html[start:end]
        result = subprocess.run(
            [node, "--check", "-"],
            input=script,
            text=True,
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
