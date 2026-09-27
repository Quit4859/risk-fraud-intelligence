# Detection evaluation

- Generated: 2026-09-27T08:49:09Z  
- Dataset fingerprint: `2b059994fc464c45` (as of 2026-09-26)  
- Population: 600 customers (81 planted fraud, 519 benign control)
- Throughput: 81.13 ms/customer

## Aggregate

| Operating point | TP | FP | FN | TN | Precision | Recall | F1 | FPR |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| High only | 56 | 28 | 25 | 491 | 0.667 | 0.691 | 0.679 | 0.054 |
| Medium or high | 77 | 34 | 4 | 485 | 0.694 | 0.951 | 0.802 | 0.066 |

## Recall by planted typology

| Typology | Planted | High | Rec@high | Med+ | Rec@med+ | Correct rule fired |
|---|---:|---:|---:|---:|---:|---:|
| account_takeover | 16 | 16 | 1.000 | 16 | 1.000 | 1.000 |
| geographic | 18 | 1 | 0.056 | 14 | 0.778 | 1.000 |
| mule_network | 23 | 23 | 1.000 | 23 | 1.000 | 1.000 |
| structuring | 14 | 14 | 1.000 | 14 | 1.000 | 1.000 |
| trade_based | 10 | 2 | 0.200 | 10 | 1.000 | 1.000 |

## False positives (34 benign customer(s) flagged)

| Customer | Level | Score | Rules | Driver |
|---|---|---:|---|---|
| CUST-000146 | medium | 0.487 | VELOCITY, STRUCTURING, GEOGRAPHIC | Daily spend on 2026-09-25 of USD 653,108 is 21.2 robust z-scores above the 90-day median of USD 26,232 (1 tran |
| CUST-000044 | high | 0.402 | STRUCTURING, VELOCITY | 3 cash deposits, each below the USD 10,000 reporting threshold (largest USD 9,800), aggregated to USD 17,332 w |
| CUST-000255 | high | 0.356 | STRUCTURING, VELOCITY | 4 cash deposits, each below the USD 10,000 reporting threshold (largest USD 9,800), aggregated to USD 16,232 w |
| CUST-000466 | high | 0.310 | STRUCTURING | 4 cash deposits, each below the USD 10,000 reporting threshold (largest USD 9,800), aggregated to USD 31,127 w |
| CUST-000199 | medium | 0.300 | VELOCITY, GEOGRAPHIC | Daily spend on 2026-09-24 of USD 23,669 is 7.0 robust z-scores above the 90-day median of USD 3,069 (2 transac |
| CUST-000170 | high | 0.292 | STRUCTURING | 3 cash deposits, each below the USD 10,000 reporting threshold (largest USD 9,800), aggregated to USD 23,455 w |
| CUST-000189 | high | 0.278 | STRUCTURING | 3 cash deposits, each below the USD 10,000 reporting threshold (largest USD 9,800), aggregated to USD 23,401 w |
| CUST-000559 | high | 0.276 | STRUCTURING | 3 cash deposits, each below the USD 10,000 reporting threshold (largest USD 9,800), aggregated to USD 23,913 w |
| CUST-000445 | high | 0.272 | STRUCTURING | 3 cash deposits, each below the USD 10,000 reporting threshold (largest USD 9,800), aggregated to USD 16,215 w |
| CUST-000016 | high | 0.266 | STRUCTURING | 3 cash deposits, each below the USD 10,000 reporting threshold (largest USD 9,800), aggregated to USD 16,086 w |

## Known misses

- None.

## Configured thresholds used for this run

- `structuring_ctr_amount`: 10000.0
- `structuring_window_days`: 7
- `velocity_zscore`: 3.0
- `geographic_high_risk_share`: 0.3
- `impossible_travel_hours`: 24
