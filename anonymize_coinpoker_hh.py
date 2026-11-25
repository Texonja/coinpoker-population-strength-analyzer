#!/usr/bin/env python3
"""
CoinPoker HH anonymizer for public GitHub samples.

What it does:
- replaces the Hero screen name with "Hero"
- optionally caps the sample to the first N hands
- optionally rewrites hand IDs and table IDs
- optionally shifts dates to a neutral synthetic date sequence

It does NOT change cards/actions/stakes by default, because the analyzer needs
realistic hand structure to work.
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta
from pathlib import Path

HAND_START_RE = re.compile(r"(?=^CoinPoker Hand #)", re.MULTILINE)
HAND_HEADER_RE = re.compile(r"^CoinPoker Hand #(\d+):(.*?)(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})(.*)$", re.MULTILINE)
GAME_ENDED_RE = re.compile(r"^(Game ended: )(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})(.*)$", re.MULTILINE)
TABLE_RE = re.compile(r"Table '\d+'")


def split_hands(text: str) -> list[str]:
    parts = [p for p in HAND_START_RE.split(text) if p.strip()]
    return parts


def safe_replace_hero(text: str, hero: str, replacement: str = "Hero") -> str:
    if not hero or hero == replacement:
        return text
    # Replace exact text. CoinPoker anon names are simple tokens, but exact replace is safest.
    return text.replace(hero, replacement)


def rewrite_hand_id(hand: str, idx: int, base_id: int = 9000000000) -> str:
    return re.sub(r"CoinPoker Hand #\d+:", f"CoinPoker Hand #{base_id + idx}:", hand, count=1)


def rewrite_table_id(hand: str, idx: int, base_table: int = 800000) -> str:
    return TABLE_RE.sub(f"Table '{base_table + (idx % 100)}'", hand)


def rewrite_dates(hand: str, idx: int, base_dt: datetime) -> str:
    # Keep order and rough within-hand duration. Use same synthetic start time plus idx*2 minutes.
    new_start = base_dt + timedelta(minutes=2 * idx)
    start_str = new_start.strftime("%Y/%m/%d %H:%M:%S")

    def repl_header(m: re.Match) -> str:
        return f"CoinPoker Hand #{m.group(1)}:{m.group(2)}{start_str}{m.group(4)}"

    hand = HAND_HEADER_RE.sub(repl_header, hand, count=1)

    # If Game ended exists, set it 90s later for privacy while preserving valid format.
    end_str = (new_start + timedelta(seconds=90)).strftime("%Y/%m/%d %H:%M:%S")
    hand = GAME_ENDED_RE.sub(lambda m: f"{m.group(1)}{end_str}{m.group(3)}", hand)
    return hand


def anonymize(
    input_path: Path,
    output_path: Path,
    hero: str,
    max_hands: int | None,
    rewrite_ids: bool,
    shift_dates: bool,
) -> dict[str, int | str]:
    if output_path.exists() and output_path.samefile(input_path):
        raise ValueError("Choose an output file different from the original hand history.")
    text = input_path.read_text(encoding="utf-8", errors="replace")
    hands = split_hands(text)
    raw_count = len(hands)
    if max_hands and max_hands > 0:
        hands = hands[:max_hands]

    base_dt = datetime(2026, 1, 1, 12, 0, 0)
    out_hands = []
    for idx, hand in enumerate(hands, start=1):
        hand = safe_replace_hero(hand, hero, "Hero")
        if rewrite_ids:
            hand = rewrite_hand_id(hand, idx)
            hand = rewrite_table_id(hand, idx)
        if shift_dates:
            hand = rewrite_dates(hand, idx, base_dt)
        out_hands.append(hand.rstrip() + "\n")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(out_hands).rstrip() + "\n", encoding="utf-8")
    return {
        "input": str(input_path),
        "output": str(output_path),
        "raw_hands": raw_count,
        "written_hands": len(out_hands),
        "hero_replacement": "Hero",
        "ids_rewritten": int(rewrite_ids),
        "dates_shifted": int(shift_dates),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Anonymize CoinPoker hand histories for public samples.")
    parser.add_argument("--input", required=True, help="Input CoinPoker HH .txt file")
    parser.add_argument("--output", default="examples/public_sample_hands.txt", help="Output anonymized sample path")
    parser.add_argument("--hero", required=True, help="Your current Hero/screen name in the HH file")
    parser.add_argument("--max-hands", type=int, default=1000, help="First N hands to export. Use 0 for all hands.")
    parser.add_argument("--keep-ids", action="store_true", help="Do not rewrite hand/table IDs")
    parser.add_argument("--keep-dates", action="store_true", help="Do not shift dates/times")
    args = parser.parse_args()

    try:
        stats = anonymize(
            input_path=Path(args.input), output_path=Path(args.output), hero=args.hero,
            max_hands=args.max_hands, rewrite_ids=not args.keep_ids, shift_dates=not args.keep_dates,
        )
    except ValueError as exc:
        parser.error(str(exc))

    print("Done.")
    for k, v in stats.items():
        print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
