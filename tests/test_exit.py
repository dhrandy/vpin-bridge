import importlib.util,threading,json,io,sys,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('exitbridge',str(Path(__file__).resolve().parents[1] / 'bridge.py'));b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
class Handler(b.Handler):
 def __init__(self,payload):
  data=json.dumps(payload).encode();self.headers={'Content-Length':str(len(data))};self.rfile=io.BytesIO(data)
 def _send(self,code,data):return code,data
class ExitTests(unittest.TestCase):
 def setUp(self):
  self.calls=[];self.status={'gameId':12,'started':'2026-11-08T12:00:00Z'};self.fail=False
  def get(path,*a,**kw):
   self.calls.append(path)
   if path.endswith('gamestatus'):return self.status
   if self.fail:raise TimeoutError()
   return True
  b.cabinet_get=get
 def run_exit(self,p=None):return Handler(p if p is not None else {'gameId':12,'started':'2026-11-08T12:00:00+00:00'})._cabinet_control('/api/exit')
 def test_success(self):
  code,data=self.run_exit();self.assertEqual(code,200);self.assertFalse(data['idleConfirmed']);self.assertEqual(self.calls.count('/api/v1/frontend/terminate'),1)
 def test_changed(self):
  self.status['gameId']=13;self.assertEqual(self.run_exit()[0],409);self.assertEqual(len(self.calls),1)
 def test_same_game_new_session(self):
  self.status['started']='2026-11-08T13:00:00Z';self.assertEqual(self.run_exit()[0],409);self.assertEqual(len(self.calls),1)
 def test_missing_timestamp(self):
  self.status.pop('started');self.assertEqual(self.run_exit()[0],409);self.assertEqual(len(self.calls),1)
 def test_bad_body(self):
  for p in [{'gameId':True,'started':'x'},{'gameId':12,'started':None},{'gameId':12,'started':'x','extra':1},[]]:
   self.assertEqual(self.run_exit(p)[0],400)
  self.assertEqual(self.calls,[])
 def test_timeout_no_retry(self):
  self.fail=True;self.assertEqual(self.run_exit()[0],502);self.assertEqual(self.calls.count('/api/v1/frontend/terminate'),1)
 def test_serialized(self):
  b._CONTROL_LOCK.acquire()
  try:
   self.assertEqual(self.run_exit()[0],409);self.assertEqual(Handler({'gameId':12})._cabinet_control('/api/play')[0],409);self.assertEqual(self.calls,[])
  finally:b._CONTROL_LOCK.release()
 def test_unknown_no_launch(self):
  self.status={};self.assertEqual(Handler({'gameId':12})._cabinet_control('/api/play')[0],502);self.assertEqual(len(self.calls),1)
 def test_busy_no_launch(self):
  self.assertEqual(Handler({'gameId':12})._cabinet_control('/api/play')[0],409);self.assertEqual(len(self.calls),1)
if __name__=='__main__':unittest.main()
