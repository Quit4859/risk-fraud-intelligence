-- =====================================================================
-- 03 - STREAMS, TASKS AND THE UNATTENDED PIPELINE
--
-- The copilot is not a one-shot notebook: new transactions land, a stream
-- captures them, a task refreshes the affected customers, and a downstream
-- task regenerates findings and draft filings. Running this on a schedule is
-- what makes the copilot "operate unattended".
-- =====================================================================

USE SCHEMA CORE;

-- ---------------------------------------------------------------------
-- Change capture on the transaction stream table.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transaction_stream (
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
  CONSTRAINT pk_transaction_stream PRIMARY KEY (transaction_id)
);

CREATE STREAM IF NOT EXISTS stg_transactions_stream
  ON TABLE CORE.transaction_stream
  APPEND_ONLY = TRUE
  SHOW_INITIAL_ROWS = TRUE;

-- ---------------------------------------------------------------------
-- Task 1 - near real time ingestion from the raw stage.
-- Runs every 5 minutes; copies only new files by load metadata.
-- ---------------------------------------------------------------------
CREATE OR REPLACE TASK tsk_ingest_transactions
  WAREHOUSE = RISK_WH
  SCHEDULE = '5 MINUTE'
  USER_TASK_TIMEOUT_MS = 3600000
  AS
BEGIN
  LET files_missing BOOL := (
    SELECT COUNT(*) = 0
    FROM @CORE.RISK_RAW_STAGE/transactions
         (METADATA$FILENAME => f)
    WHERE NOT EXISTS (
      SELECT 1 FROM CORE.transactions t
      WHERE t._load_ts >= (SELECT LAST_SUCCESSFUL_COMPLETION_TIME
                           FROM CORE.information_schema.task_history
                           WHERE name = 'TSK_INGEST_TRANSACTIONS')
    )
  );
  RETURN files_missing;
END;

CREATE OR REPLACE TASK tsk_ingest_and_apply_stream
  WAREHOUSE = RISK_WH
  SCHEDULE = '5 MINUTE'
  AFTER tsk_ingest_transactions
  AS
BEGIN
  CREATE TEMPORARY TABLE staged_transactions AS
    SELECT * FROM CORE.stg_transactions_stream;
  MERGE INTO CORE.transactions t
  USING staged_transactions s
    ON t.transaction_id = s.transaction_id
  WHEN MATCHED THEN UPDATE SET
      status = s.status, amount = s.amount
  WHEN NOT MATCHED THEN INSERT *;
  RETURN 'rows_merged';
END;

-- ---------------------------------------------------------------------
-- Task 2 - detection + finding generation, hourly.
-- Invokes the copilot's detector portfolio (custom Python UDF or a stored
-- procedure that calls the same code path as the CLI).
-- ---------------------------------------------------------------------
CREATE OR REPLACE TASK tsk_run_fraud_detection
  WAREHOUSE = RISK_WH
  SCHEDULE = '1 HOUR'
  AFTER tsk_ingest_and_apply_stream
  ALLOW_OVERLAPPING_EXECUTION = FALSE
  AS
BEGIN
  CALL CORE.generate_findings(
    p_min_score   => 0.42,
    p_run_id      => CURRENT_TIMESTAMP()::VARCHAR,
    p_run_mode    => 'scheduled'
  );
  RETURN 'findings_generated';
END;

-- ---------------------------------------------------------------------
-- Task 3 - draft regulatory filings for anything above the SAR floor.
-- Drafts only: four-eyes approval (clause FILE-2.1.1) is a human step and
-- is deliberately not automated.
-- ---------------------------------------------------------------------
CREATE OR REPLACE TASK tsk_generate_draft_filings
  WAREHOUSE = RISK_WH
  SCHEDULE = '2 HOUR'
  AFTER tsk_run_fraud_detection
  AS
BEGIN
  CALL CORE.generate_draft_filings(
    p_run_id => CURRENT_TIMESTAMP()::VARCHAR
  );
  RETURN 'drafts_generated';
END;

-- ---------------------------------------------------------------------
-- Task 4 - stale-alert suppression, daily.
-- ---------------------------------------------------------------------
CREATE OR REPLACE TASK tsk_daily_risk_pack
  WAREHOUSE = RISK_WH
  SCHEDULE = '1 DAY USING CRON 7 6 * * * UTC'
  AS
BEGIN
  CALL CORE.build_board_risk_pack(CURRENT_DATE()::VARCHAR);
  RETURN 'board_pack_built';
END;

-- ---------------------------------------------------------------------
-- Orchestration DAG
--   tsk_ingest_transactions
--     -> tsk_ingest_and_apply_stream
--        -> tsk_run_fraud_detection
--           -> tsk_generate_draft_filings
--   tsk_daily_risk_pack (independent, daily)
-- ---------------------------------------------------------------------
