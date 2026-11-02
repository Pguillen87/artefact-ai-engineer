import base64, http.client, tempfile, threading, unittest
from pathlib import Path
from test_data import write_dataset
from artefact.data import load_dataset
from artefact.admin import CatalogAdmin
from artefact.admin_web import make_server

class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); root=Path(self.tmp.name); write_dataset(root)
        self.admin=CatalogAdmin(root/'a.db',root/'s.db',load_dataset(root))
        self.server=make_server('127.0.0.1',0,self.admin,'synthetic-test-password')
        self.port=self.server.server_port
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def request(self,method,path,headers=None,body=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        conn.request(method,path,body,headers or {});response=conn.getresponse();data=response.read();status=response.status;conn.close();return status,data
    def auth(self):
        return {'Authorization':'Basic '+base64.b64encode(b'admin:synthetic-test-password').decode()}
    def test_auth_and_host(self):
        self.assertEqual(self.request('GET','/')[0],401)
        self.assertEqual(self.request('GET','/',self.auth())[0],200)
        self.assertEqual(self.request('GET','/',dict(self.auth(),Host='evil.example'))[0],400)
    def test_origin_and_csrf_required(self):
        headers=dict(self.auth(),Origin='http://evil.example',**{'Content-Type':'application/x-www-form-urlencoded'})
        self.assertEqual(self.request('POST','/rollback',headers,'version=0')[0],403)
        headers['Origin']=f'http://127.0.0.1:{self.port}'
        self.assertEqual(self.request('POST','/rollback',headers,'version=0')[0],403)
    def test_bad_upload_is_rejected(self):
        headers=dict(self.auth(),Origin=f'http://127.0.0.1:{self.port}',**{'Content-Type':'application/x-www-form-urlencoded'})
        self.assertEqual(self.request('POST','/upload',headers,'csrf='+self.server.csrf)[0],400)

    def test_real_http_upload_review_publish_rollback_and_html_escape(self):
        from test_admin import csv_bytes, product
        from urllib.parse import urlencode
        boundary='syntheticboundary'
        payload=csv_bytes(product(name='<script>alert(1)</script>'))
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n{self.server.csrf}\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="products.csv"\r\nContent-Type: text/csv\r\n\r\n'.encode()+payload+f'\r\n--{boundary}--\r\n'.encode())
        headers=dict(self.auth(),Origin=f'http://127.0.0.1:{self.port}',**{'Content-Type':f'multipart/form-data; boundary={boundary}'})
        self.assertEqual(self.request('POST','/upload',headers,body)[0],303)
        status,page=self.request('GET','/batch?id=1',self.auth())
        self.assertEqual(status,200);self.assertIn(b'&lt;script&gt;',page);self.assertNotIn(b'<script>',page)
        headers['Content-Type']='application/x-www-form-urlencoded'
        def post(path,**fields):
            return self.request('POST',path,headers,urlencode(dict(fields,csrf=self.server.csrf)))[0]
        self.assertEqual(post('/publish',batch=1),400)
        self.assertEqual(post('/approve',batch=1,review='Conferido'),400)
        for issue in self.admin.preview(1)['issues']:
            self.assertEqual(post('/decide',batch=1,issue=issue['id'],justification='Revisado individualmente'),303)
        self.assertEqual(post('/approve',batch=1,review='Conferido'),303)
        self.assertEqual(post('/publish',batch=1),303)
        self.assertEqual(len(self.admin._existing()),1)
        self.assertEqual(post('/rollback',version=0,reason='Teste sintético'),303)
        self.assertEqual(len(self.admin._existing()),0)

    def test_upload_limit_malformed_multipart_and_operational_error(self):
        from unittest.mock import patch
        from artefact.admin import MAX_BYTES
        headers=dict(self.auth(),Origin=f'http://127.0.0.1:{self.port}',**{'Content-Type':'multipart/form-data; boundary=x'})
        self.assertEqual(self.request('POST','/upload',headers,b'x'*(MAX_BYTES+65537))[0],413)
        self.assertEqual(self.request('POST','/upload',headers,b'not a multipart')[0],400)
        with patch.object(self.admin,'batches',side_effect=__import__('sqlite3').OperationalError('private secret')):
            status,body=self.request('GET','/',self.auth())
            self.assertEqual(status,503);self.assertNotIn(b'private secret',body)

    def test_concurrent_authenticated_requests(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses=list(pool.map(lambda _: self.request('GET','/',self.auth())[0],range(16)))
        self.assertEqual(statuses,[200]*16)
