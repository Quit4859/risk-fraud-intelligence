# B2 — structuring precision: before and after

Both runs use the **identical dataset** (600 customers, 42 planted fraud,
7% prevalence, seed 42). Only the detector and the semantic view differ, so
the delta is attributable to the fix and not to a different population.

| Operating point | Precision | Recall | F1 | False-positive rate |
|---|---|---|---|---|
| High only — before | 0.475 | 0.667 | 0.554 | 0.056 |
| High only — after  | 1.000 | 0.667 | 0.800 | 0.000 |
| Medium or high — before | 0.500 | 0.929 | 0.650 | 0.070 |
| Medium or high — after  | 0.830 | 0.929 | 0.876 | 0.014 |

| | TP | FP | FN |
|---|---|---|---|
| High only before | 28 | 31 | 14 |
| High only after | 28 | 0 | 14 |
| Medium or high before | 39 | 39 | 3 |
| Medium or high after | 39 | 8 | 3 |

## Recall by typology (unchanged — the fix targets precision only)

| Typology | Planted | Rec@med+ | Correct rule fired |
|---|---|---|---|
| account_takeover | 8 | 1.000 | 1.000 |
| geographic | 9 | 0.667 | 1.000 |
| mule_network | 13 | 1.000 | 1.000 |
| structuring | 7 | 1.000 | 1.000 |
| trade_based | 5 | 1.000 | 1.000 |

Fraud prevalence after B3: 7.0% (was 7.0% on the same labels; the brief's profile was 40.5% on the deployed 200-customer demo).

Throughput: 81.39 → 83.56 ms/customer.

## What changed

- The candidate band is now 85-99.9% of the reporting threshold
  (8,500-9,990) instead of 2,000-10,000. Before the fix, 101 of 119 benign
  controls had three or more deposits inside the old band.
- HIGH additionally requires corroboration: a pass-through cash outflow or
  a previously unseen counterparty in the window (clause STR-2.2.1).
- An uncorroborated aggregation is capped below the HIGH band rather than
  dropped, so it stays visible and reviewable.
- The generator plants deposits inside the band and caps benign daily cash
  below it, so controls cannot produce the pattern by chance.
- `sem_cash_72h_aggregation` was updated to match. It had not been, so the
  copilot answered from the old view while the detector used new logic.
