"""Create BigQuery resources and optionally sync local prototype records.

Run with Application Default Credentials after setting GOOGLE_CLOUD_PROJECT.
This script never creates synthetic citizen complaints.
"""

import argparse
import csv
import os
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import bigquery


ROOT = Path(__file__).resolve().parent


COMPLAINT_SCHEMA = [
    bigquery.SchemaField("id", "INTEGER"),
    bigquery.SchemaField("tracking_code", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("citizen_id", "STRING"),
    bigquery.SchemaField("original_text", "STRING"),
    bigquery.SchemaField("translated_text", "STRING"),
    bigquery.SchemaField("source_lang", "STRING"),
    bigquery.SchemaField("category", "STRING"),
    bigquery.SchemaField("severity_score", "FLOAT"),
    bigquery.SchemaField("latitude", "FLOAT"),
    bigquery.SchemaField("longitude", "FLOAT"),
    bigquery.SchemaField("region", "STRING"),
    bigquery.SchemaField("status", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
    bigquery.SchemaField("ai_summary", "STRING"),
    bigquery.SchemaField("infrastructure_need", "STRING"),
    bigquery.SchemaField("responsible_department", "STRING"),
    bigquery.SchemaField("ai_confidence", "FLOAT"),
    bigquery.SchemaField("image_evidence", "STRING"),
    bigquery.SchemaField("ai_provider", "STRING"),
    bigquery.SchemaField("ingested_at", "TIMESTAMP"),
]

REGION_SCHEMA = [
    bigquery.SchemaField("region", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("population", "INTEGER"),
    bigquery.SchemaField("infrastructure_index", "FLOAT"),
    bigquery.SchemaField("investment_penalty", "FLOAT"),
    bigquery.SchemaField("current_investment_plan", "STRING"),
    bigquery.SchemaField("data_source", "STRING"),
]


def create_resources(client, dataset_id, location):
    dataset_ref = "{}.{}".format(client.project, dataset_id)
    dataset = bigquery.Dataset(dataset_ref)
    dataset.location = location
    client.create_dataset(dataset, exists_ok=True)
    client.create_table(
        bigquery.Table("{}.complaints".format(dataset_ref), schema=COMPLAINT_SCHEMA),
        exists_ok=True,
    )
    client.create_table(
        bigquery.Table("{}.region_context".format(dataset_ref), schema=REGION_SCHEMA),
        exists_ok=True,
    )
    print("BigQuery resources ready in {} ({})".format(dataset_ref, location))


def load_region_context(client, dataset_id, path):
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["population"] = int(row["population"])
        row["infrastructure_index"] = float(row["infrastructure_index"])
        row["investment_penalty"] = float(row["investment_penalty"])
    table = "{}.{}.region_context".format(client.project, dataset_id)
    errors = client.insert_rows_json(table, rows, row_ids=[row["region"] for row in rows])
    if errors:
        raise RuntimeError(errors)
    print("Loaded {} region-context rows from {}".format(len(rows), path))


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--load-example-context", action="store_true")
    parser.add_argument("--context-csv", type=Path)
    args = parser.parse_args()
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise SystemExit("Set GOOGLE_CLOUD_PROJECT first.")
    dataset_id = os.environ.get("BIGQUERY_DATASET", "citizen_demand")
    location = os.environ.get("BIGQUERY_LOCATION", "asia-south1")
    client = bigquery.Client(project=project, location=location)
    create_resources(client, dataset_id, location)
    context_path = args.context_csv
    if args.load_example_context:
        context_path = ROOT / "bigquery" / "region_context.example.csv"
    if context_path:
        load_region_context(client, dataset_id, context_path)


if __name__ == "__main__":
    main()
