import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import time
from sqlalchemy import text
from app.db.session import SessionLocal
from app.db.models.incident import IncidentTimelineEvent, Incident
from app.db.models.company_fleet_change import CompanyFleetChange
from app.services.company_fleet_identity import lock_claim_identity
import test_company_disclosure_reports_http as disclosure
import test_company_operations_http as fixture

BASE='/api/v1/shipping-company'

class SafetyOversightHTTP(unittest.TestCase):
    setUp=disclosure.DisclosureHTTP.setUp
    tearDown=disclosure.DisclosureHTTP.tearDown
    headers=disclosure.DisclosureHTTP.headers
    request=disclosure.DisclosureHTTP.request
    escalation=disclosure.DisclosureHTTP.escalation

    def test_escalated_progress_is_tenant_bound_without_sensitive_source_text(self):
        ident=self.escalation()
        self.case.status='INVESTIGATING'; self.case.severity='high'
        self.db.add(IncidentTimelineEvent(incident_id=self.case.id,event_type='investigating',label='PRIVATE Alice',detail='PRIVATE DESCRIPTION',event_time=fixture.NOW))
        self.db.add(IncidentTimelineEvent(incident_id=self.case.id,event_type='note',label='PRIVATE MEDICAL',event_time=fixture.NOW))
        self.db.commit()
        result=self.request('GET',f'{BASE}/safety-escalations/{ident}',self.admin).json()
        self.assertEqual(result['status'],'INVESTIGATING')
        self.assertEqual(result['severity'],'high')
        self.assertEqual(result['call_id'],self.call.id)
        self.assertNotIn('Alice',str(result));self.assertNotIn('PRIVATE',str(result))
        self.assertTrue(any(r['label']=='Investigation started' for r in result['timeline']))
        listing=self.request('GET',BASE+'/safety-escalations?status=INVESTIGATING&severity=high&search='+self.call.vessel_name,self.admin).json()
        self.assertEqual(listing['total'],1);self.assertEqual(listing['summary']['investigating'],1)
        self.assertEqual(self.request('GET',BASE+'/safety-escalations?status=RESOLVED',self.admin).json()['total'],0)
        self.request('GET',f'{BASE}/safety-escalations/{ident}',self.agent,status=403)

    def test_urgent_review_priority_does_not_invent_critical_incident_severity(self):
        from app.db.models.company_disclosure import CompanySafetyEscalation
        ident=self.escalation()
        self.case.severity="high"
        self.db.get(CompanySafetyEscalation,ident).urgency="URGENT"
        self.db.commit()
        data=self.request("GET",BASE+"/safety-escalations",self.admin).json()
        self.assertEqual(data["total"],1)
        self.assertEqual(data["summary"]["critical"],0)

    def test_oversight_appends_company_history_without_closing_or_editing_incident(self):
        ident=self.escalation();before=self.case.status
        for action in ['ACKNOWLEDGE','COMMENT','CLARIFY','FLAG']:
            self.request('POST',f'{BASE}/safety-escalations/{ident}/oversight',self.admin,{'action':action,'message':'Company requires an operational update'},201)
        self.db.expire_all();self.assertEqual(self.db.get(Incident,self.case.id).status,before)
        result=self.request('GET',f'{BASE}/safety-escalations/{ident}',self.admin).json()
        self.assertEqual(len(result['oversight']),4)
        self.request('POST',f'{BASE}/safety-escalations/{ident}/oversight',self.admin,{'action':'CLOSE','message':'Cannot close operational incident'},422)
        self.request('DELETE',f'/api/v1/superadmin/safety-escalations/{ident}',self.sa,status=204)
        self.request('POST',f'{BASE}/safety-escalations/{ident}/oversight',self.admin,{'action':'FLAG','message':'No access after revocation'},404)

    def test_legacy_decision_waits_for_identity_before_locking_claim(self):
        from app.api.v1.routes_company_fleet import admin_router
        self.client.app.include_router(admin_router,prefix='/api/v1/superadmin')
        from app.db.models.shipping_company import VesselRoleClaim
        self.claim=self.db.query(VesselRoleClaim).filter_by(company_id=self.company.id,status='APPROVED').first()
        change=CompanyFleetChange(company_id=self.company.id,claim_id=self.claim.id,requested_by_user_id=self.admin.id,action='VOID',payload={},reason='Fixture reviewed removal',status='REQUESTED')
        self.db.add(change);self.db.commit()
        holder=SessionLocal(); lock_claim_identity(holder,self.claim.id)
        # Keep only the identity lock. Release the claim row lock through a
        # savepoint, then prove a waiting legacy review cannot take that row.
        holder.rollback()
        from app.services.company_fleet_identity import identity_key,lock_identity
        from app.db.models.shipping_company import VesselRoleClaim
        key=holder.query(identity_key()).filter(VesselRoleClaim.id==self.claim.id).scalar();lock_identity(holder,key)
        headers=self.headers(self.sa)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.client.post,f'/api/v1/superadmin/fleet-changes/{change.id}/decision',headers=headers,json={'approve':True,'reason':'Independent evidence reviewed for removal'})
            for _ in range(50):
                with SessionLocal() as observer:
                    waiting=observer.execute(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted")).scalar()
                if waiting:break
                time.sleep(.02)
            try:
                self.assertTrue(waiting,'Legacy decision did not wait on the identity lock')
                holder.execute(text('SELECT id FROM vessel_role_claims WHERE id=:id FOR UPDATE NOWAIT'),{'id':self.claim.id})
            finally:
                holder.rollback();holder.close()
            result=future.result(timeout=10)
            self.assertEqual(result.status_code,200,result.text)
