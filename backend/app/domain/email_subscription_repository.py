"""Small compare-and-swap repository shared by API and notification workers."""

import copy
import json
import uuid


class EmailSubscriptionRepository:
    def __init__(self, store):
        self.store = store

    def get(self, job_id):
        if not hasattr(self.store, "_Session"):
            with self.store._lock:
                return copy.deepcopy(getattr(self.store, "_email_subscriptions", {}).get(job_id))
        from app.storage_db import EmailSubscriptionRecord
        with self.store._Session() as session:
            row = session.get(EmailSubscriptionRecord, job_id)
            return json.loads(row.data_json) if row else None

    def list(self):
        if not hasattr(self.store, "_Session"):
            with self.store._lock:
                return copy.deepcopy(list(getattr(self.store, "_email_subscriptions", {}).values()))
        from app.storage_db import EmailSubscriptionRecord
        with self.store._Session() as session:
            return [json.loads(row.data_json) for row in session.query(EmailSubscriptionRecord).all()]

    def mutate(self, job_id, change):
        return self.mutate_many([job_id], lambda _id, old: change(old))[0]

    def mutate_many(self, job_ids, change):
        """All changes commit together. Callback must be pure; None means no write."""
        if not hasattr(self.store, "_Session"):
            with self.store._lock:
                if not hasattr(self.store, "_email_subscriptions"):
                    self.store._email_subscriptions = {}
                records = self.store._email_subscriptions
                values = [change(j, copy.deepcopy(records.get(j))) for j in job_ids]
                for job_id, value in zip(job_ids, values):
                    if value is not None:
                        records[job_id] = copy.deepcopy(value)
                return copy.deepcopy(values)
        from sqlalchemy import update
        from sqlalchemy.exc import IntegrityError
        from app.storage_db import EmailSubscriptionRecord
        for _ in range(8):
            with self.store._Session() as session, session.no_autoflush:
                values = []
                conflict = False
                for job_id in job_ids:
                    row = session.get(EmailSubscriptionRecord, job_id)
                    value = change(job_id, json.loads(row.data_json) if row else None)
                    values.append(value)
                    if value is None:
                        continue
                    revision = uuid.uuid4().hex
                    encoded = json.dumps(value, ensure_ascii=False)
                    if row is None:
                        session.add(EmailSubscriptionRecord(job_id=job_id, revision=revision, data_json=encoded))
                    else:
                        changed = session.execute(update(EmailSubscriptionRecord).where(
                            EmailSubscriptionRecord.job_id == job_id,
                            EmailSubscriptionRecord.revision == row.revision,
                        ).values(revision=revision, data_json=encoded))
                        if changed.rowcount != 1:
                            conflict = True
                            break
                if conflict:
                    session.rollback()
                    continue
                try:
                    session.commit()
                    return values
                except IntegrityError:
                    session.rollback()
        raise RuntimeError("email_subscription_busy")
