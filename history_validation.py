"""Integrity checks for complete CoinPoker cash-game histories."""
import collections
import re
from datetime import datetime

AMOUNT = r"(?:0|[1-9]\d*)(?:\.\d+)?"
CARD = re.compile(r"[2-9TJQKA][cdhs]")
STREETS = {"FLOP": 3, "TURN": 4, "RIVER": 5}
STREET = re.compile(r"\*\*\* (?:(FIRST|SECOND|THIRD) )?(FLOP|TURN|RIVER) \*\*\* (.+)")
BOARD = re.compile(r"(?:(FIRST|SECOND|THIRD) )?Board (\[.*\])")
SEAT = re.compile(r"Seat (\d+): (.+?) \(₮(" + AMOUNT + r") in chips\)")
HAND_ID = re.compile(r"CoinPoker Hand #(\d+):")
TOTAL = re.compile(r"Total pot ₮(" + AMOUNT + r")(?: \|.*)?")
WAGER = re.compile(r"(?:posts (?:ante|small blind|big blind|auto big blind)|STRADDLE|bets|calls|ALLIN|RETURN) ₮" + AMOUNT + r"|raises ₮" + AMOUNT + r" to ₮" + AMOUNT + r"|folds|checks")
SHOW = re.compile(r"(.+?): shows (\[[^\]]+\])(?: .*)?")
SHOWN = re.compile(r"Seat \d+: (.+?) showed (\[[^\]]+\])(?: .*)?")


def cards_in(text):
    groups = re.findall(r"\[([^\]]*)\]", text)
    if not groups or re.sub(r"\[[^\]]*\]", "", text).strip():
        raise ValueError("invalid_card_syntax")
    cards = [card for group in groups for card in group.split()]
    if any(not CARD.fullmatch(c) for c in cards):
        raise ValueError("invalid_card_token")
    if len(set(cards)) != len(cards):
        raise ValueError("duplicate_physical_card")
    return cards


def unique_blocks(blocks, issues):
    """Keep one copy per ID, reject all versions when the same ID differs."""
    seen = {}
    counts = collections.Counter()
    conflicts = set()
    for block in blocks:
        normalized = "\n".join(line.strip() for line in block.splitlines() if line.strip())
        header = HAND_ID.match(normalized)
        if not header:
            issues["malformed_header"] += 1
            continue
        key = header[1]
        counts[key] += 1
        if key not in seen:
            seen[key] = normalized
        elif normalized != seen[key]:
            conflicts.add(key)
    duplicates = sum(n - 1 for key, n in counts.items() if key not in conflicts)
    if conflicts:
        issues["conflicting_hand_id"] += sum(counts[key] for key in conflicts)
    return (block for key, block in seen.items() if key not in conflicts), duplicates, sorted(conflicts)


def validate(block, meta, hero):
    lines = block.splitlines()
    if (meta is None or not meta["bb"] or meta["bb"] <= 0 or not re.fullmatch(
        rf"₮{AMOUNT}/₮{AMOUNT}(?:/₮{AMOUNT})?", meta["stakes_str"]
    )):
        raise ValueError("malformed_header")
    try:
        datetime.strptime(meta["dt"], "%Y/%m/%d %H:%M:%S")
    except ValueError:
        raise ValueError("invalid_hand_date") from None
    if meta["plo_cards"] not in (4, 5):
        raise ValueError("unsupported_game")
    if lines.count("*** SUMMARY ***") != 1 or "*** HOLE CARDS ***" not in lines:
        raise ValueError("incomplete_hand")
    split = lines.index("*** SUMMARY ***")
    if not any(re.search(r"Seat #\d+ is the button", line) for line in lines[:split]):
        raise ValueError("missing_button")
    seat_lines = [line for line in lines[:split] if line.startswith("Seat ")]
    seats = {int(m[1]): m[2] for line in seat_lines if (m := SEAT.fullmatch(line))}
    names = set(seats.values())
    if len(seats) < 2 or len(names) != len(seats) or len(seats) != len(seat_lines):
        raise ValueError("invalid_seats")
    if hero not in names:
        raise ValueError("hero_not_seated")
    summary_seats = {int(m[1]) for line in lines[split:] if (m := re.match(r"Seat (\d+): ", line))}
    totals = [float(m[1]) for line in lines[split:] if (m := TOTAL.fullmatch(line))]
    if summary_seats != set(seats) or len(totals) != 1:
        raise ValueError("incomplete_summary")

    hole, streets, summaries = {}, collections.defaultdict(dict), {}
    for line in lines:
        street = STREET.fullmatch(line)
        summary = BOARD.fullmatch(line)
        if street:
            key, stage, text = street.groups()
            cards = cards_in(text)
            if len(cards) != STREETS[stage] or stage in streets[key]:
                raise ValueError("invalid_board_length_or_street")
            streets[key][stage] = cards
            continue
        if summary:
            if summary[1] in summaries:
                raise ValueError("duplicate_board_summary")
            summaries[summary[1]] = cards_in(summary[2])
            continue
        if re.match(r"(?:\*\*\* )?(?:FIRST |SECOND |THIRD )?(?:Board |FLOP|TURN|RIVER)", line):
            raise ValueError("invalid_board_syntax")
        show = SHOW.fullmatch(line) or SHOWN.fullmatch(line)
        if line.startswith("Dealt to ") and "[" in line:
            show = re.fullmatch(r"Dealt to (.+?) (\[.*\])", line)
            if not show:
                raise ValueError("invalid_hole_cards")
        if show:
            player = show[1]
            cards = sorted(cards_in(show[2]))
            if player not in names or len(cards) != meta["plo_cards"] or hole.get(player, cards) != cards:
                raise ValueError("inconsistent_hole_cards")
            hole[player] = cards
            continue
        if " showed [" in line or ": shows " in line or ("[" in line and line.startswith("Seat ")):
            raise ValueError("unsupported_card_disclosure")
        player, sep, action = line.partition(": ")
        if sep and player in names and action != "mucks hand" and not WAGER.fullmatch(action):
            raise ValueError("unsupported_action")
        if sep and player not in names and WAGER.fullmatch(action):
            raise ValueError("unknown_actor")

    exposed = [card for cards in hole.values() for card in cards]
    runouts = {"Hand was run once": {None}, "Hand was run two times": {"FIRST", "SECOND"},
               "Hand was run three times": {"FIRST", "SECOND", "THIRD"},
               "Hand was run with two boards": {"FIRST", "SECOND"}}
    if any(set(summaries) != runouts[line] for line in lines if line in runouts):
        raise ValueError("inconsistent_runout_count")
    if "Hand was run with two boards" in lines and len({len(cards) for cards in summaries.values()}) != 1:
        raise ValueError("incomplete_double_board")
    if not summaries or (set(streets) != set(summaries) and not (not streets and summaries == {None: []})):
        raise ValueError("missing_or_conflicting_board_summary")
    for key, run in streets.items():
        last = summaries[key]
        for stage, cards in run.items():
            if cards != last[:STREETS[stage]]:
                raise ValueError("inconsistent_board")
        if len(last) != max(map(len, run.values())):
            raise ValueError("incomplete_board")
        first_stage = min(STREETS[stage] for stage in run)
        shared = 0
        if key in {"SECOND", "THIRD"} and first_stage > 3:
            shared = first_stage - 1
            if last[:shared] != summaries.get("FIRST", [])[:shared] or "Hand was run with two boards" in lines:
                raise ValueError("inconsistent_shared_runout")
        expected = [stage for stage, size in STREETS.items() if shared < size <= len(last)]
        if list(run) != expected:
            raise ValueError("missing_or_unordered_street")
        exposed.extend(last[shared:])
    if len(set(exposed)) != len(exposed):
        raise ValueError("duplicate_physical_card")
    return totals[0]
