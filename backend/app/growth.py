"""Optional first-party funnel analytics. Never authorizes or initiates payment."""
import logging
import json
from collections import Counter
from . import geo_location
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, or_
from .storage_db import JobRecord, OrderEventRecord, PayPalOrderRecord

log = logging.getLogger(__name__)
LANGS = ('zh-CN', 'zh-TW', 'en', 'ja', 'ko')

class Visit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session: str = Field(pattern=r'^[a-f0-9]{32}$')
    landing: Literal['home','translation','repair','examples','guide','other','unknown'] = 'unknown'
    metrics_version: Literal[1, 2] = 1
    service: Literal['conversion','translation','proofreading','repair','pdf'] | None = None
    event: Literal['visit', 'file_selected','service_exposed','service_clicked','upload_succeeded']
    language: Literal['zh-CN','zh-TW','en','ja','ko'] = 'zh-CN'
    device: Literal['mobile','desktop'] = 'desktop'
    source: Literal['direct','search','community','social','campaign','other'] = 'direct'


def excluded_traffic(request):
    """Exclude TV-origin telemetry, never payment facts or website access."""
    def is_tv(value):
        try:
            return (urlsplit(value).hostname or '').lower().rstrip('.') == 'tv.fixepub.com'
        except ValueError:
            return False
    if is_tv('https://' + request.headers.get('host', '')):
        return True
    origin = request.headers.get('origin')
    if origin and origin != 'null':
        return is_tv(origin)
    return is_tv(request.headers.get('referer', ''))


def path():
    return Path(os.environ.get('FUNNEL_DB_PATH') or Path(__file__).resolve().parents[1] / 'growth.sqlite')


def write(callback):
    try:
        with sqlite3.connect(path(), timeout=.2) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS visits(session TEXT PRIMARY KEY, started REAL NOT NULL, language TEXT NOT NULL, device TEXT NOT NULL, source TEXT NOT NULL, selected INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS service_steps_v2(session TEXT NOT NULL, service TEXT NOT NULL, event TEXT NOT NULL, PRIMARY KEY(session,service,event));
CREATE TABLE IF NOT EXISTS visit_landings(session TEXT PRIMARY KEY, category TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bindings(order_no TEXT PRIMARY KEY, session TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS downloads(order_no TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS visit_regions(session TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS visits_started ON visits(started);
CREATE INDEX IF NOT EXISTS bindings_session ON bindings(session);''')
            callback(db)
        return True
    except Exception:
        log.warning('Optional funnel telemetry unavailable')
        return False


def record_visit(body, address=None):
    def save(db):
        cutoff=time.time()-90*86400
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_steps'").fetchone():
            db.execute('DELETE FROM service_steps WHERE session IN (SELECT session FROM visits WHERE started < ?)',(cutoff,))
        db.execute('DELETE FROM service_steps_v2 WHERE session IN (SELECT session FROM visits WHERE started < ?)',(cutoff,))
        db.execute('DELETE FROM bindings WHERE session IN (SELECT session FROM visits WHERE started < ?)',(cutoff,))
        db.execute('DELETE FROM visit_landings WHERE session IN (SELECT session FROM visits WHERE started < ?)',(cutoff,))
        db.execute('DELETE FROM visit_regions WHERE session IN (SELECT session FROM visits WHERE started < ?)',(cutoff,))
        db.execute('DELETE FROM visits WHERE started < ?',(cutoff,))
        db.execute('DELETE FROM downloads WHERE at < ?',(cutoff,))
        if db.execute('SELECT count(*) FROM visits').fetchone()[0] >= 100000 and not db.execute('SELECT 1 FROM visits WHERE session=?',(body.session,)).fetchone():
            raise ValueError('Analytics capacity reached')
        inserted = db.execute('INSERT OR IGNORE INTO visits VALUES(?,?,?,?,?,0)', (body.session,time.time(),body.language,body.device,body.source)).rowcount
        if inserted:
            db.execute('INSERT OR IGNORE INTO visit_landings VALUES(?,?)',(body.session,body.landing))
            db.execute('INSERT OR IGNORE INTO visit_regions VALUES(?,?)', (body.session, json.dumps(geo_location.lookup(address), ensure_ascii=False)))
        if body.metrics_version == 2 and body.service and body.event in ('service_exposed','service_clicked','file_selected','upload_succeeded'):
            db.execute('INSERT OR IGNORE INTO service_steps_v2 VALUES(?,?,?)',(body.session,body.service,body.event))
        if body.event == 'file_selected':
            db.execute('UPDATE visits SET selected=1 WHERE session=?', (body.session,))
    return write(save)


def bind(order_no, session):
    if not session:
        return
    write(lambda db: db.execute('INSERT OR IGNORE INTO bindings SELECT ?,session FROM visits WHERE session=?',(order_no,session)))


def downloaded(job):
    if not job.is_test_order:
        downloaded_order('batch_'+job.batch_id if job.batch_id else job.id)


def downloaded_order(number):
    write(lambda db: db.execute('INSERT OR IGNORE INTO downloads VALUES(?,?)',(number,time.time())))


def report(store, days=7, language='all', source='all', device='all', repair_lookup=None):
    counts=dict(visits=0,selected=0,quoted=0,clicked=0,paid=0,downloaded=0)
    breakdown={k:dict(exposed=0,clicked=0,selected=0,uploaded=0,quoted=0,confirmed=0,paid=0) for k in ('conversion','translation','proofreading','repair','pdf','batch')}
    result={'days':days,'counts':counts,'services':breakdown,'quote_to_paid':None,'paid_to_download':None,'scope':'attributed_orders','started_at':None,'geography':[],'landings':[],'entry_metrics_version':2,'geo_database':geo_location.status()}
    if not path().exists(): return result
    with sqlite3.connect('file:'+str(path())+'?mode=ro',uri=True,timeout=.2) as db:
        args=[time.time()-days*86400];where='started>=?'
        for key,value in [('language',language),('source',source),('device',device)]:
            if value!='all':where+=' AND '+key+'=?';args.append(value)
        visits=db.execute('SELECT session,selected FROM visits WHERE '+where,args).fetchall()
        bindings=dict(db.execute('SELECT order_no,session FROM bindings'))
        downloads={r[0] for r in db.execute('SELECT order_no FROM downloads')}
        region_rows = dict(db.execute('SELECT session,data FROM visit_regions')) if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='visit_regions'").fetchone() else {}
        landing_rows=dict(db.execute('SELECT session,category FROM visit_landings')) if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='visit_landings'").fetchone() else {}
        result['started_at']=db.execute('SELECT MIN(started) FROM visits').fetchone()[0]
    sessions={row[0] for row in visits}
    with sqlite3.connect('file:'+str(path())+'?mode=ro',uri=True,timeout=.2) as db:
        steps=db.execute('SELECT session,service,event FROM service_steps_v2').fetchall() if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='service_steps_v2'").fetchone() else []
    for sid,service,event in steps:
        if sid in sessions and service in breakdown:
            key={'service_exposed':'exposed','service_clicked':'clicked','file_selected':'selected','upload_succeeded':'uploaded'}.get(event)
            if key:breakdown[service][key]+=1
    landing_counts={key:dict(category=key,sessions=count,quoted=0,paid=0) for key,count in Counter(landing_rows.get(sid,'unknown') for sid in sessions).items()}
    result['landings']=list(landing_counts.values())
    regions = Counter(region_rows.get(sid, '{}') for sid in sessions)
    result['geography'] = [dict(json.loads(data), sessions=count) for data,count in regions.most_common()]
    counts['visits']=len(sessions);counts['selected']=sum(row[1] for row in visits)
    ids=[key for key,value in bindings.items() if value in sessions]
    with store._Session() as db:
        for start in range(0,len(ids),400):
            chunk=ids[start:start+400]
            jobs=db.scalars(select(JobRecord).where(or_(JobRecord.id.in_(chunk),JobRecord.batch_id.in_([key[6:] for key in chunk if key.startswith('batch_')])))).all()
            events={}
            for e in db.scalars(select(OrderEventRecord).where(OrderEventRecord.order_no.in_(chunk))):events.setdefault(e.order_no,{})[e.event]=e.source
            modes={p.local_id:p.mode for p in db.scalars(select(PayPalOrderRecord).where(PayPalOrderRecord.local_id.in_(chunk)))}
            groups={}
            for job in jobs:
                number='batch_'+job.batch_id if job.batch_id else job.id
                groups.setdefault(number,[]).append(job)
            for number in chunk:
                if modes.get(number) not in {None,'live'}:continue
                rows=groups.get(number,[])
                if number.startswith('repair_'):
                    repair=repair_lookup(number[7:]) if repair_lookup else None
                    if not repair or repair.get('is_test_order'):continue
                    kind='repair'
                else:
                    if not rows or any(job.is_test_order for job in rows):continue
                    job=rows[0]
                    kind='batch' if job.batch_id else 'pdf' if job.output_mode=='original' else 'translation' if job.enable_translation else 'proofreading' if job.enable_precision_polish else 'conversion'
                e=events.get(number,{})
                quoted='quote_shown' in e
                paid=e.get('payment_succeeded') in {'verified_webhook','verified_query','verified_capture'}
                counts['quoted']+=quoted;counts['clicked']+=quoted and 'payment_clicked' in e
                counts['paid']+=quoted and paid
                counts['downloaded']+=quoted and paid and number in downloads
                breakdown[kind]['quoted']+=quoted;breakdown[kind]['paid']+=quoted and paid
                breakdown[kind]['confirmed']+=quoted and 'profile_confirmed' in e
                landing=landing_counts.get(landing_rows.get(bindings.get(number),'unknown'))
                if landing:
                    landing['quoted']+=quoted;landing['paid']+=quoted and paid
    result['quote_to_paid']=round(counts['paid']/counts['quoted'],4) if counts['quoted'] else None
    result['paid_to_download']=round(counts['downloaded']/counts['paid'],4) if counts['paid'] else None
    return result


def router():
    routes=APIRouter()
    @routes.post('/api/v2/analytics/events')
    def event(body: Visit, request: Request):
        if excluded_traffic(request):return {'recorded':False}
        ua=request.headers.get('user-agent','').lower()
        if any(word in ua for word in ('bot','spider','crawl','headless')) or request.headers.get('dnt')=='1':return {'recorded':False}
        if request.headers.get('sec-fetch-site')=='cross-site':raise HTTPException(403,'Cross-site telemetry rejected')
        from .infra.rate_limiter import get_real_ip
        return {'recorded':record_visit(body, get_real_ip(request))}
    return routes
