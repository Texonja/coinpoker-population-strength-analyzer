# CoinPoker Population Strength Analyzer - BETA

A presentation-style population analysis tool for CoinPoker PLO hand histories. It counts eligible opponent wagers in selected preflop and postflop lines and classifies the hands whose cards were disclosed.

The default scope includes opponents at Hero’s table, including decisions after Hero folds. Use `--vs-hero` to select direct interactions.

![Presentation preview](screenshots/presentation_preview.jpg)

## Quick demo

Requirements: **Python 3.9+** on Windows. The analyzer uses only the Python standard library.

1. Extract the Windows ZIP from the [latest release](https://github.com/Texonja/coinpoker-population-strength-analyzer/releases/latest), or clone the repository.
2. Double-click `run_population_strength_beta.bat`.
3. Each prompt shows **Press ENTER for default**. Press Enter to use the included defaults, or type your own value.

The default run uses:

```text
Input: cash_example.txt
Hero: Hero
Game: PLO4 regular only
Stakes: all
Min stack: 40bb+
Min distinct hands with known cards per line: 10
```

The BAT file opens the main report automatically when processing finishes:

```text
population_strength_report\population_strength_presentation.html
```

## Included public sample

`cash_example.txt` contains **10,000 sanitized CoinPoker hands** for testing the analyzer:

```text
- Hero name standardized to Hero
- hand IDs rewritten to a synthetic sequence
- table IDs rewritten
- dates and times shifted to a synthetic period
- opponent identifiers remain anonymous hexadecimal IDs
```

The poker actions and cards are left structurally intact so the parser and reports can be tested on realistic hand histories.

A generated PL25 example report is included here:

```text
examples/coinpoker_population_strength_sample_pl25.zip
```

## Run on your own hand history

Double-click the BAT file and enter your own filename and Hero screen name when prompted, or run the script directly:

```powershell
python coinpoker_population_strength_analyzer.py --input "cash.txt" --hero YourHeroName --outdir population_strength_report --game-filter plo4 --min-stack-bb 40 --min-known 10
```

Example with a stake filter:

```powershell
python coinpoker_population_strength_analyzer.py --input "cash.txt" --hero YourHeroName --outdir pop_strength_pl50 --stake-filter PL50 --min-stack-bb 40 --min-known 10
```

Useful filters include:

```text
--game-filter plo4|plo5|bombpot|all
--stake-filter PL25,PL50
--date-from YYYY-MM-DD
--date-to YYYY-MM-DD
--min-stack-bb 40
--min-known 10
--include-bombpot
--vs-hero
```

## Main outputs

```text
population_strength_presentation.html   readable presentation report
population_strength_by_line.csv         aggregated postflop line data
population_strength_hands.csv           hand-level postflop records
preflop_population_by_line.csv          aggregated preflop data
preflop_population_hands.csv            hand-level preflop records
line_card_coverage.csv                   eligible and classified actions for every observed line
bombpot_population_by_line.csv           joint hand-strength distribution across BombPot boards
filters_applied.json                     run settings and sample counts
```

## How to read a line

The report counts eligible actions, actions with disclosed cards, and distinct hand IDs. An action is identified by the hand ID and action index. A re-raise and a later raise in the same hand are different actions. Texture and size lines can describe the same wager. The headline counts each wager once across lines.

Percentages in a strength table use the number of **known actions on that line**. Each cell includes the numerator. Percentages round independently to one decimal place. The coverage table also breaks down disclosure by whether anyone called or raised. Every observed eligible line appears in coverage. The strength tables require the configured minimum number of **distinct hands** with disclosed cards.

### Action definitions

- A first-in raise and a first raise after one or more limps have separate lines. In the preflop CSV, `wager_number` counts successive price increases with the live blind as the first wager. A short all-in that increases the price counts as a raise. `full_raise` identifies increases reaching the minimum full increment. `straddled` identifies hands with a posted straddle.
- The parser compares each all-in with the live street wager. An all-in call, including one for less than the amount due, stays a call.
- A flop donk leads into the last preflop raiser before that player acts. A turn probe follows a checked flop and leads into that raiser. A river probe follows a checked turn and leads into the last flop aggressor. The aggressor must have a subsequent action. Other opening bets after checks have separate labels for delayed follow-ups, bets after the aggressor checks, and pots without an active aggressor.
- A barrel follows the same player's unraised opening bet on the previous street. A triple barrel requires that sequence on both the flop and turn. A river raise against an opening bet and a river re-raise facing a raise have separate lines and sizing buckets.
- `--vs-hero` keeps a villain wager if Hero calls, folds or raises before any intervening raise, or if the villain raises a live wager by Hero. It applies to regular, preflop and BombPot records. The default table-wide mode and this filter both require the configured starting stacks for Hero and the acting opponent.
- `action_size_pct_pot` stores a fraction. A bet divides chips bet by the gross pot before the bet. A raise divides the increase **above the call** by the pot **after calling**. `chips_pushed_pct_pot` divides all chips pushed by the pot before acting. `action_size_pct_player_funded_pot` applies the wager formula after subtracting Splash drops. Size buckets use unrounded wager fractions.

### Card definitions

- Preflop features overlap. AA and KK each require at least two cards of that rank. A suited ace shares its suit with another hole card. Rank windows include A–2–3–4–5. The hand-class string assigns one primary category.
- On an unpaired flop, three cards of one suit are monotone. A two-tone flop with all three ranks in one five-rank window is wet. Paired flops have their own line.
- A draw completion is a distinct legal next card that completes the specified hand using two hole cards and three board cards. A qualifying draw is an immediate flush draw or at least eight straight completion cards. Backdoors and straight redraws are excluded. A completion can still lose. The calculation uses the player's hole cards and visible board cards at that decision. In a double-board BombPot it also removes the cards visible on the other board. The fields `straight_outs` and `straight_outs_approx` mirror `straight_completion_cards`.
- Each river record compares the player's rank with every available opposing hole-card pair. `river_unbeaten` allows a tie. `practical_nuts` also includes quads, straight flushes and hands that can be beaten only by either category. Flush tiers count higher attainable suited hole-card ranks. Top two pair requires both highest distinct board ranks paired by hole cards and present as the two pairs in the best hand.
- The eight river classes are exclusive. The separate Unbeaten column overlaps them. The last strength group covers other two-pair hands, one pair, and high card. A player's intent, and the cards of opponents who folded unseen, are unknown.
- BombPot rows contain one action index and separate board columns. River `unbeaten` checks remove cards exposed on both boards. Regular multi-runout hands retain the betting street and first runout for action analysis.

### Input and export changes in 0.2.0

Identical repeated histories with the same ID count once. Conflicting copies of an ID are all excluded. The analyzer checks board progression, multi-runout shared cards, player disclosures, physical card uniqueness, recognized actions, wager accounting and the reported gross pot. Partial imports show reasons and counts in the report and JSON. Wholly invalid histories and a Hero absent from all supported hands produce a nonzero exit code. Valid histories with no qualifying actions produce a zero-sample report.

The summary CSVs share one set of descriptive class columns with count and percentage suffixes. Consumers of 0.1.0 should migrate the legacy bluff, confidence and strategic-read columns. `known_records` counts actions and `known_hands` counts distinct hands. The game/stake/date count is `hands_after_game_stake_date`. The eligible-hand count is `hands_with_eligible_actions`.

Run the regression suite from the repository root with `python -m unittest discover -v`. Run `python verify_public_sample.py` to reconstruct the sample report in a temporary directory and cross-check gross pots, all showdown labels, exported best-five cards and river classifications with an independent evaluator.

## Create another public sample

Raw CoinPoker exports may still reveal your Hero name, exact hand IDs, table IDs, and session times. To create a safer sample:

1. Put the raw hand-history file next to the scripts.
2. Double-click `make_public_sample.bat`.
3. Review the generated file manually before committing it.

Default output:

```text
examples\public_sample_hands.txt
```

## Beta limitations

Hand histories disclose a selected subset of opponent cards. These reports describe observed wagers and shown hands in the imported sample. Player intent, population-wide bluff rates and unknown holdings cannot be recovered from these data. New CoinPoker export formats may require parser updates.
