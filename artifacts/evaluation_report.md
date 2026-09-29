# Detection evaluation

- Generated: 2026-09-29T14:25:55Z  
- Dataset fingerprint: `865ba9b57f509224` (as of 2026-09-28)  
- Population: 600 customers (42 planted fraud, 558 benign control)
- Throughput: 83.56 ms/customer

## Aggregate

| Operating point | TP | FP | FN | TN | Precision | Recall | F1 | FPR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| High only | 28 | 0 | 14 | 558 | 1.000 | 0.667 | 0.800 | 0.000 |
| Medium or high | 39 | 8 | 3 | 550 | 0.830 | 0.929 | 0.876 | 0.014 |

## Recall by planted typology

| Typology | Planted | High | Rec@high | Med+ | Rec@med+ | Correct rule fired |
|---|---:|---:|---:|---:|---:|---:|
| account_takeover | 8 | 8 | 1.000 | 8 | 1.000 | 1.000 |
| geographic | 9 | 0 | 0.000 | 6 | 0.667 | 1.000 |
| mule_network | 13 | 13 | 1.000 | 13 | 1.000 | 1.000 |
| structuring | 7 | 7 | 1.000 | 7 | 1.000 | 1.000 |
| trade_based | 5 | 0 | 0.000 | 5 | 1.000 | 1.000 |

## False positives (8 benign customer(s) flagged)

| Customer | Level | Score | Rules | Driver |
|---|---|---:|---|---|
| CUST-000084 | medium | 0.143 | GEOGRAPHIC | Within a 22.2 hour window the customer transacted in 3 jurisdictions (AE, VG, KP); the leg VG->KP implies 13,5 |
| CUST-000067 | medium | 0.141 | GEOGRAPHIC | Within a 23.6 hour window the customer transacted in 3 jurisdictions (US, KP, DE); the leg KP->DE implies 7,93 |
| CUST-000550 | medium | 0.140 | GEOGRAPHIC | Within a 12.1 hour window the customer transacted in 3 jurisdictions (GB, KP, LB); the leg GB->KP implies 8,66 |
| CUST-000092 | medium | 0.127 | GEOGRAPHIC | Within a 13.5 hour window the customer transacted in 3 jurisdictions (MM, IR, IN); the leg IR->IN implies 2,54 |
| CUST-000320 | medium | 0.120 | GEOGRAPHIC | Within a 23.6 hour window the customer transacted in 3 jurisdictions (NG, IN, PA); the leg NG->IN implies 7,55 |
| CUST-000156 | medium | 0.120 | GEOGRAPHIC | Within a 19.4 hour window the customer transacted in 3 jurisdictions (LB, US, KP); the leg US->KP implies 11,0 |
| CUST-000512 | medium | 0.120 | GEOGRAPHIC | 10% of transactions in 90 days were booked in elevated-risk jurisdictions (clause GEO-2.1.1). |
| CUST-000196 | medium | 0.120 | GEOGRAPHIC | Within a 22.8 hour window the customer transacted in 3 jurisdictions (GB, KP, IR); the leg GB->KP implies 8,66 |

## Known misses

- None.

## Configured thresholds used for this run

- `structuring_ctr_amount`: 10000.0
- `structuring_candidate_lookback_days`: 7
- `structuring_aggregation_window_hours`: 72
- `structuring_avoidance_band`: [8500.0, 9990.0]
- `structuring_require_corroboration`: True
- `velocity_zscore`: 3.0
- `geographic_high_risk_share`: 0.3
- `impossible_travel_hours`: 24
