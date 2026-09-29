"""离线命令行验收入口。"""
from __future__ import annotations
import argparse,json
from datetime import datetime,timezone
from .models import MonitoringRecord,ZoneRecord
from .service import BiosafetyService
from .clock_compat import make_clock

def _grant_fire(s,token,user,valid_from="2026-09-20T00:00:00Z"):
    s.record_qualification_event(token,{"user_id":user,"event_type":"training_passed","competency_code":"forest-firefighting","scope":"forest-fire-basic","valid_from":valid_from,"idempotency_key":f"{user}-train-fire"})
    s.record_qualification_event(token,{"user_id":user,"event_type":"medical_passed","competency_code":"forest-firefighting","scope":"fire-ground","valid_from":valid_from,"idempotency_key":f"{user}-med-fire"})
    for scope in ("fire-suit","breathing-apparatus"):
        s.record_qualification_event(token,{"user_id":user,"event_type":"equipment_authorized","competency_code":"forest-firefighting","scope":scope,"valid_from":valid_from,"idempotency_key":f"{user}-equip-{scope}"})

def run():
    s=BiosafetyService(clock=make_clock(datetime(2026,9,24,8,0,tzinfo=timezone.utc))); s.bootstrap(); t=s.auth.login("admin","biosafety-admin")
    # 派单处置人与调拨人需要森林消防资格（培训+火场体检+两项装备授权）
    _grant_fire(s,t,"crew-north"); _grant_fire(s,t,"admin")
    s.register_zone_record(t,ZoneRecord("CASE-DEMO","north","water",680,5)); r=s.ingest_monitoring_record(t,MonitoringRecord("RD-DEMO","CASE-DEMO","sensor_source-01",160,230,88,"2026-09-24T10:00:00+00:00")); report=s.risk_report(t,"CASE-DEMO"); order=s.create_treatment_ticket(t,"CASE-DEMO",r["alert_id"],"crew-north",1); s.add_preservation_resource(t,"PUMP-01","mobile-cold-box","north",2); allocation=s.allocate(t,"PUMP-01",order["treatment_ticket_id"],1); chain=s.qualification_chain(t)
    return {"status":"ok","zone_record":"CASE-DEMO","severity":r["risk"]["severity"],"probability":report["violation_probability"],"allocation":allocation["plan_id"],"qualification_chain":chain}
def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--workspace",default="."); args=parser.parse_args(); print(json.dumps(run(),ensure_ascii=False))
if __name__=="__main__":main()
