import os
import sys
import tempfile
import time
from pathlib import Path
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
os.environ["MISSION_DATA"] = tempfile.mkdtemp()
sys.path.insert(0,str(ROOT/"backend"))
import app as service

def task(script, **extra):
    return dict(name=script,kind="python",target=str(ROOT/"examples"/script),
                cwd=str(ROOT),args=[],env={},timeout=5,retries=0,
                enabled=True,schedule_kind="none",schedule_value=None,**extra)

def wait(client, run_id):
    for _ in range(80):
        d=client.get("/api/runs/"+run_id).json()
        if d["state"] not in ("queued","running"):
            return d
        time.sleep(.1)
    raise AssertionError("Execution did not finish")

def test_success_failure_timeout_overlap_cancel_and_recovery():
    with TestClient(service.app) as c:
        good=c.post("/api/tasks",json=task("success.py"))
        assert good.status_code==201
        rid=c.post("/api/tasks/"+good.json()["id"]+"/run").json()["run_id"]
        assert wait(c,rid)["state"]=="success"
        assert "繁體中文" in c.get("/api/runs/"+rid+"/log").text
        bad=c.post("/api/tasks",json=task("failure.py")).json()
        rid=c.post("/api/tasks/"+bad["id"]+"/run").json()["run_id"]
        assert wait(c,rid)["exit_code"]==7
        slow_data=task("slow.py");slow_data["timeout"]=1
        slow=c.post("/api/tasks",json=slow_data).json()
        rid=c.post("/api/tasks/"+slow["id"]+"/run").json()["run_id"]
        assert c.post("/api/tasks/"+slow["id"]+"/run").status_code==409
        assert wait(c,rid)["state"]=="timeout"
        slow_data["timeout"]=30
        slow=c.post("/api/tasks",json=slow_data).json()
        rid=c.post("/api/tasks/"+slow["id"]+"/run").json()["run_id"]
        for _ in range(30):
            if c.get("/api/runs/"+rid).json()["state"]=="running":break
            time.sleep(.05)
        assert c.post("/api/runs/"+rid+"/cancel").status_code==200
        assert wait(c,rid)["state"]=="cancelled"
    with service.conn() as db:
        db.execute("INSERT INTO runs(id,task_id,task_name,state,started_at,log_path) VALUES(?,?,?,?,?,?)",
                   ("orphan","x","orphan","running",service.iso(service.now()),"none"))
    with TestClient(service.app) as c:
        assert c.get("/api/runs/orphan").json()["state"]=="interrupted"

def test_schedule():
    from datetime import datetime,timezone
    t=datetime(2026,10,28,tzinfo=timezone.utc)
    assert service.calculate_next("interval","60",t)>t
    assert service.calculate_next("cron","*/5 * * * *",t)>t
    assert service.calculate_next("once","2020-01-01T01:00:00+08:00",t) is None
