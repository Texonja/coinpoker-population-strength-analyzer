#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CoinPoker PLO Population Strength Analyzer

Purpose:
  Measure what shown hands opponents have when they take specific lines.
  This is NOT a solver. It only uses hands where villain cards are known at showdown.

Default filters:
  - PLO4 regular only
  - BombPot excluded
  - relevant player stack >= 40bb and Hero stack >= 40bb
  - HU and multiway both included unless line requires clear heads-up context

Examples:
  python coinpoker_population_strength_analyzer.py --input cash.txt --hero Hero --outdir pop_strength --stake-filter PL50
  python coinpoker_population_strength_analyzer.py --input cash.txt --hero Hero --outdir pop_strength --min-known 10 --min-stack-bb 40

Reports include eligible action counts and the distribution of observed cards.
"""

from __future__ import annotations

import argparse
import collections
import csv
import sys
from datetime import date

import history_validation as validation
import strength_report
import itertools
import json
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__version__ = "0.2.0"

RANK_ORDER = "23456789TJQKA"
RANK_VALUE = {r: i for i, r in enumerate(RANK_ORDER, start=2)}
VALUE_RANK = {v: r for r, v in RANK_VALUE.items()}
MONEY_RE = re.compile(r"₮([0-9]+(?:\.[0-9]+)?)")
SPLASH_RE = re.compile(r"^(?:MEGA )?SPLASH dropped ₮([0-9.]+)")

ACTION_PATTERNS = [
    ("posts_ante", re.compile(r"^(.+?): posts ante ₮([0-9.]+)")),
    ("posts_sb", re.compile(r"^(.+?): posts small blind ₮([0-9.]+)")),
    ("posts_bb", re.compile(r"^(.+?): posts big blind ₮([0-9.]+)")),
    ("posts_auto_bb", re.compile(r"^(.+?): posts auto big blind ₮([0-9.]+)")),
    ("straddle", re.compile(r"^(.+?): STRADDLE ₮([0-9.]+)")),
    ("bets", re.compile(r"^(.+?): bets ₮([0-9.]+)")),
    ("calls", re.compile(r"^(.+?): calls ₮([0-9.]+)")),
    ("raises", re.compile(r"^(.+?): raises ₮([0-9.]+) to ₮([0-9.]+)")),
    ("allin", re.compile(r"^(.+?): ALLIN ₮([0-9.]+)")),
    ("return", re.compile(r"^(.+?): RETURN ₮([0-9.]+)")),
    ("collected", re.compile(r"^(.+?) collected ₮([0-9.]+) from pot")),
    ("folds", re.compile(r"^(.+?): folds")),
    ("checks", re.compile(r"^(.+?): checks")),
]

ACTION_MAP = {
    "bets": "bet",
    "calls": "call",
    "raises": "raise",
    "folds": "fold",
    "checks": "check",
}
AGGRO_ACTIONS = {"bet", "raise"}


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    return default if not b else a / b


def split_hand_history(text: str) -> List[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    return re.split(r"\n(?=CoinPoker Hand #)", text)


def parse_all_cards_from_brackets(text: str) -> List[str]:
    cards: List[str] = []
    for group in re.findall(r"\[([^\]]*)\]", text):
        cards.extend(group.split())
    return [c for c in cards if re.match(r"^[2-9TJQKA][cdhs]$", c)]


def parse_header(line: str) -> Optional[Dict[str, Any]]:
    m = re.match(r"CoinPoker Hand #(\d+): (.*?) \(([^)]+)\) ([0-9/]+ [0-9:]+) (\w+)", line)
    if not m:
        return None
    hand_id, desc, stakes_str, dt_str, tz = m.groups()
    nums = [float(x) for x in re.findall(r"([0-9]+(?:\.[0-9]+)?)", stakes_str)]
    sb = nums[0] if len(nums) > 0 else None
    bb = nums[1] if len(nums) > 1 else None
    third = nums[2] if len(nums) > 2 else None
    plo_match = re.search(r"PLO\s+(\d+)", desc)
    plo_cards = int(plo_match.group(1)) if plo_match else None
    is_bombpot = "BombPot" in desc
    return {
        "hand_id": hand_id,
        "desc": desc,
        "stakes_str": stakes_str,
        "dt": dt_str,
        "tz": tz,
        "sb": sb,
        "bb": bb,
        "ante": None if is_bombpot else third,
        "bomb_amount": third if is_bombpot else None,
        "plo_cards": plo_cards,
        "is_bombpot": is_bombpot,
        "stake_label": f"PL{int(round(bb * 100))}" if bb else "UNKNOWN",
    }


def assign_positions(seats: Dict[int, str], button_seat: Optional[int]) -> Dict[str, str]:
    if not seats or button_seat not in seats:
        return {}
    seat_nums = sorted(seats.keys())
    bidx = seat_nums.index(button_seat)
    order = [seat_nums[(bidx + i) % len(seat_nums)] for i in range(len(seat_nums))]
    n = len(order)
    labels_by_n = {
        2: ["BTN/SB", "BB"],
        3: ["BTN", "SB", "BB"],
        4: ["BTN", "SB", "BB", "CO"],
        5: ["BTN", "SB", "BB", "UTG", "CO"],
        6: ["BTN", "SB", "BB", "UTG", "HJ", "CO"],
        7: ["BTN", "SB", "BB", "UTG", "UTG1", "HJ", "CO"],
        8: ["BTN", "SB", "BB", "UTG", "UTG1", "MP", "HJ", "CO"],
        9: ["BTN", "SB", "BB", "UTG", "UTG1", "MP1", "MP2", "HJ", "CO"],
    }
    labels = labels_by_n.get(n, ["BTN"] + [f"P{i}" for i in range(1, n)])
    return {seats[seat]: labels[i] for i, seat in enumerate(order)}


def postflop_order_players(seats: Dict[int, str], button_seat: Optional[int]) -> List[str]:
    if button_seat is None:
        return []
    if button_seat not in seats:
        return [seats[i] for i in sorted(seats, key=lambda seat: (seat <= button_seat, seat))]
    seat_nums = sorted(seats.keys())
    bidx = seat_nums.index(button_seat)
    order = [seat_nums[(bidx + i) % len(seat_nums)] for i in range(1, len(seat_nums))] + [button_seat]
    return [seats[s] for s in order]


def classify_starting_hand(cards: List[str]) -> Dict[str, Any]:
    if not cards:
        return {"cards": "", "hand_class": "unknown", "double_suited": False, "connected_window": 0, "high_cards": 0, "broadways": 0, "pairs": 0, "ace": False}
    ranks = [c[0] for c in cards]
    suits = [c[1] for c in cards]
    rank_counts = collections.Counter(ranks)
    suit_counts = collections.Counter(suits)
    vals = sorted([RANK_VALUE.get(r, 0) for r in ranks], reverse=True)
    uniq = set(vals) | ({1} if 14 in vals else set())
    pairs = sum(1 for v in rank_counts.values() if v == 2)
    trips = any(v >= 3 for v in rank_counts.values())
    high = sum(1 for v in vals if v >= 11)
    broadway = sum(1 for v in vals if v >= 10)
    ace = "A" in ranks
    double_suited = sum(1 for v in suit_counts.values() if v >= 2) >= 2
    single_suited = any(v >= 2 for v in suit_counts.values())
    best_window = 0
    for low in range(1, 11):
        best_window = max(best_window, sum(1 for v in uniq if low <= v <= low + 4))
    if rank_counts.get("A", 0) >= 2:
        hand_class = "AAxx"
    elif rank_counts.get("K", 0) >= 2:
        hand_class = "KKxx"
    elif trips:
        hand_class = "trips_in_hand"
    elif pairs >= 1:
        hand_class = "pair"
    elif best_window >= 4:
        hand_class = "rundown"
    elif broadway >= 3:
        hand_class = "broadway"
    elif any(c[0] == "A" and suit_counts[c[1]] >= 2 for c in cards):
        hand_class = "suited_ace"
    else:
        hand_class = "other"
    return {
        "cards": " ".join(cards),
        "hand_class": hand_class,
        "double_suited": double_suited,
        "single_suited": single_suited,
        "connected_window": best_window,
        "high_cards": high,
        "broadways": broadway,
        "pairs": pairs,
        "ace": ace,
        "has_aa": rank_counts["A"] >= 2,
        "has_kk": rank_counts["K"] >= 2,
        "suited_ace": any(c[0] == "A" and suit_counts[c[1]] >= 2 for c in cards),
    }


def board_bucket(cards: List[str]) -> str:
    if not cards:
        return "no_board"
    ranks = [c[0] for c in cards]
    suits = [c[1] for c in cards]
    vals = [RANK_VALUE.get(r, 0) for r in ranks]
    rank_counts = collections.Counter(ranks)
    suit_counts = collections.Counter(suits)
    paired = any(v >= 2 for v in rank_counts.values())
    monotone = len(cards) >= 3 and len(set(suits[:3])) == 1
    flush_possible = max(suit_counts.values()) >= 3 if suit_counts else False
    broadways = sum(1 for v in vals if v >= 10)
    uniq = set(vals) | ({1} if 14 in vals else set())
    best_window = 0
    for low in range(1, 11):
        best_window = max(best_window, sum(1 for v in uniq if low <= v <= low + 4))
    wet_score = 0
    if monotone or flush_possible:
        wet_score += 2
    elif len(set(suits[:3])) == 2:
        wet_score += 1
    if best_window >= 4:
        wet_score += 2
    elif best_window == 3:
        wet_score += 1
    if paired:
        return "paired"
    if monotone:
        return "monotone"
    if wet_score >= 3 or (len(cards) == 3 and len(suit_counts) == 2 and best_window == 3):
        return "wet"
    if broadways >= 2:
        return "high"
    if wet_score <= 1:
        return "dry"
    return "medium"


@dataclass
class Event:
    street: str
    player: str
    action: str
    raw_action: str
    amount: float = 0.0
    pot_before: float = 0.0
    size_pot_pct: float = math.nan
    idx: int = 0
    raises_before: int = 0
    size_player_funded_pct: float = math.nan
    chips_pushed_fraction: float = math.nan
    call_due: float = 0.0
    full_raise: bool = False


@dataclass
class Hand:
    meta: Dict[str, Any]
    seats: Dict[int, str]
    positions: Dict[str, str]
    postflop_order: List[str]
    stacks: Dict[str, float]
    shown_cards: Dict[str, List[str]]
    hero_cards: List[str]
    boards: Dict[str, List[str]]
    events: Dict[str, List[Event]]
    active_at_street_start: Dict[str, List[str]]
    preflop_raises: int
    last_preflop_raiser: str
    pot_type: str
    second_board: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def hand_id(self) -> str:
        return str(self.meta.get("hand_id", ""))

    @property
    def bb(self) -> float:
        return float(self.meta.get("bb") or 0.0)

    @property
    def stake_label(self) -> str:
        return str(self.meta.get("stake_label", ""))

    @property
    def desc(self) -> str:
        return str(self.meta.get("desc", ""))

    @property
    def hero_position(self) -> str:
        return str(self.meta.get("hero_position", ""))

    def stack_bb(self, player: str) -> float:
        return safe_div(float(self.stacks.get(player, 0.0)), self.bb, 0.0)

    def board_for_street(self, street: str) -> List[str]:
        if street == "FLOP":
            return self.boards.get("FLOP", [])[:3]
        if street == "TURN":
            return self.boards.get("TURN", self.boards.get("FLOP", []))[:4]
        if street == "RIVER":
            return self.boards.get("RIVER", self.boards.get("TURN", self.boards.get("FLOP", [])))[:5]
        return []


def parse_hand(hand_text: str, hero: str = "Hero", *, verify_actions=False) -> Optional[Hand]:
    lines = hand_text.splitlines()
    if not lines:
        return None
    meta = parse_header(lines[0])
    if not meta:
        return None
    meta["double_board_bombpot"] = meta["is_bombpot"] and (
        "Hand was run with two boards" in lines or "*** SECOND FLOP ***" in hand_text
        and "Hand was run two times" not in lines
    )

    button_match = re.search(r"Seat #(\d+) is the button", hand_text)
    button_seat = int(button_match.group(1)) if button_match else None
    seats: Dict[int, str] = {}
    stacks: Dict[str, float] = {}
    for line in lines:
        m = re.match(r"Seat (\d+): (.+?) \(₮([0-9.]+) in chips\)", line)
        if m:
            seat = int(m.group(1))
            name = m.group(2)
            seats[seat] = name
            stacks[name] = float(m.group(3))
    if hero not in seats.values():
        return None

    positions = assign_positions(seats, button_seat)
    pf_order = postflop_order_players(seats, button_seat)
    meta["hero_position"] = positions.get(hero, "")
    meta["n_players_dealt"] = len(seats)

    hero_cards: List[str] = []
    for line in lines:
        if line.startswith(f"Dealt to {hero} "):
            hero_cards = parse_all_cards_from_brackets(line)
            break
    meta.update({"hero_" + k: v for k, v in classify_starting_hand(hero_cards).items()})

    shown_cards: Dict[str, List[str]] = {}
    for line in lines:
        m = re.match(r"^(.+?): shows \[([^\]]+)\]", line)
        if m:
            shown_cards[m.group(1)] = parse_all_cards_from_brackets("[" + m.group(2) + "]")
        m2 = re.match(r"^Seat \d+: (.+?) showed \[([^\]]+)\]", line)
        if m2:
            shown_cards[m2.group(1)] = parse_all_cards_from_brackets("[" + m2.group(2) + "]")

    street = "PREFLOP"
    street_contrib: Dict[str, float] = collections.defaultdict(float)
    committed: Dict[str, float] = collections.defaultdict(float)
    active = set(seats.values())
    active_at_street_start: Dict[str, List[str]] = {"PREFLOP": list(active)}
    boards: Dict[str, List[str]] = {}
    second_board: Dict[str, List[str]] = {}
    events: Dict[str, List[Event]] = {"PREFLOP": [], "FLOP": [], "TURN": [], "RIVER": []}

    min_raise = meta["bb"] or 0.0
    pot = 0.0
    splash = 0.0
    preflop_raises = 0
    last_preflop_raiser = ""
    event_idx = 0
    raises_by_street = collections.defaultdict(int)

    for line in lines[1:]:
        drop = SPLASH_RE.match(line)
        if drop:
            amount = float(drop[1])
            splash += amount
            pot += amount
            continue
        sm = re.match(r"\*\*\* (?:(FIRST|SECOND|THIRD) )?(FLOP|TURN|RIVER) \*\*\* (.*)", line)
        if sm:
            marker, new_street, rest = sm.groups()
            min_raise = meta["bb"] or 0.0
            street = new_street
            if marker in (None, "FIRST"):
                boards[new_street] = parse_all_cards_from_brackets(rest)
                active_at_street_start[new_street] = list(active)
            elif marker == "SECOND" and meta["double_board_bombpot"]:
                second_board[new_street] = parse_all_cards_from_brackets(rest)
            street_contrib = collections.defaultdict(float)
            continue

        if line.startswith("Board [") and "RIVER" not in boards:
            cards = parse_all_cards_from_brackets(line)
            if len(cards) >= 3:
                boards.setdefault("FLOP", cards[:3])
            if len(cards) >= 4:
                boards.setdefault("TURN", cards[:4])
            if len(cards) >= 5:
                boards.setdefault("RIVER", cards[:5])

        for raw_action, regex in ACTION_PATTERNS:
            m = regex.match(line)
            if not m:
                continue
            player = m.group(1)
            amount = 0.0
            total_to: Optional[float] = None
            if raw_action in ("posts_ante", "posts_sb", "posts_bb", "posts_auto_bb", "straddle", "bets", "calls", "allin", "return", "collected"):
                amount = float(m.group(2))
            elif raw_action == "raises":
                total_to = float(m.group(3))
                amount = max(0.0, total_to - street_contrib[player])

            pot_before = pot
            action = ACTION_MAP.get(raw_action)
            highest = max(street_contrib.values(), default=0.0)
            if street == "PREFLOP":
                highest = max(highest, meta["bb"] or 0)
            call_due = max(0.0, highest - street_contrib[player])
            if raw_action == "allin":
                if street_contrib[player] + amount <= highest + 1e-9:
                    action = "call"
                else:
                    action = "raise" if highest else "bet"
            if raw_action == "posts_auto_bb" and meta["ante"] and amount == meta["ante"] and meta["bb"]:
                # CoinPoker can print the ante here even though it posts a live BB.
                amount = min(meta["bb"], max(0.0, stacks[player] - meta["ante"]))
            if verify_actions and action:
                if player not in active:
                    raise ValueError("action_after_fold")
                if action in {"bet", "call", "raise"} and amount <= 0:
                    raise ValueError("nonpositive_wager")
                if ((action == "check" and call_due > 1e-8) or
                    (action == "bet" and highest > 1e-8) or
                    (action == "call" and (call_due <= 1e-8 or amount > call_due + 1e-8)) or
                    (action == "raise" and amount <= call_due + 1e-8)):
                    raise ValueError("action_disagrees_with_live_wager")
                if raw_action == "raises" and abs(float(m[2]) - (amount - call_due)) > 1e-8:
                    raise ValueError("inconsistent_raise_amount")
                if action == "call" and amount < call_due - 1e-8 and committed[player] + amount < stacks[player] - 1e-8:
                    raise ValueError("short_call_with_chips_behind")
            if raw_action in ("posts_ante",):
                pot += amount
            elif raw_action in ("posts_sb", "posts_bb", "posts_auto_bb", "straddle", "bets", "calls", "allin"):
                street_contrib[player] += amount
                pot += amount
            elif raw_action == "raises":
                street_contrib[player] = total_to or street_contrib[player]
                pot += amount
            elif raw_action == "return":
                street_contrib[player] = max(0.0, street_contrib[player] - amount)
                pot -= amount
            elif raw_action == "folds":
                active.discard(player)

            committed[player] += pot - pot_before
            if verify_actions and (committed[player] > stacks.get(player, 0) + 1e-8 or committed[player] < -1e-8):
                raise ValueError("wager_exceeds_available_chips")
            if raw_action == "straddle":
                min_raise = max(min_raise, street_contrib[player])
            full_raise = action == "raise" and amount - call_due + 1e-9 >= min_raise
            if full_raise or action == "bet":
                min_raise = max(min_raise, amount - call_due)
            raises_before = raises_by_street[street]
            if action == "raise":
                raises_by_street[street] += 1
                if street == "PREFLOP":
                    preflop_raises += 1
                    last_preflop_raiser = player

            if action:
                pushed = safe_div(amount, pot_before, default=math.nan)
                call_part = call_due if action == "raise" else 0.0
                wager = amount - call_part
                size_pct = safe_div(wager, pot_before + call_part, default=math.nan)
                funded_pct = safe_div(wager, pot_before - splash + call_part, default=math.nan)
                event_idx += 1
                events[street].append(Event(street, player, action, raw_action, amount, pot_before,
                                            size_pct, event_idx, raises_before, funded_pct, pushed, call_due, full_raise))
            break

    meta["gross_pot"] = round(pot, 2)
    meta["splash_drop"] = splash
    meta["straddled"] = any(": STRADDLE " in line for line in lines)
    if meta.get("is_bombpot"):
        pot_type = "bombpot"
    elif preflop_raises == 0:
        pot_type = "limped"
    elif preflop_raises == 1:
        pot_type = "srp"
    elif preflop_raises == 2:
        pot_type = "3bet"
    else:
        pot_type = "4bet+"

    return Hand(meta=meta, seats=seats, positions=positions, postflop_order=pf_order, stacks=stacks, shown_cards=shown_cards, hero_cards=hero_cards, boards=boards, events=events, active_at_street_start=active_at_street_start, preflop_raises=preflop_raises, last_preflop_raiser=last_preflop_raiser, pot_type=pot_type, second_board=second_board)


# -------------------- PLO hand strength / draw classification --------------------

def straight_high(vals: List[int]) -> int:
    s = set(vals)
    if 14 in s:
        s.add(1)
    best = 0
    for high in range(14, 4, -1):
        if all(v in s for v in range(high - 4, high + 1)):
            best = high
            break
    return best


def eval_5(cards: List[str]) -> Tuple[int, List[int], str]:
    values = tuple(sorted((RANK_VALUE[c[0]] for c in cards), reverse=True))
    category, tiebreak, name = five_key(values, len({c[1] for c in cards}) == 1)
    return category, list(tiebreak), name


@lru_cache(maxsize=16384)
def five_key(vals, is_flush):
    counts = collections.Counter(vals)
    by_count = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    sh = straight_high(vals)
    if is_flush and sh:
        return (8, (sh,), "straight_flush")
    if by_count[0][1] == 4:
        quad = by_count[0][0]
        kicker = max(v for v in vals if v != quad)
        return (7, (quad, kicker), "quads")
    if by_count[0][1] == 3 and by_count[1][1] == 2:
        return (6, (by_count[0][0], by_count[1][0]), "full_house")
    if is_flush:
        return (5, vals, "flush")
    if sh:
        return (4, (sh,), "straight")
    if by_count[0][1] == 3:
        trip = by_count[0][0]
        kickers = sorted([v for v in vals if v != trip], reverse=True)
        return (3, (trip, *kickers), "trips_or_set")
    pairs = sorted([v for v, c in counts.items() if c == 2], reverse=True)
    if len(pairs) >= 2:
        kicker = max(v for v in vals if v not in pairs[:2])
        return (2, (*pairs[:2], kicker), "two_pair")
    if len(pairs) == 1:
        pair = pairs[0]
        kickers = sorted([v for v in vals if v != pair], reverse=True)
        return (1, (pair, *kickers), "one_pair")
    return (0, vals, "high_card")


def best_plo_hand(hole: List[str], board: List[str]) -> Tuple[int, List[int], str, List[str]]:
    if len(hole) < 2 or len(board) < 3:
        return (-1, [], "unknown", [])
    best = (-1, [], "unknown", [])
    for hc in itertools.combinations(hole, 2):
        for bc in itertools.combinations(board, 3):
            rank = eval_5(list(hc) + list(bc))
            key = (rank[0], rank[1])
            best_key = (best[0], best[1])
            if key > best_key:
                best = (rank[0], rank[1], rank[2], list(hc) + list(bc))
    return best


def has_flush_draw(hole: List[str], board: List[str], dead=()) -> Tuple[bool, bool, str]:
    if len(board) not in (3, 4):
        return False, False, ""
    first_draw = ""
    for suit, count in collections.Counter(c[1] for c in board).items():
        suited = [c for c in hole if c[1] == suit]
        if count != 2 or len(suited) < 2:
            continue
        unavailable = set(board) | set(dead)
        available = [r + suit for r in reversed(RANK_ORDER) if r + suit not in unavailable]
        if not any(c not in suited for c in available):
            continue
        if available[0] in suited:
            return True, True, suit
        first_draw = first_draw or suit
    return bool(first_draw), False, first_draw


STRAIGHT_WINDOWS = {high: frozenset(range(high - 4, high + 1)) for high in range(6, 15)}
STRAIGHT_WINDOWS[5] = frozenset((14, 2, 3, 4, 5))


def straight_outs(hole: List[str], board: List[str], dead=()) -> int:
    """Count distinct available next cards completing a straight with two hole cards."""
    if len(board) not in (3, 4):
        return 0
    board_ranks = {RANK_VALUE[c[0]] for c in board}
    hole_ranks = {RANK_VALUE[c[0]] for c in hole}
    outs = set()
    for pair in itertools.combinations(hole_ranks, 2):
        for window in STRAIGHT_WINDOWS.values():
            if not window.issuperset(pair):
                continue
            missing = window.difference(pair, board_ranks)
            if not missing:
                return 0  # Already a straight. Redraws are a separate statistic.
            if len(missing) == 1:
                outs.update(missing)
    unavailable = collections.Counter(RANK_VALUE[c[0]] for c in (*hole, *board, *dead))
    return sum(4 - unavailable[r] for r in outs)


straight_outs_approx = straight_outs  # Historical import name.


def made_group(made_name: str, board: List[str]) -> str:
    if made_name in {"straight_flush", "quads", "full_house"}:
        return "boat_plus"
    if made_name == "flush":
        return "flush"
    if made_name == "straight":
        return "straight"
    if made_name == "trips_or_set":
        # On paired board trips may be trips; on unpaired board it is a set.
        return "set_or_trips"
    if made_name == "two_pair":
        return "two_pair"
    if made_name == "one_pair":
        return "one_pair"
    return "weak_or_air"



def is_board_paired(board: List[str]) -> bool:
    return any(v >= 2 for v in collections.Counter(c[0] for c in board).values())


def board_suit_counts(board: List[str]) -> collections.Counter:
    return collections.Counter(c[1] for c in board)


def has_board_flush_possible(board: List[str]) -> bool:
    return bool(board) and max(board_suit_counts(board).values() or [0]) >= 3


def flush_newly_completed_on_turn(board4: List[str]) -> bool:
    if len(board4) < 4:
        return False
    flop = board4[:3]
    return max(board_suit_counts(flop).values() or [0]) < 3 and max(board_suit_counts(board4).values() or [0]) >= 3


def flush_newly_completed_on_river(board5: List[str]) -> bool:
    if len(board5) < 5:
        return False
    turn = board5[:4]
    return max(board_suit_counts(turn).values() or [0]) < 3 and max(board_suit_counts(board5).values() or [0]) >= 3


def full_deck() -> List[str]:
    return [r + s for r in RANK_ORDER for s in "cdhs"]


@lru_cache(maxsize=256)
def opponent_river_keys(board, hole, dead=()):
    """Best Omaha rank of each legal opposing pair, including suits and blockers."""
    remaining = [c for c in full_deck() if c not in board and c not in hole and c not in dead]
    triples = [(tuple(RANK_VALUE[c[0]] for c in t), t[0][1] if len({c[1] for c in t}) == 1 else "")
               for t in itertools.combinations(board, 3)]
    ranks = {}
    flush_ranks = {}
    result = set()
    for left, right in itertools.combinations(remaining, 2):
        pair = (RANK_VALUE[left[0]], RANK_VALUE[right[0]])
        if pair not in ranks:
            ranks[pair] = max(five_key(tuple(sorted(pair + t, reverse=True)), False)[:2] for t, _ in triples)
        best = ranks[pair]
        if left[1] == right[1]:
            key = (pair, left[1])
            if key not in flush_ranks:
                flush_ranks[key] = max((five_key(tuple(sorted(pair + t, reverse=True)), True)[:2]
                                        for t, suit in triples if suit == left[1]), default=best)
            best = max(best, flush_ranks[key])
        result.add(best)
    return frozenset(result)


RIVER_PLO_CLASS_ORDER = [
    "practical_nuts",
    "close_to_nuts",
    "second_third_nut_flush",
    "medium_flush",
    "low_flush",
    "weak_straight",
    "other_made_top_two_or_better",
    "other_two_pair_or_less",
]

RIVER_PLO_CLASS_LABELS = {
    "practical_nuts": "Practical nuts",
    "close_to_nuts": "Close to nuts",
    "second_third_nut_flush": "Second / third nut flush",
    "medium_flush": "Medium flush",
    "low_flush": "Low flush",
    "weak_straight": "Weak straight",
    "other_made_top_two_or_better": "Other made hand, top two or better",
    "other_two_pair_or_less": "Other two pair, one pair or high card",
}

def is_top_two_pair(best, board, hole):
    if best[0] != 2:
        return False
    top = sorted({RANK_VALUE[c[0]] for c in board}, reverse=True)[:2]
    return len(top) == 2 and best[1][:2] == top and set(top) <= {RANK_VALUE[c[0]] for c in hole}


def classify_river_plo_display_class(hole, board, best):
    actual = (best[0], tuple(best[1]))
    possible = opponent_river_keys(tuple(board), tuple(sorted(hole)))
    beaten_by = {rank for rank in possible if rank > actual}
    ordinary = {rank for rank in beaten_by if rank[0] < 7}
    cat = best[0]
    if cat >= 7 or not ordinary:
        cls = "practical_nuts"
        reason = "Quads or straight flush" if cat >= 7 else "No stronger ordinary opposing hand"
    elif cat == 6:
        cls, reason = "close_to_nuts", "Full house below the top legal full house"
    elif cat == 5 and not is_board_paired(board):
        suit = best[3][0][1]
        board_part = sorted((RANK_VALUE[c[0]] for c in board if c[1] == suit), reverse=True)[:3]
        higher = set()
        for category, value in ordinary:
            if category == 5:
                private = list(value)
                for rank in board_part:
                    private.remove(rank)
                higher.add(max(private))
        tier = len(higher) + 1
        cls = "second_third_nut_flush" if tier <= 3 else "medium_flush" if tier <= 6 else "low_flush"
        reason = f"Flush tier {tier} by highest attainable suited hole card"
    elif cat == 4 and all(r[0] == 4 for r in ordinary):
        cls = "close_to_nuts" if len(ordinary) == 1 else "weak_straight"
        reason = f"Straight tier {len(ordinary) + 1}"
    elif cat == 3 and all(r[0] == 3 for r in ordinary) and not is_board_paired(board):
        cls, reason = "close_to_nuts", "Set on a board with no higher ordinary hand category"
    elif cat >= 3 or is_top_two_pair(best, board, hole):
        cls = "other_made_top_two_or_better"
        reason = "Top two pair" if cat == 2 else best[2]
    else:
        cls, reason = "other_two_pair_or_less", best[2]
    return {"river_plo_class": cls, "river_plo_class_label": RIVER_PLO_CLASS_LABELS[cls],
            "river_plo_class_reason": reason, "river_unbeaten": not beaten_by}


def classify_river_relative_strength(hole, board, best):
    result = classify_river_plo_display_class(hole, board, best)
    return {"river_practical_value_class": result["river_plo_class"],
            "river_practical_value_reason": result["river_plo_class_reason"]}


def bet_size_bucket(size_pct: Any) -> str:
    try:
        x = float(size_pct)
    except Exception:
        return "unknown_size"
    if math.isnan(x):
        return "unknown_size"
    x = round(x, 10)
    if x < 0.40:
        return "vs_small_bet_<40"
    if x < 0.70:
        return "vs_half_bet_40-70"
    if x < 1.15:
        return "vs_large_pot_bet_70-115"
    return "vs_overbet_115_plus"


def previous_bet_on_street(events: List[Event], ev: Event) -> Optional[Event]:
    prev = [e for e in events if e.idx < ev.idx and e.action in AGGRO_ACTIONS]
    return prev[-1] if prev else None

def classify_street_strength(hole: List[str], board: List[str], street: str) -> Dict[str, Any]:
    rank_cat, tiebreak, made_name, best_cards = best_plo_hand(hole, board)
    fd, nut_fd, fd_suit = has_flush_draw(hole, board)
    sd_outs = straight_outs(hole, board) if street in {"FLOP", "TURN"} else 0
    good_sd = sd_outs >= 8
    made = made_group(made_name, board)

    if street in {"FLOP", "TURN"}:
        if made in {"boat_plus", "set_or_trips"}:
            bucket = "set_trips_boat_plus"
        elif made in {"straight", "flush"}:
            bucket = "made_straight_or_flush"
        elif made == "two_pair":
            bucket = "two_pair"
        elif made == "one_pair" and (fd or good_sd):
            bucket = "pair_plus_good_draw"
        elif made == "weak_or_air" and (fd or good_sd):
            bucket = "unpaired_completion_draw"
        elif made == "one_pair":
            bucket = "one_pair_no_good_draw"
        else:
            bucket = "weak_or_air"
    else:
        if made in {"boat_plus"}:
            bucket = "river_boat_plus"
        elif made == "flush":
            bucket = "river_flush"
        elif made == "straight":
            bucket = "river_straight"
        elif made in {"set_or_trips", "two_pair"}:
            bucket = "river_medium_value"
        else:
            bucket = "river_one_pair_or_high_card"

    out = {
        "made_name": made_name,
        "made_group": made,
        "strength_bucket": bucket,
        "best_cards": " ".join(best_cards),
        "has_flush_draw": fd,
        "has_nut_flush_draw": nut_fd,
        "flush_draw_suit": fd_suit,
        "straight_completion_cards": sd_outs,
        "straight_outs": sd_outs,
        "straight_outs_approx": sd_outs,
        "has_good_straight_draw": good_sd,
        "rank_category": rank_cat,
        "board_paired": is_board_paired(board),
        "board_flush_possible": has_board_flush_possible(board),
        "flush_completed_on_turn": flush_newly_completed_on_turn(board) if street == "TURN" else False,
        "flush_completed_on_river": flush_newly_completed_on_river(board) if street == "RIVER" else False,
    }
    if street == "RIVER":
        out.update(classify_river_plo_display_class(hole, board, (rank_cat, tiebreak, made_name, best_cards)))
        out["river_practical_value_class"] = out["river_plo_class"]
    else:
        out.update(river_practical_value_class="", river_plo_class="", river_plo_class_label="", river_plo_class_reason="")
    return out

# -------------------- line extraction --------------------

def first_event(events: List[Event], player: Optional[str] = None, actions: Optional[set] = None, after_idx: int = -1) -> Optional[Event]:
    for ev in events:
        if ev.idx <= after_idx:
            continue
        if player is not None and ev.player != player:
            continue
        if actions is not None and ev.action not in actions:
            continue
        return ev
    return None


def next_event_by_player(events: List[Event], player: str, after_event: Event) -> Optional[Event]:
    return first_event(events, player=player, after_idx=after_event.idx)


def previous_events(events: List[Event], ev: Event) -> List[Event]:
    return [e for e in events if e.idx < ev.idx]


def player_checked_before(events: List[Event], player: str, ev: Event) -> bool:
    own = [e for e in events if e.idx < ev.idx and e.player == player]
    return bool(own and own[-1].action == "check")


def player_bet_before(events: List[Event], player: str, ev: Event) -> bool:
    return any(e.idx < ev.idx and e.player == player and e.action == "bet" for e in events)


def postflop_acts_before(h: Hand, player_a: str, player_b: str) -> bool:
    """Return True if player_a is normally out of position to player_b postflop."""
    try:
        return h.postflop_order.index(player_a) < h.postflop_order.index(player_b)
    except ValueError:
        return False


def no_prior_aggression(events: List[Event], ev: Event) -> bool:
    return not any(e.idx < ev.idx and e.action in AGGRO_ACTIONS for e in events)


def no_prior_hero_action(events: List[Event], hero: str, ev: Event) -> bool:
    return not any(e.idx < ev.idx and e.player == hero and e.action in {"check", "bet", "call", "raise", "allin", "fold"} for e in events)


def get_preflop_raiser_order(h: Hand) -> List[Event]:
    return [e for e in h.events.get("PREFLOP", []) if e.action == "raise"]


def relevant_stack_ok(h: Hand, player: str, hero: str, min_stack_bb: float) -> bool:
    if h.stack_bb(player) + 1e-9 < min_stack_bb:
        return False
    if h.stack_bb(hero) + 1e-9 < min_stack_bb:
        return False
    return True


def postflop_record(h: Hand, street: str, ev: Event, hero: str) -> Optional[Dict[str, Any]]:
    villain = ev.player
    hole = h.shown_cards.get(villain, [])
    board = h.board_for_street(street)
    if len(hole) != h.meta["plo_cards"] or len(board) != {"FLOP": 3, "TURN": 4, "RIVER": 5}[street]:
        return
    st = classify_street_strength(hole, board, street)
    pre = classify_starting_hand(hole)
    return {
        "hand_id": h.hand_id,
        "dt": h.meta.get("dt", ""),
        "stake_label": h.stake_label,
        "desc": h.desc,
        "pot_type": h.pot_type,
        "street": street,
        "villain": villain,
        "villain_position": h.positions.get(villain, ""),
        "villain_stack_bb": round(h.stack_bb(villain), 1),
        "hero_position": h.positions.get(hero, ""),
        "hero_stack_bb": round(h.stack_bb(hero), 1),
        "villain_action": ev.action,
        "villain_all_in": ev.raw_action == "allin", "full_raise": ev.full_raise if ev.action == "raise" else "",
        "action_index": ev.idx,
        "splash_drop": h.meta["splash_drop"],
        "chips_pushed_pct_pot": round(ev.chips_pushed_fraction, 6),
        "action_size_pct_player_funded_pot": round(ev.size_player_funded_pct, 6),
        "action_size_pct_pot": round(ev.size_pot_pct, 6) if not math.isnan(ev.size_pot_pct) else "",
        "villain_cards": " ".join(hole),
        "villain_preflop_class": pre["hand_class"],
        "villain_double_suited": pre.get("double_suited", False),
        "villain_connected_window": pre.get("connected_window", 0),
        "board": " ".join(board),
        "board_bucket": board_bucket(board),
        **st,
    }


def postflop_line_variants(h: Hand, street: str, base_line_id: str, base_line_name: str, ev: Event):
    board = h.board_for_street(street)
    evs = h.events.get(street, [])
    prev_bet = previous_bet_on_street(evs, ev) if ev.action == "raise" else None
    extras = {
        "facing_action": prev_bet.action if prev_bet else "",
        "facing_size_pct_pot": round(prev_bet.size_pot_pct, 6) if prev_bet else "",
        "facing_bet_size_pct_pot": round(prev_bet.size_pot_pct, 6) if prev_bet and prev_bet.action == "bet" else "",
        "facing_bet_size_bucket": bet_size_bucket(prev_bet.size_pot_pct) if prev_bet and prev_bet.action == "bet" else "",
    }
    yield base_line_id, base_line_name, extras

    # Add separate rows for the board textures used in the report.
    bbucket = board_bucket(board)
    if base_line_id == "FLOP_check_raise":
        if is_board_paired(board):
            yield "FLOP_check_raise_paired_board", "Population flop check-raise on paired board", extras
        if bbucket in {"wet", "monotone"}:
            yield "FLOP_check_raise_wet_board", "Population flop check-raise on wet/monotone board", extras
    if base_line_id == "TURN_check_raise" and flush_newly_completed_on_turn(board):
        yield "TURN_check_raise_flush_completed", "Population turn check-raise when flush completes", extras
    if base_line_id == "RIVER_reraise" and prev_bet:
        bucket = bet_size_bucket(prev_bet.size_pot_pct).replace("_bet_", "_raise_")
        yield f"RIVER_reraise_{bucket}", f"River re-raise facing raise ({bucket})", extras
    if base_line_id in {"RIVER_check_raise", "RIVER_raise_not_checkraise"}:
        if is_board_paired(board):
            yield "RIVER_raise_paired_board", "Population river raise on paired board", extras
        if flush_newly_completed_on_river(board):
            # Keep paired boards separate because full houses can beat a flush.
            if is_board_paired(board):
                yield "RIVER_raise_flush_completed_paired_board", "Population river raise when flush completes on paired board", extras
            else:
                yield "RIVER_raise_flush_completed_unpaired_board", "Population river raise when flush completes on unpaired board", extras
        if prev_bet:
            bucket = bet_size_bucket(prev_bet.size_pot_pct)
            yield f"RIVER_raise_{bucket}", f"Population river raise facing {bucket}", extras

    if base_line_id == "RIVER_oop_turn_call_flush_river_lead":
        if is_board_paired(board):
            yield "RIVER_oop_turn_call_flush_river_lead_paired_board", "Population OOP turn call then river flush-completing lead on paired board", extras
        else:
            yield "RIVER_oop_turn_call_flush_river_lead_unpaired_board", "Population OOP turn call then river flush-completing lead on unpaired board", extras

def wager_context(events, ev, hero):
    """Responses until the next wager. A raise closes this wager and starts its own."""
    facing = previous_bet_on_street(events, ev)
    hero_involved = ev.action == "raise" and facing is not None and facing.player == hero
    continued = False
    for response in events:
        if response.idx <= ev.idx:
            continue
        if response.action in {"fold", "call", "raise"} and response.player == hero:
            hero_involved = True
        if response.action in {"call", "raise"}:
            continued = True
        if response.action in AGGRO_ACTIONS:
            break
    return bool(hero_involved), continued


def track_action(counts, key, h, ev, continued):
    if counts is None:
        return
    if isinstance(counts, collections.Counter):  # Original public helper interface.
        counts[key] += 1
    else:
        counts[key][(h.hand_id, ev.idx)] = continued


def bombpot_records(h: Hand, hero: str, min_stack_bb: float, line_counts, vs_hero=False):
    records = []
    for street, board_size in (("FLOP", 3), ("TURN", 4), ("RIVER", 5)):
        board = h.boards.get(street, [])
        second = h.second_board.get(street, [])
        events = h.events[street]
        for ev in events:
            if ev.action not in AGGRO_ACTIONS or ev.player == hero:
                continue
            if not relevant_stack_ok(h, ev.player, hero, min_stack_bb):
                continue
            involves_hero, continued = wager_context(events, ev, hero)
            if vs_hero and not involves_hero:
                continue
            action = ev.action
            facing = previous_bet_on_street(events, ev)
            if action == "raise" and facing and facing.action == "raise":
                action = "reraise"
            elif action == "raise" and player_checked_before(events, ev.player, ev):
                action = "check_raise"
            line_id = f"BOMBPOT_{street}_{action}"
            track_action(line_counts, line_id, h, ev, continued)
            hole = h.shown_cards.get(ev.player, [])
            if len(hole) != h.meta["plo_cards"] or len(board) != board_size:
                continue
            if h.meta["double_board_bombpot"] and len(second) != board_size:
                continue
            row = {
                "line_id": line_id, "hand_id": h.hand_id, "street": street,
                "pot_type": h.pot_type, "stake_label": h.stake_label, "action_index": ev.idx,
                "villain": ev.player, "villain_cards": " ".join(hole), "villain_action": ev.action,
                "villain_all_in": ev.raw_action == "allin", "full_raise": ev.full_raise if ev.action == "raise" else "", "action_size_pct_pot": round(ev.size_pot_pct, 6),
                "board_count": 2 if second else 1, "vs_hero": involves_hero,
                "called_or_raised": continued, "facing_action": facing.action if facing else "",
                "splash_drop": h.meta["splash_drop"],
                "chips_pushed_pct_pot": round(ev.chips_pushed_fraction, 6),
                "action_size_pct_player_funded_pot": round(ev.size_player_funded_pct, 6),
            }
            for index, (cards, other_board) in enumerate(((board, second), (second, board)), 1):
                if not cards:
                    continue
                best = best_plo_hand(hole, cards)
                prefix = f"board_{index}"
                row[prefix] = " ".join(cards)
                row[prefix + "_made"] = best[2]
                row[prefix + "_best_cards"] = " ".join(best[3])
                row[prefix + "_straight_outs"] = straight_outs(hole, cards, other_board)
                row[prefix + "_straight_completion_cards"] = row[prefix + "_straight_outs"]
                row[prefix + "_flush_draw"] = has_flush_draw(hole, cards, other_board)[0]
                if street == "RIVER":
                    opponent = opponent_river_keys(tuple(cards), tuple(sorted(hole)), tuple(sorted(other_board)))
                    row[prefix + "_unbeaten"] = max(opponent) <= (best[0], tuple(best[1]))
            records.append(row)
    return records


def extract_population_records(h: Hand, hero: str, min_stack_bb: float, line_counts=None, vs_hero=False) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    post: List[Dict[str, Any]] = []
    pre: List[Dict[str, Any]] = []

    def add_post(street, line_id, line_name, ev):
        involves_hero, continued = wager_context(h.events[street], ev, hero)
        if vs_hero and not involves_hero:
            return
        row = postflop_record(h, street, ev, hero)
        if row is not None:
            row.update(vs_hero=involves_hero, called_or_raised=continued)
        for key, name, extras in postflop_line_variants(h, street, line_id, line_name, ev):
            track_action(line_counts, key, h, ev, continued)
            if row is not None:
                post.append({"line_id": key, "line_name": name, **row, **extras})

    if h.meta["is_bombpot"]:
        return bombpot_records(h, hero, min_stack_bb, line_counts, vs_hero), []

    # Match each eligible action before checking whether its cards were shown.
    raises = get_preflop_raiser_order(h)
    for i, ev in enumerate(raises):
        if ev.player == hero:
            continue
        if not relevant_stack_ok(h, ev.player, hero, min_stack_bb):
            continue
        involves_hero, continued = wager_context(h.events["PREFLOP"], ev, hero)
        if vs_hero and not involves_hero:
            continue
        if i == 0 and any(e.action == "call" and e.idx < ev.idx for e in h.events["PREFLOP"]):
            line_id = "PFR_iso_shown"
            line_name = "First raise after a limp"
        elif i == 0:
            line_id = "PFR_open_shown"
            line_name = "First-in preflop raise"
        elif i == 1:
            line_id = "PFR_3bet_shown"
            line_name = "Preflop 3-bet"
        else:
            line_id = "PFR_4bet_plus_shown"
            line_name = "Preflop 4-bet or higher"
        hole = h.shown_cards.get(ev.player, [])
        track_action(line_counts, line_id, h, ev, continued)
        if len(hole) != h.meta["plo_cards"]:
            continue
        c = classify_starting_hand(hole)
        pre.append({
            "line_id": line_id,
            "line_name": line_name,
            "hand_id": h.hand_id,
            "dt": h.meta.get("dt", ""),
            "stake_label": h.stake_label,
            "desc": h.desc,
            "pot_type": h.pot_type,
            "villain": ev.player,
            "villain_action": ev.action,
            "villain_all_in": ev.raw_action == "allin", "full_raise": ev.full_raise if ev.action == "raise" else "",
            "action_index": ev.idx,
            "splash_drop": h.meta["splash_drop"],
            "chips_pushed_pct_pot": round(ev.chips_pushed_fraction, 6),
            "action_size_pct_player_funded_pot": round(ev.size_player_funded_pct, 6),
            "villain_position": h.positions.get(ev.player, ""),
            "villain_stack_bb": round(h.stack_bb(ev.player), 1),
            "hero_stack_bb": round(h.stack_bb(hero), 1),
            "action_size_pct_pot": round(ev.size_pot_pct, 6) if not math.isnan(ev.size_pot_pct) else "",
            "villain_cards": " ".join(hole),
            "vs_hero": involves_hero, "called_or_raised": continued,
            "hand_class": c["hand_class"],
            "double_suited": c.get("double_suited", False),
            "single_suited": c.get("single_suited", False),
            "connected_window": c.get("connected_window", 0),
            "high_cards": c.get("high_cards", 0),
            "broadways": c.get("broadways", 0),
            "pairs": c.get("pairs", 0),
            "ace": c.get("ace", False),
            "suited_ace": c["suited_ace"],
            "wager_number": i + 2,
            "straddled": h.meta["straddled"],
            "has_aa": c["has_aa"],
            "has_kk": c["has_kk"],
        })

    # Postflop population lines with shown villain hand.
    for street in ["FLOP", "TURN", "RIVER"]:
        evs = h.events.get(street, [])
        if not evs:
            continue
        for ev in evs:
            if ev.player == hero:
                continue
            if not relevant_stack_ok(h, ev.player, hero, min_stack_bb):
                continue

            if ev.action == "raise":
                checked_before = player_checked_before(evs, ev.player, ev)
                facing = previous_bet_on_street(evs, ev)
                if facing and facing.action == "raise":
                    line_id = f"{street}_reraise"
                    line_name = f"{street.title()} re-raise"
                elif checked_before:
                    if street == "FLOP":
                        line_id = "FLOP_check_raise"
                        line_name = "Population flop check-raise"
                    elif street == "TURN":
                        line_id = "TURN_check_raise"
                        line_name = "Population turn check-raise"
                    else:
                        line_id = "RIVER_check_raise"
                        line_name = "Population river check-raise"
                else:
                    line_id = f"{street}_raise_not_checkraise"
                    line_name = f"Population {street.lower()} raise without prior check"
                add_post(street, line_id, line_name, ev)

            elif ev.action == "bet":
                if street in {"TURN", "RIVER"}:
                    prev_street = "FLOP" if street == "TURN" else "TURN"
                    prior = h.events[prev_street]
                    wagers = [e for e in prior if e.action in AGGRO_ACTIONS]
                    if prior and all(e.action == "check" for e in prior):
                        initiative = h.last_preflop_raiser
                        if street == "RIVER":
                            flop = [e for e in h.events["FLOP"] if e.action in AGGRO_ACTIONS]
                            initiative = flop[-1].player if flop else ""
                        before = any(e.player == initiative and e.idx < ev.idx for e in evs)
                        after = first_event(evs, player=initiative, after_idx=ev.idx) if initiative else None
                        if initiative == ev.player:
                            kind = "delayed_cbet" if street == "TURN" else "delayed_followup"
                        elif initiative and after and not before and postflop_acts_before(h, ev.player, initiative):
                            kind = "probe"
                        elif initiative and before:
                            kind = "bet_after_aggressor_checked"
                        elif not initiative and not h.last_preflop_raiser:
                            kind = "bet_in_limped_pot"
                        elif not initiative and street == "RIVER":
                            kind = "bet_after_two_checked_streets"
                        else:
                            kind = "bet_without_active_aggressor"
                        key = f"{street}_{kind}_after_{prev_street.lower()}_xx"
                        labels = {"probe": "OOP probe", "delayed_cbet": "Delayed continuation bet",
                                  "delayed_followup": "Delayed follow-up by the flop aggressor",
                                  "bet_after_aggressor_checked": "Bet after the prior aggressor checks",
                                  "bet_in_limped_pot": "Bet in a limped pot without prior aggression",
                                  "bet_after_two_checked_streets": "First postflop bet",
                                  "bet_without_active_aggressor": "Bet without an active prior aggressor"}
                        add_post(street, key, f"{street.title()} · {labels[kind]} after a checked {prev_street.lower()}", ev)
                    elif wagers and wagers[0].player == ev.player and wagers[0].action == "bet":
                        kind = "barrel" if len(wagers) == 1 else "bet_after_raised_bet"
                        key = f"{street}_{kind}_after_{prev_street.lower()}_bet"
                        name = "Barrel after an unraised bet" if len(wagers) == 1 else "New bet after facing a raise"
                        add_post(street, key, f"{street.title()} · {name} on the {prev_street.lower()}", ev)
                    elif wagers and wagers[-1].action == "raise" and wagers[-1].player == ev.player:
                        key = f"{street}_followup_after_{prev_street.lower()}_raise"
                        add_post(street, key, f"{street.title()} · Follow-up after raising the {prev_street.lower()}", ev)
                elif street == "FLOP":
                    raiser = h.last_preflop_raiser
                    if (raiser and ev.player != raiser and postflop_acts_before(h, ev.player, raiser) and no_prior_aggression(evs, ev)
                            and first_event(evs, player=raiser, after_idx=ev.idx)
                            and not any(e.player == raiser and e.idx < ev.idx for e in evs)):
                        add_post(street, "FLOP_donk_bet", "Population flop donk bet", ev)

    # Villain calls Hero's turn bet out of position, then leads a river that completes the flush.
    river_board = h.board_for_street("RIVER")
    if flush_newly_completed_on_river(river_board):
        turn_evs = h.events.get("TURN", [])
        river_evs = h.events.get("RIVER", [])
        for villain in h.seats.values():
            if villain == hero:
                continue
            if not relevant_stack_ok(h, villain, hero, min_stack_bb):
                continue
            if not postflop_acts_before(h, villain, hero):
                continue
            turn_call_vs_hero_bet = False
            for call_ev in [e for e in turn_evs if e.player == villain and e.action == "call"]:
                prev_bet = previous_bet_on_street(turn_evs, call_ev)
                if prev_bet and prev_bet.player == hero and prev_bet.action == "bet":
                    turn_call_vs_hero_bet = True
                    break
            if not turn_call_vs_hero_bet:
                continue
            river_lead = first_event(river_evs, player=villain, actions={"bet"})
            if river_lead and first_event(river_evs, player=hero, after_idx=river_lead.idx) and no_prior_aggression(river_evs, river_lead) and no_prior_hero_action(river_evs, hero, river_lead):
                add_post("RIVER", "RIVER_oop_turn_call_flush_river_lead", "Population OOP turn-call then river flush-completing lead", river_lead)

    # A triple barrel is an opening bet by the same player on each street.
    for villain in h.seats.values():
        if villain == hero:
            continue
        if not relevant_stack_ok(h, villain, hero, min_stack_bb):
            continue
        flop_bet = first_event(h.events.get("FLOP", []), player=villain, actions={"bet"})
        turn_bet = first_event(h.events.get("TURN", []), player=villain, actions={"bet"})
        river_bet = first_event(h.events.get("RIVER", []), player=villain, actions={"bet"})
        if (flop_bet and turn_bet and river_bet and not any(
                e.action == "raise" for s in ("FLOP", "TURN") for e in h.events[s])):
            add_post("RIVER", "RIVER_bet_bet_bet", "Population bet-bet-bet / triple barrel shown at river", river_bet)

    return post, pre

# -------------------- summarization / output --------------------

def write_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys = strength_report.csv_columns(path.stem, RIVER_PLO_CLASS_ORDER)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def pass_filters(h: Hand, args: argparse.Namespace) -> bool:
    gf = args.game_filter.lower()
    if gf == "plo4":
        if h.meta.get("plo_cards") != 4:
            return False
        if h.meta.get("is_bombpot") and not args.include_bombpot:
            return False
    elif gf == "plo5":
        if h.meta.get("plo_cards") != 5:
            return False
        if h.meta.get("is_bombpot") and not args.include_bombpot:
            return False
    elif gf == "bombpot":
        if not h.meta.get("is_bombpot"):
            return False
    elif gf == "all":
        if h.meta.get("is_bombpot") and not args.include_bombpot:
            return False
    else:
        raise ValueError(f"Unknown game filter: {args.game_filter}")

    if args.stake_filter:
        allowed = {x.strip().upper() for x in args.stake_filter.split(",") if x.strip()}
        if h.stake_label.upper() not in allowed:
            return False

    date_part = str(h.meta.get("dt", "")).split()[0].replace("/", "-")
    if args.date_from and date_part < args.date_from:
        return False
    if args.date_to and date_part > args.date_to:
        return False
    return True


def analyze(input_path: Path, hero: str, outdir: Path, args: argparse.Namespace) -> Dict[str, Any]:
    if not hero.strip() or not math.isfinite(args.min_stack_bb) or args.min_stack_bb < 0 or args.min_known < 1:
        raise ValueError("Hero must be nonempty, min-stack-bb finite and nonnegative, and min-known at least 1.")
    for value in (args.date_from, args.date_to):
        if value and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) or date.fromisoformat(value).isoformat() != value):
            raise ValueError("Dates must use YYYY-MM-DD.")
    if args.date_from and args.date_to and args.date_from > args.date_to:
        raise ValueError("date-from must be on or before date-to.")
    if args.stake_filter and not all(re.fullmatch(r"PL\d+", s.strip().upper()) for s in args.stake_filter.split(",")):
        raise ValueError("Stakes must be comma-separated PL labels, such as PL25,PL50.")

    raw = split_hand_history(input_path.read_text(encoding="utf-8-sig"))
    issues = collections.Counter()
    blocks, duplicates, conflicts = validation.unique_blocks(raw, issues)
    post_records, pre_records, bomb_records = [], [], []
    eligible = collections.defaultdict(dict)
    valid = matched = 0
    vs_hero = getattr(args, "vs_hero", False)
    for block in blocks:
        try:
            total = validation.validate(block, parse_header(block.splitlines()[0]), hero)
            hand = parse_hand(block, hero, verify_actions=True)
            if hand is None:
                raise ValueError("unreadable_hand")
            if abs(hand.meta["gross_pot"] - total) > 0.000001:
                raise ValueError("pot_disagrees_with_summary")
        except ValueError as exc:
            issues[str(exc)] += 1
            continue
        valid += 1
        if pass_filters(hand, args):
            matched += 1
            post, pre = extract_population_records(hand, hero, args.min_stack_bb, eligible, vs_hero)
            (bomb_records if hand.meta["is_bombpot"] else post_records).extend(post)
            pre_records.extend(pre)
    if not valid:
        raise ValueError(f"No complete supported hands for Hero '{hero}'. Skipped blocks: {dict(issues)}; identical duplicates: {duplicates}.")

    known = strength_report.group_rows(post_records + pre_records + bomb_records)
    coverage = strength_report.coverage_rows(eligible, known)
    post, pre, bomb = strength_report.summary_rows(known, coverage, args.min_known, RIVER_PLO_CLASS_ORDER)
    eligible_ids = {key for actions in eligible.values() for key in actions}
    all_known = post_records + bomb_records + pre_records
    filters = {
        "version": __version__, "input": str(input_path), "hero": hero, "game_filter": args.game_filter,
        "include_bombpot": bool(args.include_bombpot or args.game_filter == "bombpot"), "vs_hero": vs_hero,
        "action_scope": "Direct action against Hero" if vs_hero else "All opponents at Hero's table",
        "stake_filter": args.stake_filter or "all", "date_from": args.date_from or "any", "date_to": args.date_to or "any",
        "min_stack_bb": args.min_stack_bb, "min_known": args.min_known,
        "raw_hands_before_filters": len(raw), "valid_unique_hands": valid, "identical_duplicate_blocks": duplicates,
        "conflicting_hand_ids": conflicts, "input_issues": dict(issues), "parse_errors": sum(issues.values()),
        "hands_after_game_stake_date": matched, "hands_with_eligible_actions": len({k[0] for k in eligible_ids}),
        "hands_analyzed_after_filters": len({k[0] for k in eligible_ids}), "unique_eligible_actions": len(eligible_ids),
        "unique_known_actions": len({(r["hand_id"], r["action_index"]) for r in all_known}),
        "postflop_known_line_records": len(post_records) + len(bomb_records),
        "bombpot_known_line_records": len(bomb_records), "preflop_known_line_records": len(pre_records),
    }
    river = [r for r in post if r["line_id"].startswith("RIVER_")]
    early = [r for r in post if not r["line_id"].startswith("RIVER_")]
    files = {
        "population_strength_hands": post_records + bomb_records, "preflop_population_hands": pre_records,
        "line_card_coverage": coverage, "bombpot_population_by_line": bomb,
        "population_strength_by_line": post, "preflop_population_by_line": pre,
        "population_strength_summary_friendly": post, "preflop_population_summary_friendly": pre,
        "population_strength_summary_river_compact": river, "population_strength_summary_flop_turn_compact": early,
        "population_strength_summary_compact": post,
    }
    page, markdown = strength_report.report(filters, coverage, post, pre, bomb, RIVER_PLO_CLASS_LABELS)
    outputs = [outdir / (key + ".csv") for key in files]
    outputs += [outdir / (base + ext) for base in ("population_strength_presentation", "population_strength_report")
                for ext in (".md", ".html")] + [outdir / "filters_applied.json"]
    if any(path.resolve() == input_path.resolve() or (path.exists() and path.samefile(input_path)) for path in outputs):
        raise ValueError("An output filename collides with the input history.")
    outdir.mkdir(parents=True, exist_ok=True)
    for name, rows in files.items():
        write_csv(rows, outdir / (name + ".csv"))
    for name in ("population_strength_presentation", "population_strength_report"):
        (outdir / (name + ".html")).write_text(page, encoding="utf-8")
        (outdir / (name + ".md")).write_text(markdown, encoding="utf-8")
    (outdir / "filters_applied.json").write_text(json.dumps(filters, indent=2, ensure_ascii=False), encoding="utf-8")
    return filters


def main() -> None:
    p = argparse.ArgumentParser(description="CoinPoker PLO population strength analyzer")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--input", required=True, help="CoinPoker HH txt file")
    p.add_argument("--hero", default="Hero", help="Hero screen name in HH")
    p.add_argument("--outdir", default="population_strength_report", help="Output directory")
    p.add_argument("--game-filter", default="plo4", choices=["plo4", "plo5", "bombpot", "all"], help="Default is plo4 regular only")
    p.add_argument("--include-bombpot", action="store_true", help="Include BombPot hands with plo4/plo5/all filters")
    p.add_argument("--stake-filter", default="", help="Comma-separated stakes, e.g. PL25,PL50")
    p.add_argument("--date-from", default="", help="YYYY-MM-DD inclusive")
    p.add_argument("--date-to", default="", help="YYYY-MM-DD inclusive")
    p.add_argument("--min-stack-bb", type=float, default=40.0, help="Minimum starting stack in bb for villain and Hero")
    p.add_argument("--min-known", type=int, default=10, help="Minimum known/showdown hands to show a line in summary")
    p.add_argument("--vs-hero", action="store_true", help="Keep wagers directly answered by Hero or raises facing Hero.")
    args = p.parse_args()

    try:
        result = analyze(Path(args.input), args.hero, Path(args.outdir), args)
    except (ValueError, OSError) as exc:
        p.exit(2, f"Analysis failed: {exc}\n")
    if result["parse_errors"] or result["identical_duplicate_blocks"]:
        print(f"Input diagnostics: {result['input_issues']}, identical duplicates: {result['identical_duplicate_blocks']}", file=sys.stderr)
    print("Done.")
    print(f"Raw hands before filters: {result['raw_hands_before_filters']:,}")
    print(f"Hands matching game, stakes and dates: {result['hands_after_game_stake_date']:,}")
    print(f"Hands with eligible actions: {result['hands_analyzed_after_filters']:,}")
    print(f"Postflop known line records: {result['postflop_known_line_records']:,}")
    print(f"Preflop known line records: {result['preflop_known_line_records']:,}")
    print(f"Output: {args.outdir}")
    print("Open population_strength_presentation.html first. Raw CSV files remain available for drilldown.")


if __name__ == "__main__":
    main()
