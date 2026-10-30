"""Round 3 actual HTTP privacy/integrity regressions; guarded disposable Postgres only."""
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from unittest.mock import patch
import test_welfare_feedback_http as welfare_fixture
import test_company_operations_http as fixture
from app.db.models.welfare_feedback import WelfareFeedback
from app.db.models.booking_review import BookingReview
from app.db.models.shipping_company import CompanyAuditEvent, VesselRoleClaim, ShippingCompany, CompanyUserMembership
from app.core.config import settings

class Round3WelfareHTTP(welfare_fixture.WelfareHTTP):
    booking = fixture.OperationsHTTP.booking
    def period(self, **kw):
        return urlencode(dict(start='2026-09-01T00:00:00Z',end='2026-11-01T00:00:00Z',**kw))
    def get(self, path, actor=None, **kw):
        return self.request('GET','/api/v1/'+('superadmin' if actor is self.sa else 'shipping-company')+'/welfare-intelligence/'+path+'?'+self.period(**kw),actor or self.admin).json()
    def separate_company(self):
        company=ShippingCompany(legal_name='Other company',normalized_name='OTHER COMPANY',status='VERIFIED')
        self.db.add(company);self.db.flush()
        self.db.query(CompanyUserMembership).filter_by(user_id=self.admin2.id).update({'company_id':company.id});self.db.commit()
    def populate(self,n=5,message='Good service',**answers):
        for index in range(n):
            user,assignment=(self.crew,self.crew_assignment) if index==0 else self.person(index)
            body=self.body(assignment)
            body.update(message=message,**answers)
            self.request('POST','/api/v1/crew/welfare-feedback',user,body,201)
    def test_screened_feedback_groups_no_identity_or_precise_payment(self):
        self.populate(extra_charge='PAID',amount_paid=123.45,currency='INR')
        page=self.get('feedback')
        self.assertEqual(page['items'][0]['respondents'],5)
        self.assertEqual(page['items'][0]['message'],'Good service.')
        self.assertEqual(page['items'][0]['month'],fixture.NOW.strftime('%Y-%m'))
        self.assertNotIn('123.45',str(page)); self.assertNotIn('contact_opt_in',page['items'][0]);self.assertNotIn('user_id',page['items'][0])
        self.separate_company()
        other=self.get('feedback',self.admin2)
        self.assertEqual(other['total'],0)
    def test_pii_allegation_and_unusual_prose_never_auto_publish(self):
        self.populate(message='Captain John Smith assaulted me; call +91 1234567890 john@example.com')
        page=self.get('feedback')
        self.assertEqual(page['items'][0]['text_status'],'withheld')
        for text in ('John','1234567890','john@example.com','assaulted'):
            self.assertNotIn(text,str(page))
    def test_complementary_answer_signature_suppresses_whole_cell(self):
        self.populate(6)
        row=self.db.query(WelfareFeedback).order_by(WelfareFeedback.id.desc()).first()
        row.answers=dict(row.answers,difficulty='DELAYED');self.db.commit()
        data=self.get('summary');self.assertFalse(data['suppressed']);self.assertEqual(data['questions']['difficulty']['status'],'insufficient_sample');self.assertEqual(data['obstacles']['numerator'],6)
        self.assertEqual(self.get('feedback')['total'],0)
        self.assertEqual(self.get('summary',self.sa)['respondents'],6)
    def test_threshold_config_and_narrow_date_scope_are_backend_enforced(self):
        self.populate()
        wide=self.get('summary')
        query=urlencode({'start':(fixture.NOW-timedelta(minutes=1)).isoformat(),'end':(fixture.NOW+timedelta(minutes=1)).isoformat()})
        narrow=self.request('GET','/api/v1/shipping-company/welfare-intelligence/summary?'+query,self.admin).json()
        self.assertEqual(narrow['respondents'],wide['respondents']);self.assertEqual(narrow['start'][8:10],'01')
        with patch.object(settings,'WELFARE_MIN_RESPONDENTS',6):
            self.assertTrue(self.get('summary')['suppressed'])
    def test_internal_singleton_evidence_requires_purpose_and_audits_reveal(self):
        self.populate(1,message='Sensitive original phone +12345678901')
        page=self.get('feedback',self.sa)
        self.assertEqual(page['total'],1);self.assertNotIn('+12345678901',str(page))
        record=page['items'][0]
        self.assertIn('experience_at',record)
        endpoint=f"/api/v1/superadmin/welfare-intelligence/investigation/feedback/{record['id']}"
        body={'purpose':'Review reported welfare access issue','evidence_reference':'CASE-ROUND3'}
        self.request('POST',endpoint,self.admin,body,403)
        self.request('POST',endpoint,self.sa,{'purpose':'x','evidence_reference':'x'},422)
        result=self.request('POST',endpoint,self.sa,body).json()
        self.assertIn('+12345678901',str(result));self.assertIn('contact_opt_in',result['evidence'])
        audit=self.db.query(CompanyAuditEvent).filter_by(action='welfare_sensitive_evidence_revealed').one()
        self.assertEqual(audit.actor_user_id,self.sa.id);self.assertEqual(audit.evidence_reference,'CASE-ROUND3');self.assertNotIn('+12345678901',str(audit.after_state))
    def test_all_stored_questions_have_contract_and_internal_no_response_state(self):
        data=self.get('summary',self.sa)
        self.assertEqual(data['status'],'no_responses')
        self.assertEqual(set(data['questions']),{'went_ashore','difficulty','barriers','facility_access','outcome','nonparticipation_reason','satisfaction','extra_charge'})
        for key in ('amount_paid','currency','message','contact_opt_in','experience_at','user_id'):
            self.assertIn(key,data['field_map'])
    def test_monthly_port_additive_metrics_and_distinct_period_reconciliation(self):
        for at,finish in [('2026-09-15T12:00:00+00:00','2026-09-15T13:00:00+00:00'),('2026-09-30T23:30:00+00:00','2026-10-01T00:30:00+00:00'),('2026-10-15T12:00:00+00:00','2026-10-15T13:00:00+00:00')]:
            self.booking(crew_id=self.crew_assignment.crew_profile_id,num_passengers=1,trip_started_at=datetime.fromisoformat(at),trip_completed_at=datetime.fromisoformat(finish))
        page=self.get('monthly-ports')
        port=page['items'][0];months=port['months']
        self.assertEqual(sum(r['participation_seats'] for r in months),port['operations']['crew_participation'])
        self.assertEqual(sum(r['completed_journeys'] for r in months),3)
        self.assertEqual(port['operations']['completed_journeys'],3)
        self.assertEqual(sum(r['distinct_crew'] for r in months),2)
        self.assertEqual(port['operations']['distinct_crew'],1)
        self.assertEqual(port['operations']['calls_with_shore_leave'],1)
        self.assertEqual(sum(r['calls_with_shore_leave'] for r in months),2)
    def test_reviews_are_company_scoped_screened_grouped_and_paginated(self):
        for index in range(5):
            user,assignment=(self.crew,self.crew_assignment) if index==0 else self.person(index)
            b=self.booking(crew_id=assignment.crew_profile_id,provider_id=self.provider.id)
            self.db.add(BookingReview(booking_id=b.id,crew_id=assignment.crew_profile_id,review_type='driver',rating=4,review_text='Good service',created_at=fixture.NOW))
        self.db.commit()
        self.separate_company()
        page=self.get('reviews',limit=1)
        self.assertEqual(page['total'],1);self.assertEqual(page['items'][0]['review'],'Good service.');self.assertNotIn('driver_id',page['items'][0]);self.assertEqual(self.get('reviews',self.admin2)['total'],0)
        self.assertEqual(self.get('reviews',offset=1)['items'],[])
        self.assertEqual(self.get('reviews',search='not present')['items'],[])
    def test_historical_tenure_uses_experience_not_submission_and_updates_deduplicate(self):
        self.populate()
        rows=self.db.query(WelfareFeedback).all()
        for row in rows:
            row.experience_at=datetime(2026,9,15,tzinfo=timezone.utc)
        claim=self.db.query(VesselRoleClaim).filter_by(company_id=self.company.id).first()
        claim.effective_end=datetime(2026,10,1,tzinfo=timezone.utc);self.db.commit()
        result=self.get('summary');self.assertEqual(result['respondents'],5)
        for row in rows:
            row.experience_at=datetime(2026,10,15,tzinfo=timezone.utc)
        self.db.commit()
        self.assertEqual(self.get('feedback')['total'],0)
    def test_overlapping_port_cohorts_do_not_disclose_singleton_by_subtraction(self):
        from app.db.models.port import Port
        from app.db.models.vessel_call import VesselCall
        from app.db.models.crew_assignment import CrewAssignment
        from app.db.models.crew_profile import CrewProfile
        self.populate()
        original=self.db.query(WelfareFeedback).order_by(WelfareFeedback.id).all()
        other,_=self.person(5)
        port=Port(name='Second fixture port',code='second-fixture')
        self.db.add(port);self.db.flush()
        from app.db.models.vessel import Vessel
        second_vessel=Vessel(name='Second fixture vessel',imo_number='SECOND-FIXTURE',vessel_type='Bulk Carrier',status='Active',agent_id=self.agent.id)
        self.db.add(second_vessel);self.db.flush()
        self.db.add(VesselRoleClaim(company_id=self.company.id,vessel_id=second_vessel.id,role='ISM_MANAGER',effective_start=fixture.JAN,status='APPROVED'))
        call=VesselCall(vessel_id=second_vessel.id,agency_id=self.agency.id,port_id=port.id,vessel_name=second_vessel.name,started_at=fixture.JAN)
        self.db.add(call);self.db.flush()
        # Five respondents at each port, four overlap. A naive union reveals 6-5=1.
        for who in [r.user_id for r in original[:4]]+[other.id]:
            profile=self.db.query(CrewProfile).filter_by(user_id=who).one()
            assignment=CrewAssignment(vessel_call_id=call.id,crew_profile_id=profile.id,crew_name='Synthetic crew',passport_number=profile.passport_number,nationality='IN',source='MANIFEST',started_at=fixture.JAN)
            self.db.add(assignment);self.db.flush()
            self.db.add(WelfareFeedback(user_id=who,assignment_id=assignment.id,vessel_call_id=call.id,vessel_id=second_vessel.id,port_id=port.id,experience_at=fixture.NOW,answers=original[0].answers,message='Good service'))
        self.db.commit()
        self.assertEqual(self.get('summary',self.sa)['respondents'],6)
        self.assertTrue(self.get('summary')['suppressed'])
        self.assertTrue(self.get('summary',port_id=self.port.id)['suppressed'])
        self.assertTrue(self.get('summary',port_id=port.id)['suppressed'])
        self.assertEqual(self.get('feedback',port_id=self.port.id)['total'],0)

    def test_published_cohort_coverage_does_not_reveal_excluded_source_population(self):
        self.populate()
        before=self.get('summary')
        self.assertEqual(before['coverage_status'],'partial_withheld')
        self.assertIn('published cohorts only',before['coverage_note'])
        self.assertIn('not totals for all source responses',before['coverage_note'])
        other,assignment=self.person(5)
        # A disjoint earlier-month singleton is withheld; the five-person cohort
        # remains useful. Coverage metadata must not confirm that singleton exists.
        body=self.body(assignment)
        body['experience_at']='2026-09-15T12:00:00+00:00'
        self.request('POST','/api/v1/crew/welfare-feedback',other,body,201)
        after=self.get('summary')
        self.assertEqual(after['respondents'],5)
        self.assertEqual(after['responses'],5)
        self.assertEqual(after['coverage_status'],before['coverage_status'])
        self.assertEqual(after['coverage_note'],before['coverage_note'])
        self.assertEqual(set(after),set(before))
        self.assertNotIn('withheld_count',after)
        internal=self.get('summary',self.sa)
        self.assertEqual(internal['respondents'],6)
        self.assertEqual(internal['coverage_status'],'all_recorded')
        self.assertIn('All recorded',internal['coverage_note'])
