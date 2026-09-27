-- =====================================================================
-- 04 - SEMANTIC VIEWS (portable: executes on Snowflake AND on the local
-- sqlite runtime, so the demo and the warehouse share one definition).
--
-- These are the verified queries behind ontology/verified_queries.yaml.
-- Each view owns one grain, so a question can never be answered from the
-- wrong aggregation level.
--
-- Portability rules applied throughout:
--   * no RANGE window frames (sqlite supports ROWS only)
--   * date arithmetic via date()/datetime() string modifiers
--   * no IFF/NVL2 - ANSI COALESCE and CASE only
--   * the dataset as-of date is read from sem_dataset_clock, never CURRENT_DATE
-- =====================================================================

-- ---------------------------------------------------------------------
-- SV-0  Dataset clock. All lookback windows anchor to the dataset as-of
-- date so results are reproducible and replayable, never wall-clock
-- dependent.
-- ---------------------------------------------------------------------
DROP VIEW IF EXISTS sem_dataset_clock;
CREATE VIEW sem_dataset_clock AS
SELECT
  REPLACE(REPLACE(m.as_of_value, '"', ''), "'", '') AS as_of_date,
  (SELECT REPLACE(REPLACE(value, '"', ''), "'", '')
   FROM dataset_meta WHERE key = 'seed')            AS data_seed,
  (SELECT REPLACE(REPLACE(value, '"', ''), "'", '')
   FROM dataset_meta WHERE key = 'generator_version') AS generator_version
FROM (SELECT value AS as_of_value FROM dataset_meta WHERE key = 'as_of_date') m;

-- ---------------------------------------------------------------------
-- SV-1  Customer profile: one row per customer. KYC + credit + behaviour.
-- Question: "Who is this customer and how risky do they look?"
-- ---------------------------------------------------------------------
DROP VIEW IF EXISTS sem_customer_profile;
CREATE VIEW sem_customer_profile AS
WITH t AS (
  SELECT customer_id,
         COUNT(DISTINCT device_id)                       AS distinct_devices,
         COUNT(*)                                        AS txn_count_90d,
         SUM(amount)                                     AS gross_amount_90d,
         SUM(CASE WHEN is_cash = 1 THEN amount ELSE 0 END) AS cash_amount_90d,
         MAX(amount)                                     AS max_single_txn_90d,
         COUNT(DISTINCT business_date)                   AS active_days_90d
  FROM transactions
  WHERE status = 'posted'
    AND business_date >= DATE((SELECT as_of_date FROM sem_dataset_clock), '-90 day')
  GROUP BY customer_id
)
SELECT
  c.customer_id,
  c.name,
  c.segment,
  c.jurisdiction,
  c.jurisdiction_name,
  c.is_pep,
  c.kyc_status,
  c.kyc_last_review,
  c.risk_rating                    AS source_risk_rating,
  c.expected_monthly_volume,
  (SELECT COUNT(*) FROM accounts a WHERE a.customer_id = c.customer_id) AS account_count,
  (SELECT SUM(opening_balance) FROM accounts a WHERE a.customer_id = c.customer_id)
                                    AS total_opening_balance,
  COALESCE(t.distinct_devices, 0)   AS distinct_devices,
  COALESCE(t.txn_count_90d, 0)      AS txn_count_90d,
  COALESCE(t.gross_amount_90d, 0)   AS gross_amount_90d,
  COALESCE(t.cash_amount_90d, 0)    AS cash_amount_90d,
  COALESCE(t.max_single_txn_90d, 0) AS max_single_txn_90d,
  COALESCE(t.active_days_90d, 0)    AS active_days_90d,
  cp.probability_of_default,
  cp.loss_given_default,
  cp.ecl_12m,
  cp.exposure_at_default,
  cp.stage                  AS credit_stage
FROM customers c
LEFT JOIN t           ON t.customer_id  = c.customer_id
LEFT JOIN credit_portfolio cp ON cp.customer_id = c.customer_id;

-- ---------------------------------------------------------------------
-- SV-2  Daily activity: the velocity baseline grain.
-- ---------------------------------------------------------------------
DROP VIEW IF EXISTS sem_daily_activity;
CREATE VIEW sem_daily_activity AS
SELECT
  customer_id,
  business_date,
  SUM(amount)                                        AS gross_amount,
  COUNT(*)                                           AS txn_count,
  SUM(CASE WHEN is_cash = 1 THEN amount ELSE 0 END)  AS cash_amount,
  SUM(CASE WHEN is_cash = 1 THEN 1 ELSE 0 END)      AS cash_txn_count,
  SUM(CASE WHEN channel = 'WIRE' THEN amount ELSE 0 END) AS wire_amount,
  COUNT(DISTINCT merchant_country)                   AS country_count,
  MAX(amount)                                        AS max_single_txn
FROM transactions
WHERE status = 'posted'
GROUP BY customer_id, business_date;

-- ---------------------------------------------------------------------
-- SV-3  72-hour rolling cash aggregation (clause STR-2.1.2).
-- Returns, per customer, the single worst window in which sub-threshold
-- cash deposits aggregate to or above the reporting threshold.
--
-- Written as a self-join rather than a RANGE frame so the same text runs
-- on Snowflake and sqlite.
-- ---------------------------------------------------------------------
DROP VIEW IF EXISTS sem_cash_72h_aggregation;
CREATE VIEW sem_cash_72h_aggregation AS
WITH cash AS (
  SELECT customer_id, transaction_id, transaction_ts, amount
  FROM transactions
  WHERE status = 'posted'
    AND is_cash = 1
    AND amount < 10000
    AND amount >= 2000
    AND business_date >= DATE((SELECT as_of_date FROM sem_dataset_clock), '-7 day')
),
windows AS (
  SELECT
    c.customer_id,
    c.transaction_id                         AS window_anchor_txn,
    c.transaction_ts                         AS window_start,
    COUNT(o.transaction_id)                  AS deposit_count,
    SUM(o.amount)                            AS aggregate_amount,
    AVG(o.amount)                            AS avg_deposit,
    MAX(o.transaction_ts)                    AS window_end
  FROM cash c
  JOIN cash o
    ON o.customer_id = c.customer_id
   AND o.transaction_ts >= c.transaction_ts
   AND o.transaction_ts <= DATETIME(c.transaction_ts, '+72 hours')
  GROUP BY c.customer_id, c.transaction_id, c.transaction_ts
  HAVING COUNT(o.transaction_id) >= 3 AND SUM(o.amount) >= 10000
)
SELECT
  customer_id,
  window_anchor_txn,
  window_start,
  window_end,
  deposit_count,
  ROUND(aggregate_amount, 2) AS aggregate_amount,
  ROUND(avg_deposit, 2)       AS avg_deposit,
  ROW_NUMBER() OVER (PARTITION BY customer_id
                     ORDER BY aggregate_amount DESC, window_start ASC) AS rn
FROM windows;

-- ---------------------------------------------------------------------
-- SV-4  Elevated-risk geography exposure (clause GEO-2.1.1).
-- ---------------------------------------------------------------------
DROP VIEW IF EXISTS sem_elevated_risk_exposure;
CREATE VIEW sem_elevated_risk_exposure AS
SELECT
  g.customer_id,
  g.txn_count,
  g.risk_txns,
  g.risk_countries,
  CASE WHEN g.txn_count = 0 THEN 0.0
       ELSE ROUND(1.0 * g.risk_txns / g.txn_count, 4) END AS risk_share,
  CASE WHEN g.txn_count = 0 THEN 'NO_ACTIVITY'
       WHEN 1.0 * g.risk_txns / g.txn_count > 0.30 THEN 'CONCENTRATED'
       WHEN 1.0 * g.risk_txns / g.txn_count > 0.10 THEN 'ELEVATED'
       ELSE 'NORMAL' END                          AS geography_status
FROM (
  SELECT
    customer_id,
    COUNT(*) AS txn_count,
    SUM(CASE WHEN merchant_country IN
              ('NG','RU','IR','KP','SY','PA','KY','VG','MM','LB') THEN 1 ELSE 0 END) AS risk_txns,
    COUNT(DISTINCT CASE WHEN merchant_country IN
              ('NG','RU','IR','KP','SY','PA','KY','VG','MM','LB')
              THEN merchant_country END) AS risk_countries
  FROM transactions
  WHERE status = 'posted'
    AND business_date >= DATE((SELECT as_of_date FROM sem_dataset_clock), '-90 day')
  GROUP BY customer_id
) g;

-- ---------------------------------------------------------------------
-- SV-5  Mule network membership: device fingerprints spanning customers.
-- ---------------------------------------------------------------------
DROP VIEW IF EXISTS sem_mule_network_clusters;
CREATE VIEW sem_mule_network_clusters AS
SELECT
  device_id,
  COUNT(DISTINCT customer_id) AS customers_on_device,
  COUNT(*)                    AS txn_count,
  SUM(amount)                 AS total_amount,
  MIN(business_date)          AS first_seen,
  MAX(business_date)          AS last_seen
FROM transactions
WHERE status = 'posted' AND device_id IS NOT NULL
GROUP BY device_id
HAVING COUNT(DISTINCT customer_id) > 1
   AND SUM(amount) > 50000;

-- ---------------------------------------------------------------------
-- SV-6  Credit watchlist (Basel ECL-4.1.2, vulnerable obligor > 10% PD).
-- ---------------------------------------------------------------------
DROP VIEW IF EXISTS sem_credit_watchlist;
CREATE VIEW sem_credit_watchlist AS
SELECT
  cp.customer_id,
  c.segment,
  c.jurisdiction,
  c.name,
  cp.exposure_at_default,
  cp.probability_of_default,
  cp.loss_given_default,
  cp.ecl_12m,
  cp.ecl_lifetime,
  cp.stage,
  cp.limit_utilisation,
  CASE WHEN cp.probability_of_default > 0.10 THEN 'VULNERABLE_OBLIGOR'
       WHEN cp.probability_of_default > 0.05 THEN 'WATCH'
       ELSE 'OK' END AS credit_status
FROM credit_portfolio cp
JOIN customers c ON c.customer_id = cp.customer_id;

-- ---------------------------------------------------------------------
-- SV-7  Liquidity position (LCR / NSFR / concentration).
-- ---------------------------------------------------------------------
DROP VIEW IF EXISTS sem_liquidity_position;
CREATE VIEW sem_liquidity_position AS
SELECT
  entity_id,
  report_date,
  hqla_total,
  net_cash_outflow_30d,
  lcr,
  nsfr,
  counterparty_concentration_pct,
  CASE WHEN lcr < 1.0  THEN 'LCR_BREACH'  ELSE 'LCR_OK'  END AS lcr_status,
  CASE WHEN nsfr < 1.0 THEN 'NSFR_BREACH' ELSE 'NSFR_OK' END AS nsfr_status,
  CASE WHEN counterparty_concentration_pct > 0.25 THEN 'CONCENTRATION_BREACH'
       ELSE 'CONCENTRATION_OK' END AS concentration_status
FROM liquidity_metrics
WHERE report_date = (SELECT MAX(report_date) FROM liquidity_metrics);

-- ---------------------------------------------------------------------
-- SV-8  Large exposures against the Basel 25% limit (clause LE-2.1.1).
-- ---------------------------------------------------------------------
DROP VIEW IF EXISTS sem_large_exposures;
CREATE VIEW sem_large_exposures AS
SELECT
  e.entity_id,
  e.counterparty_id,
  cp.name                  AS counterparty_name,
  cp.jurisdiction          AS counterparty_jurisdiction,
  cp.is_related_party,
  e.exposure_at_default,
  e.eligible_capital,
  e.large_exposure_ratio,
  e.limit_utilisation,
  CASE WHEN e.breached_25pct_limit = 1 THEN 'BREACH'
       WHEN e.limit_utilisation >= 0.80  THEN 'BOARD_REPORT'
       ELSE 'WITHIN_LIMIT' END AS limit_status
FROM entity_exposures e
JOIN counterparties cp ON cp.counterparty_id = e.counterparty_id;

-- ---------------------------------------------------------------------
-- SV-9  Filing obligation queue: which obligations are live for a customer
-- from the policy bindings, independent of whether a rule has fired.
-- ---------------------------------------------------------------------
DROP VIEW IF EXISTS sem_filing_obligation_queue;
CREATE VIEW sem_filing_obligation_queue AS
SELECT
  c.customer_id,
  c.jurisdiction,
  c.segment,
  p.policy_id,
  p.framework,
  p.name          AS policy_name,
  p.obligation,
  p.binding_rule_code,
  p.key_thresholds,
  p.source_doc_id,
  p.regulatory_body
FROM customers c
JOIN policies p
  ON (p.applies_to LIKE '%"all"%' OR p.applies_to LIKE '%"' || c.segment || '"%')
WHERE p.jurisdiction IN ('International', 'Internal', c.jurisdiction);

-- ---------------------------------------------------------------------
-- SV-10  Alert backlog: existing operational alerts the copilot de-duplicates
-- against so an analyst is never shown the same case twice.
-- ---------------------------------------------------------------------
DROP VIEW IF EXISTS sem_alert_backlog;
CREATE VIEW sem_alert_backlog AS
SELECT
  alert_id,
  customer_id,
  rule_code,
  severity,
  raised_at,
  status,
  CASE WHEN status = 'open' THEN 'ACTIONABLE' ELSE 'CLOSED' END AS backlog_status
FROM alert_log;
