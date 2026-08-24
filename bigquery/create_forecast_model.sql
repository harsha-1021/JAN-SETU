-- Replace PROJECT_ID and DATASET_ID before running this query.
-- Requires meaningful historical coverage; do not train this model on only a
-- handful of same-day hackathon submissions.
CREATE OR REPLACE MODEL `PROJECT_ID.DATASET_ID.hotspot_forecast`
OPTIONS(
  MODEL_TYPE = 'ARIMA_PLUS',
  TIME_SERIES_TIMESTAMP_COL = 'complaint_date',
  TIME_SERIES_DATA_COL = 'complaint_count',
  TIME_SERIES_ID_COL = ['region', 'category'],
  DATA_FREQUENCY = 'DAILY',
  HOLIDAY_REGION = 'IN',
  CLEAN_SPIKES_AND_DIPS = TRUE,
  ADJUST_STEP_CHANGES = TRUE
) AS
SELECT
  region,
  category,
  DATE(created_at) AS complaint_date,
  COUNT(*) AS complaint_count
FROM `PROJECT_ID.DATASET_ID.complaints`
GROUP BY region, category, complaint_date;

