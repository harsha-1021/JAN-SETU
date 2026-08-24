"""Copy existing SQLite demo data into Firebase and BigQuery.

Safe to rerun for Firebase because tracking codes and escalation IDs are stable.
BigQuery streaming inserts are best used once for the initial migration.
"""

import main


def main_sync():
    complaints = main._sqlite_complaints()
    escalations = main._sqlite_escalations()
    firebase_count = 0
    bigquery_count = 0
    for complaint in complaints:
        if main.firebase.enabled:
            main.firebase.save_complaint(complaint)
            firebase_count += 1
        if main.bigquery_analytics.insert_complaint(complaint):
            bigquery_count += 1
    if main.firebase.enabled:
        for escalation in escalations:
            main.firebase.save_escalation(escalation)
        main.firebase.ensure_counter(
            "complaints", max((item["id"] for item in complaints), default=0)
        )
        main.firebase.ensure_counter(
            "escalations", max((item["id"] for item in escalations), default=0)
        )
    print("Firebase complaints synced: {}".format(firebase_count))
    print("Firebase escalations synced: {}".format(len(escalations) if main.firebase.enabled else 0))
    print("BigQuery complaints inserted: {}".format(bigquery_count))


if __name__ == "__main__":
    main_sync()
