"""BigQuery operational analytics and BigQuery ML forecast access."""

import os
from datetime import datetime, timezone
from typing import Dict, Iterable, List


class BigQueryAnalytics:
    def __init__(self) -> None:
        self.project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        self.dataset = os.environ.get("BIGQUERY_DATASET", "citizen_demand").strip()
        self.location = os.environ.get("BIGQUERY_LOCATION", "asia-south1").strip()
        self._client = None
        self._initialized = False

    def _get_client(self):
        if self._initialized:
            return self._client
        self._initialized = True
        if not self.project_id:
            return None
        try:
            from google.cloud import bigquery

            self._client = bigquery.Client(project=self.project_id, location=self.location)
        except Exception:
            self._client = None
        return self._client

    @property
    def enabled(self) -> bool:
        return self._get_client() is not None

    @property
    def complaints_table(self) -> str:
        return "{}.{}.complaints".format(self.project_id, self.dataset)

    def insert_complaint(self, record: dict) -> bool:
        client = self._get_client()
        if client is None:
            return False
        row = dict(record)
        row["ingested_at"] = datetime.now(timezone.utc).isoformat()
        try:
            return not client.insert_rows_json(self.complaints_table, [row])
        except Exception:
            return False

    def get_region_contexts(self, regions: Iterable[str]) -> Dict[str, dict]:
        client = self._get_client()
        region_list = sorted(set(region for region in regions if region))
        if client is None or not region_list:
            return {}
        try:
            from google.cloud import bigquery

            query = """
                SELECT region, population, infrastructure_index, investment_penalty,
                       current_investment_plan
                FROM `{project}.{dataset}.region_context`
                WHERE region IN UNNEST(@regions)
            """.format(project=self.project_id, dataset=self.dataset)
            config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("regions", "STRING", region_list)
                ]
            )
            rows = client.query(query, job_config=config).result()
            return {
                row.region: {
                    "population": int(row.population),
                    "infrastructure_index": float(row.infrastructure_index),
                    "investment_penalty": float(row.investment_penalty),
                    "current_investment_plan": row.current_investment_plan or "",
                }
                for row in rows
            }
        except Exception:
            return {}

    def forecast_hotspots(self, horizon_days: int = 30) -> List[dict]:
        client = self._get_client()
        if client is None:
            return []
        try:
            from google.cloud import bigquery

            query = """
                SELECT region, category, forecast_timestamp,
                       forecast_value, prediction_interval_lower_bound,
                       prediction_interval_upper_bound
                FROM ML.FORECAST(
                    MODEL `{project}.{dataset}.hotspot_forecast`,
                    STRUCT(@horizon AS horizon, 0.90 AS confidence_level)
                )
                ORDER BY forecast_value DESC
                LIMIT 100
            """.format(project=self.project_id, dataset=self.dataset)
            config = bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("horizon", "INT64", horizon_days)]
            )
            rows = client.query(query, job_config=config).result()
            return [dict(row.items()) for row in rows]
        except Exception:
            return []

    def capability_status(self) -> dict:
        return {
            "configured": bool(self.project_id),
            "connected": self.enabled,
            "dataset": self.dataset,
            "forecast_model": "hotspot_forecast",
        }

