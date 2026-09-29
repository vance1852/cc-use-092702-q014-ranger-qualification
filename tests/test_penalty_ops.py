import unittest
from datetime import datetime, timezone
from biosafety_ops.models import MonitoringRecord,ZoneRecord
from biosafety_ops.risk import score_monitoring_record
from biosafety_ops.service import BiosafetyService
from qualification_ledger import FrozenClock
def grant_fire(s,token,user,valid_from="2026-09-01T00:00:00Z"):
    s.record_qualification_event(token,{"user_id":user,"event_type":"training_passed","competency_code":"forest-firefighting","scope":"forest-fire-basic","valid_from":valid_from,"idempotency_key":f"{user}-train"})
    s.record_qualification_event(token,{"user_id":user,"event_type":"medical_passed","competency_code":"forest-firefighting","scope":"fire-ground","valid_from":valid_from,"idempotency_key":f"{user}-med"})
    for scope in ("fire-suit","breathing-apparatus"):
        s.record_qualification_event(token,{"user_id":user,"event_type":"equipment_authorized","competency_code":"forest-firefighting","scope":scope,"valid_from":valid_from,"idempotency_key":f"{user}-equip-{scope}"})
class BiosafetyOperationsTests(unittest.TestCase):
    def setUp(self):
        self.s=BiosafetyService(clock=FrozenClock(datetime(2026,9,24,8,0,tzinfo=timezone.utc))); self.s.bootstrap(); self.t=self.s.auth.login("admin","biosafety-admin"); self.s.register_zone_record(self.t,ZoneRecord("S1","east","quarantine",100,4))
        # 处置人 crew 与调拨人 admin 均持有有效森林消防资格。
        grant_fire(self.s,self.t,"crew"); grant_fire(self.s,self.t,"admin")
    def test_risk_and_idempotent_monitoring_record(self):
        r=MonitoringRecord("R1","S1","sensor_source",120,250,90,"2026-01-01T00:00:00+00:00"); a=self.s.ingest_monitoring_record(self.t,r); b=self.s.ingest_monitoring_record(self.t,r); self.assertFalse(a["duplicate"]); self.assertTrue(b["duplicate"]); self.assertEqual(self.s.risk_report(self.t,"S1")["monitoring_records"],1)
    def test_treatment_ticket_and_allocation(self):
        r=self.s.ingest_monitoring_record(self.t,MonitoringRecord("R2","S1","sensor_source",100,250,90,"2026-01-01T00:00:00+00:00")); o=self.s.create_treatment_ticket(self.t,"S1",r["alert_id"],"crew"); self.s.transition_treatment_ticket(self.t,o["treatment_ticket_id"],"assigned","crew accepted"); self.s.add_preservation_resource(self.t,"R1","cold-box","east",2); self.assertFalse(self.s.allocate(self.t,"R1",o["treatment_ticket_id"],1)["duplicate"]); self.assertEqual(self.s.preservation_resource(self.t,"R1")["available"],1)
    def test_risk_validation(self):
        with self.assertRaises(ValueError):score_monitoring_record(-1,1,1,2)
