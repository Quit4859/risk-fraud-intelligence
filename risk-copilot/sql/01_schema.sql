-- =====================================================================
-- 01 - PHYSICAL SCHEMA (Snowflake)
-- Authored with CoCo; executed verbatim on Snowflake, and the portable
-- subset in backend/warehouse.py::BASE_DDL is asserted equivalent by
-- tests/test_ddl_parity.py.
-- =====================================================================

CREATE DATABASE IF NOT EXISTS RISK_INTELLIGENCE;
USE DATABASE RISK_INTELLIGENCE;

CREATE SCHEMA IF NOT EXISTS CORE;
CREATE SCHEMA IF NOT EXISTS RAW;
CREATE SCHEMA IF NOT EXISTS SEMANTIC;
CREATE SCHEMA IF NOT EXISTS EVIDENCE;
CREATE SCHEMA IF NOT EXISTS FILINGS;
USE SCHEMA CORE;

-- ---------------------------------------------------------------------
-- Internal stage. Production would use an external stage over the lake;
-- for the hackathon the synthetic CSVs are uploaded here.
-- ---------------------------------------------------------------------
CREATE STAGE IF NOT EXISTS RISK_RAW_STAGE
  FILE_FORMAT = (TYPE = CSV SKIP_HEADER = 1 FIELD_OPTIONALLY_ENCLOSED_BY = '"');

-- ---------------------------------------------------------------------
-- RAW layer: landing, one table per generated extract.
-- ---------------------------------------------------------------------
USE SCHEMA RAW;

CREATE TABLE IF NOT EXISTS customers (
  customer_id            VARCHAR(64)  NOT NULL,
  name                   VARCHAR(256),
  segment                VARCHAR(32),
  business_type          VARCHAR(32),
  jurisdiction           VARCHAR(8),
  jurisdiction_name      VARCHAR(64),
  domestic_currency      VARCHAR(8),
  industry               VARCHAR(64),
  is_pep                 BOOLEAN,
  kyc_status             VARCHAR(32),
  kyc_last_review        DATE,
  onboarded_at           DATE,
  ubo_count              INTEGER,
  risk_rating            VARCHAR(16),
  expected_monthly_volume NUMBER(20,2),
  expected_monthly_txns  INTEGER,
  data_classification    VARCHAR(32),
  _load_ts               TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
  CONSTRAINT pk_customers PRIMARY KEY (customer_id)
);

CREATE TABLE IF NOT EXISTS accounts (
  account_id             VARCHAR(64) NOT NULL,
  customer_id            VARCHAR(64) NOT NULL,
  account_number_masked  VARCHAR(32),
  account_type           VARCHAR(32),
  currency               VARCHAR(8),
  opened_at              DATE,
  status                 VARCHAR(16),
  opening_balance        NUMBER(20,2),
  expected_daily_volume  NUMBER(20,2),
  branch_code            VARCHAR(16),
  _load_ts               TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
  CONSTRAINT pk_accounts PRIMARY KEY (account_id)
);

CREATE TABLE IF NOT EXISTS transactions (
  transaction_id         VARCHAR(64) NOT NULL,
  account_id             VARCHAR(64) NOT NULL,
  customer_id            VARCHAR(64) NOT NULL,
  transaction_ts         TIMESTAMP_NTZ NOT NULL,
  business_date          DATE NOT NULL,
  amount                 NUMBER(20,2) NOT NULL,
  currency               VARCHAR(8),
  txn_type               VARCHAR(32),
  channel                VARCHAR(16),
  is_cash                BOOLEAN,
  merchant_name          VARCHAR(128),
  merchant_category      VARCHAR(32),
  merchant_country       VARCHAR(8),
  merchant_mcc           VARCHAR(16),
  device_id              VARCHAR(64),
  ip_country             VARCHAR(8),
  status                 VARCHAR(16),
  is_reversal            BOOLEAN,
  ingested_at            TIMESTAMP_NTZ,
  planted_scenario       VARCHAR(64),
  _load_ts               TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
  CONSTRAINT pk_transactions PRIMARY KEY (transaction_id)
);

CREATE TABLE IF NOT EXISTS credit_portfolio (
  customer_id            VARCHAR(64) NOT NULL,
  as_of_date             DATE,
  exposure_at_default    NUMBER(20,2),
  probability_of_default NUMBER(9,6),
  loss_given_default     NUMBER(6,3),
  stage                  VARCHAR(16),
  ecl_12m                NUMBER(20,2),
  ecl_lifetime           NUMBER(20,2),
  limit_utilisation      NUMBER(9,4),
  rating_model           VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS liquidity_metrics (
  entity_id                      VARCHAR(64),
  report_date                    DATE,
  hqla_total                     NUMBER(20,2),
  net_cash_outflow_30d           NUMBER(20,2),
  lcr                            NUMBER(9,4),
  available_stable_funding       NUMBER(20,2),
  required_stable_funding        NUMBER(20,2),
  nsfr                           NUMBER(9,4),
  counterparty_concentration_pct NUMBER(9,4)
);

CREATE TABLE IF NOT EXISTS counterparties (
  counterparty_id   VARCHAR(64) NOT NULL,
  name               VARCHAR(256),
  jurisdiction       VARCHAR(8),
  internal_rating    VARCHAR(8),
  is_related_party   BOOLEAN,
  CONSTRAINT pk_counterparties PRIMARY KEY (counterparty_id)
);

CREATE TABLE IF NOT EXISTS entity_exposures (
  entity_id              VARCHAR(64),
  counterparty_id        VARCHAR(64),
  as_of_date             DATE,
  exposure_at_default    NUMBER(20,2),
  eligible_capital       NUMBER(20,2),
  large_exposure_ratio   NUMBER(9,4),
  limit_utilisation      NUMBER(9,4),
  breached_25pct_limit   BOOLEAN
);

CREATE TABLE IF NOT EXISTS alert_log (
  alert_id       VARCHAR(64) NOT NULL,
  customer_id    VARCHAR(64),
  rule_code      VARCHAR(32),
  severity       VARCHAR(16),
  raised_at      DATE,
  status         VARCHAR(16),
  analyst_notes  VARCHAR(1024)
);

-- ---------------------------------------------------------------------
-- CORE: parsed policy text, bindings, findings, filings, audit trail.
-- ---------------------------------------------------------------------
USE SCHEMA CORE;

CREATE TABLE IF NOT EXISTS document_chunks (
  chunk_id       VARCHAR(128) NOT NULL,
  doc_id         VARCHAR(64),
  framework      VARCHAR(32),
  title          VARCHAR(512),
  jurisdiction   VARCHAR(32),
  section        VARCHAR(256),
  clause_ref     VARCHAR(32),
  title_text     VARCHAR(512),
  body           VARCHAR(8192),
  effective_date DATE,
  token_count    INTEGER,
  CONSTRAINT pk_document_chunks PRIMARY KEY (chunk_id)
);

CREATE TABLE IF NOT EXISTS policies (
  policy_id          VARCHAR(64) NOT NULL,
  framework          VARCHAR(32),
  name               VARCHAR(256),
  jurisdiction       VARCHAR(32),
  regulatory_body    VARCHAR(128),
  description        VARCHAR(2048),
  applies_to         VARIANT,
  risk_levels        VARIANT,
  effective_date     DATE,
  version            VARCHAR(16),
  key_thresholds     VARIANT,
  binding_rule_code  VARCHAR(32),
  obligation         VARCHAR(32),
  source_doc_id      VARCHAR(64),
  CONSTRAINT pk_policies PRIMARY KEY (policy_id)
);

CREATE TABLE IF NOT EXISTS findings (
  finding_id          VARCHAR(64) NOT NULL,
  customer_id         VARCHAR(64),
  run_id              VARCHAR(64),
  created_at          TIMESTAMP_NTZ,
  composite_risk_score NUMBER(9,4),
  risk_level          VARCHAR(16),
  confidence          NUMBER(9,4),
  signals_json        VARIANT,
  evidence_json       VARIANT,
  clauses_json        VARIANT,
  summary             VARCHAR(8192),
  guardrail_status    VARCHAR(32),
  content_hash        VARCHAR(64),
  CONSTRAINT pk_findings PRIMARY KEY (finding_id)
);

CREATE TABLE IF NOT EXISTS filings (
  filing_id     VARCHAR(64) NOT NULL,
  filing_type   VARCHAR(16),
  customer_id   VARCHAR(64),
  report_json   VARIANT,
  status        VARCHAR(16),
  generated_at  TIMESTAMP_NTZ,
  generated_by  VARCHAR(64),
  run_id        VARCHAR(64),
  content_hash  VARCHAR(64),
  approver      VARCHAR(64),
  approved_at   TIMESTAMP_NTZ,
  CONSTRAINT pk_filings PRIMARY KEY (filing_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
  audit_id     NUMBER AUTOINCREMENT,
  run_id       VARCHAR(64),
  ts           TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
  actor        VARCHAR(64),
  action       VARCHAR(64),
  target       VARCHAR(256),
  decision     VARCHAR(64),
  rationale    VARCHAR(8192),
  payload_json VARIANT
);

CREATE TABLE IF NOT EXISTS dataset_meta (
  key   VARCHAR(64) NOT NULL,
  value VARCHAR(4096),
  CONSTRAINT pk_dataset_meta PRIMARY KEY (key)
);
