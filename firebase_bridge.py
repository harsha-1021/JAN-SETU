"""Firebase Realtime Database and Authentication bridge.

Firebase is authoritative when configured. SQLite remains a local compatibility
fallback so contributors can run the Digital Public Good without cloud access.
"""

import os
import time
from typing import Optional


def _without_none(value):
    if isinstance(value, dict):
        return {key: _without_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_none(item) for item in value]
    return value


class FirebaseBridge:
    def __init__(self) -> None:
        self.database_url = os.environ.get("FIREBASE_DATABASE_URL", "").strip()
        self.project_id = (
            os.environ.get("FIREBASE_PROJECT_ID")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or ""
        ).strip()
        self._app = None
        self._initialized = False

    def _initialize(self) -> bool:
        if self._initialized:
            return self._app is not None
        self._initialized = True
        if not self.database_url:
            return False
        try:
            import firebase_admin

            try:
                self._app = firebase_admin.get_app()
            except ValueError:
                options = {"databaseURL": self.database_url}
                if self.project_id:
                    options["projectId"] = self.project_id
                self._app = firebase_admin.initialize_app(options=options)
            return True
        except Exception:
            self._app = None
            return False

    @property
    def enabled(self) -> bool:
        return self._initialize()

    def public_config(self) -> dict:
        config = {
            "apiKey": os.environ.get("FIREBASE_API_KEY", ""),
            "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN", ""),
            "databaseURL": self.database_url,
            "projectId": self.project_id,
            "storageBucket": os.environ.get("FIREBASE_STORAGE_BUCKET", ""),
            "messagingSenderId": os.environ.get("FIREBASE_MESSAGING_SENDER_ID", ""),
            "appId": os.environ.get("FIREBASE_APP_ID", ""),
        }
        required = ("apiKey", "authDomain", "databaseURL", "projectId", "appId")
        return {"enabled": all(config[key] for key in required), "config": config}

    def verify_policymaker_token(self, token: str) -> Optional[dict]:
        if not token or not self.enabled:
            return None
        try:
            from firebase_admin import auth

            claims = auth.verify_id_token(token, app=self._app)
            if claims.get("policymaker") is not True:
                return None
            return claims
        except Exception:
            return None

    def allocate_id(self, counter_name: str) -> Optional[int]:
        if not self.enabled:
            return None
        from firebase_admin import db

        reference = db.reference("counters/{}".format(counter_name), app=self._app)
        value = reference.transaction(lambda current: int(current or 0) + 1)
        return int(value)

    def ensure_counter(self, counter_name: str, minimum: int) -> None:
        if not self.enabled:
            return
        from firebase_admin import db

        reference = db.reference("counters/{}".format(counter_name), app=self._app)
        reference.transaction(lambda current: max(int(current or 0), int(minimum)))

    def save_complaint(self, record: dict) -> None:
        if not self.enabled:
            return
        from firebase_admin import db

        clean = _without_none(record)
        tracking_code = clean["tracking_code"]
        db.reference("complaints/{}".format(tracking_code), app=self._app).set(clean)
        db.reference("public_status/{}".format(tracking_code), app=self._app).set(
            {
                key: clean[key]
                for key in ("id", "tracking_code", "category", "status", "region", "created_at")
            }
        )
        self.publish("complaint_created")

    def get_complaint(self, tracking_code: str) -> Optional[dict]:
        if not self.enabled:
            return None
        from firebase_admin import db

        return db.reference("complaints/{}".format(tracking_code), app=self._app).get()

    def list_complaints(self) -> list:
        if not self.enabled:
            return []
        from firebase_admin import db

        payload = db.reference("complaints", app=self._app).get() or {}
        return list(payload.values()) if isinstance(payload, dict) else []

    def update_complaint(self, tracking_code: str, updates: dict) -> None:
        if not self.enabled:
            return
        from firebase_admin import db

        clean = _without_none(updates)
        db.reference("complaints/{}".format(tracking_code), app=self._app).update(clean)
        public_updates = {
            key: value
            for key, value in clean.items()
            if key in {"category", "status", "region", "created_at"}
        }
        if public_updates:
            db.reference("public_status/{}".format(tracking_code), app=self._app).update(
                public_updates
            )
        self.publish("complaint_updated")

    def update_group_status(self, region: str, category: str, status: str) -> None:
        for complaint in self.list_complaints():
            if complaint.get("region") == region and complaint.get("category") == category:
                self.update_complaint(complaint["tracking_code"], {"status": status})

    def save_escalation(self, record: dict) -> None:
        if not self.enabled:
            return
        from firebase_admin import db

        db.reference("escalations/{}".format(record["id"]), app=self._app).set(
            _without_none(record)
        )
        self.publish("escalation_created")

    def list_escalations(self) -> list:
        if not self.enabled:
            return []
        from firebase_admin import db

        payload = db.reference("escalations", app=self._app).get() or {}
        records = list(payload.values()) if isinstance(payload, dict) else []
        return sorted(records, key=lambda item: item.get("created_at", ""), reverse=True)

    def update_escalation(self, escalation_id: int, updates: dict) -> None:
        if not self.enabled:
            return
        from firebase_admin import db

        db.reference("escalations/{}".format(escalation_id), app=self._app).update(
            _without_none(updates)
        )
        self.publish("escalation_updated")

    def publish(self, event_type: str) -> None:
        if not self.enabled:
            return
        from firebase_admin import db

        db.reference("dashboard/last_update", app=self._app).set(
            {"event": event_type, "version": time.time_ns()}
        )
