"""CAS storage for merchant payment mail; never queries historical orders."""
import copy
import json
import uuid


class PaymentEmailRepository:
    def __init__(self, store):
        self.store = store

    def get(self, order_no):
        if not hasattr(self.store, "_Session"):
            with self.store._lock:
                return copy.deepcopy(getattr(self.store, "_payment_emails", {}).get(order_no))
        from app.storage_db import PaymentEmailRecord
        with self.store._Session() as session:
            row = session.get(PaymentEmailRecord, order_no)
            return json.loads(row.data_json) if row else None

    def due(self, now, limit=20):
        if not hasattr(self.store, "_Session"):
            with self.store._lock:
                rows = [row for row in getattr(self.store, "_payment_emails", {}).values()
                        if row["status"] in {"pending", "retry", "sending"}
                        and row.get("next_attempt_at", 0) <= now and row.get("lease_until", 0) <= now]
                return copy.deepcopy(sorted(rows, key=lambda row: row["created_at"])[:limit])
        from app.storage_db import PaymentEmailRecord
        with self.store._Session() as session:
            rows = session.query(PaymentEmailRecord).filter(
                PaymentEmailRecord.status.in_(["pending", "retry", "sending"]),
                PaymentEmailRecord.next_attempt_at <= now,
                PaymentEmailRecord.lease_until <= now,
            ).order_by(PaymentEmailRecord.created_at).limit(limit).all()
            return [json.loads(row.data_json) for row in rows]

    def mutate(self, order_no, change):
        """Callback must be pure. None means no write. Unique order id + CAS."""
        if not hasattr(self.store, "_Session"):
            with self.store._lock:
                if not hasattr(self.store, "_payment_emails"):
                    self.store._payment_emails = {}
                records = self.store._payment_emails
                value = change(copy.deepcopy(records.get(order_no)))
                if value is not None:
                    records[order_no] = copy.deepcopy(value)
                return copy.deepcopy(value)
        from sqlalchemy import update
        from sqlalchemy.exc import IntegrityError
        from app.storage_db import PaymentEmailRecord
        for _ in range(8):
            with self.store._Session() as session, session.no_autoflush:
                row = session.get(PaymentEmailRecord, order_no)
                value = change(json.loads(row.data_json) if row else None)
                if value is None:
                    return None
                values = dict(revision=uuid.uuid4().hex, status=value["status"],
                              next_attempt_at=value.get("next_attempt_at", 0),
                              lease_until=value.get("lease_until", 0), created_at=value["created_at"],
                              data_json=json.dumps(value, ensure_ascii=False))
                try:
                    if row is None:
                        session.add(PaymentEmailRecord(order_no=order_no, **values))
                    else:
                        changed = session.execute(update(PaymentEmailRecord).where(
                            PaymentEmailRecord.order_no == order_no,
                            PaymentEmailRecord.revision == row.revision,
                        ).values(**values))
                        if changed.rowcount != 1:
                            session.rollback()
                            continue
                    session.commit()
                    return value
                except IntegrityError:
                    session.rollback()
        raise RuntimeError("payment_email_outbox_busy")
