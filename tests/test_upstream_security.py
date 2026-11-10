"""Synthetic upstream errors, redirect traps and bounded-response checks."""
import http.client
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import importlib.util
import io
import json
from pathlib import Path
import threading
import unittest
import urllib.error
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('audit_bridge',str(Path(__file__).resolve().parents[1]/'bridge.py'))
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
class UpstreamTests(unittest.TestCase):
    def test_redirect_never_follows_or_forwards_key(self):
        sink=[]
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                if self.path=='/sink':sink.append(self.headers.get('X-Api-Key'));self.send_response(200)
                else:self.send_response(302);self.send_header('Location','/sink')
                self.send_header('Content-Length','0');self.end_headers()
        server=ThreadingHTTPServer(('127.0.0.1',0),Redirect)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            req=b.urllib.request.Request(f'http://127.0.0.1:{server.server_port}/redirect',headers={'X-Api-Key':'synthetic-private-key'})
            with self.assertRaises(urllib.error.HTTPError):b.upstream_open(req,timeout=2)
            self.assertEqual(sink,[])
        finally:server.shutdown();server.server_close()
    def test_post_put_bodies_bounded(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n=-1):
                self.asserted=n
                if n<0:raise AssertionError('Unbounded read')
                return b'x'*n
        for method in [b.cabinet_post_json,b.cabinet_put_json]:
            response=Response()
            with patch.object(b,'upstream_open',return_value=response),patch.object(b,'cabinet_base',return_value='http://example.invalid'):
                with self.assertRaisesRegex(ValueError,'byte limit'):method('/fixture',{})
                self.assertEqual(response.asserted,b.MAX_JSON_RESPONSE+1)
    def test_response_helper_is_bounded(self):
        class Response:
            def read(self,n):return b'x'*n
        with self.assertRaises(ValueError):b.bounded_response(Response(),32)
    def test_error_paths_do_not_return_private_upstream_details(self):
        class Handler(b.Handler):
            def __init__(self,path,method='GET'):
                self.path=path;self.headers={'X-Api-Key':b.API_KEY,'Content-Length':'2','Content-Type':'application/json'};self.rfile=io.BytesIO(b'{}')
            def _send(self,code,data):return code,data
        old_key=b.API_KEY;b.API_KEY='fixture'
        try:
            for path in ['/health','/api/highscores','/api/details/1']:
                with patch.object(b,'cabinet_get',side_effect=RuntimeError('http://private.invalid/ C:\\cabinet\\ secret=fixture')):
                    _,data=Handler(path).do_GET();self.assertNotIn('private.invalid',json.dumps(data));self.assertNotIn('secret=',json.dumps(data))
            for path in ['/api/details/1','/api/playlists/1/1']:
                with patch.object(b,'cabinet_post_json',side_effect=RuntimeError('secret=fixture')),patch.object(b,'cabinet_put_json',side_effect=RuntimeError('secret=fixture')):
                    _,data=Handler(path).do_POST();self.assertNotIn('secret=',json.dumps(data))
        finally:b.API_KEY=old_key
if __name__=='__main__':unittest.main()
