"""Playlist save and table-removal routes; every upstream action is mocked."""
import http.client
import importlib.util
import json
from pathlib import Path
import secrets
import threading
import unittest

spec=importlib.util.spec_from_file_location('playlists_bridge',str(Path(__file__).resolve().parents[1]/'bridge.py'))
b=importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
b.API_KEY=secrets.token_urlsafe(32)
calls=[]
def cabinet(path,**kwargs):
    calls.append(('GET',path))
    return [{'id':5,'name':'Favorites'}]
def post(path,payload):
    calls.append(('POST',path,payload))
    return {'id':7,'name':payload.get('name')}
b.cabinet_get=cabinet
b.cabinet_post_json=post
b.cabinet_put_json=post
b.cabinet_delete_json=lambda path:calls.append(('DELETE',path)) or {'id':5}
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

class PlaylistRouteTests(unittest.TestCase):
    def setUp(self):
        calls.clear()
        b.DRY_RUN=True
    def test_auth_required_on_new_routes(self):
        self.assertEqual(request('/api/playlists/save','POST',{'name':'x'},auth=False)[0],401)
        self.assertEqual(request('/api/playlists/5/11','DELETE',auth=False)[0],401)
        self.assertEqual(calls,[])
    def test_create_forwards_only_id_and_name(self):
        code,_,data=request('/api/playlists/save','POST',{'name':'Game Shows'})
        self.assertEqual(code,200)
        self.assertEqual(calls,[('POST','/api/v1/playlists/save',{'id':-1,'name':'Game Shows'})])
        self.assertEqual(json.loads(data)['result'],{'id':7,'name':'Game Shows'})
    def test_extra_fields_are_rejected(self):
        for body in [{'name':'x','sqlPlayList':True},{'name':'x','playListSQL':'select 1'},{'name':'x','menuColor':1},{'name':'x','games':[]},{'name':'x','visible':False,'dofCommand':'x'}]:
            self.assertEqual(request('/api/playlists/save','POST',body)[0],400,body)
        self.assertEqual(calls,[])
    def test_name_and_id_validation(self):
        for body in [{},{'name':''},{'name':'   '},{'name':'x'*121},{'name':'a\tb'},{'name':'ok','id':0},{'name':'ok','id':-2},{'name':'ok','id':True},{'name':'ok','id':'5'},{'name':5}]:
            self.assertEqual(request('/api/playlists/save','POST',body)[0],400,body)
        self.assertEqual(calls,[])
    def test_rename_checks_playlist_exists(self):
        code,_,_=request('/api/playlists/save','POST',{'id':5,'name':'New name'})
        self.assertEqual(code,200)
        self.assertEqual(calls, [('GET','/api/v1/playlists'),('POST','/api/v1/playlists/save',{'id':5,'name':'New name'})])
        calls.clear()
        self.assertEqual(request('/api/playlists/save','POST',{'id':99,'name':'New name'})[0],404)
        self.assertEqual(calls,[('GET','/api/v1/playlists')])
    def test_save_body_and_framing_limits(self):
        self.assertEqual(request('/api/playlists/save','POST',{'name':'x'},headers={'Content-Type':'text/plain'})[0],415)
        self.assertEqual(request('/api/playlists/save','POST','{broken')[0],400)
        self.assertEqual(request('/api/playlists/save','POST','[]')[0],400)
        self.assertEqual(calls,[])
    def test_remove_table_from_playlist(self):
        code,_,data=request('/api/playlists/5/11','DELETE')
        self.assertEqual(code,200)
        self.assertEqual(calls,[('DELETE','/api/v1/playlists/5/11')])
    def test_delete_id_validation_and_no_other_deletes(self):
        for path in ['/api/playlists/5','/api/playlists/abc/1','/api/playlists/5/-1','/api/games/5','/api/details/5']:
            self.assertEqual(request(path,'DELETE')[0],404,path)
        self.assertEqual(calls,[])

if __name__=='__main__':
    unittest.main()
