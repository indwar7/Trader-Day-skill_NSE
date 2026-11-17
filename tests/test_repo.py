import json
import os
import re
import unittest

from _load import ROOT

# the NSE port: nine skill folders at the root
SKILLS = ["nse-scanner", "nse-news", "nse-entry", "nse-exit", "nse-timemachine",
          "nse-budget", "nse-risk", "nse-groww", "nse-alerts"]
# skills on the way to an order carry the safety block
ORDER_PATH = ["nse-scanner", "nse-entry", "nse-exit", "nse-timemachine", "nse-budget",
              "nse-risk", "nse-groww", "nse-alerts"]
# upstream names and US-only pieces that must not survive the port
GONE = ["trader-scanner", "trader-news", "trader-entry", "trader-exit", "trader-timemachine",
        "trader-budget", "trader-risk", "trader-broker", "trader-alerts", "trader-analyst",
        "trader-pelosi", "trader-copycat", "trader-screener", "trader-tax", "trader-rebalance",
        "NYSE", "Nasdaq Trader", "EDGAR", "Alpaca", "pdt_guard", "_usd"]


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def every_text_file(include_tests=True):
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "trader")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml")) or f == "LICENSE":
                path = os.path.join(base, f)
                rel = os.path.relpath(path, ROOT)
                if not include_tests and rel.startswith("tests" + os.sep):
                    continue
                yield path


class Repo(unittest.TestCase):
    def test_nine_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT)
                       if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_root_holds_only_the_pack(self):
        root = sorted(d for d in os.listdir(ROOT)
                      if not d.startswith(".") and d not in ("trader", "__pycache__"))
        self.assertEqual(root, sorted(["LICENSE", "README.md", "NSE-PORT.md", "tests"] + SKILLS))

    def test_upstream_license_notice_is_kept(self):
        with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("MIT License", text)
        self.assertIn("Copyright (c) 2026 Jake Schincariol", text)

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 40, s)

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)

    def test_skills_speak_nse_and_ist(self):
        for s in SKILLS:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertTrue("NSE" in text, "%s never mentions NSE" % s)

    def test_no_upstream_or_us_leftovers_in_the_tools(self):
        for path in every_text_file(include_tests=False):
            rel = os.path.relpath(path, ROOT)
            if rel in ("README.md", "NSE-PORT.md", "LICENSE"):
                continue  # these describe the port and credit upstream on purpose
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for old in GONE:
                self.assertNotIn(old, text, "%s still says %r" % (rel, old))

    def test_readme_lists_every_skill_and_the_fine_print(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        self.assertTrue(readme.startswith("# NSE Day Trader"))
        for s in SKILLS:
            self.assertIn("/" + s, readme, s)
        for line in ("## Fine print", "Not financial advice", "Jakeschincariol/day-trader-skill"):
            self.assertIn(line, readme)

    def test_no_em_dashes(self):
        for path in every_text_file():
            rel = os.path.relpath(path, ROOT)
            if rel.startswith(os.path.join("tests", "fixtures")):
                continue  # fixtures are real data, kept as published
            with open(path, encoding="utf-8", errors="replace") as fh:
                self.assertNotIn(chr(0x2014), fh.read(), rel)

    def test_every_tool_runs_on_python_3_8(self):
        # no 3.9+ only modules or syntax in the tools (macOS /usr/bin/python3 is 3.9; README says 3.8+)
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                where = "%s/%s" % (s, f)
                for bad in ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix("):
                    self.assertNotIn(bad, src, where)
                self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", where)
                for bad in ("list[", "dict[", "tuple["):
                    self.assertNotRegex(src, r"(?<![\w.\"'])%s" % re.escape(bad), where)

    def test_every_network_tool_uses_the_honest_agent_and_the_ssl_fallback(self):
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                if "urlopen" not in src:
                    continue
                where = "%s/%s" % (s, f)
                self.assertIn("nse-day-trader/1.0", src, where)
                self.assertIn("/etc/ssl/cert.pem", src, where)
                self.assertNotIn("CERT_NONE", src, where)
                self.assertNotIn("check_hostname = False", src, where)
                self.assertNotRegex(src, r"nseindia\.com/api/", where)

    def test_day_trading_limits_ship_in_the_example_config(self):
        with open(os.path.join(ROOT, "nse-budget", "config.example.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        for k in ("budget", "max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day",
                  "min_reward_risk", "require_stop", "no_entries_before", "no_entries_after",
                  "flat_by", "product", "exchange", "mis_leverage", "costs"):
            self.assertIn(k, cfg, k)
        self.assertIs(cfg["require_stop"], True)
        self.assertIs(cfg.get("allow_short", False), False)
        self.assertEqual(cfg["product"], "MIS")
        self.assertEqual(cfg["exchange"], "NSE")
        self.assertEqual(cfg["mis_leverage"], 1)
        self.assertEqual(cfg["flat_by"], "15:00")
        self.assertEqual(cfg["no_entries_after"], "14:30")
        for gone in ("paper", "broker", "pdt_guard", "sec_user_agent", "allow_options", "allow_margin"):
            self.assertNotIn(gone, cfg, gone)

    def test_groww_ticket_never_talks_to_a_broker(self):
        folder = os.path.join(ROOT, "nse-groww")
        for f in os.listdir(folder):
            if f.endswith(".py"):
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                for bad in ("urlopen", "http.client", "socket", "requests", "growwapi"):
                    self.assertNotIn(bad, src, "nse-groww/%s imports %s" % (f, bad))


if __name__ == "__main__":
    unittest.main()
