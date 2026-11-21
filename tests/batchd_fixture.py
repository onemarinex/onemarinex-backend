"""QA-only synthetic Batch D prerequisites over the guarded, genuine router fixture."""
from batchc_fixture import app, fixture, observer, engine, SessionLocal, Base
from fastapi import HTTPException
from pydantic import BaseModel
from app.db.models.user import User
from app.db.models.shipping_company import ShippingCompany, CompanyUserMembership, VesselRoleClaim
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.services.auth import get_password_hash
from app.api.v1 import routes_privacy
from datetime import datetime, timezone

app.include_router(routes_privacy.router, prefix='/api/v1')
app.include_router(routes_privacy.admin_router, prefix='/api/v1/superadmin')
Base.metadata.create_all(engine)
observer.SEED_TABLES.update({'shipping_companies','company_invitations','company_management_reports','company_audit_events','company_onboarding_drafts','data_deletion_requests','incident_notes','incident_timeline_events','company_disclosure_requests','safety_escalations'})

class Scope(BaseModel):
    case_id: str

@app.post('/__fixture/batchd/company')
def company_scope(body: Scope):
    if not body.case_id.startswith(('M13-','M14-','M16-','X04-')):
        raise HTTPException(400, 'Synthetic Batch D scope only')
    with SessionLocal() as db:
        result={'case_id':body.case_id,'users':{},'companies':{},'memberships':{}}
        for k in ['A','B']:
            co=ShippingCompany(legal_name='QA '+body.case_id+' '+k,normalized_name='QA '+body.case_id+' '+k,status='VERIFIED')
            db.add(co);db.flush();result['companies'][k]=co.id
        for name,role,tenant,member in [('admin','shipping_company','A','ADMIN'),('admin2','shipping_company','A','ADMIN'),('viewer','shipping_company','A','VIEWER'),('other','shipping_company','B','ADMIN'),('outsider','shipping_company',None,None),('moving','shipping_company','B','ANALYST'),('returning','shipping_company',None,None)]:
            email=body.case_id.lower()+'.'+name+'@example.com'
            u=User(email=email,name='QA '+body.case_id+' '+name,role=role,hashed_password=get_password_hash('fixture-only-secret'))
            db.add(u);db.flush();result['users'][name]={'id':u.id,'email':email}
            if tenant:
                m=CompanyUserMembership(company_id=result['companies'][tenant],user_id=u.id,role=member,enabled=True);db.add(m);db.flush();result['memberships'][name]=m.id
            if name in ['moving','returning']:
                m=CompanyUserMembership(company_id=result['companies']['A'],user_id=u.id,role='VIEWER',enabled=False);db.add(m);db.flush();result['memberships'][name+'_A']=m.id
        vessel=Vessel(name='QA '+body.case_id+' vessel',imo_number='QA-'+body.case_id,vessel_type='Bulk Carrier',status='Active',agent_id=fixture.agent.id)
        db.add(vessel);db.flush()
        call=VesselCall(vessel_id=vessel.id,agency_id=fixture.agency.id,port_id=fixture.port.id,vessel_name=vessel.name,agency_name='Agency',started_at=datetime(2026,6,1,tzinfo=timezone.utc),port_name=fixture.port.name,imo_number=vessel.imo_number)
        db.add(call);db.flush()
        claim=VesselRoleClaim(company_id=result['companies']['A'],vessel_id=vessel.id,role='ISM_MANAGER',effective_start=datetime(2026,1,1,tzinfo=timezone.utc),status='APPROVED')
        db.add(claim);db.commit()
        result.update(vessel_id=vessel.id,call_id=call.id,claim_id=claim.id,port_id=fixture.port.id)
        return result

from sqlalchemy import event
SQL_QUERIES=[]
@event.listens_for(engine, 'before_cursor_execute')
def count_queries(conn,cursor,statement,parameters,context,executemany):
    if statement.lstrip().lower().startswith('select'):
        SQL_QUERIES.append(statement[:500])

@app.get('/__fixture/batchd/queries')
def queries():
    return {'count':len(SQL_QUERIES),'recent_without_parameters':SQL_QUERIES[-150:]}

class Fault(BaseModel):
    kind: str

from app.api.v1 import routes_company_reports
original_renderer=routes_company_reports.render_report
RENDER_FAILURE={'remaining':0}
def renderer_boundary(*args,**kwargs):
    if RENDER_FAILURE['remaining']:
        RENDER_FAILURE['remaining']-=1
        raise RuntimeError('QA one-shot PDF constructor failure after authorization')
    return original_renderer(*args,**kwargs)
routes_company_reports.render_report=renderer_boundary

@app.post('/__fixture/batchd/fault')
def arm_fault(body:Fault):
    if body.kind=='report-render':
        RENDER_FAILURE['remaining']=1
    elif body.kind=='invitation-commit':
        observer.FAULT.update(match='insert into company_audit_events',remaining=1,events=[])
    else:
        raise HTTPException(400,'Only specified disposable fault boundaries allowed')
    return {'armed':body.kind,'fixture_only':True}

from app.db.models.port import Port
from app.db.models.port_rule import PortRule
from app.db.models.crew_profile import CrewProfile
from app.services import port_time
real_utc_now=port_time.utc_now

@app.post('/__fixture/batchd/ports')
def port_scope(body:Scope):
    if not body.case_id.startswith(('M14-','X04-')):raise HTTPException(400,'Batch D ports only')
    with SessionLocal() as db:
        result={}
        for key in ['Vizag','Mumbai','London']:
            port=Port(name='QA '+body.case_id+' '+key,is_active=True)
            db.add(port);db.flush();result[key]={'id':port.id,'code':port.code}
            db.add(PortRule(port_name=port.code,timezone='Europe/London' if key=='London' else 'Asia/Kolkata',opening_time='00:00',closing_time='23:59',working_days=['Mon','Tue','Wed','Thu','Fri','Sat','Sun']))
        agent=db.get(User,fixture.agent.id);agent.agent_profile.assigned_port=result['Vizag']['code']
        u=User(email=body.case_id.lower()+'.crew@example.com',name='QA port crew',role='crew',hashed_password=get_password_hash('fixture-only-secret'))
        db.add(u);db.flush();db.add(CrewProfile(user_id=u.id,full_name='QA port crew',rank='AB',nationality='IN',passport_number='QA-'+body.case_id,current_port=result['London']['code']))
        db.commit();result['crew_email']=u.email
        return result

class Clock(BaseModel):
    instant:str|None=None
@app.post('/__fixture/batchd/clock')
def clock(body:Clock):
    if body.instant:
        instant=datetime.fromisoformat(body.instant.replace('Z','+00:00'))
        if instant.tzinfo is None:raise HTTPException(400,'Offset required')
        port_time.utc_now=lambda:instant
    else:port_time.utc_now=real_utc_now
    return {'clock_dependency_only':True,'instant':body.instant}
