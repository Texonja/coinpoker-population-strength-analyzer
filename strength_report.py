"""Build every presentation and CSV summary from the same action counts."""
import collections
import html

DRAW_GROUP = {
    "set_trips_boat_plus": "two_pair_or_better",
    "made_straight_or_flush": "two_pair_or_better",
    "two_pair": "two_pair_or_better",
    "pair_plus_good_draw": "pair_or_high_card_with_draw",
    "unpaired_completion_draw": "pair_or_high_card_with_draw",
    "one_pair_no_good_draw": "one_pair_without_draw",
    "weak_or_air": "high_card_without_draw",
}
FEATURES = ("has_aa", "has_kk", "suited_ace", "double_suited", "single_suited",
            "three_plus_broadways", "four_in_five_rank_window")
FEATURE_LABELS = ("AA", "KK", "Suited ace", "Double suited", "Any suited pair",
                  "3+ Broadway cards", "4+ ranks in a five-rank window")
DRAW_LABELS = ("Two pair or better", "Pair or high card + draw", "One pair without qualifying draw",
               "High card without qualifying draw")


def percentage(n, total):
    return round(100 * n / total, 1) if total else ""


def group_rows(rows):
    groups = collections.defaultdict(list)
    for row in rows:
        groups[row["line_id"]].append(row)
    return groups


def coverage_rows(eligible, known):
    rows = []
    for key, actions in sorted(eligible.items()):
        records = known.get(key, [])
        shown = {(r["hand_id"], r["action_index"]) for r in records}
        if len(shown) != len(records) or not shown.issubset(actions):
            raise ValueError(f"Inconsistent actions for {key}")
        row = {"line_id": key, "eligible_actions": len(actions), "known_records": len(shown),
               "eligible_hands": len({h for h, i in actions}), "known_hands": len({h for h, i in shown}),
               "known_pct": percentage(len(shown), len(actions))}
        for status, label in [(True, "called_or_raised"), (False, "no_call_or_raise")]:
            matching = {k for k, continued in actions.items() if continued == status}
            row[label + "_eligible"] = len(matching)
            row[label + "_known"] = len(matching & shown)
        rows.append(row)
    return rows


def summary_rows(groups, coverage, min_known, river_classes):
    post, pre, bomb = [], [], []
    for c in coverage:
        key = c["line_id"]
        if c["known_hands"] < min_known:
            continue
        rows = groups[key]
        n = len(rows)
        if key.startswith("BOMBPOT_"):
            pairs = collections.Counter((r["board_1_made"], r.get("board_2_made", "single board")) for r in rows)
            for (first, second), count in sorted(pairs.items()):
                bomb.append({**c, "board_1_made": first, "board_2_made": second,
                             "records": count, "share_pct": percentage(count, n)})
            continue
        row = {**c, "line_name": rows[0]["line_name"]}
        if key.startswith("PFR_"):
            counts = collections.Counter()
            for r in rows:
                counts.update(k for k in FEATURES[:5] if r[k])
                counts["three_plus_broadways"] += r["broadways"] >= 3
                counts["four_in_five_rank_window"] += r["connected_window"] >= 4
            keys = FEATURES
            row["class_distribution"] = distribution(r["hand_class"] for r in rows)
            target = pre
        else:
            is_river = rows[0]["street"] == "RIVER"
            if is_river:
                count = sum(r["river_unbeaten"] for r in rows)
                row.update(unbeaten_n=count, unbeaten_pct=percentage(count, n))
            keys = river_classes if is_river else tuple(dict.fromkeys(DRAW_GROUP.values()))
            counts = collections.Counter(r["river_plo_class"] if is_river else DRAW_GROUP[r["strength_bucket"]] for r in rows)
            if sum(counts[k] for k in keys) != n:
                raise ValueError(f"Unclassified record in {key}")
            row["made_distribution"] = distribution(r["made_name"] for r in rows)
            row["board_distribution"] = distribution(r["board_bucket"] for r in rows)
            target = post
        for k in keys:
            row[k + "_n"] = counts[k]
            row[k + "_pct"] = percentage(counts[k], n)
        target.append(row)
    return post, pre, bomb


def distribution(values):
    counts = collections.Counter(values)
    return ", ".join(f"{key} ({value})" for key, value in sorted(counts.items()))


def csv_columns(name, river_classes):
    """Keep the export schema readable even when filters select zero actions."""
    coverage = ("line_id eligible_actions known_records eligible_hands known_hands known_pct "
                "called_or_raised_eligible called_or_raised_known no_call_or_raise_eligible no_call_or_raise_known").split()
    if name == "line_card_coverage":
        return coverage
    if name == "bombpot_population_by_line":
        return coverage + ["board_1_made", "board_2_made", "records", "share_pct"]
    if name.endswith("_hands"):
        common = ("line_id line_name hand_id action_index dt stake_label desc pot_type villain villain_position "
                  "villain_stack_bb hero_stack_bb villain_cards villain_action villain_all_in full_raise splash_drop "
                  "action_size_pct_pot chips_pushed_pct_pot action_size_pct_player_funded_pot vs_hero called_or_raised").split()
        if name.startswith("preflop_"):
            return common + ("wager_number straddled hand_class double_suited single_suited connected_window "
                             "high_cards broadways pairs ace suited_ace has_aa has_kk").split()
        fields = ("street hero_position villain_preflop_class villain_double_suited villain_connected_window board "
                  "board_bucket made_name made_group strength_bucket best_cards has_flush_draw has_nut_flush_draw "
                  "flush_draw_suit straight_completion_cards straight_outs straight_outs_approx has_good_straight_draw "
                  "rank_category board_paired board_flush_possible flush_completed_on_turn flush_completed_on_river "
                  "river_plo_class river_plo_class_label river_plo_class_reason river_unbeaten river_practical_value_class "
                  "facing_action facing_size_pct_pot facing_bet_size_pct_pot facing_bet_size_bucket board_count").split()
        return common + fields + [f"board_{i}{suffix}" for i in (1, 2) for suffix in
                                 ("", "_made", "_best_cards", "_straight_outs", "_straight_completion_cards", "_flush_draw", "_unbeaten")]
    base = coverage + ["line_name"]
    if name.startswith("preflop_"):
        base.append("class_distribution")
        keys = FEATURES
    else:
        base += ["made_distribution", "board_distribution"]
        draw = list(dict.fromkeys(DRAW_GROUP.values()))
        river = [*river_classes, "unbeaten"]
        keys = draw if "_flop_turn_" in name else river if "_river_" in name else draw + river
    return base + [key + suffix for key in keys for suffix in ("_n", "_pct")]


def report(filters, coverage, post, pre, bomb, river_labels):
    lines = []
    title = "CoinPoker · Observed hand strength"
    lines.append(("text", f"Game {filters['game_filter'].upper()} · Stakes {filters['stake_filter']} · "
                           f"Hero {filters['hero']} · {filters['action_scope']} · Stack minimum {filters['min_stack_bb']:g} bb "
                           f"· Dates {filters['date_from']} to {filters['date_to']} · Include BombPot {filters['include_bombpot']}"))
    lines.append(("text", f"{filters['hands_after_game_stake_date']:,} hands match the game, stakes and dates. "
                           f"{filters['hands_with_eligible_actions']:,} hands contribute an eligible action. "
                           f"{filters['unique_known_actions']:,} of {filters['unique_eligible_actions']:,} distinct eligible actions have known cards."))
    if filters["input_issues"] or filters["identical_duplicate_blocks"]:
        lines.append(("text", f"Input diagnostics · Skipped blocks {filters['input_issues']} · "
                              f"Repeated identical blocks {filters['identical_duplicate_blocks']}"))
    lines.append(("text", f"Strength percentages count shown actions within a line. Some hands contribute several actions "
                           f"and one action can appear in several lines. Strength tables require {filters['min_known']} distinct "
                           "hands with shown cards. Percentages round independently to one decimal."))
    def table(heading, headers, data):
        lines.append(("heading", heading))
        if data:
            lines.append(("table", (headers, data)))
        else:
            lines.append(("text", "No qualifying observations for this table."))
    def counts(rows, keys):
        return [[r["line_name"], f"{r['known_records']} / {r['eligible_actions']}", r["known_hands"],
                 *[f"{r[k + '_n']} ({r[k + '_pct']:.1f}%)" for k in keys]] for r in rows]
    base = ["Line", "Known / eligible actions", "Known hands"]
    table("Preflop starting cards", base + list(FEATURE_LABELS), counts(pre, FEATURES))
    lines.append(("text", "Preflop features can overlap. A hand holding two aces and two kings enters both columns."))
    draws = [r for r in post if not r["line_id"].startswith("RIVER_")]
    table("Flop and turn strength", base + list(DRAW_LABELS), counts(draws, dict.fromkeys(DRAW_GROUP.values())))
    lines.append(("text", "A qualifying draw has an immediate flush draw or at least eight distinct next cards that complete "
                           "a straight using two hole cards. A completion can lose to another hand."))
    rivers = [r for r in post if r["line_id"].startswith("RIVER_")]
    table("River strength", base + list(river_labels.values()) + ["Unbeaten (overlapping)"], counts(rivers, [*river_labels, "unbeaten"]))
    lines.append(("text", "Practical nuts groups the top ordinary tier with quads and straight flushes. Unbeaten checks every legal opposing pair and allows ties. Top two pair requires "
                           "both highest distinct board ranks in the hand and in the best two pair. These classes describe the cards shown."))
    table("BombPot hand strength across boards", ["Line", "Board 1", "Board 2", "Actions", "Share of known actions"],
          [[r["line_id"], r["board_1_made"], r["board_2_made"], r["records"], f"{r['share_pct']:.1f}%"] for r in bomb])
    table("Card coverage by line", ["Line", "Known / eligible actions", "Coverage", "Known / eligible hands",
                                   "Called or raised · known / eligible", "No call or raise · known / eligible"],
          [[r["line_id"], f"{r['known_records']} / {r['eligible_actions']}", f"{r['known_pct']:.1f}%",
            f"{r['known_hands']} / {r['eligible_hands']}", f"{r['called_or_raised_known']} / {r['called_or_raised_eligible']}",
            f"{r['no_call_or_raise_known']} / {r['no_call_or_raise_eligible']}"] for r in coverage])
    lines.append(("text", "Response columns follow the focal wager until the next raise. Card disclosure can differ sharply "
                           "between called and uncalled wagers. The hidden cards and betting intentions are unknown."))
    def esc(v):
        return html.escape(str(v))
    def md_escape(v):
        return str(v).replace("\\", "\\\\").replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")
    md = [f"# {title}"]
    page = ['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{title}</title><style>body{{font:16px/1.5 system-ui;color:#17302c;background:#f5f7f4;margin:0}}"
            "main{max-width:1600px;margin:auto;padding:2rem clamp(1rem,4vw,5rem)}h1{color:#185a49;line-height:1.1}"
            "h2{margin:2.2rem 0 .7rem}p{max-width:105ch}.table{overflow:auto;background:white;border:1px solid #cadad4}"
            "table{border-collapse:collapse;width:100%;font-size:.86rem;font-variant-numeric:tabular-nums}th,td{padding:.6rem;text-align:left;"
            "border-bottom:1px solid #dce6e2}th{background:#194c42;color:white}tr:nth-child(even){background:#edf4f0}"
            "td:not(:first-child){white-space:nowrap}</style><main>", f"<h1>{title}</h1>"]
    for kind, value in lines:
        if kind == "table":
            headers, data = value
            page.append('<div class="table"><table><thead><tr>' + "".join(f"<th>{esc(v)}</th>" for v in headers) + "</tr></thead><tbody>")
            md.append("| " + " | ".join(map(md_escape, headers)) + " |\n| " + " | ".join("---" for _ in headers) + " |")
            for cells in data:
                page.append("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in cells) + "</tr>")
            md[-1] += "".join("\n| " + " | ".join(map(md_escape, cells)) + " |" for cells in data)
            page.append("</tbody></table></div>")
        else:
            tag = "h2" if kind == "heading" else "p"
            page.append(f"<{tag}>{esc(value)}</{tag}>")
            md.append(("## " if kind == "heading" else "") + md_escape(value))
    return "\n".join(page) + "</main></html>\n", "\n\n".join(md) + "\n"
