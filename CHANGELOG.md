# Changelog

Release notes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- Moved the betting-line and card definitions and the public sample instructions into the [methodology guide](docs/METHODOLOGY.md).

## [0.2.0] - 2026-11-26

### Added

- An independent verifier for the bundled public sample. Run the regression suite from the repository root with `python -m unittest discover -v`. Run `python verify_public_sample.py` to reconstruct the sample report in a temporary directory and cross-check gross pots, all showdown labels, exported best-five cards and river classifications with an independent evaluator.

### Changed

- Identical repeated histories with the same ID count once. Conflicting copies of an ID are all excluded. The analyzer checks board progression, multi-runout shared cards, player disclosures, physical card uniqueness, recognized actions, wager accounting and the reported gross pot. Partial imports show reasons and counts in the report and JSON. Wholly invalid histories and a Hero absent from all supported hands produce a nonzero exit code. Valid histories with no qualifying actions produce a zero-sample report.
- The summary CSVs share one set of descriptive class columns with count and percentage suffixes. Consumers of 0.1.0 should migrate the legacy bluff, confidence and strategic-read columns. `known_records` counts actions and `known_hands` counts distinct hands. The game/stake/date count is `hands_after_game_stake_date`. The eligible-hand count is `hands_with_eligible_actions`.

[Unreleased]: https://github.com/Texonja/coinpoker-population-strength-analyzer/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Texonja/coinpoker-population-strength-analyzer/compare/v0.1.0...v0.2.0
