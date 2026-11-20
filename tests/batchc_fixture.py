"""Batch C synthetic actors and metadata. No business handlers are replaced."""
from company_operations_browser_fixture import app, fixture
from pydantic import BaseModel
from fastapi import HTTPException
from sqlalchemy import select
from app.db.session import SessionLocal, engine
from app.db.base import Base
from app.db.models.user import User
from app.db.models.driver import Driver
from app.db.models.port import Port
from app.db.models.crew_profile import CrewProfile
from app.db.models.aggregator_profile import AggregatorProfile
from app.services.auth import get_password_hash
from app.api.v1 import routes_reviews, routes_files, routes_expenses, routes_facilities, routes_hotels, routes_restaurants, routes_sightseeing, routes_pubs, routes_payments
import batcha_observer as observer

assert engine.url.database == 'company_ops_fixture' and engine.url.host == '127.0.0.1' and engine.url.port == 55957
for router,prefix in [(routes_reviews.router,'/api/v1/reviews'),(routes_files.router,'/api/v1/files'),(routes_expenses.router,'/api/v1/crew/expense-bills'),(routes_facilities.router,'/api/v1/facilities'),(routes_hotels.router,'/api/v1/hotels'),(routes_restaurants.router,'/api/v1/restaurants'),(routes_sightseeing.router,'/api/v1/sightseeing'),(routes_pubs.router,'/api/v1/pubs'),(routes_payments.router,'/api/v1/crew/payments')]:
    app.include_router(router,prefix=prefix)
observer.SEED_TABLES.update({'drivers','venue_reviews','booking_invitations','driver_magic_links','driver_magic_link_reach_events','port_service_requests','pricing_vehicle_categories','pricing_ride_types','pricing_rules','restaurants','hotels','sightseeing','pubs','vendors','facility_scans','pricing_durations','pricing_provider_settings','payments','booking_reviews'})

class Actor(BaseModel):
    label: str
    role: str='crew'
    email: str|None=None
    provider_id: int|None=None
    port_id: int|None=None
    status: str='Active'

@app.post('/__fixture/batchb/actor')
def actor(body:Actor):
    if not body.label.startswith(('M09-','M10-','M11-','M12-','X03-')) or body.role not in {'crew','aggregator','driver'}:raise HTTPException(400,'Synthetic Batch C actor only')
    email=body.email or body.label.lower()+'@example.com'
    if not email.endswith('@example.com'):raise HTTPException(400,'Synthetic domain required')
    with SessionLocal() as db:
        if body.role=='driver':
            d=Driver(aggregator_id=body.provider_id or db.query(AggregatorProfile.id).join(User,User.id==AggregatorProfile.user_id).filter(User.email=='provider@example.com').scalar(),name=body.label,email=email,phone='0000000000',vehicle_number='QA-'+body.label,vehicle_type='ac',status='Available',hashed_password=get_password_hash('QA-Driver-Only-2027!'),is_temp_password=0)
            db.add(d);db.commit();return {'driver_id':d.id,'email':email,'fixture_only':True}
        u=User(email=email,name=body.label,role=body.role,hashed_password=get_password_hash('fixture-only-secret'));db.add(u);db.flush();result={'user_id':u.id,'email':email,'fixture_only':True}
        if body.role=='crew':
            profile=CrewProfile(user_id=u.id,full_name=body.label,rank='AB',nationality='IN',passport_number='QA'+body.label.replace('-',''));db.add(profile);db.flush();result['crew_profile_id']=profile.id
        else:
            profile=AggregatorProfile(user_id=u.id,company_name=body.label,operating_port_id=body.port_id or db.query(Port.id).order_by(Port.id).first()[0],provider_type='partnered_driver',status=body.status);db.add(profile);db.flush();result['provider_id']=profile.id
        db.commit();return result

@app.get('/__fixture/batchb/challenges')
def challenges(email:str):
    from app.db.models.email_verification import EmailVerification
    from app.db.models.password_reset import PasswordReset
    with SessionLocal() as db:
        user=db.query(User).filter(User.email==email).first()
        return {'email':email,'registration':[{'id':r.id,'attempts':r.attempts,'verified':r.verified,'expires_at':r.expires_at.isoformat()} for r in db.query(EmailVerification).filter(EmailVerification.email==email).all()], 'reset':[{'id':r.id,'expires_at':r.expires_at.isoformat()} for r in db.query(PasswordReset).filter(PasswordReset.user_id==user.id).all()] if user else []}

# External gateway boundary only. Genuine create/verify handlers and HMAC remain.
from app.services import payments as payment_service
from types import SimpleNamespace
import uuid
PAYMENT_CALLS = []
class SyntheticGatewayOrders:
    def create(self, payload):
        result = {"id": "order_qa_" + uuid.uuid4().hex[:16]}
        PAYMENT_CALLS.append({"operation": "order.create", "payload": payload, "result": result, "external_egress": False})
        return result
payment_service.RAZORPAY_KEY_ID = "qa_synthetic_key"
payment_service.RAZORPAY_KEY_SECRET = "QA-Payment-Only-2027!"
payment_service._client = SimpleNamespace(order=SyntheticGatewayOrders())

@app.get('/__fixture/batchb/payment-boundary')
def payment_boundary():
    return {"adapter": "synthetic_gateway_orders", "configured_signature_mode": payment_service.payments_enabled(), "external_egress": False, "calls": PAYMENT_CALLS}

@app.get('/__fixture/batchb/model-contracts')
def model_contracts():
    from sqlalchemy import inspect
    from pathlib import Path
    import hashlib
    selected = ('aggregator_profiles', 'drivers', 'cab_bookings', 'agency_transfers', 'booking_provider_rejections')
    inspector = inspect(engine)
    root = Path(__file__).resolve().parents[1]
    paths = ('app/db/models/aggregator_profile.py', 'app/db/models/cab_booking.py', 'app/api/v1/registration.py', 'app/services/booking_service.py', 'app/api/v1/routes_agency_transfers.py')
    return {"database_columns": {name:[c['name'] for c in inspector.get_columns(name)] for name in selected if inspector.has_table(name)}, "mounted_routes":[{"path": r.path, "methods": sorted(r.methods or [])} for r in app.routes if hasattr(r,'methods')], "source_sha256": {name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in paths}, "assessment_boundary": "Observed genuine pinned schema and mounted routes; absence of guessed field names alone does not establish missing functionality."}
# Read-only observer of the isolated QA private-upload directory. No storage handler is replaced.
@app.get('/__fixture/batchc/private-storage')
def private_storage_observer():
    from app.services import storage
    from pathlib import Path
    import hashlib
    folder = Path(storage.PRIVATE_UPLOAD_DIR).resolve()
    workspace = Path.cwd().resolve()
    if not folder.is_relative_to(workspace):
        raise HTTPException(400, 'QA observer requires workspace-private storage')
    files = [{'relative_path': p.relative_to(folder).as_posix(), 'bytes': p.stat().st_size,
              'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
             for p in folder.rglob('*') if p.is_file()] if folder.exists() else []
    return {'fixture_only': True, 'backend': 'local-private', 'files': files}

# QA-only deterministic observation gates for routine own-notification concurrency and SOS rollback.
import threading
from sqlalchemy import event
_C_READ_GATE = None
_C_SOS_FAULT = {'armed': False, 'fired': False, 'proof': []}

@app.post('/__fixture/batchc/notification-read-barrier')
def arm_own_read_gate():
    global _C_READ_GATE
    _C_READ_GATE = {'barrier': threading.Barrier(2), 'pids': set(), 'events': []}
    return {'fixture_only': True, 'stage': 'after real negative NotificationRead SELECT', 'participants': 2}

@app.get('/__fixture/batchc/notification-read-barrier')
def own_read_gate_result():
    g = _C_READ_GATE
    return {'pids': sorted(g['pids']) if g else [], 'events': g['events'] if g else []}

@app.post('/__fixture/batchc/sos-commit-fault')
def arm_sos_commit_fault():
    _C_SOS_FAULT.update(armed=True, fired=False, proof=[])
    return {'fixture_only': True, 'stage': 'real DB fault after SOS and timeline flush, before COMMIT'}

@app.get('/__fixture/batchc/sos-commit-fault')
def sos_commit_fault_result():
    return _C_SOS_FAULT

@event.listens_for(engine, 'after_cursor_execute')
def batchc_after_real_statement(conn, cursor, statement, parameters, context, executemany):
    sql = statement.lower().strip()
    if sql.startswith('insert into crew_sos_requests'):
        conn.info['batchc_sos_insert'] = True
    if sql.startswith('insert into crew_sos_timeline_events'):
        conn.info['batchc_sos_timeline_insert'] = True
    g = _C_READ_GATE
    if g and sql.startswith('select') and 'from notification_reads' in sql and 'where notification_reads.notification_id =' in sql:
        pid = conn.connection.driver_connection.get_backend_pid()
        with observer.LOCK:
            first = pid not in g['pids'] and len(g['pids']) < 2
            if first:
                g['pids'].add(pid)
                g['events'].append({'stage': 'after-negative-select', 'pid': pid, 'row_count': cursor.rowcount})
        if first:
            try:
                g['barrier'].wait(timeout=12)
                g['events'].append({'stage': 'released', 'pid': pid})
            except threading.BrokenBarrierError:
                g['events'].append({'stage': 'timeout', 'pid': pid})

@event.listens_for(engine, 'commit')
def batchc_before_real_commit(conn):
    case_insert = conn.info.pop('batchc_sos_insert', False)
    timeline_insert = conn.info.pop('batchc_sos_timeline_insert', False)
    if _C_SOS_FAULT['armed'] and case_insert and timeline_insert:
        _C_SOS_FAULT.update(armed=False, fired=True)
        _C_SOS_FAULT['proof'].append({'case_insert_flushed': True, 'timeline_insert_flushed': True,
                                    'stage': 'before real COMMIT', 'pid': conn.connection.driver_connection.get_backend_pid()})
        conn.exec_driver_sql('SELECT 1 / 0')

observer.SEED_TABLES.update({'notifications'})
