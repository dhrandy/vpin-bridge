import os,sys,threading,unittest,http.client,json,importlib.util,secrets
os.environ['API_KEY']=secrets.token_urlsafe(20)
spec=importlib.util.spec_from_file_location('vb',str(__import__('pathlib').Path(__file__).resolve().parents[1]/'bridge.py'));b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
value={};calls=[]
def cabinet(path, max_bytes=None):calls.append(path);return value
real_cabinet_get=b.cabinet_get
b.cabinet_get=cabinet
server=b.ThreadingHTTPServer(('127.0.0.1',0),b.Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
def get(path='/api/analytics',auth=True):
 c=http.client.HTTPConnection('127.0.0.1',server.server_port);c.request('GET',path,headers={'X-Api-Key':b.API_KEY} if auth else {});r=c.getresponse();data=json.loads(r.read());c.close();return r.status,data
class Tests(unittest.TestCase):
 def test_01_auth(self):
  calls.clear();self.assertEqual(get(auth=False)[0],401);self.assertFalse(calls)
 def test_02_allowlist(self):
  global value
  value={'entries':[{'gameId':123,'timePlayedSecs':125,'path':'C:/private','name':'Private name'}],'secret':'test'}
  self.assertEqual(get(),(200,{'ok':True,'entries':[{'gameId':123,'timePlayedSecs':125}]}));self.assertEqual(calls[-1],'/api/v1/alx')
 def test_03_invalid_totals(self):
  global value
  value={'entries':[{},None,{'gameId':1,'timePlayedSecs':-1},{'gameId':True,'timePlayedSecs':1},{'gameId':1,'timePlayedSecs':True},{'gameId':1,'timePlayedSecs':'10'},{'gameId':1,'timePlayedSecs':0}]}
  self.assertEqual(get()[1]['entries'],[{'gameId':1,'timePlayedSecs':0}])
 def test_04_bounds(self):
  global value
  value={'entries':[{}]*10001};self.assertEqual(get()[0],502)
 def test_05_bad_shape(self):
  global value
  for value in [[],None,{}, {'entries':None}]:self.assertEqual(get()[0],502)
 def test_06_upstream_byte_limit(self):
  oldopen=b.upstream_open;oldbase=b.cabinet_base
  class Response:
   def __enter__(self):return self
   def __exit__(self,*args):pass
   def read(self,n=-1):return b'x'*n
  try:
   b.cabinet_base=lambda:'http://127.0.0.1'
   b.upstream_open=lambda request,timeout:Response()
   with self.assertRaises(ValueError):real_cabinet_get('/api/v1/alx',max_bytes=10)
  finally:b.upstream_open=oldopen;b.cabinet_base=oldbase
def tearDownModule():
 server.shutdown();server.server_close()

if __name__=='__main__':unittest.main(verbosity=2)
