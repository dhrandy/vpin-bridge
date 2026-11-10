"""Local hostile-request regression checks; every upstream action is mocked."""
import http.client
import importlib.util
import json
from pathlib import Path
import secrets
import threading
import unittest

spec=importlib.util.spec_from_file_location('security_bridge',str(Path(__file__).resolve().parents[1]/'bridge.py'))
b=importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
b.API_KEY=secrets.token_urlsafe(32)
calls=[]
def cabinet(path,**kwargs):
    calls.append(('GET',path))
    return {'gameId':0}
def post(path,payload):
    calls.append(('POST',path))
    return True
b.cabinet_get=cabinet
b.cabinet_post_json=post
b.cabinet_put_json=post
server=b.LimitedServer(('127.0.0.1',0),b.Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
def request(path,method='GET',body=None,auth=True,headers=None):
    h={'X-Api-Key':b.API_KEY} if auth else {}
    if body is not None:
        if isinstance(body,dict):body=json.dumps(body)
        h['Content-Type']='application/json'
    h.update(headers or {})
    c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
    c.request(method,path,body=body,headers=h)
    r=c.getresponse();data=r.read();result=(r.status,dict(r.getheaders()),data);c.close();return result

class SecurityTests(unittest.TestCase):
    def setUp(self):
        calls.clear()
        b.DRY_RUN=True
    def test_auth_on_all_routes(self):
        for path in ['/health','/api/status','/api/games','/api/games/json','/api/highscores','/api/analytics','/api/media/1','/api/thumb/1','/api/details/1','/api/scorehistory/1','/api/playlists','/api/validations/1','/api/reload/1']:
            self.assertEqual(request(path,auth=False)[0],401)
        for path in ['/api/play','/api/exit','/api/details/1','/api/playlists/1/1','/api/upload','/api/media','/api/rom']:
            self.assertEqual(request(path,'POST',{},auth=False)[0],401)
        self.assertEqual(calls,[])
    def test_wrong_key_and_spoofed_identity(self):
        self.assertEqual(request('/health',headers={'X-Api-Key':'wrong','X-Forwarded-User':'admin'})[0],401)
        self.assertEqual(calls,[])
    def test_proxy_parameter_injection(self):
        for kind in ['validations','reload','media','thumb','details','scorehistory']:
            for value in ['-1','0','2147483648','1%2f..','1%3fx=2','abc']:
                self.assertEqual(request('/api/'+kind+'/'+value)[0],400)
        self.assertEqual(request('/api/highscores?gameId=1%26foo=bar')[0],400)
        self.assertEqual(request('/api/rom?gameId=1%2f..','POST',{})[0],400)
        self.assertEqual(request('/api/media?screen=../games&append=true','POST',{})[0],400)
        self.assertEqual(request('/api/media?append=malformed','POST',{})[0],400)
        self.assertEqual(request('/api/playlists/1/1%2f..','POST',{})[0],400)
        self.assertEqual(calls,[])
    def test_body_and_framing_limits(self):
        self.assertEqual(request('/api/exit','POST',{},headers={'Transfer-Encoding':'chunked'})[0],400)
        self.assertEqual(request('/api/exit','POST',{},headers={'Content-Length':'200001'})[0],413)
        self.assertEqual(request('/api/upload','POST',{},headers={'Content-Length':str(b.MAX_UPLOAD_BYTES+1)})[0],413)
        self.assertEqual(request('/api/exit','POST',{},headers={'Content-Type':'text/plain'})[0],415)
        self.assertEqual(request('/api/details/1','POST','[]')[0],400)
        self.assertEqual(request('/api/details/1','POST','{broken')[0],400)
        self.assertEqual(calls,[])
    def test_dry_run_and_unknown_routes_do_not_touch_cabinet(self):
        self.assertEqual(request('/api/upload','POST',{})[0],403)
        self.assertEqual(request('/api/not-an-endpoint','POST',{})[0],404)
        self.assertEqual(calls,[])
    def test_header_hygiene(self):
        status,headers,_=request('/health',auth=False)
        self.assertEqual(status,401)
        self.assertEqual(headers['Cache-Control'],'private, no-store')
        self.assertEqual(headers['X-Content-Type-Options'],'nosniff')
        self.assertNotIn('Access-Control-Allow-Origin',headers)
        self.assertNotIn('Python',headers['Server'])
    def test_unknown_import_state_fails_closed(self):
        old=b.cabinet_get;b.DRY_RUN=False;b.cabinet_get=lambda *_:{'unexpected':True}
        try:self.assertEqual(request('/api/upload','POST',{})[0],503)
        finally:b.cabinet_get=old
    def test_control_and_upload_locks(self):
        b._CONTROL_LOCK.acquire();b._UPLOAD_LOCK.acquire()
        try:
            self.assertEqual(request('/api/exit','POST',{'gameId':1,'started':'2026-01-01T00:00:00Z'})[0],409)
            self.assertEqual(request('/api/upload','POST',{})[0],409)
            self.assertEqual(calls,[])
        finally:b._CONTROL_LOCK.release();b._UPLOAD_LOCK.release()

def tearDownModule():
    server.shutdown();server.server_close()
if __name__=='__main__':unittest.main()
