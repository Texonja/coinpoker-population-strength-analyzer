"""Regression cases from the repository's public cash_example.txt.

Run from the repository root, or place this file beside the analyzer and run
python test_coinpoker_regressions.py -v. Only the standard library is needed.
"""

import csv
import re
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


import anonymize_coinpoker_hh as anonymizer
import coinpoker_population_strength_analyzer as analyzer
repo = Path(__file__).resolve().parent


class PublicSampleRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sample = (repo / "cash_example.txt").read_text(encoding="utf-8")
        wanted = {"9000000072", "9000000114", "9000000132", "9000000245",
                  "9000000406", "9000003333", "9000000044", "9000000238", "9000001141"}
        cls.blocks = {}
        for block in re.split(r"(?m)(?=^CoinPoker Hand #)", sample):
            header = re.match(r"CoinPoker Hand #(\d+):", block)
            if header and header[1] in wanted:
                cls.blocks[header[1]] = block
        missing = wanted.difference(cls.blocks)
        if missing:
            raise ValueError(f"Missing hands in cash_example.txt: {sorted(missing)}")

    def hand(self, hand_id):
        parsed = analyzer.parse_hand(self.blocks[hand_id], hero="Hero")
        self.assertIsNotNone(parsed)
        return parsed

    def records(self, hand_id):
        return analyzer.extract_population_records(self.hand(hand_id), hero="Hero", min_stack_bb=40)

    def strength(self, hand_id, villain, street):
        hand = self.hand(hand_id)
        return analyzer.classify_street_strength(
            hand.shown_cards[villain], hand.board_for_street(street), street
        )

    def test_genuine_four_bet_is_included(self):
        _, pre = self.records("9000000406")
        self.assertIn(("61a7ec75", "PFR_4bet_plus_shown"),
                      [(r["villain"], r["line_id"]) for r in pre])

    def test_short_all_in_call_is_excluded_from_preflop_raisers(self):
        _, pre = self.records("9000000406")
        self.assertNotIn("13a655b4", [r["villain"] for r in pre])

    def test_all_in_over_a_flop_bet_after_checking_is_a_check_raise(self):
        post, _ = self.records("9000000132")
        lines = {r["line_id"] for r in post if r["villain"] == "4f4f1a74"}
        self.assertIn("FLOP_check_raise", lines)
        self.assertNotIn("FLOP_donk_bet", lines)

    def test_limped_heads_up_pot_has_no_flop_donk(self):
        post, _ = self.records("9000000114")
        self.assertNotIn("FLOP_donk_bet", [r["line_id"] for r in post])

    def test_straddle_counts_towards_flop_pot(self):
        hand = self.hand("9000000072")
        flop_bet = next(e for e in hand.events["FLOP"] if e.action == "bet")
        self.assertAlmostEqual(flop_bet.pot_before, 4.29, places=2)

    def test_auto_blind_is_a_live_big_blind_in_an_ante_game(self):
        hand = self.hand("9000000044")
        self.assertAlmostEqual(hand.events["FLOP"][0].pot_before, 1.01)

    def test_pot_reconciles_to_every_public_hand_summary(self):
        sample = (repo / "cash_example.txt").read_text(encoding="utf-8")
        checked = 0
        for block in analyzer.split_hand_history(sample):
            expected = re.search(r"^Total pot ₮([\d.]+)", block, re.MULTILINE)
            hand = analyzer.parse_hand(block)
            self.assertIsNotNone(expected, block.splitlines()[0])
            self.assertIsNotNone(hand, block.splitlines()[0])
            self.assertAlmostEqual(hand.meta["gross_pot"], float(expected[1]), places=2, msg=hand.hand_id)
            checked += 1
        self.assertEqual(checked, 10_000)

    def test_splash_and_player_funded_pot_are_distinct(self):
        hand = self.hand("9000000238")
        bet = next(e for e in hand.events["FLOP"] if e.action == "bet")
        self.assertAlmostEqual(bet.pot_before, 4.29)
        self.assertAlmostEqual(bet.size_player_funded_pct, 0.64 / 2.79)
        self.assertEqual(hand.meta["gross_pot"], 5.57)

    def test_ace_with_small_spade_is_nut_flush_in_both_report_taxonomies(self):
        board = "Qs 8s 2s Kd 5c".split()
        for hole, expected in [("As 3s Jh Th", "practical_nuts"),
                               ("Ks 3s Jh Th", "second_third_nut_flush"),
                               ("5s 3s Jh Th", "low_flush")]:
            with self.subTest(hole=hole):
                result = analyzer.classify_street_strength(hole.split(), board, "RIVER")
                self.assertEqual(result["river_plo_class"], expected)
                if hole.startswith("As"):
                    self.assertEqual(result["river_practical_value_class"], expected)

    def test_next_card_straight_outs(self):
        cases = [
            ("5c 6d 7h Ks", "8c Qd 2s", 0),
            ("5c 6d Kh Ad", "7h 8c 2s", 8),
            ("Ac 2d Qh Ks", "3h 4c 9s", 4),
            # Three remaining fours, fives and sixes, plus all four nines.
            ("5c 6d Kh Ad 4c", "7h 8c 2s Qd", 13),
        ]
        for hole, board, expected in cases:
            with self.subTest(hole=hole):
                self.assertEqual(analyzer.straight_outs_approx(hole.split(), board.split()), expected)

    def test_checked_to_in_position_bet_is_excluded_from_donks(self):
        # In this real hand the button opens. Swap who opens and who calls,
        # keeping the flop check and in-position bet.
        block = self.blocks["9000003333"].replace("9639f88b: raises ₮1.25 to ₮1.75", "9639f88b: calls ₮0.50")
        block = block.replace("db833f28: calls ₮1.25", "db833f28: raises ₮1.25 to ₮1.75\n9639f88b: calls ₮1.25", 1)
        hand = analyzer.parse_hand(block)
        post, _ = analyzer.extract_population_records(hand, "Hero", 40)
        self.assertNotIn("FLOP_donk_bet", [r["line_id"] for r in post])

    def test_exact_all_in_call(self):
        block = self.blocks["9000000132"].replace("4f4f1a74: ALLIN ₮9.56", "4f4f1a74: ALLIN ₮6.35")
        block = block.replace("Hero: calls ₮3.21\n", "")
        hand = analyzer.parse_hand(block)
        last = hand.events["FLOP"][-1]
        self.assertEqual((last.action, last.raw_action), ("call", "allin"))

    def test_all_in_opening_bet_remains_a_bet(self):
        block = self.blocks["9000000132"].replace("Hero: bets ₮6.35", "Hero: ALLIN ₮6.35")
        block = block.replace("4f4f1a74: ALLIN ₮9.56\nHero: calls ₮3.21", "4f4f1a74: calls ₮6.35")
        bet = analyzer.parse_hand(block).events["FLOP"][1]
        self.assertEqual((bet.action, bet.raw_action), ("bet", "allin"))

    def test_bombpot_keeps_two_boards_for_one_action(self):
        post, _ = self.records("9000001141")
        self.assertEqual(len(post), len({r["action_index"] for r in post}))
        flop = next(r for r in post if r["street"] == "FLOP" and r["villain"] == "32b99168")
        self.assertEqual((flop["board_1_made"], flop["board_2_made"]), ("trips_or_set", "two_pair"))
        turn = next(r for r in post if r["street"] == "TURN" and r["villain"] == "cb6ad232")
        self.assertEqual(turn["villain_action"], "raise")
        self.assertEqual(self.hand("9000000406").second_board, {})

    def test_mixed_report_has_known_and_unknown_actions_and_separate_bombpot_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            source = path / "mixed.txt"
            source.write_text("".join(self.blocks[k] for k in ("9000000072", "9000000245", "9000001141")), encoding="utf-8")
            args = Namespace(game_filter="all", include_bombpot=True, min_stack_bb=40,
                             min_known=1, date_from="", date_to="", stake_filter="")
            run = analyzer.analyze(source, "Hero", path, args)
            with (path / "line_card_coverage.csv").open(encoding="utf-8", newline="") as f:
                coverage = {r["line_id"]: r for r in csv.DictReader(f)}
            opens = coverage["PFR_open_shown"]
            self.assertEqual((opens["eligible_actions"], opens["known_records"]), ("2", "1"))
            with (path / "bombpot_population_by_line.csv").open(encoding="utf-8", newline="") as f:
                self.assertEqual(sum(int(r["records"]) for r in csv.DictReader(f)), run["bombpot_known_line_records"])
            for extension in ("html", "md"):
                report = (path / f"population_strength_presentation.{extension}").read_text(encoding="utf-8")
                self.assertIn("Card coverage by line", report)
                self.assertIn("BombPot hand strength across boards", report)

    def test_king_high_hearts_with_ace_on_board_are_the_nuts(self):
        result = self.strength("9000000245", "cd11ded4", "RIVER")
        self.assertEqual(result["river_plo_class"], "practical_nuts")

    def test_broadway_can_use_a_hole_king_when_the_board_has_a_king(self):
        result = self.strength("9000003333", "9639f88b", "RIVER")
        self.assertEqual(result["river_plo_class"], "practical_nuts")

    def test_pocket_jacks_with_only_backdoors_are_not_pair_plus_good_draw(self):
        result = self.strength("9000000132", "4f4f1a74", "FLOP")
        self.assertEqual(result["strength_bucket"], "one_pair_no_good_draw")

    def test_anonymizer_preserves_source_when_output_is_the_same_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "hands.txt"
            source.write_text(self.blocks["9000000072"] + self.blocks["9000000114"], encoding="utf-8")
            original = source.read_bytes()
            try:
                anonymizer.anonymize(source, source, hero="Hero", max_hands=1,
                                     rewrite_ids=True, shift_dates=True)
            except (OSError, ValueError):
                pass
            self.assertTrue(source.read_bytes() == original, "Original hand history was overwritten")


if __name__ == "__main__":
    unittest.main()
