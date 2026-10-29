"""Full-collection SQL paging and 500-vessel report acceptance, local fixture only."""
import unittest,io,time
from datetime import timedelta
try:
 from pypdf import PdfReader
except ImportError:
 PdfReader=None
from app.db.models.shipping_company import VesselRoleClaim,ShippingCompany,CompanyUserMembership
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.models.port import Port
from app.db.models.company_disclosure import CompanyDisclosureRequest
from app.api.v1 import routes_company_administration as administration
import test_company_portal_v13_http as portal
import test_company_operations_http as fixture
from test_company_fleet_acceptance_http import number

class Round2ScaleHTTP(unittest.TestCase):
 setUp=portal.PortalAcceptanceHTTP.setUp
 tearDown=portal.PortalAcceptanceHTTP.tearDown
 request=portal.PortalAcceptanceHTTP.request
 headers=portal.PortalAcceptanceHTTP.headers

 def test_thousand_request_history_pages_and_search_never_release_pending_records(self):
  rows=[CompanyDisclosureRequest(company_id=self.company.id,source_kind='CALL',source_id=self.call.id,vessel_call_id=self.call.id,requested_by_user_id=self.admin.id,fields=['rides.status'],reason='Synthetic historical request',event_start=fixture.JAN,event_end=fixture.NOW,status='REJECTED') for _ in range(1000)]
  self.db.add_all(rows);self.db.commit()
  result=self.request('GET','/api/v1/shipping-company/disclosure-requests?offset=900&limit=100&source_kind=CALL',self.admin).json()
  self.assertEqual(result['total'],1000);self.assertEqual(len(result['items']),100)
  ident=rows[-1].id
  result=self.request('GET',f'/api/v1/shipping-company/disclosure-requests?search={ident}',self.admin).json()
  self.assertTrue(any(row['id']==ident for row in result['items']))
  self.request('GET',f'/api/v1/shipping-company/disclosure-requests/{ident}/data',self.admin,status=403)
  self.request('GET',f'/api/v1/shipping-company/disclosure-requests/{ident}/data?format=pdf',self.admin,status=403)
  other=ShippingCompany(legal_name='Other scale company',normalized_name='OTHER SCALE COMPANY',status='VERIFIED');self.db.add(other);self.db.flush()
  self.db.query(CompanyUserMembership).filter_by(user_id=self.admin2.id).update({'company_id':other.id});self.db.commit()
  self.request('GET',f'/api/v1/shipping-company/disclosure-requests/{ident}',self.admin2,status=404)
  self.assertEqual(self.request('GET','/api/v1/shipping-company/disclosure-requests',self.admin2).json()['total'],0)

 def test_500_vessels_100_ports_scope_and_true_pdf_use_complete_counts(self):
  ports=[Port(name=f'Scale Port {i:03d}',code=f'scale-{i}',is_active=True) for i in range(100)];self.db.add_all(ports);self.db.flush()
  vessels=[Vessel(name=f'Scale MV {i:03d}',imo_number=number(3000+i),vessel_type='Bulk Carrier',agent_id=self.agent.id,status='Active') for i in range(500)];self.db.add_all(vessels);self.db.flush()
  for i,v in enumerate(vessels):
   self.db.add(VesselRoleClaim(company_id=self.company.id,vessel_id=v.id,imo_number=v.imo_number,declared_vessel_name=v.name,role='REGISTERED_OWNER',effective_start=fixture.JAN,status='AUTO_ACCEPTED'))
   self.db.add(VesselCall(vessel_id=v.id,agency_id=self.agency.id,port_id=ports[i%100].id,vessel_name=v.name,imo_number=v.imo_number,port_name=ports[i%100].name,started_at=fixture.NOW-timedelta(days=1),status='ACTIVE'))
  self.db.commit()
  path='/api/v1/shipping-company/intelligence/ports/page'
  first=self.request('GET',path+'?search=Scale&limit=25',self.admin).json();last=self.request('GET',path+'?search=Scale&offset=75&limit=25',self.admin).json()
  self.assertEqual(first['total'],100);self.assertEqual(len(last['items']),25)
  self.assertFalse({r['id'] for r in first['items']} & {r['id'] for r in last['items']})
  body={'report_type':'EXECUTIVE','start':fixture.JAN.isoformat(),'end':(fixture.NOW+timedelta(days=1)).isoformat(),'configuration':{'sections':['executive_summary'],'detail':'summary','charts':True,'tables':True}}
  before=time.monotonic();report=self.request('POST','/api/v1/shipping-company/report-centre',self.admin,body,201).json()
  self.assertEqual(report['payload']['summary']['recorded_vessels'],501)
  self.assertEqual(report['payload']['summary']['recorded_calls'],501)
  preview=self.request('GET',f"/api/v1/shipping-company/report-centre/{report['id']}/preview.pdf",self.admin)
  export=self.request('GET',f"/api/v1/shipping-company/report-centre/{report['id']}/export?format=pdf",self.admin)
  self.assertEqual(preview.content,export.content)
  if PdfReader:
   self.assertEqual(len(PdfReader(io.BytesIO(export.content)).pages),1)
  else:
   self.assertRegex(export.content,rb'/Count\s+1\b')
  self.assertLess(time.monotonic()-before,60,'500-vessel fixture exceeded the acceptance budget')
