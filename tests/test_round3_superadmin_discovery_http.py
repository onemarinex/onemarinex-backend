"""Authorized bounded vessel discovery; only guarded disposable Postgres fixture."""
import json
import unittest
from datetime import datetime, timedelta, timezone
import test_company_operations_http as fixture
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.services.superadmin_vessel_search import display_status_expression
from app.services.vessel_lifecycle import effective_vessel_status

COLLECTION='routes_superadmin.list_all_vessels_superadmin.0'

class SuperadminDiscoveryHTTP(unittest.TestCase):
    setUp=fixture.OperationsHTTP.setUp
    tearDown=fixture.OperationsHTTP.tearDown
    request=fixture.OperationsHTTP.request
    headers=fixture.OperationsHTTP.headers

    def path(self,**params):
        from urllib.parse import urlencode
        return '/api/v1/superadmin/vessels?'+urlencode(params)

    def seed_page_two(self):
        self.vessel.name='WAN HAI 366'
        self.vessel.imo_number='9958080'
        self.vessel.vessel_type='Container'
        self.vessel.status='Active'
        self.vessel.etd=datetime.now(timezone.utc)-timedelta(days=1)
        for index in range(105):
            self.db.add(Vessel(name=f'Paging vessel {index}',imo_number=f'PAGING-{index}',vessel_type='Tanker',status='Active',etd=datetime.now(timezone.utc)+timedelta(days=2)))
        self.db.commit()

    def test_name_imo_history_search_finds_record_beyond_first_bounded_page(self):
        self.seed_page_two()
        initial=self.request('GET',self.path(),self.sa)
        self.assertEqual(len(initial.json()),100)
        self.assertNotIn(self.vessel.id,{row['id'] for row in initial.json()})
        metadata=json.loads(initial.headers['X-Collection-Pages'])[COLLECTION]
        self.assertTrue(metadata['has_more']);self.assertEqual(metadata['next_offset'],100)
        for search in ('9958080','wan hai'):
            response=self.request('GET',self.path(search=search,lifecycle='History',vessel_type='Container'),self.sa)
            self.assertEqual([row['id'] for row in response.json()],[self.vessel.id])
            self.assertEqual(response.json()[0]['status'],'Departed')
            self.assertEqual(response.headers['X-Result-Count'],'1')
        self.assertEqual(self.request('GET',self.path(search='9958080',lifecycle='Active'),self.sa).json(),[])
        self.assertEqual(self.request('GET',self.path(search='9958080',lifecycle='History',vessel_type='Tanker'),self.sa).json(),[])
        self.db.refresh(self.vessel);self.db.refresh(self.call)
        self.assertEqual(self.vessel.status,'Active')
        self.assertIsNone(self.call.ended_at)

    def test_filters_precede_bounded_pagination_and_wildcards_are_literal(self):
        self.seed_page_two()
        first=self.request('GET',self.path(search='Paging vessel',lifecycle='Active',vessel_type='Tanker'),self.sa)
        self.assertEqual(len(first.json()),100);self.assertEqual(first.headers['X-Result-Count'],'105')
        second=self.request('GET',self.path(search='Paging vessel',lifecycle='Active',vessel_type='Tanker',collection=COLLECTION,collection_offset=100,collection_limit=100),self.sa)
        self.assertEqual(len(second.json()),5)
        metadata=json.loads(second.headers['X-Collection-Pages'])[COLLECTION]
        self.assertEqual(metadata['offset'],100);self.assertFalse(metadata['has_more'])
        self.vessel.name='Literal %_ vessel';self.db.commit()
        literal=self.request('GET',self.path(search='%_'),self.sa)
        self.assertEqual([row['id'] for row in literal.json()],[self.vessel.id])
        self.request('GET',self.path(search='x'*121),self.sa,status=422)
        self.request('GET',self.path(lifecycle='invalid'),self.sa,status=422)
        self.request('GET',self.path(),self.agent,status=403)
        self.request('GET',self.path(),status=401)

    def test_sql_display_status_matches_clock_manual_states_and_latest_closed_call(self):
        now=datetime(2026,10,30,4,tzinfo=timezone.utc)
        cases=[('Archived',now+timedelta(days=1)),('Active',now-timedelta(microseconds=1)),('Active',now),('Active',now+timedelta(hours=5)),('Active',now+timedelta(hours=5,microseconds=1)),('Departed',now+timedelta(days=1)),('Departed',None),('Departing',None),('Archived',None),('Active',None)]
        for index,(stored,etd) in enumerate(cases):
            row=Vessel(name=f'Parity {index}',imo_number=f'PARITY-{index}',vessel_type='Container',status=stored,etd=etd)
            self.db.add(row);self.db.flush()
            value=self.db.query(display_status_expression(now)).filter(Vessel.id==row.id).scalar()
            self.assertEqual(value,effective_vessel_status(row,now=now))
        for index,call_status in enumerate(('DEPARTED','ARCHIVED','REASSIGNED')):
            row=Vessel(name=f'Closed {index}',imo_number=f'CLOSED-{index}',vessel_type='Container',status='Active',etd=now+timedelta(days=1))
            self.db.add(row);self.db.flush()
            self.db.add(VesselCall(vessel_id=row.id,agency_id=self.agency.id,port_id=self.port.id,vessel_name=row.name,started_at=now-timedelta(days=1),ended_at=now-timedelta(hours=1),status=call_status))
            self.db.flush()
            value=self.db.query(display_status_expression(now)).filter(Vessel.id==row.id).scalar()
            self.assertEqual(value,'Archived' if call_status=='ARCHIVED' else 'Departed')
