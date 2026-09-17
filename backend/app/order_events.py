"""First-observed checkout milestones. Browser events never authorize payments."""
import logging
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from .storage_db import OrderEventRecord

logger = logging.getLogger(__name__)


def record_event(store, order_no, event, source):
    if not hasattr(store, '_Session'):
        return False
    try:
        with store._Session() as session:
            session.add(OrderEventRecord(order_no=order_no,event=event,source=source,
                                         occurred_at=datetime.now(timezone.utc)))
            session.commit()
        return True
    except IntegrityError:
        return True  # First event wins, including duplicate payment notifications.
    except Exception:
        logger.warning('Order milestone write failed', extra={'job_id':order_no,'event':event})
        return False


def milestones(store, number):
    with store._Session() as session:
        rows=session.query(OrderEventRecord).filter_by(order_no=number).all()
        result={r.event:{'at':r.occurred_at.isoformat(), 'source':r.source} for r in rows}
    return {name:result.get(name) for name in ('quote_shown','payment_clicked','payment_succeeded')}


class BrowserEvent(BaseModel):
    event: Literal['quote_shown','payment_clicked']


def make_event_router(store, authorize):
    router=APIRouter()

    @router.post('/api/v2/jobs/{job_id}/checkout-events')
    def report(job_id: str, body: BrowserEvent, request: Request):
        job=store.get(job_id)
        if not job:
            raise HTTPException(404,'任务不存在')
        if not authorize(request,job):
            raise HTTPException(403,'无权访问该任务')
        number=f'batch_{job.batch_id}' if job.batch_id else job.id
        if not record_event(store,number,body.event,'browser'):
            raise HTTPException(503,'记录暂时不可用')
        return {'ok':True}
    return router
