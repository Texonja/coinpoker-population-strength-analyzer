"""Verify the bundled sample with an independent five-card ranking implementation."""
import csv
import itertools
import re
import subprocess
import sys
import tempfile
from collections import Counter
from functools import lru_cache
from pathlib import Path

import coinpoker_population_strength_analyzer as analyzer

ROOT = Path(__file__).resolve().parent
DECK = [r + s for r in "23456789TJQKA" for s in "cdhs"]
LABELS = ["High Card", "One Pair", "Two Pair", "Three Of A Kind", "Straight", "Flush",
          "Full House", "Four Of A Kind", "Straight Flush"]


@lru_cache(maxsize=150_000)
def rank_five(cards):
    values = sorted(("23456789TJQKA".index(c[0]) + 2 for c in cards), reverse=True)
    groups = sorted(((n, rank) for rank, n in Counter(values).items()), reverse=True)
    flush = len({c[1] for c in cards}) == 1
    high = 0
    if len(groups) == 5:
        if values == [14, 5, 4, 3, 2]:
            high = 5
        elif values[0] - values[-1] == 4:
            high = values[0]
    if high and flush:
        return 8, (high,)
    if flush:
        return 5, tuple(values)
    if high:
        return 4, (high,)
    shape = tuple(count for count, rank in groups)
    paired = {(4, 1): 7, (3, 2): 6, (3, 1, 1): 3, (2, 2, 1): 2, (2, 1, 1, 1): 1}
    return paired.get(shape, 0), tuple(rank for count, rank in groups)


def rank_plo(hole, board):
    return max(rank_five(tuple(sorted(pair + triple)))
               for pair in itertools.combinations(hole, 2) for triple in itertools.combinations(board, 3))


def verify_history():
    hands = shows = 0
    text = (ROOT / "cash_example.txt").read_text(encoding="utf-8")
    for block in re.split(r"(?m)(?=^CoinPoker Hand #)", text):
        if not block.strip():
            continue
        hand = analyzer.parse_hand(block)
        total = re.search(r"(?m)^Total pot ₮([\d.]+)", block)
        if not hand or not total or hand.meta["gross_pot"] != float(total[1]):
            raise ValueError(f"Pot mismatch in {block.splitlines()[0]}")
        boards = {m[1]: m[2].split() for m in re.finditer(r"(?m)^(?:(FIRST|SECOND|THIRD) )?Board \[([^]]*)\]", block)}
        board_id = None
        for line in block.splitlines():
            marker = re.match(r"\*\*\* (?:(FIRST|SECOND|THIRD) )?SHOWDOWN", line)
            if marker:
                board_id = marker[1]
            shown = re.match(r"(.+?): shows \[([^]]+)\] \(([^)]+)\)", line)
            if shown:
                hole, board = shown[2].split(), boards[board_id]
                rank = rank_plo(hole, board)
                actual = analyzer.best_plo_hand(hole, board)
                expected_label = "Straight Flush" if shown[3] == "Royal Flush" else shown[3]
                if LABELS[rank[0]] != expected_label or rank != (actual[0], tuple(actual[1])):
                    raise ValueError(f"Showdown mismatch in {hand.hand_id} for {shown[1]}")
                shows += 1
        hands += 1
    return hands, shows


def verify_export(path):
    rivers = {}
    records = 0
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            hole = tuple(row["villain_cards"].split())
            bomb = row["line_id"].startswith("BOMBPOT_")
            boards = [tuple(row[key].split()) for key in ("board_1", "board_2") if row[key]] if bomb else [tuple(row["board"].split())]
            for i, board in enumerate(boards):
                prefix = f"board_{i + 1}_" if bomb else ""
                rank = rank_plo(hole, board)
                best = tuple(row[prefix + "best_cards"].split())
                if (rank_five(tuple(sorted(best))) != rank or len(set(hole) & set(best)) != 2
                        or len(set(board) & set(best)) != 3):
                    raise ValueError(f"Best five mismatch in {row['hand_id']} on {board}")
                records += 1
                if len(board) != 5:
                    continue
                dead = boards[1 - i] if len(boards) > 1 else ()
                key = hole, board, dead
                if key not in rivers:
                    remaining = set(DECK).difference(hole, board, dead)
                    opponents = [rank_plo(pair, board) for pair in itertools.combinations(remaining, 2)]
                    unbeaten = max(opponents) <= rank
                    practical = rank[0] >= 7 or all(o <= rank or o[0] >= 7 for o in opponents)
                    rivers[key] = unbeaten, practical, len(opponents)
                unbeaten, practical, _ = rivers[key]
                column = prefix + "unbeaten" if bomb else "river_unbeaten"
                if (row[column] == "True") != unbeaten or (
                        not bomb and (row["river_plo_class"] == "practical_nuts") != practical):
                    raise ValueError(f"River tier mismatch in {row['hand_id']} on {board}")
    return records, len(rivers), sum(v[2] for v in rivers.values())


if __name__ == "__main__":
    hands, shows = verify_history()
    print(f"Matched gross pots in {hands:,} hands and {shows:,} showdown labels.", flush=True)
    with tempfile.TemporaryDirectory() as folder:
        subprocess.run([sys.executable, str(ROOT / "coinpoker_population_strength_analyzer.py"),
                        "--input", str(ROOT / "cash_example.txt"), "--outdir", folder, "--game-filter", "all",
                        "--include-bombpot", "--min-known", "1"], check=True)
        records, rivers, pairs = verify_export(Path(folder) / "population_strength_hands.csv")
    observed = hands, shows, records, rivers, pairs
    expected = 10000, 4952, 1992, 481, 418565
    if observed != expected:
        raise ValueError(f"Public sample baseline changed: expected {expected}, observed {observed}")
    print(f"Matched {records:,} best-five records and {rivers:,} river inputs across {pairs:,} legal opposing pairs.")
