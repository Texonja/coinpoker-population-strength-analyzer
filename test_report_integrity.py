"""Card, wager and report regressions against the distributed sample."""
import csv
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace

import coinpoker_population_strength_analyzer as a

ROOT = Path(__file__).parent


class ReportIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = (ROOT / "cash_example.txt").read_text(encoding="utf-8")
        cls.blocks = {b.split("#", 1)[1].split(":", 1)[0]: b for b in a.split_hand_history(raw)}

    def rows(self, hand_id, vs_hero=False):
        return a.extract_population_records(a.parse_hand(self.blocks[hand_id]), "Hero", 40, vs_hero=vs_hero)

    def run_report(self, tmp, text, **filters):
        path = Path(tmp)
        hh = path / "hands.txt"
        hh.write_text(text, encoding="utf-8-sig")
        args = dict(game_filter="all", include_bombpot=True, min_stack_bb=40, min_known=1,
                    date_from="", date_to="", stake_filter="")
        return a.analyze(hh, "Hero", path, Namespace(**{**args, **filters}))

    def test_pot_raise_and_facing_raise(self):
        post, _ = self.rows("9000001556")
        first = next(r for r in post if r["line_id"] == "RIVER_raise_not_checkraise")
        again = next(r for r in post if r["line_id"] == "RIVER_reraise")
        self.assertEqual((first["action_size_pct_pot"], first["chips_pushed_pct_pot"]), (1, 2))
        self.assertEqual((again["facing_action"], again["facing_size_pct_pot"], again["facing_bet_size_bucket"]),
                         ("raise", 1, ""))

    def test_short_all_in_raise_remains_a_price_increase(self):
        hand = a.parse_hand(self.blocks["9000000132"])
        ev = next(e for e in hand.events["FLOP"] if e.raw_action == "allin")
        self.assertEqual((ev.action, ev.full_raise), ("raise", False))

    def test_first_raise_after_a_limp(self):
        _, pre = self.rows("9000000047")
        self.assertIn("PFR_iso_shown", [r["line_id"] for r in pre])
        self.assertNotIn("PFR_open_shown", [r["line_id"] for r in pre])

    def test_barrel_and_probe_require_initiative(self):
        post, _ = self.rows("9000003931")
        self.assertNotIn("RIVER_bet_bet_bet", [r["line_id"] for r in post])
        self.assertNotIn("RIVER_barrel_after_turn_bet", [r["line_id"] for r in post])
        post, _ = self.rows("9000000344")
        ids = [r["line_id"] for r in post]
        self.assertIn("TURN_delayed_cbet_after_flop_xx", ids)
        self.assertNotIn("TURN_probe_after_flop_xx", ids)

    def test_hero_response_stops_at_intervening_raise(self):
        def event(idx, player, action):
            return a.Event("TURN", player, action, action, idx=idx)
        bet = event(1, "A", "bet")
        raise_ = event(2, "B", "raise")
        events = [bet, raise_, event(3, "Hero", "call")]
        self.assertEqual(a.wager_context(events, bet, "Hero"), (False, True))
        self.assertEqual(a.wager_context(events, raise_, "Hero"), (True, True))
        post, _ = self.rows("9000000047", vs_hero=True)
        self.assertFalse(post)

    def test_suited_ace_wheel_and_overlapping_pairs(self):
        self.assertFalse(a.classify_starting_hand("Ac Ts 7s 6h".split())["suited_ace"])
        aa_kk = a.classify_starting_hand("As Ad Ks Kc".split())
        self.assertEqual((aa_kk["has_aa"], aa_kk["has_kk"], aa_kk["hand_class"]), (True, True, "AAxx"))
        self.assertEqual(a.classify_starting_hand("As 2d 3h 4s".split())["connected_window"], 4)

    def test_paired_board_and_actual_top_two(self):
        row = next(r for r in self.rows("9000001906")[0] if r["street"] == "RIVER")
        self.assertEqual((row["made_name"], row["river_plo_class"]), ("two_pair", "other_two_pair_or_less"))
        board = "Ac Kd 7h 6s 2c".split()
        for cards, expected in (("As Kh Jh 3h", True), ("As 7c Jh 3h", False)):
            hole = cards.split()
            self.assertEqual(a.is_top_two_pair(a.best_plo_hand(hole, board), board, hole), expected)

    def test_practical_and_unbeaten_have_distinct_meanings(self):
        hole = "As 3s 4d 2c".split()
        classy = a.classify_street_strength(hole, "Qs Js 8s Kd 5c".split(), "RIVER")
        self.assertEqual((classy["river_plo_class"], classy["river_unbeaten"]), ("practical_nuts", False))

    def test_wet_flop_and_unpaired_draw(self):
        for cards in ("8h 9c 6h", "Ad 2d 5s"):
            self.assertEqual(a.board_bucket(cards.split()), "wet")
        self.assertEqual(a.board_bucket("8h 8c 6h".split()), "paired")
        row = next(r for r in self.rows("9000009046")[0] if r["street"] == "TURN")
        self.assertEqual((row["straight_completion_cards"], row["strength_bucket"]), (17, "unpaired_completion_draw"))

    def test_quarantine_conflicts_regardless_of_order(self):
        good = self.blocks["9000001556"]
        another = self.blocks["9000000709"]
        different = good.replace("CEST", "UTC")
        for first, second in [(different, good), (good, different)]:
            with tempfile.TemporaryDirectory() as d:
                run = self.run_report(d, "\n".join((first, second, another, another)))
                self.assertEqual(run["valid_unique_hands"], 1)
                self.assertEqual(run["identical_duplicate_blocks"], 1)
                self.assertEqual(run["input_issues"], {"conflicting_hand_id": 2})
                self.assertIn("9000001556", run["conflicting_hand_ids"])

    def test_bad_token_duplicate_board_and_broken_ledger(self):
        block = self.blocks["9000000001"]
        invalid = [(block.replace("Qc 9d 6h 4s", "Qc 9d 6h Xx"), "invalid_card_token"),
                   (block.replace("Qc 9d 6h 4s", "Qc 9d 6h 7h"), "duplicate_physical_card"),
                   (block.replace("Hero: bets ", "Hero: calls "), "action_disagrees_with_live_wager"),
                   (block.replace("Hero: bets ", "Hero: BETS "), "unsupported_action"),
                   (block.replace("Hero (₮25 in chips)", "Hero (₮0.30 in chips)"), "wager_exceeds_available_chips"),
                   (block.replace("Total pot ₮0.84", "Total pot ₮0.85"), "pot_disagrees_with_summary"),
                   (block.split("*** SUMMARY ***")[0], "incomplete_hand")]
        for text, issue in invalid:
            with self.subTest(issue=issue), tempfile.TemporaryDirectory() as d:
                run = self.run_report(d, text + "\n" + self.blocks["9000000709"])
                self.assertEqual(run["input_issues"], {issue: 1})
                self.assertEqual(run["valid_unique_hands"], 1)
                self.assertIn(issue, (Path(d) / "population_strength_presentation.html").read_text(encoding="utf-8"))

    def test_valid_but_filtered_and_failed_cli_are_distinct(self):
        with tempfile.TemporaryDirectory() as d:
            run = self.run_report(d, self.blocks["9000000709"], min_stack_bb=10000)
            self.assertEqual((run["hands_after_game_stake_date"], run["hands_with_eligible_actions"]), (1, 0))
            path = Path(d)
            with (path / "population_strength_hands.csv").open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                self.assertIn("river_unbeaten", reader.fieldnames)
                self.assertEqual(list(reader), [])
            failed = subprocess.run([sys.executable, str(ROOT / "coinpoker_population_strength_analyzer.py"),
                                     "--input", str(path / "hands.txt"), "--hero", "Unknown", "--outdir", str(path / "failed")],
                                    capture_output=True, text=True)
            self.assertEqual(failed.returncode, 2)
            self.assertIn("hero_not_seated", failed.stderr)
            self.assertFalse((path / "failed").exists())

    def test_summary_counts_actions_and_distinct_hands(self):
        from strength_report import coverage_rows, summary_rows
        _, pre = self.rows("9000000614")
        records = [r for r in pre if r["line_id"] == "PFR_4bet_plus_shown"]
        self.assertGreater(len(records), 1)
        key = records[0]["line_id"]
        groups = {key: records}
        eligible = {key: {(r["hand_id"], r["action_index"]): r["called_or_raised"] for r in records}}
        coverage = coverage_rows(eligible, groups)
        self.assertEqual((coverage[0]["known_hands"], coverage[0]["known_records"]), (1, len(records)))
        self.assertEqual(summary_rows(groups, coverage, 2, a.RIVER_PLO_CLASS_ORDER), ([], [], []))

    def test_summary_markdown_html_and_compact_csv_agree(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_report(d, self.blocks["9000000709"])
            path = Path(d)
            for suffix in ("by_line", "summary_friendly", "summary_river_compact", "summary_compact"):
                with (path / f"population_strength_{suffix}.csv").open(encoding="utf-8", newline="") as f:
                    row = next(r for r in csv.DictReader(f) if r["line_id"] == "RIVER_probe_after_turn_xx")
                self.assertEqual((row["known_records"], row["other_two_pair_or_less_n"], row["other_two_pair_or_less_pct"]),
                                 ("1", "1", "100.0"))
            for ext in ("html", "md"):
                report = (path / f"population_strength_report.{ext}").read_text(encoding="utf-8")
                self.assertIn("1 (100.0%)", report)
                self.assertNotIn("VALUE-HEAVY", report)
