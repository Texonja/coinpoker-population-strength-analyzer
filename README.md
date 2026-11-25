# CoinPoker Population Strength Analyzer - BETA

A presentation-style population analysis tool for CoinPoker PLO hand histories. It parses known/showdown cards and reports what opponents actually show when they take specific preflop, flop, turn, and river lines.

This is not a solver. It is population evidence from known cards, with explicit sample-size and selection-bias warnings.

![Presentation preview](screenshots/presentation_preview.jpg)

## Quick demo

Requirements: **Python 3** on Windows. The analyzer uses only the Python standard library.

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
Min known hands per line: 10
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

## How to read the corrected reports

The card coverage table counts every eligible opponent action after the game, stake, date and stack filters. Its "known records" column counts actions with the cards needed for classification. Hand-strength percentages use those known records. A line with zero shown hands still appears in the coverage table.

- An `ALLIN` token can be a bet, raise or call. The parser compares the player's new street contribution with the live wager. Hand-level rows retain the all-in flag and action index.
- A flop donk is an opening bet before the last preflop raiser's first flop action. Limped pots and bets following that raiser's check are excluded.
- A straight out is a distinct available next card that completes a straight using two hole cards and three board cards. Backdoors and redraws for an already made straight are excluded. The older CSV field `straight_outs_approx` mirrors `straight_outs`.
- River flush tiers use the highest suited cards a legal opponent can hold. The existing practical-nuts convention groups the top ordinary flush with rare straight flushes on unpaired boards.
- Bet sizes use the gross pot before the action, including recorded blinds, straddles and Splash drops. In Splash hands, `action_size_pct_player_funded_pot` also gives the fraction using only player contributions. Both size fields store fractions (0.50 means 50%).
- BombPot rows in `population_strength_hands.csv` have one action index and separate board columns. They show the made hand, best five cards and draws on each board. On the river, `unbeaten` allows ties and checks all legal opposing hole-card pairs with both boards and the player's cards removed. The BombPot summary groups board combinations together. Regular hands run twice retain the board and street where betting occurred.

Run the regression suite from the repository root with `python -m unittest discover -v`.

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

- Only known/showdown cards can be classified.
- Folded bluffs are normally invisible, so observed bluff frequency is biased downward.
- A single shown hand can match more than one line type; line records are not unique hands.
- River strength classes are practical rule-based PLO categories, not solver output.
- Showdown samples are selection-biased and should not be read as exact full-range frequencies.
- CoinPoker hand-history format changes may require parser updates.

## Repository status

**BETA:** the end-to-end workflow is functional, but line definitions and classifications may still be refined as more hand-history formats and edge cases are tested.
