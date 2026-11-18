#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CoinPoker PLO Population Strength Analyzer v14 BETA

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

Outputs:
  population_strength_report.md/html
  - river classes: practical_nuts / close_to_nuts / second_third_nut_flush / medium_flush / low_flush / weak_straight / thin_value / bluff
  - board texture split lines for flop/turn/river
  population_strength_by_line.csv
  population_strength_hands.csv
  preflop_population_by_line.csv
  filters_applied.json
"""

from __future__ import annotations

import argparse
import collections
import csv
import html
import itertools
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RANK_ORDER = "23456789TJQKA"
RANK_VALUE = {r: i for i, r in enumerate(RANK_ORDER, start=2)}
VALUE_RANK = {v: r for r, v in RANK_VALUE.items()}
MONEY_RE = re.compile(r"₮([0-9]+(?:\.[0-9]+)?)")

ACTION_PATTERNS = [
    ("posts_ante", re.compile(r"^(.+?): posts ante ₮([0-9.]+)")),
    ("posts_sb", re.compile(r"^(.+?): posts small blind ₮([0-9.]+)")),
    ("posts_bb", re.compile(r"^(.+?): posts big blind ₮([0-9.]+)")),
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
    "allin": "allin",
    "folds": "fold",
    "checks": "check",
}
AGGRO_ACTIONS = {"bet", "raise", "allin"}


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
    if not seats or button_seat not in seats:
        return list(seats.values())
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
    uniq = sorted(set(vals))
    pairs = sum(1 for v in rank_counts.values() if v == 2)
    trips = any(v >= 3 for v in rank_counts.values())
    high = sum(1 for v in vals if v >= 11)
    broadway = sum(1 for v in vals if v >= 10)
    ace = "A" in ranks
    double_suited = sum(1 for v in suit_counts.values() if v >= 2) >= 2
    single_suited = any(v >= 2 for v in suit_counts.values())
    best_window = 0
    for low in range(2, 11):
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
    elif ace and single_suited:
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
    uniq = sorted(set(vals))
    best_window = 0
    for low in range(2, 11):
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
    if wet_score >= 3:
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


def parse_hand(hand_text: str, hero: str = "Hero") -> Optional[Hand]:
    lines = hand_text.splitlines()
    if not lines:
        return None
    meta = parse_header(lines[0])
    if not meta:
        return None

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
    active = set(seats.values())
    active_at_street_start: Dict[str, List[str]] = {"PREFLOP": list(active)}
    boards: Dict[str, List[str]] = {}
    events: Dict[str, List[Event]] = {"PREFLOP": [], "FLOP": [], "TURN": [], "RIVER": []}

    pot = 0.0
    preflop_raises = 0
    last_preflop_raiser = ""
    event_idx = 0
    raises_by_street = collections.defaultdict(int)

    for line in lines[1:]:
        sm = re.match(r"\*\*\* (?:(FIRST|SECOND|THIRD) )?(FLOP|TURN|RIVER) \*\*\* (.*)", line)
        if sm:
            marker, new_street, rest = sm.groups()
            street = new_street
            if marker in (None, "FIRST"):
                boards[new_street] = parse_all_cards_from_brackets(rest)
                active_at_street_start[new_street] = list(active)
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
            if raw_action in ("posts_ante", "posts_sb", "posts_bb", "bets", "calls", "allin", "return", "collected"):
                amount = float(m.group(2))
            elif raw_action == "raises":
                total_to = float(m.group(3))
                amount = max(0.0, total_to - street_contrib[player])

            pot_before = pot
            if raw_action in ("posts_ante",):
                pot += amount
            elif raw_action in ("posts_sb", "posts_bb", "bets", "calls"):
                street_contrib[player] += amount
                pot += amount
            elif raw_action == "raises":
                street_contrib[player] = total_to or street_contrib[player]
                pot += amount
            elif raw_action == "allin":
                street_contrib[player] += amount
                pot += amount
            elif raw_action == "return":
                street_contrib[player] = max(0.0, street_contrib[player] - amount)
                pot -= amount
            elif raw_action == "folds":
                active.discard(player)

            raises_before = raises_by_street[street]
            if raw_action == "raises":
                raises_by_street[street] += 1
                if street == "PREFLOP":
                    preflop_raises += 1
                    last_preflop_raiser = player

            action = ACTION_MAP.get(raw_action)
            if action:
                size_pct = safe_div(amount, pot_before, default=math.nan) if action in ("bet", "call", "raise", "allin") else math.nan
                event_idx += 1
                events.setdefault(street, []).append(Event(street=street, player=player, action=action, raw_action=raw_action, amount=amount, pot_before=pot_before, size_pot_pct=size_pct, idx=event_idx, raises_before=raises_before))
            break

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

    return Hand(meta=meta, seats=seats, positions=positions, postflop_order=pf_order, stacks=stacks, shown_cards=shown_cards, hero_cards=hero_cards, boards=boards, events=events, active_at_street_start=active_at_street_start, preflop_raises=preflop_raises, last_preflop_raiser=last_preflop_raiser, pot_type=pot_type)


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
    vals = sorted([RANK_VALUE[c[0]] for c in cards], reverse=True)
    suits = [c[1] for c in cards]
    counts = collections.Counter(vals)
    by_count = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    is_flush = len(set(suits)) == 1
    sh = straight_high(vals)
    if is_flush and sh:
        return (8, [sh], "straight_flush")
    if by_count[0][1] == 4:
        quad = by_count[0][0]
        kicker = max(v for v in vals if v != quad)
        return (7, [quad, kicker], "quads")
    if by_count[0][1] == 3 and by_count[1][1] == 2:
        return (6, [by_count[0][0], by_count[1][0]], "full_house")
    if is_flush:
        return (5, vals, "flush")
    if sh:
        return (4, [sh], "straight")
    if by_count[0][1] == 3:
        trip = by_count[0][0]
        kickers = sorted([v for v in vals if v != trip], reverse=True)
        return (3, [trip] + kickers, "trips_or_set")
    pairs = sorted([v for v, c in counts.items() if c == 2], reverse=True)
    if len(pairs) >= 2:
        kicker = max(v for v in vals if v not in pairs[:2])
        return (2, pairs[:2] + [kicker], "two_pair")
    if len(pairs) == 1:
        pair = pairs[0]
        kickers = sorted([v for v in vals if v != pair], reverse=True)
        return (1, [pair] + kickers, "one_pair")
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


def has_flush_draw(hole: List[str], board: List[str]) -> Tuple[bool, bool, str]:
    # PLO: needs exactly 2 hole cards of same suit and 2 board cards of that suit to have FD on flop/turn.
    if len(board) >= 5:
        return (False, False, "")
    board_suits = collections.Counter(c[1] for c in board)
    hole_suits = collections.defaultdict(list)
    for c in hole:
        hole_suits[c[1]].append(c)
    for s, bc in board_suits.items():
        if bc == 2 and len(hole_suits.get(s, [])) >= 2:
            ranks = sorted([RANK_VALUE[c[0]] for c in hole_suits[s]], reverse=True)
            nut = 14 in ranks
            return (True, nut, s)
    return (False, False, "")


def straight_outs_approx(hole: List[str], board: List[str]) -> int:
    """Fast approximation of straight-draw strength.

    This is intentionally not a solver-grade PLO outs calculator. It is used only
    to split shown population hands into coarse buckets such as pair+good draw.
    We count rank-gaps in hole+board and return an approximate out count.
    """
    if len(board) >= 5 or len(board) < 3:
        return 0
    vals = {RANK_VALUE[c[0]] for c in (hole + board)}
    if 14 in vals:
        vals.add(1)
    best = 0
    for low in range(1, 11):
        window = set(range(low, low + 5))
        hits = len(vals & window)
        best = max(best, hits)
    # Coarse mapping: 4 ranks in a 5-card window = OESD/wrap-ish.
    if best >= 5:
        return 13
    if best == 4:
        return 8
    if best == 3:
        return 4
    return 0

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


def practical_rank_key(best: Tuple[int, List[int], str, List[str]], board: List[str]) -> Tuple[int, Tuple[int, ...]]:
    """Rank a river hand for the practical nuts and near-nuts buckets.

    On unpaired boards, count straight flushes by their flush ranks. This lets
    an ace-high flush count as the practical nuts even when a straight flush
    is possible.
    Full houses/quads still outrank flushes on paired boards.
    """
    cat, tb, made_name, best_cards = best
    if made_name == "straight_flush" and len(board) == 5 and has_board_flush_possible(board) and not is_board_paired(board):
        vals = sorted([RANK_VALUE[c[0]] for c in best_cards], reverse=True)
        return (5, tuple(vals))
    return (cat, tuple(tb))


RIVER_TIERS_CACHE: Dict[Tuple[str, ...], List[Tuple[int, Tuple[int, ...]]]] = {}

def possible_river_rank_tiers(board: List[str]) -> List[Tuple[int, Tuple[int, ...]]]:
    if len(board) < 5:
        return []
    key = tuple(board[:5])
    if key in RIVER_TIERS_CACHE:
        return RIVER_TIERS_CACHE[key]
    blocked = set(board)
    deck = [c for c in full_deck() if c not in blocked]
    keys = set()
    for h1, h2 in itertools.combinations(deck, 2):
        b = best_plo_hand([h1, h2], board)
        keys.add(practical_rank_key(b, board))
    tiers = sorted(keys, reverse=True)
    RIVER_TIERS_CACHE[key] = tiers
    return tiers


def actual_flush_suit_from_best(best_cards: List[str]) -> str:
    sc = collections.Counter(c[1] for c in best_cards)
    for s, n in sc.items():
        if n >= 5:
            return s
    return ""


def flush_pair_key_for_suit(cards: List[str], suit: str) -> Tuple[int, ...]:
    return tuple(sorted([RANK_VALUE[c[0]] for c in cards if c[1] == suit], reverse=True)[:2])


FLUSH_TIER_CACHE: Dict[Tuple[Tuple[str, ...], str], List[Tuple[int, Tuple[int, ...]]]] = {}

def possible_flush_hole_pair_tiers(board: List[str], suit: str) -> List[Tuple[int, Tuple[int, ...]]]:
    key = (tuple(board[:5]), suit)
    if key in FLUSH_TIER_CACHE:
        return FLUSH_TIER_CACHE[key]
    board_cards = set(board)
    suited_remaining = [c for c in full_deck() if c[1] == suit and c not in board_cards]
    board_suit_ranks = sorted([RANK_VALUE[c[0]] for c in board if c[1] == suit], reverse=True)
    tiers = set()
    for a, b in itertools.combinations(suited_remaining, 2):
        vals = tuple(sorted(board_suit_ranks[:3] + [RANK_VALUE[a[0]], RANK_VALUE[b[0]]], reverse=True))
        # category fixed as flush; straight-flush ignored by practical rule on unpaired boards
        tiers.add((5, vals))
    out = sorted(tiers, reverse=True)
    FLUSH_TIER_CACHE[key] = out
    return out


STRAIGHT_TIER_CACHE: Dict[Tuple[str, ...], List[int]] = {}

def possible_straight_high_tiers_fast(board: List[str]) -> List[int]:
    key = tuple(board[:5])
    if key in STRAIGHT_TIER_CACHE:
        return STRAIGHT_TIER_CACHE[key]
    board_ranks = [RANK_VALUE[c[0]] for c in board]
    remaining_ranks = sorted(set(range(2, 15)) - set(board_ranks))
    highs = set()
    for a, b in itertools.combinations(remaining_ranks, 2):
        # Need exactly two hole ranks and three board ranks; approximate by testing all 3-card board subsets.
        for br in itertools.combinations(board_ranks, 3):
            sh = straight_high(list(br) + [a, b])
            if sh:
                highs.add(sh)
    out = sorted(highs, reverse=True)
    STRAIGHT_TIER_CACHE[key] = out
    return out


PAIRED_BOARD_TIER_CACHE: Dict[Tuple[str, ...], List[Tuple[int, Tuple[int, ...]]]] = {}

def rank_only_key(vals: List[int]) -> Tuple[int, Tuple[int, ...]]:
    """Fast rank-only 5-card key. Used for paired boards to rank quads/FH tiers.

    Rank counts are enough to separate quads and full houses on paired boards.
    This key ignores suits and does not detect straight flushes.
    """
    counts = collections.Counter(vals)
    items = sorted(counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
    cnts = sorted(counts.values(), reverse=True)
    # quads
    if cnts[0] == 4:
        q = max(r for r, n in counts.items() if n == 4)
        k = max(r for r, n in counts.items() if n != 4)
        return (7, (q, k))
    # full house
    trips = sorted([r for r, n in counts.items() if n >= 3], reverse=True)
    pairs = sorted([r for r, n in counts.items() if n >= 2 and r not in trips[:1]], reverse=True)
    if trips and (pairs or len(trips) > 1):
        t = trips[0]
        p = pairs[0] if pairs else trips[1]
        return (6, (t, p))
    sh = straight_high(vals)
    if sh:
        return (4, (sh,))
    if trips:
        t = trips[0]
        kick = sorted([r for r in vals if r != t], reverse=True)[:2]
        return (3, tuple([t] + kick))
    pr = sorted([r for r, n in counts.items() if n >= 2], reverse=True)
    if len(pr) >= 2:
        kicker = max(r for r in vals if r not in pr[:2])
        return (2, tuple(pr[:2] + [kicker]))
    if len(pr) == 1:
        p = pr[0]
        kicks = sorted([r for r in vals if r != p], reverse=True)[:3]
        return (1, tuple([p] + kicks))
    return (0, tuple(sorted(vals, reverse=True)[:5]))


def paired_board_practical_tiers_fast(board: List[str]) -> List[Tuple[int, Tuple[int, ...]]]:
    """Fast practical rank tiers for paired river boards under PLO 2-card rule.

    Enumerate two-card rank pairs using only ranks still available after
    accounting for the board.
    """
    key = tuple(board[:5])
    if key in PAIRED_BOARD_TIER_CACHE:
        return PAIRED_BOARD_TIER_CACHE[key]
    board_vals = [RANK_VALUE[c[0]] for c in board[:5]]
    board_count = collections.Counter(board_vals)
    tiers = set()
    ranks = list(range(2, 15))
    for i, r1 in enumerate(ranks):
        for r2 in ranks[i:]:
            # availability after board removal
            need = collections.Counter([r1, r2])
            if any(need[r] > 4 - board_count.get(r, 0) for r in need):
                continue
            hole = [r1, r2]
            for idxs in itertools.combinations(range(5), 3):
                vals = hole + [board_vals[i] for i in idxs]
                tiers.add(rank_only_key(vals))
    out = sorted(tiers, reverse=True)
    PAIRED_BOARD_TIER_CACHE[key] = out
    return out


def actual_practical_key_for_paired_board(best: Tuple[int, List[int], str, List[str]]) -> Tuple[int, Tuple[int, ...]]:
    cat, tb, made_name, best_cards = best
    # Use normal poker key produced by best_plo_hand; demote straight-flush to flush
    # is NOT done on paired boards because the practical flush exception only applies
    # to unpaired flush boards.
    return (cat, tuple(tb))



def full_house_key_from_best(best: Tuple[int, List[int], str, List[str]]) -> Tuple[int, Tuple[int, ...]]:
    cat, tb, made_name, best_cards = best
    if cat == 6 and len(tb) >= 2:
        return (6, (int(tb[0]), int(tb[1])))
    return (cat, tuple(tb))


FULL_HOUSE_TIER_CACHE: Dict[Tuple[str, ...], List[Tuple[int, Tuple[int, ...]]]] = {}

def possible_full_house_tiers_practical(board: List[str]) -> List[Tuple[int, Tuple[int, ...]]]:
    """Possible full-house tiers under PLO 2-card rule, excluding quads.

    The top full house counts as the practical nuts. On K9966, KK keeps
    that label even though quads are possible.
    """
    key = tuple(board[:5])
    if key in FULL_HOUSE_TIER_CACHE:
        return FULL_HOUSE_TIER_CACHE[key]
    if len(board) < 5:
        return []
    board_vals = [RANK_VALUE[c[0]] for c in board[:5]]
    board_count = collections.Counter(board_vals)
    tiers = set()
    ranks = list(range(2, 15))
    for i, r1 in enumerate(ranks):
        for r2 in ranks[i:]:
            need = collections.Counter([r1, r2])
            if any(need[r] > 4 - board_count.get(r, 0) for r in need):
                continue
            for idxs in itertools.combinations(range(5), 3):
                vals = [r1, r2] + [board_vals[j] for j in idxs]
                k = rank_only_key(vals)
                if k[0] == 6:  # full house only; quads intentionally excluded
                    tiers.add(k)
    out = sorted(tiers, reverse=True)
    FULL_HOUSE_TIER_CACHE[key] = out
    return out


def dry_set_tiers_practical(board: List[str]) -> List[int]:
    """Possible set ranks on dry unpaired boards under PLO 2-card rule.

    This is intentionally coarse: top set / second set / lower set are sufficient
    for practical river value classification when no straight/flush/paired-board
    complication exists.
    """
    vals = sorted(set(RANK_VALUE[c[0]] for c in board), reverse=True)
    return vals


def classify_river_practical_value_strength(hole: List[str], board: List[str], best: Tuple[int, List[int], str, List[str]]) -> Dict[str, Any]:
    """River practical value classification.

    Main goal: separate *decision-relevant* river raise strength from strict
    mathematical nuts. We treat quads / straight-flush / royal as ultra-cooler
    hands that exist but should not demote top full / nut flush / nut straight
    in normal poker reasoning.

    Output classes:
      - ultra_cooler_nuts: quads or straight flush / royal
      - practical_nuts: top normal value hand, ignoring ultra-coolers
      - near_nuts: second practical value tier
      - strong_value: clear value below top tiers
      - thin_value: made hand that is not a natural raise monster
      - bluff: one pair or weaker / not credible value
    """
    cat, tb, made_name, best_cards = best
    made = made_group(made_name, board)
    paired = is_board_paired(board)
    flush_possible = has_board_flush_possible(board)

    def out(cls: str, reason: str, top: Any = '', second: Any = '', actual: Any = '') -> Dict[str, Any]:
        # Keep old names as backward-compatible aliases, but the report should
        # use river_practical_value_class.
        return {
            'river_practical_value_class': cls,
            'river_practical_value_reason': reason,
            'river_nut_class': cls,
            'river_nut_reason': reason,
            'river_actual_practical_rank': str(actual),
            'river_top_practical_rank': str(top),
            'river_second_practical_rank': str(second),
        }

    # Rare cooler monsters. We count them separately rather than letting them
    # redefine practical nuts for normal decision-making.
    if made_name in {'straight_flush', 'quads'}:
        return out('ultra_cooler_nuts', f'{made_name}: rare cooler-tier absolute monster', actual=(cat, tuple(tb)))

    # One pair or weaker is not credible river-raise value in this population tool.
    if cat <= 1:
        return out('bluff', 'one pair or weaker at river; bluff/blocker/thin-spew', actual=(cat, tuple(tb)))

    # Paired board: top full = practical nuts, second full = near-nuts, lower full remains value below top tiers.
    # Flush/straight/trips on paired board are capped bluff-catchers for river raise purposes.
    if paired:
        if made_name == 'full_house':
            actual_key = full_house_key_from_best(best)
            tiers = possible_full_house_tiers_practical(board)
            top = tiers[0] if tiers else ''
            second = tiers[1] if len(tiers) > 1 else ''
            if actual_key == top:
                return out('practical_nuts', 'top full house on paired board; quads ignored as ultra-cooler', top, second, actual_key)
            if actual_key == second:
                return out('near_nuts', 'second practical full-house tier on paired board', top, second, actual_key)
            return out('strong_value', 'lower full house on paired board: clear value but below top practical tiers', top, second, actual_key)
        if made_name in {'trips_or_set', 'flush', 'straight', 'two_pair'}:
            return out('thin_value', f'{made} on paired board without full house: not natural river-raise monster', actual=(cat, tuple(tb)))
        return out('bluff', f'{made} on paired board below credible value threshold', actual=(cat, tuple(tb)))

    # Unpaired flush board: nut flush = practical nuts; second flush = near-nuts.
    # Straight flush is already removed as ultra-cooler. Non-flush hands are thin/value-capped.
    if flush_possible:
        if made_name == 'flush':
            fsuit = actual_flush_suit_from_best(best_cards)
            if fsuit:
                board_suit_ranks = sorted([RANK_VALUE[c[0]] for c in board if c[1] == fsuit], reverse=True)
                actual_key = (5, tuple(sorted(board_suit_ranks[:3] + list(flush_pair_key_for_suit(hole, fsuit)), reverse=True)))
                tiers = possible_flush_hole_pair_tiers(board, fsuit)
                top = tiers[0] if tiers else ''
                second = tiers[1] if len(tiers) > 1 else ''
                if actual_key == top:
                    return out('practical_nuts', 'nut flush on unpaired board; straight flush ignored as ultra-cooler', top, second, actual_key)
                if actual_key == second:
                    return out('near_nuts', 'second practical flush on unpaired board', top, second, actual_key)
                return out('strong_value', 'lower flush on unpaired board', top, second, actual_key)
        if made_name == 'straight':
            return out('thin_value', 'straight on flush board: value-capped below flushes', actual=(cat, tuple(tb)))
        if made_name in {'trips_or_set', 'two_pair'}:
            return out('thin_value', f'{made} on flush board: thin/value-capped', actual=(cat, tuple(tb)))
        return out('bluff', f'{made} on flush board below credible river-raise value', actual=(cat, tuple(tb)))

    # Unpaired, no flush board. If straight is possible, top straight = practical nuts.
    straight_tiers = possible_straight_high_tiers_fast(board)
    if straight_tiers:
        if made_name == 'straight':
            actual_high = tb[0] if tb else 0
            top = straight_tiers[0]
            second = straight_tiers[1] if len(straight_tiers) > 1 else ''
            if actual_high == top:
                return out('practical_nuts', 'top straight on no-flush unpaired board', top, second, actual_high)
            if second and actual_high == second:
                return out('near_nuts', 'second practical straight on no-flush unpaired board', top, second, actual_high)
            return out('strong_value', 'lower straight on no-flush unpaired board', top, second, actual_high)
        if made_name == 'trips_or_set':
            return out('strong_value', 'set on straight-possible board: strong but below straights', actual=(cat, tuple(tb)))
        if made_name == 'two_pair':
            return out('thin_value', 'two pair on straight-possible board: thin/value-capped', actual=(cat, tuple(tb)))
        return out('bluff', f'{made} on straight-possible board below credible river-raise value', actual=(cat, tuple(tb)))

    # Dry unpaired board: top set = practical nuts, second set = near-nuts.
    if made_name == 'trips_or_set':
        tiers = dry_set_tiers_practical(board)
        trip_rank = tb[0] if tb else 0
        top = tiers[0] if tiers else ''
        second = tiers[1] if len(tiers) > 1 else ''
        if trip_rank == top:
            return out('practical_nuts', 'top set on dry no-straight/no-flush board', top, second, trip_rank)
        if second and trip_rank == second:
            return out('near_nuts', 'second set on dry no-straight/no-flush board', top, second, trip_rank)
        return out('strong_value', 'lower set/trips on dry board', top, second, trip_rank)
    if made_name == 'two_pair':
        return out('strong_value', 'two pair on dry no-straight/no-flush board', actual=(cat, tuple(tb)))
    return out('thin_value', f'{made} on dry board: value but not top-tier river raise class', actual=(cat, tuple(tb)))




RIVER_PLO_CLASS_ORDER = [
    "practical_nuts",
    "close_to_nuts",
    "second_third_nut_flush",
    "medium_flush",
    "low_flush",
    "weak_straight",
    "thin_value",
    "bluff",
]

RIVER_PLO_CLASS_LABELS = {
    "practical_nuts": "Practical nuts",
    "close_to_nuts": "Close to nuts",
    "second_third_nut_flush": "Second / third nut flush",
    "medium_flush": "Medium flush",
    "low_flush": "Low flush",
    "weak_straight": "Weak straight",
    "thin_value": "Thin value",
    "bluff": "Bluff",
}

RIVER_COMPACT_CLASS_ORDER = RIVER_PLO_CLASS_ORDER[:]


def top_board_rank(board: List[str]) -> int:
    vals = [RANK_VALUE[c[0]] for c in board]
    return max(vals) if vals else 0


def is_top_two_pair(best: Tuple[int, List[int], str, List[str]], board: List[str]) -> bool:
    """True when the two-pair hand contains top-board pair.

    In PLO river reporting, two-pair without top pair is not credible raise value.
    Overpairs are still just one-pair hands and are never upgraded here.
    """
    cat, tb, made_name, best_cards = best
    if made_name != "two_pair" or len(tb) < 2:
        return False
    return int(tb[0]) == top_board_rank(board) or int(tb[1]) == top_board_rank(board)


def flush_tier_index(hole: List[str], board: List[str], best: Tuple[int, List[int], str, List[str]]) -> Optional[int]:
    cat, tb, made_name, best_cards = best
    fsuit = actual_flush_suit_from_best(best_cards)
    if not fsuit:
        return None
    board_suit_ranks = sorted([RANK_VALUE[c[0]] for c in board if c[1] == fsuit], reverse=True)
    actual_key = (5, tuple(sorted(board_suit_ranks[:3] + list(flush_pair_key_for_suit(hole, fsuit)), reverse=True)))
    tiers = possible_flush_hole_pair_tiers(board, fsuit)
    try:
        return tiers.index(actual_key)
    except ValueError:
        return None


def straight_tier_index(board: List[str], high: int) -> Optional[int]:
    tiers = possible_straight_high_tiers_fast(board)
    try:
        return tiers.index(int(high))
    except Exception:
        return None


def classify_river_plo_display_class(hole: List[str], board: List[str], best: Tuple[int, List[int], str, List[str]]) -> Dict[str, Any]:
    """Group a river hand by its strength on this board.

    Custom practical PLO rules:
      1) Paired board: practical nuts = top boat; close to nuts = weaker boat;
         thin value = flush/straight/trips/top two pair; bluff = weaker than top two pair.
      2) Unpaired flush board: practical nuts = nut flush; second/third nut flush;
         medium flush; low flush; thin value = straight/trips/top two pair; bluff = weaker.
      3) Unpaired no-flush board: practical nuts = nut straight where possible;
         close to nuts = second nut straight; weak straight = lower straight;
         thin value = trips/top two pair; bluff = no top pair in two-pair, overpair, pair or weaker.

    For completely dry unpaired boards where no straight is possible, top set is
    treated as practical nuts and weaker sets as close to nuts.
    """
    cat, tb, made_name, best_cards = best
    paired = is_board_paired(board)
    flush_possible = has_board_flush_possible(board)

    def out(cls: str, reason: str) -> Dict[str, Any]:
        return {"river_plo_class": cls, "river_plo_class_label": RIVER_PLO_CLASS_LABELS.get(cls, cls), "river_plo_class_reason": reason}

    # Paired boards: full-house logic dominates. Quads/SF are rare monsters, but
    # we fold them into practical nuts instead of letting them demote top boat.
    if paired:
        if made_name in {"straight_flush", "quads"}:
            return out("practical_nuts", "rare cooler monster on paired board; grouped with practical nuts")
        if made_name == "full_house":
            actual_key = full_house_key_from_best(best)
            tiers = possible_full_house_tiers_practical(board)
            top = tiers[0] if tiers else None
            if actual_key == top:
                return out("practical_nuts", "top full house on paired board")
            return out("close_to_nuts", "weaker full house on paired board")
        if made_name in {"flush", "straight", "trips_or_set"}:
            return out("thin_value", f"{made_name} on paired board without full house")
        if is_top_two_pair(best, board):
            return out("thin_value", "top two pair on paired board")
        return out("bluff", "not top two pair / one pair or weaker on paired board")

    # Unpaired flush board: flush hierarchy is the main truth.
    if flush_possible:
        if made_name == "straight_flush":
            return out("practical_nuts", "straight flush on unpaired flush board; grouped with practical nuts")
        if made_name == "flush":
            idx = flush_tier_index(hole, board, best)
            if idx == 0:
                return out("practical_nuts", "nut flush on unpaired flush board")
            if idx in (1, 2):
                return out("second_third_nut_flush", "second or third nut flush on unpaired flush board")
            if idx is not None and idx <= 5:
                return out("medium_flush", "medium flush on unpaired flush board")
            return out("low_flush", "low flush on unpaired flush board")
        if made_name in {"straight", "trips_or_set"}:
            return out("thin_value", f"{made_name} without flush on unpaired flush board")
        if is_top_two_pair(best, board):
            return out("thin_value", "top two pair without flush on unpaired flush board")
        return out("bluff", "not top two pair / one pair or weaker on unpaired flush board")

    # Unpaired, no-flush board. If straight exists, use straight tiers.
    straight_tiers = possible_straight_high_tiers_fast(board)
    if straight_tiers:
        if made_name == "straight":
            actual_high = tb[0] if tb else 0
            idx = straight_tier_index(board, actual_high)
            if idx == 0:
                return out("practical_nuts", "nut straight on unpaired no-flush board")
            if idx == 1:
                return out("close_to_nuts", "second nut straight on unpaired no-flush board")
            return out("weak_straight", "weaker straight on unpaired no-flush board")
        if made_name == "trips_or_set":
            return out("thin_value", "set/trips without straight on unpaired no-flush board")
        if is_top_two_pair(best, board):
            return out("thin_value", "top two pair on unpaired no-flush board")
        return out("bluff", "not top pair in two-pair / overpair / one pair or weaker")

    # Dry unpaired, no flush, no straight possible. Here top set is the practical
    # nuts. Use the same report labels as on other boards.
    if made_name == "trips_or_set":
        tiers = dry_set_tiers_practical(board)
        trip_rank = tb[0] if tb else 0
        if tiers and int(trip_rank) == tiers[0]:
            return out("practical_nuts", "top set on dry unpaired board")
        return out("close_to_nuts", "weaker set on dry unpaired board")
    if is_top_two_pair(best, board):
        return out("thin_value", "top two pair on dry unpaired board")
    return out("bluff", "not top two pair / overpair / one pair or weaker on dry unpaired board")


def classify_river_relative_strength(hole: List[str], board: List[str], best: Tuple[int, List[int], str, List[str]]) -> Dict[str, Any]:
    """Backward-compatible wrapper around the v4 practical river class."""
    return classify_river_practical_value_strength(hole, board, best)

def bet_size_bucket(size_pct: Any) -> str:
    try:
        x = float(size_pct)
    except Exception:
        return "unknown_size"
    if math.isnan(x):
        return "unknown_size"
    if x < 0.40:
        return "vs_small_bet_<40"
    if x < 0.70:
        return "vs_half_bet_40-70"
    if x < 1.15:
        return "vs_large_pot_bet_70-115"
    return "vs_overbet_>115"


def previous_bet_on_street(events: List[Event], ev: Event) -> Optional[Event]:
    prev = [e for e in events if e.idx < ev.idx and e.action in {"bet", "allin"}]
    return prev[-1] if prev else None

def classify_street_strength(hole: List[str], board: List[str], street: str) -> Dict[str, Any]:
    rank_cat, tiebreak, made_name, best_cards = best_plo_hand(hole, board)
    fd, nut_fd, fd_suit = has_flush_draw(hole, board)
    sd_outs = straight_outs_approx(hole, board) if street in {"FLOP", "TURN"} else 0
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
            bucket = "clean_good_draw_no_pair"
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
            bucket = "river_one_pair_or_bluff"

    out = {
        "made_name": made_name,
        "made_group": made,
        "strength_bucket": bucket,
        "best_cards": " ".join(best_cards),
        "has_flush_draw": fd,
        "has_nut_flush_draw": nut_fd,
        "flush_draw_suit": fd_suit,
        "straight_outs_approx": sd_outs,
        "has_good_straight_draw": good_sd,
        "rank_category": rank_cat,
        "board_paired": is_board_paired(board),
        "board_flush_possible": has_board_flush_possible(board),
        "flush_completed_on_turn": flush_newly_completed_on_turn(board) if street == "TURN" else False,
        "flush_completed_on_river": flush_newly_completed_on_river(board) if street == "RIVER" else False,
    }
    if street == "RIVER":
        out.update(classify_river_relative_strength(hole, board, (rank_cat, tiebreak, made_name, best_cards)))
        out.update(classify_river_plo_display_class(hole, board, (rank_cat, tiebreak, made_name, best_cards)))
    else:
        out.update({
            "river_practical_value_class": "",
            "river_practical_value_reason": "",
            "river_nut_class": "",
            "river_nut_reason": "",
            "river_actual_practical_rank": "",
            "river_top_practical_rank": "",
            "river_second_practical_rank": "",
            "river_plo_class": "",
            "river_plo_class_label": "",
            "river_plo_class_reason": "",
        })
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
    return any(e.idx < ev.idx and e.player == player and e.action == "check" for e in events)


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
    return [e for e in h.events.get("PREFLOP", []) if e.action in {"raise", "allin"}]


def relevant_stack_ok(h: Hand, player: str, hero: str, min_stack_bb: float) -> bool:
    if h.stack_bb(player) < min_stack_bb:
        return False
    if h.stack_bb(hero) < min_stack_bb:
        return False
    return True


def add_record(records: List[Dict[str, Any]], h: Hand, street: str, line_id: str, line_name: str, villain: str, ev: Event, hero: str) -> None:
    hole = h.shown_cards.get(villain, [])
    board = h.board_for_street(street)
    if not hole or len(hole) < 4 or len(board) < 3:
        return
    st = classify_street_strength(hole, board, street)
    pre = classify_starting_hand(hole)
    records.append({
        "line_id": line_id,
        "line_name": line_name,
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
        "action_size_pct_pot": round(ev.size_pot_pct, 3) if not math.isnan(ev.size_pot_pct) else "",
        "villain_cards": " ".join(hole),
        "villain_preflop_class": pre["hand_class"],
        "villain_double_suited": pre.get("double_suited", False),
        "villain_connected_window": pre.get("connected_window", 0),
        "board": " ".join(board),
        "board_bucket": board_bucket(board),
        **st,
    })



def add_record_with_extras(records: List[Dict[str, Any]], h: Hand, street: str, line_id: str, line_name: str, villain: str, ev: Event, hero: str, extras: Optional[Dict[str, Any]] = None) -> None:
    before = len(records)
    add_record(records, h, street, line_id, line_name, villain, ev, hero)
    if len(records) > before and extras:
        records[-1].update(extras)


def add_postflop_line_variants(records: List[Dict[str, Any]], h: Hand, street: str, base_line_id: str, base_line_name: str, villain: str, ev: Event, hero: str) -> None:
    board = h.board_for_street(street)
    evs = h.events.get(street, [])
    prev_bet = previous_bet_on_street(evs, ev) if ev.action == "raise" else None
    extras = {
        "facing_bet_size_pct_pot": round(prev_bet.size_pot_pct, 3) if prev_bet and not math.isnan(prev_bet.size_pot_pct) else "",
        "facing_bet_size_bucket": bet_size_bucket(prev_bet.size_pot_pct) if prev_bet else "",
    }
    add_record_with_extras(records, h, street, base_line_id, base_line_name, villain, ev, hero, extras)

    # Add separate rows for the board textures used in the report.
    bbucket = board_bucket(board)
    if base_line_id == "FLOP_check_raise":
        if is_board_paired(board):
            add_record_with_extras(records, h, street, "FLOP_check_raise_paired_board", "Population flop check-raise on paired board", villain, ev, hero, extras)
        if bbucket in {"wet", "monotone"}:
            add_record_with_extras(records, h, street, "FLOP_check_raise_wet_board", "Population flop check-raise on wet/monotone board", villain, ev, hero, extras)
    if base_line_id == "TURN_check_raise" and flush_newly_completed_on_turn(board):
        add_record_with_extras(records, h, street, "TURN_check_raise_flush_completed", "Population turn check-raise when flush completes", villain, ev, hero, extras)
    if base_line_id in {"RIVER_check_raise", "RIVER_raise_not_checkraise"}:
        if is_board_paired(board):
            add_record_with_extras(records, h, street, "RIVER_raise_paired_board", "Population river raise on paired board", villain, ev, hero, extras)
        if flush_newly_completed_on_river(board):
            # Keep paired boards separate because full houses can beat a flush.
            if is_board_paired(board):
                add_record_with_extras(records, h, street, "RIVER_raise_flush_completed_paired_board", "Population river raise when flush completes on paired board", villain, ev, hero, extras)
            else:
                add_record_with_extras(records, h, street, "RIVER_raise_flush_completed_unpaired_board", "Population river raise when flush completes on unpaired board", villain, ev, hero, extras)
        if prev_bet:
            bucket = bet_size_bucket(prev_bet.size_pot_pct)
            add_record_with_extras(records, h, street, f"RIVER_raise_{bucket}", f"Population river raise facing {bucket}", villain, ev, hero, extras)

    if base_line_id == "RIVER_oop_turn_call_flush_river_lead":
        if is_board_paired(board):
            add_record_with_extras(records, h, street, "RIVER_oop_turn_call_flush_river_lead_paired_board", "Population OOP turn call then river flush-completing lead on paired board", villain, ev, hero, extras)
        else:
            add_record_with_extras(records, h, street, "RIVER_oop_turn_call_flush_river_lead_unpaired_board", "Population OOP turn call then river flush-completing lead on unpaired board", villain, ev, hero, extras)

def extract_population_records(h: Hand, hero: str, min_stack_bb: float) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    post: List[Dict[str, Any]] = []
    pre: List[Dict[str, Any]] = []

    # Preflop population open / 3bet / 4bet, only if shown.
    raises = get_preflop_raiser_order(h)
    for i, ev in enumerate(raises):
        if ev.player == hero or ev.player not in h.shown_cards:
            continue
        if not relevant_stack_ok(h, ev.player, hero, min_stack_bb):
            continue
        if i == 0:
            line_id = "PFR_open_shown"
            line_name = "Population open-raise, shown hand"
        elif i == 1:
            line_id = "PFR_3bet_shown"
            line_name = "Population 3-bet, shown hand"
        else:
            line_id = "PFR_4bet_plus_shown"
            line_name = "Population 4-bet+ raise, shown hand"
        hole = h.shown_cards.get(ev.player, [])
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
            "villain_position": h.positions.get(ev.player, ""),
            "villain_stack_bb": round(h.stack_bb(ev.player), 1),
            "hero_stack_bb": round(h.stack_bb(hero), 1),
            "action_size_pct_pot": round(ev.size_pot_pct, 3) if not math.isnan(ev.size_pot_pct) else "",
            "villain_cards": " ".join(hole),
            "hand_class": c["hand_class"],
            "double_suited": c.get("double_suited", False),
            "single_suited": c.get("single_suited", False),
            "connected_window": c.get("connected_window", 0),
            "high_cards": c.get("high_cards", 0),
            "broadways": c.get("broadways", 0),
            "pairs": c.get("pairs", 0),
            "ace": c.get("ace", False),
        })

    # Postflop population lines with shown villain hand.
    for street in ["FLOP", "TURN", "RIVER"]:
        evs = h.events.get(street, [])
        if not evs:
            continue
        for ev in evs:
            if ev.player == hero or ev.player not in h.shown_cards:
                continue
            if not relevant_stack_ok(h, ev.player, hero, min_stack_bb):
                continue

            if ev.action == "raise":
                checked_before = player_checked_before(evs, ev.player, ev)
                if checked_before:
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
                add_postflop_line_variants(post, h, street, line_id, line_name, ev.player, ev, hero)

            elif ev.action in {"bet", "allin"}:
                if street == "TURN":
                    flop_evs = h.events.get("FLOP", [])
                    flop_aggr = any(e.action in AGGRO_ACTIONS for e in flop_evs)
                    if not flop_aggr:
                        add_postflop_line_variants(post, h, street, "TURN_probe_after_flop_xx", "Population turn probe after flop checks through", ev.player, ev, hero)
                    if any(e.player == ev.player and e.action in {"bet", "allin"} for e in flop_evs):
                        add_postflop_line_variants(post, h, street, "TURN_barrel_after_flop_bet", "Population turn barrel after betting flop", ev.player, ev, hero)
                elif street == "RIVER":
                    turn_evs = h.events.get("TURN", [])
                    if any(e.player == ev.player and e.action in {"bet", "allin"} for e in turn_evs):
                        add_postflop_line_variants(post, h, street, "RIVER_barrel_after_turn_bet", "Population river barrel after betting turn", ev.player, ev, hero)
                    elif not any(e.action in AGGRO_ACTIONS for e in turn_evs):
                        add_postflop_line_variants(post, h, street, "RIVER_probe_after_turn_xx", "Population river probe after turn checks through", ev.player, ev, hero)
                elif street == "FLOP":
                    if ev.player != h.last_preflop_raiser:
                        add_postflop_line_variants(post, h, street, "FLOP_donk_bet", "Population flop donk bet", ev.player, ev, hero)

    # Villain calls Hero's turn bet out of position, then leads a river that completes the flush.
    river_board = h.board_for_street("RIVER")
    if flush_newly_completed_on_river(river_board):
        turn_evs = h.events.get("TURN", [])
        river_evs = h.events.get("RIVER", [])
        for villain in [p for p in h.shown_cards if p != hero]:
            if not relevant_stack_ok(h, villain, hero, min_stack_bb):
                continue
            if not postflop_acts_before(h, villain, hero):
                continue
            turn_call_vs_hero_bet = False
            for call_ev in [e for e in turn_evs if e.player == villain and e.action == "call"]:
                prev_bet = previous_bet_on_street(turn_evs, call_ev)
                if prev_bet and prev_bet.player == hero and prev_bet.action in {"bet", "allin"}:
                    turn_call_vs_hero_bet = True
                    break
            if not turn_call_vs_hero_bet:
                continue
            river_lead = first_event(river_evs, player=villain, actions={"bet", "allin"})
            if river_lead and no_prior_aggression(river_evs, river_lead) and no_prior_hero_action(river_evs, hero, river_lead):
                add_postflop_line_variants(post, h, "RIVER", "RIVER_oop_turn_call_flush_river_lead", "Population OOP turn-call then river flush-completing lead", villain, river_lead, hero)

    # Bet-bet-bet / triple barrel: same villain makes a bet/all-in on all three streets and shows hand.
    shown_villains = [p for p in h.shown_cards if p != hero]
    for villain in shown_villains:
        if not relevant_stack_ok(h, villain, hero, min_stack_bb):
            continue
        flop_bet = first_event(h.events.get("FLOP", []), player=villain, actions={"bet", "allin"})
        turn_bet = first_event(h.events.get("TURN", []), player=villain, actions={"bet", "allin"})
        river_bet = first_event(h.events.get("RIVER", []), player=villain, actions={"bet", "allin"})
        if flop_bet and turn_bet and river_bet:
            add_postflop_line_variants(post, h, "RIVER", "RIVER_bet_bet_bet", "Population bet-bet-bet / triple barrel shown at river", villain, river_bet, hero)

    return post, pre

# -------------------- summarization / output --------------------

def clean_output_text(value: Any) -> Any:
    """Remove presentation-unfriendly wording/characters from generated outputs."""
    if not isinstance(value, str):
        return value
    return (
        value.replace("\u2014", "-")
        .replace("\u2013", "-")
        .replace("—", "-")
        .replace("–", "-")
        .replace("bluff-candidate", "bluff")
        .replace("bluff_candidate", "bluff")
    )

def clean_output_recursive(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: clean_output_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_output_recursive(v) for v in obj]
    return clean_output_text(obj)

def write_csv(rows: List[Dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = list(rows[0].keys())
    for r in rows:
        for k in r.keys():
            if k not in keys:
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: clean_output_text(v) for k, v in r.items()})


def pct(n: int, d: int) -> float:
    return round(100 * safe_div(n, d), 1)


ALWAYS_SHOW_POSTFLOP_LINES = {
    # Keep these lines visible even when fewer than --min-known hands were shown.
    "RIVER_raise_flush_completed_paired_board",
    "RIVER_raise_flush_completed_unpaired_board",
    "RIVER_oop_turn_call_flush_river_lead",
    "RIVER_oop_turn_call_flush_river_lead_paired_board",
    "RIVER_oop_turn_call_flush_river_lead_unpaired_board",
    "TURN_check_raise_flush_completed",
}


def show_postflop_line(line_id: str, n: int, min_known: int) -> bool:
    return n >= min_known or line_id in ALWAYS_SHOW_POSTFLOP_LINES


def summarize_postflop(records: List[Dict[str, Any]], min_known: int) -> List[Dict[str, Any]]:
    groups = collections.defaultdict(list)
    for r in records:
        groups[r["line_id"]].append(r)
    out = []
    bucket_order = [
        "set_trips_boat_plus", "made_straight_or_flush", "two_pair", "pair_plus_good_draw",
        "clean_good_draw_no_pair", "one_pair_no_good_draw", "weak_or_air",
        "river_boat_plus", "river_flush", "river_straight", "river_medium_value", "river_one_pair_or_bluff",
    ]
    practical_order = ["ultra_cooler_nuts", "practical_nuts", "near_nuts", "strong_value", "thin_value", "bluff"]
    river_nut_order = practical_order
    river_plo_order = RIVER_PLO_CLASS_ORDER
    for line_id, rows in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        n = len(rows)
        if not show_postflop_line(line_id, n, min_known):
            continue
        cnt = collections.Counter(r.get("strength_bucket", "") for r in rows)
        made_cnt = collections.Counter(r.get("made_group", "") for r in rows)
        board_cnt = collections.Counter(r.get("board_bucket", "") for r in rows)
        nut_cnt = collections.Counter((r.get("river_practical_value_class") or r.get("river_nut_class", "")) for r in rows if r.get("street") == "RIVER")
        river_plo_cnt = collections.Counter(r.get("river_plo_class", "") for r in rows if r.get("street") == "RIVER" and r.get("river_plo_class"))
        size_cnt = collections.Counter(r.get("facing_bet_size_bucket", "") for r in rows if r.get("facing_bet_size_bucket"))
        row = {
            "line_id": line_id,
            "line_name": rows[0].get("line_name", line_id),
            "known_hands": n,
            "most_common_board_bucket": board_cnt.most_common(1)[0][0] if board_cnt else "",
            "bucket_distribution": "; ".join(f"{b} {pct(cnt.get(b,0),n)}%" for b in bucket_order if cnt.get(b, 0)),
            "made_distribution": "; ".join(f"{b} {pct(c,n)}%" for b, c in made_cnt.most_common() if b),
            "river_practical_value_distribution": "; ".join(f"{b} {pct(nut_cnt.get(b,0),sum(nut_cnt.values()))}%" for b in practical_order if nut_cnt.get(b,0)) if nut_cnt else "",
            "river_nut_distribution": "; ".join(f"{b} {pct(nut_cnt.get(b,0),sum(nut_cnt.values()))}%" for b in practical_order if nut_cnt.get(b,0)) if nut_cnt else "",
            "river_plo_class_distribution": "; ".join(f"{RIVER_PLO_CLASS_LABELS.get(b,b)} {pct(river_plo_cnt.get(b,0),sum(river_plo_cnt.values()))}%" for b in river_plo_order if river_plo_cnt.get(b,0)) if river_plo_cnt else "",
            "facing_bet_size_distribution": "; ".join(f"{b} {pct(c,sum(size_cnt.values()))}%" for b, c in size_cnt.most_common()) if size_cnt else "",
            "river_board_family_distribution": river_board_family_distribution_text(rows),
        }
        for b in bucket_order:
            if cnt.get(b, 0):
                row[b + "_pct"] = pct(cnt[b], n)
                row[b + "_n"] = cnt[b]
        if nut_cnt:
            river_n = sum(nut_cnt.values())
            for b in river_nut_order:
                row[b + "_pct"] = pct(nut_cnt.get(b, 0), river_n)
                row[b + "_n"] = nut_cnt.get(b, 0)
        if river_plo_cnt:
            river_plo_n = sum(river_plo_cnt.values())
            for b in river_plo_order:
                row["plo_" + b + "_pct"] = pct(river_plo_cnt.get(b, 0), river_plo_n)
                row["plo_" + b + "_n"] = river_plo_cnt.get(b, 0)
        out.append(row)
    return out

def summarize_preflop(records: List[Dict[str, Any]], min_known: int) -> List[Dict[str, Any]]:
    groups = collections.defaultdict(list)
    for r in records:
        groups[r["line_id"]].append(r)
    out = []
    for line_id, rows in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        n = len(rows)
        if n < min_known:
            continue
        cls = collections.Counter(r["hand_class"] for r in rows)
        ds = sum(1 for r in rows if str(r.get("double_suited", "False")) == "True" or r.get("double_suited") is True)
        ss = sum(1 for r in rows if str(r.get("single_suited", "False")) == "True" or r.get("single_suited") is True)
        aa = cls.get("AAxx", 0)
        kk = cls.get("KKxx", 0)
        high3 = sum(1 for r in rows if int(r.get("broadways", 0) or 0) >= 3)
        conn4 = sum(1 for r in rows if int(r.get("connected_window", 0) or 0) >= 4)
        out.append({
            "line_id": line_id,
            "line_name": rows[0].get("line_name", line_id),
            "known_hands": n,
            "AAxx_pct": pct(aa, n),
            "KKxx_pct": pct(kk, n),
            "double_suited_pct": pct(ds, n),
            "single_suited_or_better_pct": pct(ss, n),
            "three_plus_broadways_pct": pct(high3, n),
            "connected_4_window_pct": pct(conn4, n),
            "class_distribution": "; ".join(f"{b} {pct(c,n)}%" for b, c in cls.most_common()),
        })
    return out


def markdown_table(rows: List[Dict[str, Any]], cols: List[str], max_rows: int = 50) -> str:
    if not rows:
        return "_No rows._"
    rows = rows[:max_rows]
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join(["---"] * len(cols)) + " |"
    lines = [header, sep]
    for r in rows:
        vals = []
        for c in cols:
            s = str(r.get(c, "")).replace("|", "/")
            vals.append(s)
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


# -------------------- friendly presentation layer --------------------

def num(row: Dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        v = row.get(key, default)
        if v in (None, ""):
            return default
        return float(v)
    except Exception:
        return default


def line_category(line_id: str) -> str:
    if line_id.startswith("RIVER_"):
        return "River pressure"
    if line_id.startswith("TURN_"):
        return "Turn aggression / probes"
    if line_id.startswith("FLOP_"):
        return "Flop aggression"
    return "Postflop line types"


def compact_line_name(line_id: str, line_name: str = "") -> str:
    mapping = {
        "FLOP_check_raise": "Flop check-raise",
        "FLOP_check_raise_paired_board": "Flop check-raise - paired board",
        "FLOP_check_raise_wet_board": "Flop check-raise - wet board",
        "FLOP_raise_not_checkraise": "Flop raise vs bet",
        "FLOP_donk_bet": "Flop donk bet",
        "TURN_check_raise": "Turn check-raise",
        "TURN_check_raise_flush_completed": "Turn check-raise - flush completes",
        "TURN_raise_not_checkraise": "Turn raise vs bet",
        "TURN_probe_after_flop_xx": "Turn probe after flop checks through",
        "TURN_barrel_after_flop_bet": "Turn barrel after betting flop",
        "RIVER_check_raise": "River check-raise",
        "RIVER_raise_not_checkraise": "River raise vs bet",
        "RIVER_raise_paired_board": "River raise - paired board",
        "RIVER_raise_flush_completed_paired_board": "River raise - flush completes, paired board",
        "RIVER_raise_flush_completed_unpaired_board": "River raise - flush completes, unpaired board",
        "RIVER_oop_turn_call_flush_river_lead": "OOP turn call, river flush lead",
        "RIVER_oop_turn_call_flush_river_lead_paired_board": "OOP turn call, river flush lead - paired board",
        "RIVER_oop_turn_call_flush_river_lead_unpaired_board": "OOP turn call, river flush lead - unpaired board",
        "RIVER_raise_vs_small_bet_<40": "River raise vs small bet <40% pot",
        "RIVER_raise_vs_half_bet_40-70": "River raise vs half-pot bet 40-70%",
        "RIVER_raise_vs_large_pot_bet_70-115": "River raise vs large/pot bet 70-115%",
        "RIVER_bet_bet_bet": "Triple barrel / bet-bet-bet",
        "RIVER_barrel_after_turn_bet": "River barrel after betting turn",
        "RIVER_probe_after_turn_xx": "River probe after turn checks through",
    }
    return mapping.get(line_id, line_name or line_id)


def preflop_line_name(line_id: str, line_name: str = "") -> str:
    mapping = {
        "PFR_open_shown": "Shown PFR opens",
        "PFR_3bet_shown": "Shown 3-bets",
        "PFR_4bet_plus_shown": "Shown 4-bet+ ranges",
    }
    return mapping.get(line_id, line_name or line_id)


def _normalized_display_buckets(values: List[float]) -> List[float]:
    """Round the displayed percentages so they add up to 100.0."""
    buckets = [round(float(v or 0.0), 1) for v in values]
    diff = round(100.0 - sum(buckets), 1)
    if abs(diff) >= 0.05 and buckets:
        largest_idx = max(range(len(buckets)), key=lambda i: buckets[i])
        buckets[largest_idx] = round(max(0.0, buckets[largest_idx] + diff), 1)
    return buckets



def river_plo_top_classes(row: Dict[str, Any], limit: int = 5) -> List[Tuple[str, float, str]]:
    vals = []
    for cls in RIVER_PLO_CLASS_ORDER:
        v = num(row, "plo_" + cls + "_pct")
        if v > 0:
            vals.append((RIVER_PLO_CLASS_LABELS.get(cls, cls), v, cls))
    vals.sort(key=lambda x: -x[1])
    return vals[:limit]


def river_plo_total_from_row(row: Dict[str, Any]) -> float:
    return round(sum(num(row, "plo_" + cls + "_pct") for cls in RIVER_PLO_CLASS_ORDER), 1)


def river_plo_distribution_text(row: Dict[str, Any], limit: int = 8) -> str:
    tops = river_plo_top_classes(row, limit=limit)
    return "; ".join(f"{label} {val:.1f}%" for label, val, _ in tops) if tops else "no river class data"


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in {"true", "1", "yes", "y"}


def river_board_family_from_record(r: Dict[str, Any]) -> str:
    if _truthy(r.get("board_paired")):
        return "paired"
    if _truthy(r.get("board_flush_possible")):
        return "unpaired flush"
    board_txt = str(r.get("board", "") or "")
    board_cards = [c for c in board_txt.split() if len(c) >= 2]
    try:
        if len(board_cards) >= 5 and possible_straight_high_tiers_fast(board_cards):
            return "unpaired straight"
    except Exception:
        pass
    return "dry / no flush / no paired"


def river_board_family(line_id: str, board_bucket: str, board_family_distribution: str = "") -> str:
    b = str(board_bucket or "mixed")
    # Important: check unpaired before paired because "unpaired_board" contains "paired_board" as a substring.
    if "flush_completed_unpaired" in line_id or "flush_river_lead_unpaired" in line_id or "_unpaired_board" in line_id:
        return "unpaired flush"
    if "flush_completed_paired" in line_id or "flush_river_lead_paired" in line_id or "_paired_board" in line_id or "paired_board" in line_id:
        return "paired"
    if line_id.startswith("RIVER_"):
        return board_family_distribution or ("mixed, most common " + b)
    return b


def river_board_family_distribution_text(rows: List[Dict[str, Any]]) -> str:
    fams = collections.Counter(river_board_family_from_record(r) for r in rows if r.get("street") == "RIVER")
    total = sum(fams.values())
    if not total:
        return ""
    parts = [f"{k} {pct(v, total)}%" for k, v in fams.most_common()]
    return "; ".join(parts)


def postflop_friendly_metrics(row: Dict[str, Any]) -> Dict[str, Any]:
    line_id = str(row.get("line_id", ""))
    known = int(num(row, "known_hands"))
    is_river = line_id.startswith("RIVER_")

    # Keep the older buckets for the existing CSV columns. The river report
    # uses more specific classes, such as nut flush and lower flushes.
    ultra = num(row, "ultra_cooler_nuts_pct")
    practical = num(row, "practical_nuts_pct")
    near = num(row, "near_nuts_pct")
    river_practical_nuts = ultra + practical
    second_near = near
    strong = num(row, "strong_value_pct")
    thin = num(row, "thin_value_pct")
    legacy_bluff = num(row, "bluff_pct")
    nutsish = river_practical_nuts + second_near

    # Exact PLO display classes.
    plo_bluff = num(row, "plo_bluff_pct")
    plo_value = 100.0 - plo_bluff if is_river else 0.0
    top_classes = river_plo_top_classes(row, limit=8) if is_river else []
    top_classes_text = river_plo_distribution_text(row, limit=8) if is_river else ""

    monster = num(row, "set_trips_boat_plus_pct") + num(row, "made_straight_or_flush_pct")
    eq_value = num(row, "two_pair_pct") + num(row, "pair_plus_good_draw_pct") + num(row, "clean_good_draw_no_pair_pct")
    one_pair_no_draw = num(row, "one_pair_no_good_draw_pct")
    weak_or_air_raw = num(row, "weak_or_air_pct")
    weak = one_pair_no_draw + weak_or_air_raw + num(row, "river_one_pair_or_bluff_pct")
    board = row.get("most_common_board_bucket", "")

    if is_river:
        if known < 15:
            confidence = "LOW"
        elif known < 40:
            confidence = "MEDIUM"
        else:
            confidence = "GOOD"
        if plo_bluff <= 5 and plo_value >= 90:
            read_label = "VALUE-HEAVY"
            traffic = "green"
        elif plo_bluff <= 12 and plo_value >= 80:
            read_label = "MOSTLY VALUE"
            traffic = "green"
        elif plo_bluff >= 20:
            read_label = "MIXED / BLUFFS PRESENT"
            traffic = "yellow"
        else:
            read_label = "MIXED VALUE"
            traffic = "yellow"
        if plo_bluff <= 5:
            exploit = "Few of the shown hands were bluffs."
        elif plo_bluff >= 20:
            exploit = "Bluffs made up a noticeable share of the hands shown. Check which boards and bet sizes they came from."
        else:
            exploit = "Most shown hands were value bets. Use the board, blockers and bet size to judge close calls."

        line_name = compact_line_name(line_id)
        board_family = river_board_family(line_id, board, str(row.get("river_board_family_distribution", "") or ""))
        if "flush_completed_unpaired" in line_id or "flush_river_lead_unpaired" in line_id:
            summary = (
                f"{line_name} had {known} shown hands on unpaired rivers that completed a flush. "
                f"practical nuts {num(row,'plo_practical_nuts_pct'):.1f}%, second/third nut flush {num(row,'plo_second_third_nut_flush_pct'):.1f}%, "
                f"medium flush {num(row,'plo_medium_flush_pct'):.1f}%, low flush {num(row,'plo_low_flush_pct'):.1f}%, "
                f"thin value {num(row,'plo_thin_value_pct'):.1f}%, bluff {plo_bluff:.1f}%."
            )
        elif "paired_board" in line_id or "_paired_board" in line_id:
            summary = (
                f"{line_name} had {known} shown hands on paired boards. Practical nuts {num(row,'plo_practical_nuts_pct'):.1f}%, "
                f"close to nuts/weaker boat {num(row,'plo_close_to_nuts_pct'):.1f}%, thin value {num(row,'plo_thin_value_pct'):.1f}%, bluff {plo_bluff:.1f}%."
            )
        else:
            summary = f"{line_name} had {known} shown hands. Most common river classes were {top_classes_text}. Bluffs made up {plo_bluff:.1f}%."
        r_practical, r_second, r_strong, r_thin, r_bluff = _normalized_display_buckets([
            river_practical_nuts, second_near, strong, thin, legacy_bluff
        ])
        f_monster = f_equity = f_medium = f_weak = 0.0
    else:
        f_monster, f_equity, f_medium, f_weak = _normalized_display_buckets([
            monster, eq_value, one_pair_no_draw, weak_or_air_raw
        ])
        if known < 25:
            confidence = "LOW"
        elif known < 75:
            confidence = "MEDIUM"
        else:
            confidence = "GOOD"
        if weak <= 15 and (monster + eq_value) >= 70:
            read_label = "RARELY AIR"
            traffic = "green"
            exploit = "Made hands and draws dominate the shown sample. Keep that in mind when reviewing weak bluff catchers."
        elif weak >= 35:
            read_label = "MIXED / CAN BE LIGHT"
            traffic = "yellow"
            exploit = "Weak hands show up often here. Check how that changes with the board and bet size."
        else:
            read_label = "VALUE + EQUITY"
            traffic = "green"
            exploit = "Review calls against the made hands and draws that showed up, especially with one pair."
        summary = f"{compact_line_name(line_id)} had {known} shown hands. Strong made hands {monster:.1f}%, other value and draws {eq_value:.1f}%, weak hands {weak:.1f}%."
        r_practical = r_second = r_strong = r_thin = r_bluff = 0.0
        board_family = str(board or "mixed")

    if known < 5:
        read_label = "LOW SAMPLE"
        traffic = "gray"
        exploit = "Too few hands were shown for a reliable read."

    if is_river:
        display_top, display_strong, display_medium, display_weak = _normalized_display_buckets([nutsish, strong, thin, legacy_bluff])
    else:
        display_top, display_strong, display_medium, display_weak = _normalized_display_buckets([monster, eq_value, one_pair_no_draw, weak_or_air_raw])
    display_total = round(display_top + display_strong + display_medium + display_weak, 1)

    out = {
        "line_id": line_id,
        "line": compact_line_name(line_id, str(row.get("line_name", ""))),
        "category": line_category(line_id),
        "known_hands": known,
        "board_bucket": board,
        "river_board_family": board_family if is_river else "",
        "river_board_family_distribution": str(row.get("river_board_family_distribution", "") or "") if is_river else "",
        "river_plo_top_classes": top_classes_text,
        "river_plo_distribution": row.get("river_plo_class_distribution", ""),
        "river_bluff_pct": round(plo_bluff if is_river else 0.0, 1),
        "river_total_pct": 100.0 if is_river and known > 0 else 0.0,
        # Legacy fields retained for compatibility.
        "river_practical_nuts_pct": round(r_practical, 1),
        "river_second_near_nuts_pct": round(r_second, 1),
        "river_strong_value_pct": round(r_strong, 1),
        "river_thin_value_pct": round(r_thin, 1),
        "nutsish_pct": round(nutsish, 1),
        "strong_value_pct": round(strong, 1),
        "thin_value_pct": round(thin, 1),
        "bluff_pct": round(plo_bluff if is_river else legacy_bluff, 1),
        "monster_made_pct": round(monster, 1),
        "equity_value_pct": round(eq_value, 1),
        "one_pair_medium_pct": round(one_pair_no_draw, 1),
        "weak_air_pct": round(weak, 1),
        "flop_turn_monster_pct": round(f_monster, 1),
        "flop_turn_equity_value_pct": round(f_equity, 1),
        "flop_turn_one_pair_medium_pct": round(f_medium, 1),
        "flop_turn_weak_air_pct": round(f_weak, 1),
        "flop_turn_total_pct": round(f_monster + f_equity + f_medium + f_weak, 1) if not is_river else 0.0,
        "summary_top_tier_pct": display_top,
        "summary_strong_equity_pct": display_strong,
        "summary_medium_thin_pct": display_medium,
        "summary_weak_bluff_pct": display_weak,
        "summary_total_pct": display_total,
        "read_label": read_label,
        "confidence": confidence,
        "traffic_light": traffic,
        "plain_english_summary": summary,
        "exploit_note": exploit,
    }
    # Add exact class percentages to friendly rows.
    for cls in RIVER_COMPACT_CLASS_ORDER:
        out["plo_" + cls + "_pct"] = round(num(row, "plo_" + cls + "_pct"), 1) if is_river else 0.0
    return out

def preflop_friendly_metrics(row: Dict[str, Any]) -> Dict[str, Any]:
    line_id = str(row.get("line_id", ""))
    known = int(num(row, "known_hands"))
    aa = num(row, "AAxx_pct")
    kk = num(row, "KKxx_pct")
    ds = num(row, "double_suited_pct")
    broad = num(row, "three_plus_broadways_pct")
    conn = num(row, "connected_4_window_pct")
    if line_id == "PFR_4bet_plus_shown":
        read = "AA-HEAVY BUT NOT ONLY AA" if aa >= 35 else "WIDER THAN AA"
        note = "The AAxx share is based on hands that were shown after a 4-bet or higher."
    elif line_id == "PFR_3bet_shown":
        read = "BROADWAY / AA MIX"
        note = "Check how often broadway-heavy, suited and connected hands show up alongside premium pairs."
    else:
        read = "OPEN RANGE SNAPSHOT"
        note = "These are open raises where the cards became known. Hands that folded before showdown are often missing."
    conf = "LOW" if known < 25 else "MEDIUM" if known < 100 else "GOOD"
    return {
        "line_id": line_id,
        "line": preflop_line_name(line_id, str(row.get("line_name", ""))),
        "known_hands": known,
        "AAxx_pct": round(aa, 1),
        "KKxx_pct": round(kk, 1),
        "double_suited_pct": round(ds, 1),
        "three_plus_broadways_pct": round(broad, 1),
        "connected_4_window_pct": round(conn, 1),
        "read_label": read,
        "confidence": conf,
        "plain_english_summary": f"{preflop_line_name(line_id)}: AAxx {aa:.1f}%, KKxx {kk:.1f}%, double-suited {ds:.1f}%, 3+ broadways {broad:.1f}%, connected-window {conn:.1f}%.",
        "exploit_note": note,
    }


def make_friendly_postflop_summary(post_summary: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = [postflop_friendly_metrics(r) for r in post_summary]
    priority = {"River pressure": 0, "Turn aggression / probes": 1, "Flop aggression": 2, "Postflop line types": 3}
    rows.sort(key=lambda r: (priority.get(r["category"], 9), -r["known_hands"], r["line"]))
    return rows


def make_friendly_preflop_summary(pre_summary: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = [preflop_friendly_metrics(r) for r in pre_summary]
    rows.sort(key=lambda r: -r["known_hands"])
    return rows


def top_insights(
    post_friendly: List[Dict[str, Any]],
    pre_friendly: List[Dict[str, Any]],
    limit: int = 6,
    *,
    post_summary: List[Dict[str, Any]],
) -> List[str]:
    by_line = {row["line_id"]: row for row in post_summary}
    insights: List[str] = []
    for r in post_friendly:
        if r["known_hands"] < 10:
            continue
        if r["category"] == "River pressure" and r["bluff_pct"] == 0:
            source = by_line[r["line_id"]]
            classified = sum(int(num(source, f"plo_{cls}_n")) for cls in RIVER_PLO_CLASS_ORDER)
            if classified == 0:
                continue
            bluffs = int(num(source, "plo_bluff_n"))
            if bluffs == 0:
                insights.append(f"No bluffs in {classified:,} shown hands for {r['line']}.")
            else:
                insights.append(f"Bluffs accounted for {bluffs:,} of {classified:,} shown hands in {r['line']}.")
        elif r["line_id"].startswith("RIVER_oop_turn_call_flush_river_lead") and r["bluff_pct"] <= 10:
            insights.append(f"Bluffs made up {r['bluff_pct']:.1f}% of shown hands in {r['line']}. That line looks value-heavy in this sample.")
        elif r["category"] == "River pressure" and r["bluff_pct"] <= 6:
            insights.append(f"Only {r['bluff_pct']:.1f}% of the shown hands in {r['line']} were bluffs.")
        elif r["line_id"].startswith("TURN_check_raise") and r["weak_air_pct"] <= 10:
            insights.append(f"In {r['line']}, the hands shown were mostly made hands and draws. Weak hands were uncommon.")
        elif r["line_id"].startswith("FLOP_check_raise") and (r["monster_made_pct"] + r["equity_value_pct"]) >= 75:
            insights.append(f"Made hands and draws accounted for {r['monster_made_pct'] + r['equity_value_pct']:.1f}% of the shown hands in {r['line']}.")
        elif r["line_id"] == "RIVER_bet_bet_bet" and r["bluff_pct"] >= 8:
            insights.append(f"Bluffs made up {r['bluff_pct']:.1f}% of the shown hands in {r['line']}. Check the blockers in those hands when reviewing calls.")
        if len(insights) >= limit:
            break
    for r in pre_friendly:
        if len(insights) >= limit:
            break
        if r["line_id"] == "PFR_4bet_plus_shown":
            insights.append(f"AAxx made up {r['AAxx_pct']:.1f}% of the hands shown in {r['line']}.")
        elif r["line_id"] == "PFR_3bet_shown":
            insights.append(f"In {r['line']}, {r['three_plus_broadways_pct']:.1f}% of shown hands had at least three broadway cards.")
    return insights[:limit]


def html_bar(p: float, cls: str = "bar") -> str:
    p = max(0.0, min(100.0, float(p)))
    return f"<div class='bar-bg'><div class='{cls}' style='width:{p:.1f}%'></div><span>{p:.1f}%</span></div>"


def css_class_for_light(light: str) -> str:
    return {"green": "tag-green", "yellow": "tag-yellow", "red": "tag-red"}.get(light, "tag-gray")


def make_presentation_html(filters: Dict[str, Any], post_friendly: List[Dict[str, Any]], pre_friendly: List[Dict[str, Any]], insights: List[str]) -> str:
    total_known = sum(int(r["known_hands"]) for r in post_friendly)
    river_lines = [r for r in post_friendly if r["category"] == "River pressure"]
    low_bluff_river = sum(1 for r in river_lines if r["bluff_pct"] <= 6)
    css = """
    :root{--bg:#0b1020;--panel:#111827;--panel2:#172033;--text:#e5e7eb;--muted:#9ca3af;--line:#293244;--green:#22c55e;--yellow:#f59e0b;--red:#ef4444;--blue:#38bdf8;--purple:#a78bfa;--accent:#e8a15b;--accent-soft:rgba(232,161,91,.08);}
    *{box-sizing:border-box} body{margin:0;background:linear-gradient(135deg,#0b1020 0%,#141927 55%,#0b1020 100%);color:var(--text);font-family:Inter,Segoe UI,Arial,sans-serif;line-height:1.45}
    .deck{max-width:1180px;margin:0 auto;padding:32px 28px 60px}.hero{padding:34px;border:1px solid var(--line);border-top:3px solid var(--accent);background:linear-gradient(135deg,rgba(56,189,248,.16),rgba(232,161,91,.06));border-radius:24px;box-shadow:0 24px 80px rgba(0,0,0,.35)}
    h1{font-size:38px;line-height:1.05;margin:0 0 10px} h2{font-size:24px;margin:34px 0 14px;padding-left:12px;border-left:3px solid var(--accent)} h3{font-size:18px;margin:0 0 12px}.subtitle{color:var(--muted);font-size:16px;max-width:900px}.meta{display:flex;flex-wrap:wrap;gap:10px;margin-top:22px}.pill{padding:8px 12px;border-radius:999px;background:rgba(255,255,255,.08);border:1px solid var(--line);color:#d1d5db;font-size:13px}
    .kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-top:22px}.kpi{background:rgba(17,24,39,.78);border:1px solid var(--line);border-radius:18px;padding:18px}.kpi .num{font-size:28px;font-weight:750}.kpi .lbl{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
    .insights{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.insight{background:rgba(17,24,39,.82);border:1px solid var(--line);border-radius:18px;padding:16px}.insight b{color:var(--accent)}.small{font-size:12px;color:var(--muted)}
    .section-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.card{background:rgba(17,24,39,.86);border:1px solid var(--line);border-radius:20px;padding:18px;box-shadow:0 14px 50px rgba(0,0,0,.25)}.card-header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:10px}.card-title{font-size:18px;font-weight:750}.tags{display:flex;gap:8px;flex-wrap:wrap}.tag{font-size:11px;font-weight:750;letter-spacing:.04em;padding:5px 8px;border-radius:999px}.tag-green{background:rgba(34,197,94,.16);color:#86efac;border:1px solid rgba(34,197,94,.35)}.tag-yellow{background:rgba(245,158,11,.16);color:#fde68a;border:1px solid rgba(245,158,11,.35)}.tag-red{background:rgba(239,68,68,.16);color:#fca5a5;border:1px solid rgba(239,68,68,.35)}.tag-gray{background:rgba(156,163,175,.12);color:#d1d5db;border:1px solid rgba(156,163,175,.3)}
    .metrics{display:grid;grid-template-columns:1fr;gap:9px;margin:12px 0}.metric{display:grid;grid-template-columns:145px 1fr;gap:10px;align-items:center}.metric label{font-size:12px;color:var(--muted)}.bar-bg{height:22px;background:#0b1220;border:1px solid #263246;border-radius:999px;position:relative;overflow:hidden}.bar-bg span{position:absolute;right:8px;top:2px;font-size:12px;color:#fff;font-weight:700}.bar{height:100%;background:linear-gradient(90deg,var(--blue),var(--purple));border-radius:999px}.bar-good{height:100%;background:linear-gradient(90deg,#22c55e,#84cc16)}.bar-warn{height:100%;background:linear-gradient(90deg,#f59e0b,#f97316)}.bar-bad{height:100%;background:linear-gradient(90deg,#ef4444,#f97316)}
    .note{font-size:13px;color:#d1d5db;background:rgba(255,255,255,.045);border:1px solid var(--line);border-radius:14px;padding:11px;margin-top:10px}.table-wrap{overflow-x:auto;background:rgba(17,24,39,.7);border:1px solid var(--line);border-radius:18px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left}th{color:#f3f4f6;background:rgba(255,255,255,.06);font-size:12px;text-transform:uppercase;letter-spacing:.04em}tr:last-child td{border-bottom:0}.footer{margin-top:34px;color:var(--muted);font-size:12px}.project-note{border-left:4px solid var(--accent);padding:12px 14px;background:var(--accent-soft);border-radius:12px;margin-top:16px;color:#e7ddd3}
    @media(max-width:900px){.kpis,.insights,.section-grid{grid-template-columns:1fr}h1{font-size:30px}.metric{grid-template-columns:120px 1fr}}
    @media print{body{background:#fff;color:#111}.deck{max-width:none}.hero,.card,.insight,.kpi,.table-wrap{box-shadow:none;background:#fff;color:#111}.subtitle,.small,.metric label,.footer{color:#555}.bar-bg{border-color:#ccc;background:#eee}.tag-green,.tag-yellow,.tag-red,.tag-gray{border-color:#aaa;color:#111;background:#eee}}
    """
    parts = ["<!doctype html><html><head><meta charset='utf-8'>", "<title>CoinPoker Population Strength Report</title>", f"<style>{css}</style>", "</head><body><main class='deck'>"]
    parts.append("<section class='hero'>")
    parts.append("<h1>CoinPoker Population Strength Report</h1>")
    parts.append("<p class='subtitle'>What hands did opponents show after taking these PLO lines?</p>")
    parts.append("<div class='meta'>")
    for label, val in [("Game", filters.get("game_filter")), ("Stake", filters.get("stake_filter")), ("Min stack", str(filters.get("min_stack_bb"))+"bb"), ("Hands after filters", f"{int(filters.get('hands_analyzed_after_filters',0)):,}"), ("Known postflop records", f"{int(filters.get('postflop_known_line_records',0)):,}")]:
        parts.append(f"<span class='pill'><b>{html.escape(str(label))}:</b> {html.escape(str(val))}</span>")
    parts.append("</div>")
    parts.append("<div class='kpis'>")
    kpi_data = [("Postflop line types", len(post_friendly)), ("Known-card line samples", total_known), ("River line types", len(river_lines)), ("River lines with <=6% shown bluff", low_bluff_river)]
    for lbl, val in kpi_data:
        parts.append(f"<div class='kpi'><div class='num'>{val:,}</div><div class='lbl'>{html.escape(lbl)}</div></div>")
    parts.append("</div>")
    parts.append("<div class='small' style='margin-top:10px;color:#cbd5e1'>A hand can count toward more than one line. Bluff percentages cover hands where the cards were known.</div>")
    parts.append("<div class='project-note'>The hand-level CSVs list the cards and actions behind each line.</div>")
    parts.append("</section>")

    parts.append("<h2>Highlights from shown hands</h2><div class='insights'>")
    for i, ins in enumerate(insights or ["No highlights for this sample."], start=1):
        parts.append(f"<div class='insight'><b>{i}.</b> {html.escape(ins)}</div>")
    parts.append("</div>")

    for cat in ["River pressure", "Turn aggression / probes", "Flop aggression", "Postflop line types"]:
        rows = [r for r in post_friendly if r["category"] == cat]
        if not rows:
            continue
        parts.append(f"<h2>{html.escape(cat)}</h2><div class='section-grid'>")
        for r in rows:
            tag_cls = css_class_for_light(r["traffic_light"])
            parts.append("<article class='card'>")
            parts.append("<div class='card-header'>")
            board_label = str(r.get('river_board_family_distribution') or r.get('river_board_family') or r.get('board_bucket') or 'mixed') if cat == "River pressure" else str(r.get('board_bucket') or 'mixed')
            parts.append(f"<div><div class='card-title'>{html.escape(r['line'])}</div><div class='small'>{r['known_hands']} known hands · board mix: {html.escape(board_label)}</div></div>")
            parts.append(f"<div class='tags'><span class='tag {tag_cls}'>{html.escape(r['read_label'])}</span><span class='tag tag-gray'>{html.escape(r['confidence'])}</span></div>")
            parts.append("</div>")
            if cat == "River pressure":
                parts.append("<div class='metrics'>")
                cls_vals = []
                for cls in RIVER_COMPACT_CLASS_ORDER:
                    v = float(r.get('plo_' + cls + '_pct', 0.0) or 0.0)
                    if v > 0:
                        cls_vals.append((cls, v))
                shown = 0
                for cls, v in cls_vals[:8]:
                    label = RIVER_PLO_CLASS_LABELS.get(cls, cls)
                    bar_cls = 'bar-bad' if cls == 'bluff' else ('bar-good' if cls in {'practical_nuts','close_to_nuts'} else 'bar')
                    parts.append(f"<div class='metric'><label>{html.escape(label)}</label>{html_bar(v, bar_cls)}</div>")
                    shown += 1
                if shown == 0:
                    parts.append("<div class='small'>No river hand classes available.</div>")
                parts.append("</div>")
            else:
                parts.append("<div class='metrics'>")
                parts.append(f"<div class='metric'><label>Made / monster</label>{html_bar(r['monster_made_pct'], 'bar-good')}</div>")
                parts.append(f"<div class='metric'><label>Equity + value</label>{html_bar(r['equity_value_pct'], 'bar')}</div>")
                parts.append(f"<div class='metric'><label>Weak / air</label>{html_bar(r['weak_air_pct'], 'bar-bad')}</div>")
                parts.append("</div>")
            parts.append(f"<div class='note'>{html.escape(r['plain_english_summary'])}<br>{html.escape(r['exploit_note'])}</div>")
            parts.append("</article>")
        parts.append("</div>")

    if pre_friendly:
        parts.append("<h2>Preflop hands shown</h2><div class='table-wrap'><table><thead><tr><th>Line</th><th>Known</th><th>AAxx</th><th>KKxx</th><th>Double-suited</th><th>3+ broadways</th><th>Connected</th><th>Read</th></tr></thead><tbody>")
        for r in pre_friendly:
            parts.append("<tr>" + "".join([
                f"<td>{html.escape(str(r['line']))}</td>", f"<td>{r['known_hands']}</td>", f"<td>{r['AAxx_pct']:.1f}%</td>", f"<td>{r['KKxx_pct']:.1f}%</td>", f"<td>{r['double_suited_pct']:.1f}%</td>", f"<td>{r['three_plus_broadways_pct']:.1f}%</td>", f"<td>{r['connected_4_window_pct']:.1f}%</td>", f"<td>{html.escape(r['read_label'])}</td>"
            ]) + "</tr>")
        parts.append("</tbody></table></div>")

    river_compact = [r for r in post_friendly if r["category"] == "River pressure"]
    flop_turn_compact = [r for r in post_friendly if r["category"] != "River pressure"]

    if river_compact:
        parts.append("<h2>River hand breakdown</h2>")
        parts.append("<p class='small'>The hand classes depend on the board. For example, practical nuts usually means top boat on a paired board or the nut flush on an unpaired flush board. The percentages in each row add to 100%.</p>")
        parts.append("<div class='table-wrap'><table><thead><tr><th>Line</th><th>Known</th><th>Board</th><th>Read</th><th>Practical nuts</th><th>Close to nuts</th><th>2nd/3rd nut flush</th><th>Medium flush</th><th>Low flush</th><th>Weak straight</th><th>Thin value</th><th>Bluff</th><th>Total</th></tr></thead><tbody>")
        for r in river_compact:
            total = float(r.get('river_total_pct',0.0) or 0.0)
            parts.append("<tr>" + "".join([
                f"<td>{html.escape(str(r['line']))}</td>", f"<td>{r['known_hands']}</td>", f"<td>{html.escape(str(r.get('river_board_family','')))}</td>", f"<td>{html.escape(r['read_label'])}</td>",
                f"<td>{float(r.get('plo_practical_nuts_pct',0.0)):.1f}%</td>", f"<td>{float(r.get('plo_close_to_nuts_pct',0.0)):.1f}%</td>",
                f"<td>{float(r.get('plo_second_third_nut_flush_pct',0.0)):.1f}%</td>", f"<td>{float(r.get('plo_medium_flush_pct',0.0)):.1f}%</td>", f"<td>{float(r.get('plo_low_flush_pct',0.0)):.1f}%</td>",
                f"<td>{float(r.get('plo_weak_straight_pct',0.0)):.1f}%</td>", f"<td>{float(r.get('plo_thin_value_pct',0.0)):.1f}%</td>", f"<td>{float(r.get('plo_bluff_pct',0.0)):.1f}%</td>", f"<td>{total:.1f}%</td>"
            ]) + "</tr>")
        parts.append("</tbody></table></div>")

    if flop_turn_compact:
        parts.append("<h2>Flop and turn hand breakdown</h2>")
        parts.append("<p class='small'>Hands are grouped by made hand strength and draws. The percentages in each row add to 100%.</p>")
        parts.append("<div class='table-wrap'><table><thead><tr><th>Line</th><th>Known</th><th>Read</th><th>Made / monster</th><th>Equity + value</th><th>One-pair / medium</th><th>Weak / air</th><th>Total</th></tr></thead><tbody>")
        for r in flop_turn_compact:
            parts.append("<tr>" + "".join([
                f"<td>{html.escape(str(r['line']))}</td>", f"<td>{r['known_hands']}</td>", f"<td>{html.escape(r['read_label'])}</td>",
                f"<td>{r['flop_turn_monster_pct']:.1f}%</td>", f"<td>{r['flop_turn_equity_value_pct']:.1f}%</td>",
                f"<td>{r['flop_turn_one_pair_medium_pct']:.1f}%</td>", f"<td>{r['flop_turn_weak_air_pct']:.1f}%</td>",
                f"<td>{r['flop_turn_total_pct']:.1f}%</td>"
            ]) + "</tr>")
        parts.append("</tbody></table></div>")

    parts.append("<div class='footer'><b>About the sample</b> Only hands with known cards are included. Many successful bluffs end without a showdown, so the reported bluff share can understate how often players bluff.</div>")
    parts.append("</main></body></html>")
    return "\n".join(parts)



def make_presentation_markdown(filters: Dict[str, Any], post_friendly: List[Dict[str, Any]], pre_friendly: List[Dict[str, Any]], insights: List[str]) -> str:
    md: List[str] = []
    md.append("# CoinPoker Population Strength Report")
    md.append("")
    md.append("The hand-level CSVs list the cards and actions behind each line.")
    md.append("")
    md.append("## Filters")
    for k in ["game_filter", "stake_filter", "date_from", "date_to", "min_stack_bb", "hands_analyzed_after_filters", "postflop_known_line_records", "preflop_known_line_records"]:
        md.append(f"- **{k}**: {filters.get(k)}")
    md.append("")
    md.append("## Highlights from shown hands")
    for i, ins in enumerate(insights, 1):
        md.append(f"{i}. {ins}")
    md.append("")
    md.append("## River hand breakdown")
    md.append("The strength classes depend on the board. See the hand mix for each line.")
    md.append("")
    river_rows = [r for r in post_friendly if r["category"] == "River pressure"]
    rcols = ["line", "known_hands", "read_label", "confidence", "river_board_family", "river_board_family_distribution", "river_plo_top_classes", "river_bluff_pct", "river_total_pct", "plain_english_summary"]
    md.append(markdown_table(river_rows, rcols, max_rows=200))
    md.append("")
    md.append("## Flop and turn hand breakdown")
    md.append("Hands are grouped by made hand strength and draws.")
    md.append("")
    ft_rows = [r for r in post_friendly if r["category"] != "River pressure"]
    ftcols = ["line", "known_hands", "read_label", "confidence", "flop_turn_monster_pct", "flop_turn_equity_value_pct", "flop_turn_one_pair_medium_pct", "flop_turn_weak_air_pct", "flop_turn_total_pct", "plain_english_summary"]
    md.append(markdown_table(ft_rows, ftcols, max_rows=200))
    md.append("")
    md.append("## Preflop hands shown")
    pcols = ["line", "known_hands", "AAxx_pct", "KKxx_pct", "double_suited_pct", "three_plus_broadways_pct", "connected_4_window_pct", "read_label", "plain_english_summary"]
    md.append(markdown_table(pre_friendly, pcols, max_rows=50))
    md.append("")
    md.append("## About the sample")
    md.append("Only hands with known cards are included. Many successful bluffs end without a showdown, so the reported bluff share can understate how often players bluff.")
    return "\n".join(md)


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
    text = input_path.read_text(encoding="utf-8", errors="replace")
    raw = split_hand_history(text)
    hands: List[Hand] = []
    parse_errors = 0
    for block in raw:
        h = parse_hand(block, hero=hero)
        if h is None:
            parse_errors += 1
            continue
        if pass_filters(h, args):
            hands.append(h)

    post_records: List[Dict[str, Any]] = []
    pre_records: List[Dict[str, Any]] = []
    for h in hands:
        post, pre = extract_population_records(h, hero=hero, min_stack_bb=args.min_stack_bb)
        post_records.extend(post)
        pre_records.extend(pre)

    post_summary = summarize_postflop(post_records, min_known=args.min_known)
    pre_summary = summarize_preflop(pre_records, min_known=args.min_known)

    outdir.mkdir(parents=True, exist_ok=True)
    write_csv(post_records, outdir / "population_strength_hands.csv")
    write_csv(post_summary, outdir / "population_strength_by_line.csv")
    write_csv(pre_records, outdir / "preflop_population_hands.csv")
    write_csv(pre_summary, outdir / "preflop_population_by_line.csv")

    filters = {
        "input": str(input_path),
        "hero": hero,
        "game_filter": args.game_filter,
        "include_bombpot": args.include_bombpot,
        "stake_filter": args.stake_filter or "all",
        "date_from": args.date_from or "none",
        "date_to": args.date_to or "none",
        "min_stack_bb": args.min_stack_bb,
        "min_known": args.min_known,
        "raw_hands_before_filters": len(raw),
        "hands_analyzed_after_filters": len(hands),
        "postflop_known_line_records": len(post_records),
        "preflop_known_line_records": len(pre_records),
        "parse_errors": parse_errors,
    }
    (outdir / "filters_applied.json").write_text(json.dumps(filters, indent=2, ensure_ascii=False), encoding="utf-8")

    post_friendly = make_friendly_postflop_summary(post_summary)
    pre_friendly = make_friendly_preflop_summary(pre_summary)
    insights = top_insights(post_friendly, pre_friendly, limit=6, post_summary=post_summary)

    write_csv(post_friendly, outdir / "population_strength_summary_friendly.csv")
    river_compact_rows = []
    for r in post_friendly:
        if r["category"] != "River pressure":
            continue
        river_compact_rows.append({
            "line": r["line"],
            "known_hands": r["known_hands"],
            "board_family": r.get("river_board_family", ""),
            "read_label": r["read_label"],
            "confidence": r["confidence"],
            "practical_nuts_pct": r.get("plo_practical_nuts_pct", 0.0),
            "close_to_nuts_pct": r.get("plo_close_to_nuts_pct", 0.0),
            "second_third_nut_flush_pct": r.get("plo_second_third_nut_flush_pct", 0.0),
            "medium_flush_pct": r.get("plo_medium_flush_pct", 0.0),
            "low_flush_pct": r.get("plo_low_flush_pct", 0.0),
            "weak_straight_pct": r.get("plo_weak_straight_pct", 0.0),
            "thin_value_pct": r.get("plo_thin_value_pct", 0.0),
            "bluff_pct": r.get("plo_bluff_pct", 0.0),
            "total_pct": r["river_total_pct"],
            "top_classes": r.get("river_plo_top_classes", ""),
            "plain_english_summary": r["plain_english_summary"],
            "exploit_note": r["exploit_note"],
        })
    flop_turn_compact_rows = [
        {
            "line": r["line"],
            "known_hands": r["known_hands"],
            "read_label": r["read_label"],
            "confidence": r["confidence"],
            "made_monster_pct": r["flop_turn_monster_pct"],
            "equity_value_pct": r["flop_turn_equity_value_pct"],
            "one_pair_medium_pct": r["flop_turn_one_pair_medium_pct"],
            "weak_air_pct": r["flop_turn_weak_air_pct"],
            "total_pct": r["flop_turn_total_pct"],
            "plain_english_summary": r["plain_english_summary"],
            "exploit_note": r["exploit_note"],
        }
        for r in post_friendly if r["category"] != "River pressure"
    ]
    write_csv(river_compact_rows, outdir / "population_strength_summary_river_compact.csv")
    write_csv(flop_turn_compact_rows, outdir / "population_strength_summary_flop_turn_compact.csv")
    write_csv(river_compact_rows + flop_turn_compact_rows, outdir / "population_strength_summary_compact.csv")
    write_csv(pre_friendly, outdir / "preflop_population_summary_friendly.csv")

    report_md = clean_output_text(make_presentation_markdown(filters, post_friendly, pre_friendly, insights))
    (outdir / "population_strength_presentation.md").write_text(report_md, encoding="utf-8")

    html_text = clean_output_text(make_presentation_html(filters, post_friendly, pre_friendly, insights))
    (outdir / "population_strength_presentation.html").write_text(html_text, encoding="utf-8")

    # Keep backward-compatible file names, but make them human-readable now.
    (outdir / "population_strength_report.md").write_text(report_md, encoding="utf-8")
    (outdir / "population_strength_report.html").write_text(html_text, encoding="utf-8")

    return filters


def main() -> None:
    p = argparse.ArgumentParser(description="CoinPoker PLO population strength analyzer v14 BETA")
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
    args = p.parse_args()

    result = analyze(Path(args.input), args.hero, Path(args.outdir), args)
    print("Done.")
    print(f"Raw hands before filters: {result['raw_hands_before_filters']:,}")
    print(f"Hands analyzed after filters: {result['hands_analyzed_after_filters']:,}")
    print(f"Postflop known line records: {result['postflop_known_line_records']:,}")
    print(f"Preflop known line records: {result['preflop_known_line_records']:,}")
    print(f"Output: {args.outdir}")
    print("Open population_strength_presentation.html first. Raw CSV files remain available for drilldown.")


if __name__ == "__main__":
    main()
