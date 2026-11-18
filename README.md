# CoinPoker Population Strength Analyzer - BETA

A presentation-style population analysis tool for CoinPoker PLO hand histories. It parses known/showdown cards and reports what opponents actually show when they take specific preflop, flop, turn, and river lines.

This is not a solver. It is population evidence from known cards, with explicit sample-size and selection-bias warnings.

![Presentation preview](screenshots/presentation_preview.jpg)

## Quick demo

Requirements: **Python 3** on Windows. The analyzer uses only the Python standard library.

1. Download or clone the repository.
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
filters_applied.json                     run settings and sample counts
```

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
