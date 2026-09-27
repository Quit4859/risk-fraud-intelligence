-- =====================================================================
-- 02 - DYNAMIC TABLES (Snowflake incremental transformation)
--
-- Each dynamic table owns one grain, declares its dependencies, and
-- refreshes on a schedule. The local runtime executes the identical
-- SELECT bodies through sqlite, so the metric definition cannot drift
-- between the demo and the warehouse.
-- =====================================================================

USE SCHEMA SEMANTIC;

-- ---------------------------------------------------------------------
-- DT-1  Customer 90-day behavioural baseline
-- Grain: one row per customer per business day.
-- Refresh: every 15 minutes (near real time for a demo cadence).
-- ---------------------------------------------------------------------
CREATE OR REPLACE DYNAMIC TABLE dt_customer_daily_activity
  TARGET_LAG = '15 minutes'
  WAREHOUSE = RISK_WH
  INITIAL_WS = 32
AS
SELECT
  t.customer_id,
  t.business_date,
  SUM(t.amount)                                   AS gross_amount,
  COUNT(*)                                        AS txn_count,
  SUM(CASE WHEN t.is_cash THEN t.amount ELSE 0 END)        AS cash_amount,
  SUM(CASE WHEN t.is_cash THEN 1 ELSE 0 END)                AS cash_txn_count,
  SUM(CASE WHEN t.channel = 'WIRE' THEN t.amount ELSE 0 END) AS wire_amount,
  COUNT(DISTINCT t.merchant_country)              AS country_count,
  MAX(t.amount)                                   AS max_single_txn,
  MIN(t.ingested_at)                              AS first_ingest_ts,
  MAX(t.ingested_at)                              AS last_ingest_ts
FROM CORE.transactions t
WHERE t.status = 'posted'
GROUP BY t.customer_id, t.business_date;

-- ---------------------------------------------------------------------
-- DT-2  Robust behavioural baseline (median + MAD)
-- Snowflake has MEDIAN(); the portable form uses PERCENTILE_CONT.
-- Grain: one row per customer.
-- ---------------------------------------------------------------------
CREATE OR REPLACE DYNAMIC TABLE dt_customer_behaviour_baseline
  TARGET_LAG = '1 hour'
  WAREHOUSE = RISK_WH
  INITIAL_WS = 24
AS
WITH daily AS (
  SELECT customer_id, business_date, gross_amount, txn_count
  FROM SEMANTIC.dt_customer_daily_activity
  WHERE business_date >= DATEADD('day', -90, CURRENT_DATE())
),
ranked AS (
  SELECT customer_id, gross_amount,
         PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY gross_amount)
           OVER (PARTITION BY customer_id) AS median_amount
  FROM daily
)
SELECT
  customer_id,
  median_amount                                              AS baseline_median,
  AVG(gross_amount)                                          AS baseline_mean,
  PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY gross_amount)
    OVER (PARTITION BY customer_id)                          AS p75_amount,
  COUNT(DISTINCT business_date)                              AS observed_days,
  MAX(business_date)                                         AS last_observed_date
FROM ranked
GROUP BY customer_id, baseline_median, baseline_mean, p75_amount;

-- ---------------------------------------------------------------------
-- DT-3  Customer risk profile
-- Grain: one row per customer. Joins KYC, credit and behavioural signals.
-- ---------------------------------------------------------------------
CREATE OR REPLACE DYNAMIC TABLE dt_customer_risk_profile
  TARGET_LAG = '15 minutes'
  WAREHOUSE = RISK_WH
  INITIAL_WS = 32
AS
SELECT
  c.customer_id,
  c.name,
  c.segment,
  c.jurisdiction,
  c.is_pep,
  c.kyc_status,
  c.risk_rating                                        AS source_risk_rating,
  c.expected_monthly_volume,
  COUNT(DISTINCT a.account_id)                          AS account_count,
  NVL(SUM(a.opening_balance), 0)                       AS total_opening_balance,
  NVL(b.baseline_median, 0)                            AS baseline_median_daily,
  NVL(b.observed_days, 0)                              AS observed_days,
  b.last_observed_date,
  cp.exposure_at_default,
  cp.probability_of_default,
  cp.loss_given_default,
  cp.ecl_12m,
  cp.stage                                              AS credit_stage
FROM CORE.customers c
LEFT JOIN CORE.accounts a            ON a.customer_id = c.customer_id
LEFT JOIN SEMANTIC.dt_customer_behaviour_baseline b ON b.customer_id = c.customer_id
LEFT JOIN CORE.credit_portfolio cp   ON cp.customer_id = c.customer_id
GROUP BY ALL;

-- ---------------------------------------------------------------------
-- DT-4  Counterparty large-exposure monitoring (Basel LE-2.1)
-- Grain: one row per entity x counterparty.
-- ---------------------------------------------------------------------
CREATE OR REPLACE DYNAMIC TABLE dt_large_exposure_breaches
  TARGET_LAG = '1 hour'
  WAREHOUSE = RISK_WH
  INITIAL_WS = 24
AS
SELECT
  e.entity_id,
  e.counterparty_id,
  cp.name                                       AS counterparty_name,
  cp.jurisdiction,
  cp.is_related_party,
  e.exposure_at_default,
  e.eligible_capital,
  e.large_exposure_ratio,
  e.limit_utilisation,
  IFF(e.breached_25pct_limit, 'BREACH', IFF(e.limit_utilisation >= 0.80,
      'BOARD_REPORT', 'WITHIN_LIMIT'))           AS limit_status
FROM CORE.entity_exposures e
JOIN CORE.counterparties cp ON cp.counterparty_id = e.counterparty_id;

-- ---------------------------------------------------------------------
-- DT-5  Liquidity position (LCR / NSFR)
-- Grain: one row per entity per report date.
-- ---------------------------------------------------------------------
CREATE OR REPLACE DYNAMIC TABLE dt_liquidity_position
  TARGET_LAG = '4 hours'
  WAREHOUSE = RISK_WH
  INITIAL_WS = 24
AS
SELECT
  entity_id,
  report_date,
  hqla_total,
  net_cash_outflow_30d,
  lcr,
  nsfr,
  available_stable_funding,
  required_stable_funding,
  counterparty_concentration_pct,
  IFF(lcr < 1.0, 'LCR_BREACH', 'COMPLIANT')      AS lcr_status,
  IFF(nsfr < 1.0, 'NSFR_BREACH', 'COMPLIANT')    AS nsfr_status,
  IFF(counterparty_concentration_pct > 0.25, 'CONCENTRATION_BREACH', 'CONCENTRATION_OK')
                                                      AS concentration_status
FROM CORE.liquidity_metrics;
