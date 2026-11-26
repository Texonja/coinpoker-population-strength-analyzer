# Methodology

## Action definitions

- A first-in raise and a first raise after one or more limps have separate lines. In the preflop CSV, `wager_number` counts successive price increases with the live blind as the first wager. A short all-in that increases the price counts as a raise. `full_raise` identifies increases reaching the minimum full increment. `straddled` identifies hands with a posted straddle.
- The parser compares each all-in with the live street wager. An all-in call, including one for less than the amount due, stays a call.
- A flop donk leads into the last preflop raiser before that player acts. A turn probe follows a checked flop and leads into that raiser. A river probe follows a checked turn and leads into the last flop aggressor. The aggressor must have a subsequent action. Other opening bets after checks have separate labels for delayed follow-ups, bets after the aggressor checks, and pots without an active aggressor.
- A barrel follows the same player's unraised opening bet on the previous street. A triple barrel requires that sequence on both the flop and turn. A river raise against an opening bet and a river re-raise facing a raise have separate lines and sizing buckets.
- `--vs-hero` keeps a villain wager if Hero calls, folds or raises before any intervening raise, or if the villain raises a live wager by Hero. It applies to regular, preflop and BombPot records. The default table-wide mode and this filter both require the configured starting stacks for Hero and the acting opponent.
- `action_size_pct_pot` stores a fraction. A bet divides chips bet by the gross pot before the bet. A raise divides the increase **above the call** by the pot **after calling**. `chips_pushed_pct_pot` divides all chips pushed by the pot before acting. `action_size_pct_player_funded_pot` applies the wager formula after subtracting Splash drops. Size buckets use unrounded wager fractions.

## Card definitions

- Preflop features overlap. AA and KK each require at least two cards of that rank. A suited ace shares its suit with another hole card. Rank windows include A–2–3–4–5. The hand-class string assigns one primary category.
- On an unpaired flop, three cards of one suit are monotone. A two-tone flop with all three ranks in one five-rank window is wet. Paired flops have their own line.
- A draw completion is a distinct legal next card that completes the specified hand using two hole cards and three board cards. A qualifying draw is an immediate flush draw or at least eight straight completion cards. Backdoors and straight redraws are excluded. A completion can still lose. The calculation uses the player's hole cards and visible board cards at that decision. In a double-board BombPot it also removes the cards visible on the other board. The fields `straight_outs` and `straight_outs_approx` mirror `straight_completion_cards`.
- Each river record compares the player's rank with every available opposing hole-card pair. `river_unbeaten` allows a tie. `practical_nuts` also includes quads, straight flushes and hands that can be beaten only by either category. Flush tiers count higher attainable suited hole-card ranks. Top two pair requires both highest distinct board ranks paired by hole cards and present as the two pairs in the best hand.
- The eight river classes are exclusive. The separate Unbeaten column overlaps them. The last strength group covers other two-pair hands, one pair, and high card. A player's intent, and the cards of opponents who folded unseen, are unknown.
- BombPot rows contain one action index and separate board columns. River `unbeaten` checks remove cards exposed on both boards. Regular multi-runout hands retain the betting street and first runout for action analysis.

## Create another public sample

Raw CoinPoker exports may still reveal your Hero name, exact hand IDs, table IDs, and session times. To create a safer sample:

1. Put the raw hand-history file next to the scripts.
2. Double-click `make_public_sample.bat`.
3. Review the generated file manually before committing it.

Default output:

```text
examples\public_sample_hands.txt
```
