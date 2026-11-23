import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync_steam as s  # noqa: E402

FIXTURE = json.loads(
    (Path(__file__).parent / "fixture_news.json").read_text(encoding="utf-8")
)
WEBHOOK = "https://discord.com/api/webhooks/1/SECRET_TOKEN"


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_429(retry_after):
    return urllib.error.HTTPError(
        WEBHOOK, 429, "Too Many Requests", {},
        io.BytesIO(json.dumps({"retry_after": retry_after}).encode()),
    )


class BBCodeTests(unittest.TestCase):
    def test_real_steam_post(self):
        text, images = s.bbcode_to_markdown(FIXTURE[0]["contents"])

        self.assertIn("### ✨ New", text)
        self.assertIn("### 🐛 Fixes", text)
        self.assertIn("- Gear strengthened", text)
        self.assertNotIn("[", text)
        self.assertNotIn("{STEAM_CLAN_IMAGE}", text)
        self.assertEqual(
            images,
            [s.CLAN_IMAGE_BASE
             + "/46288169/79d471dd6ae7ea5d800284da0743cb560ca3eb89.gif"],
        )
        # Pas de ligne vide entre deux puces.
        self.assertNotIn("\n\n- ", text.split("### ⚖️")[0].split("- ", 1)[1])

    def test_inline_tags(self):
        text, _ = s.bbcode_to_markdown(
            "[h1]T[/h1]\n[b]gras[/b] [i]ital[/i] [u]sou[/u] [b] [/b]"
            "[url=https://a.b/x_(y)]lien[/url] [url]https://c.d[/url]"
            " [Fix] garde"
        )
        self.assertIn("# T", text)
        self.assertIn("**gras** *ital* __sou__", text)
        self.assertIn("[lien](https://a.b/x_(y%29)", text)
        self.assertIn("https://c.d", text)
        self.assertIn("[Fix] garde", text)

    def test_quote_code_hr_nested_list(self):
        text, _ = s.bbcode_to_markdown(
            "[quote=Bob]a\nb[/quote][code][b]x[/b][/code][hr]"
            "[list][*]un[list][*]sous[/list][*]deux[/list]"
            "[olist][*]a[*]b[/olist]"
        )
        self.assertIn("> **Bob**\n> a\n> b", text)
        self.assertIn("```\n[b]x[/b]\n```", text)
        self.assertIn("──────────", text)
        self.assertIn("- un\n  - sous\n- deux", text)
        self.assertIn("1. a\n2. b", text)

    def test_closing_list_item_tags(self):
        text, _ = s.bbcode_to_markdown(
            "Intro\n[list]\n[*]un[/*]\n[*]deux[/*]\n[/list]\nFin"
        )
        self.assertEqual(text, "Intro\n- un\n- deux\n\nFin")

    def test_html_and_entities(self):
        text, _ = s.bbcode_to_markdown("a<br>b &amp; <b>c</b>")
        self.assertEqual(text, "a\nb & c")


class SplitTests(unittest.TestCase):
    def test_long_text_split_on_newlines_nothing_lost(self):
        body = "\n".join(f"- ligne {i} " + "x" * 40 for i in range(400))
        chunks = s.split_text(body, 4000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 4000 for c in chunks))
        self.assertEqual("\n".join(chunks), body)

    def test_messages_respect_discord_limits(self):
        item = {"gid": "1", "date": 0, "title": "t",
                "contents": "[b]x[/b]\n" * 3000}
        for embeds in s.build_messages(item):
            self.assertLessEqual(len(embeds), 10)
            total = sum(len(e["description"]) for e in embeds)
            self.assertLessEqual(total, 6000)
            for e in embeds:
                self.assertLessEqual(len(e["description"]), 4096)


class DiscordTests(unittest.TestCase):
    def test_429_then_success(self):
        calls = []

        def fake_urlopen(request, timeout=0):
            calls.append(1)
            if len(calls) == 1:
                raise http_429(1.5)
            return FakeResponse()

        with mock.patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": WEBHOOK}), \
                mock.patch("urllib.request.urlopen", fake_urlopen), \
                mock.patch("time.sleep") as sleep, \
                contextlib.redirect_stdout(io.StringIO()):
            s.post_to_discord([{"description": "x"}])

        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(2.0)

    def test_429_gives_up_after_three_attempts(self):
        calls = []

        def fake_urlopen(request, timeout=0):
            calls.append(1)
            raise http_429(0.1)

        out = io.StringIO()
        with mock.patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": WEBHOOK}), \
                mock.patch("urllib.request.urlopen", fake_urlopen), \
                mock.patch("time.sleep"), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(urllib.error.HTTPError):
                s.post_to_discord([{"description": "x"}])

        self.assertEqual(len(calls), 3)
        self.assertNotIn("SECRET_TOKEN", out.getvalue())


class SteamApiTests(unittest.TestCase):
    def test_retries_on_5xx_then_ok(self):
        calls = []
        ok = io.BytesIO(json.dumps(
            {"appnews": {"newsitems": [{"gid": "1"}]}}).encode())

        class Resp(FakeResponse):
            def read(self):
                return ok.getvalue()

        def fake_urlopen(request, timeout=0):
            calls.append(1)
            if len(calls) < 3:
                raise urllib.error.HTTPError("u", 503, "x", {}, io.BytesIO())
            return Resp()

        with mock.patch("urllib.request.urlopen", fake_urlopen), \
                mock.patch("time.sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(s.api_news(), [{"gid": "1"}])

        self.assertEqual(len(calls), 3)

    def test_gives_up(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("down")), \
                mock.patch("time.sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                s.api_news()


class MainFlowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_file = Path(tmp.name) / "state.json"
        patches = [
            mock.patch.object(s, "STATE_FILE", self.state_file),
            mock.patch("time.sleep"),
            mock.patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": WEBHOOK}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        self.items = [
            {"gid": str(i), "date": 1000 + i, "title": f"T{i}",
             "url": "https://x", "contents": f"[b]n{i}[/b]"}
            for i in range(1, 4)
        ]
        self.state_file.write_text(json.dumps({
            "initialized": True,
            "seen_ids": ["1", "2", "3"],
            "pending": self.items,
        }))

    def run_main(self, post, argv=()):
        with mock.patch.object(s, "api_news", return_value=[]), \
                mock.patch.object(s, "post_to_discord", post), \
                contextlib.redirect_stdout(io.StringIO()):
            s.main(list(argv))

    def test_failure_mid_batch_no_duplicates(self):
        sent = []
        failed = []

        def flaky(embeds):
            title = embeds[0]["title"]
            if title == "T2" and not failed:
                failed.append(1)
                sent.append("T2-failed")
                raise RuntimeError("boom")
            sent.append(title)

        with self.assertRaises(RuntimeError):
            self.run_main(flaky)

        state = json.loads(self.state_file.read_text())
        self.assertEqual([i["gid"] for i in state["pending"]], ["2", "3"])

        self.run_main(flaky)
        self.assertEqual(sent, ["T1", "T2-failed", "T2", "T3"])
        self.assertEqual(json.loads(self.state_file.read_text())["pending"], [])

    def test_multi_part_resume(self):
        self.state_file.write_text(json.dumps({
            "initialized": True, "seen_ids": ["9"],
            "pending": [{"gid": "9", "date": 1, "title": "Long", "url": "u",
                         "contents": "ligne\n" * 3000}],
        }))
        posted = []

        def flaky(embeds):
            if len(posted) == 1 and not getattr(flaky, "failed", False):
                flaky.failed = True
                raise RuntimeError("boom")
            posted.append(embeds)

        with self.assertRaises(RuntimeError):
            self.run_main(flaky)

        pending = json.loads(self.state_file.read_text())["pending"]
        self.assertEqual(pending[0]["sent_parts"], 1)

        self.run_main(flaky)
        total = len(s.build_messages(
            {"gid": "9", "date": 1, "title": "Long", "url": "u",
             "contents": "ligne\n" * 3000}))
        self.assertEqual(len(posted), total)  # chaque partie une seule fois

    def test_dry_run_posts_nothing_and_keeps_state(self):
        before = self.state_file.read_text()
        post = mock.Mock()
        self.run_main(post, ["--dry-run"])
        post.assert_not_called()
        self.assertEqual(self.state_file.read_text(), before)

    def test_last_check_written(self):
        self.run_main(mock.Mock())
        self.assertIn("last_check", json.loads(self.state_file.read_text()))


if __name__ == "__main__":
    unittest.main()
