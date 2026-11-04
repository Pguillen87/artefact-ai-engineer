import http.client
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from artefact.product_web import ProductHandler, make_product_server
from test_data import write_dataset


class ProductWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = make_product_server('127.0.0.1', 0, self.root / 'state', 'admin', agent_url='http://127.0.0.1:1')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.cookie = ''

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.tmp.cleanup()

    def request(self, method, path, fields=None, headers=None, body=None):
        supplied = {'Cookie': self.cookie}
        if method == 'POST':
            supplied.update({'Origin': self.origin, 'Content-Type': 'application/x-www-form-urlencoded'})
            if body is None: body = urlencode({'csrf': self.server.csrf, **(fields or {})})
        supplied.update(headers or {})
        supplied = {k:v for k,v in supplied.items() if v is not None}
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        conn.request(method, path, body=body, headers=supplied)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read().decode()
        conn.close()
        return result

    def login(self):
        status, headers, _ = self.request('POST', '/login', {'username':'admin', 'password':'admin'})
        self.assertEqual(status, 303)
        self.cookie = headers['Set-Cookie'].split(';')[0]
        return headers

    def test_login_session_logout_expiry_and_basic_rejection(self):
        status, headers, _ = self.request('GET', '/')
        self.assertEqual((status, headers['Location']), (303, '/login'))
        self.assertNotIn('WWW-Authenticate', headers)
        self.assertEqual(self.request('GET', '/', headers={'Authorization':'Basic YWRtaW46YWRtaW4='})[0], 303)
        self.assertEqual(self.request('POST','/login',{'username':'admin','password':'bad'})[0],401)
        headers = self.login()
        for value in ('HttpOnly', 'SameSite=Strict', 'Max-Age=28800'):
            self.assertIn(value, headers['Set-Cookie'])
        self.assertEqual(self.request('GET','/')[0],200)
        self.server.login_sessions[self.cookie.split('=',1)[1]] = time.time() - 1
        self.assertEqual(self.request('GET','/')[0],303)
        self.login()
        self.assertEqual(self.request('POST','/logout')[0],303)
        self.assertEqual(self.request('GET','/')[0],303)

    def test_dashboard_and_legacy_redirects(self):
        self.login()
        status, headers, body = self.request('GET','/')
        self.assertEqual(status,200)
        self.assertEqual(headers['Referrer-Policy'],'same-origin')
        self.assertIn('multiple',body)
        self.assertIn('class="advanced"',body)
        self.assertNotIn('class="advanced" open',body)
        for removed in ('Importa\u00e7\u00e3o e vers\u00f5es', 'Cat\u00e1logo e revis\u00e3o', '<pre>', 'preserve_legacy', 'name="official"'):
            self.assertNotIn(removed,body)
        for path, target in [('/catalog','/#import'),('/ai','/#ai'),('/chat?id=abc','/?id=abc#chat')]:
            self.assertEqual(self.request('GET',path)[1]['Location'],target)

    def multipart(self, files, csrf=None):
        boundary='artefact-test-boundary'
        chunks=[f'--{boundary}\r\nContent-Disposition: form-data; name="csrf"\r\n\r\n{self.server.csrf if csrf is None else csrf}\r\n'.encode()]
        for name, content in files:
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="datasets"; filename="{name}"\r\nContent-Type: text/csv\r\n\r\n'.encode()+content+b'\r\n')
        chunks.append(f'--{boundary}--\r\n'.encode())
        return b''.join(chunks), 'multipart/form-data; boundary='+boundary

    def test_six_file_import_referer_fallback_atomic_failure_and_csrf(self):
        self.login()
        data=self.root/'csv';data.mkdir();write_dataset(data)
        files=[(p.name,p.read_bytes()) for p in data.glob('*.csv')]
        body, content_type = self.multipart(files)
        status, _, _ = self.request('POST','/dataset/import',body=body,headers={'Content-Type':content_type,'Origin':'null','Referer':self.origin+'/', 'Sec-Fetch-Site':'same-origin'})
        self.assertEqual(status,303)
        active=self.server.datasets.active_version()
        self.assertIsNotNone(active)
        for invalid in (files[:-1], files+[files[0]], [(n,b'bad') if n=='products.csv' else (n,b) for n,b in files]):
            body, content_type = self.multipart(invalid)
            self.assertEqual(self.request('POST','/dataset/import',body=body,headers={'Content-Type':content_type})[0],400)
            self.assertEqual(active,self.server.datasets.active_version())
        body, content_type=self.multipart(files,csrf='wrong')
        self.assertEqual(self.request('POST','/dataset/import',body=body,headers={'Content-Type':content_type})[0],403)
        self.assertEqual(self.request('POST','/seller-contact',{'contact':'(67) 3341-4444'},headers={'Origin':'http://external.test'})[0],403)

    def test_basic_ai_save_preserves_settings_and_never_calls_api(self):
        self.login(); config=self.server.provider_config; model=config.models()['google'][0]
        config.save('google',model,'test-key',max_calls=57,max_usd='3',input_price='2',output_price='7')
        with patch.object(ProductHandler,'api') as api:
            status, _, _=self.request('POST','/ai/save',{'provider':'google','model':model,'api_key':''})
            self.assertEqual(status,303);api.assert_not_called()
        state=config.status()['providers']['google']
        self.assertEqual((state['max_calls'],state['max_usd'],state['input_price'],state['output_price']),(57,'3','2','7'))
        self.assertEqual(config.credentials('google'),'test-key')
        self.assertEqual(self.request('POST','/seller-contact',{'name':'Ana','contact':'(67) 3341-4444'})[0],303)
        self.assertEqual(self.server.admin.seller_name(),'Ana')

    def test_chat_callback_does_not_deadlock_and_errors_are_friendly(self):
        self.login()
        def callback(handler,method,path,payload=None):
            nested=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=2)
            nested.request('POST','/internal/reviews',body='{}',headers={'Authorization':'Bearer '+self.server.review_token,'Content-Type':'application/json'})
            response=nested.getresponse();response.read();nested.close()
            self.assertEqual(response.status,400)
            return {'answer':'ok'}
        with patch.object(ProductHandler,'api',callback):
            self.assertEqual(self.request('POST','/chat/send',{'id':'synthetic','message':'review'})[0],303)
        with patch.object(ProductHandler,'api',side_effect=ValueError('budget_exhausted')):
            status,_,body=self.request('POST','/chat/send',{'id':'synthetic','message':'review'})
            self.assertEqual(status,400);self.assertNotIn('budget_exhausted',body)
            self.assertIn('limite',body)

    def test_internal_review_response_remains_json(self):
        import json
        with patch.object(self.server.admin, 'create_review', return_value=123):
            status, headers, body = self.request('POST', '/internal/reviews',
                headers={'Authorization': 'Bearer ' + self.server.review_token, 'Content-Type': 'application/json'},
                body=json.dumps({'product_id':1,'field':'brand','reason':'inconsistencia_catalogo','catalog_version':1}))
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body), {'id':123})
        self.assertIn('application/json', headers['Content-Type'])

    def test_restart_loads_published_catalog_for_reviews(self):
        data = self.root / 'csv'; data.mkdir(); write_dataset(data)
        version = self.server.datasets.import_files([(p.name,p.read_bytes()) for p in data.glob('*.csv')])
        restarted = make_product_server('127.0.0.1',0,self.root/'state','admin',agent_url='http://127.0.0.1:1')
        try:
            self.assertIsNotNone(restarted.admin.dataset_store)
            self.assertEqual(restarted.admin.baseline.catalog_version, version)
        finally:
            restarted.server_close()

    def test_failed_first_message_keeps_draft_and_created_session(self):
        self.login()
        data = self.root/'csv'; data.mkdir(); write_dataset(data)
        self.server.datasets.import_files([(p.name,p.read_bytes()) for p in data.glob('*.csv')])
        model = self.server.provider_config.models()['google'][0]
        status = {'active':'google','providers':{'google':{'model':model,'has_key':True,'verified':True}}}
        def api(handler, method, path, payload=None):
            if method == 'POST' and path == '/sessions': return {'id':'new-session'}
            if method == 'POST': raise ValueError('budget_exhausted')
            if path == '/sessions': return {'sessions':[{'id':'new-session'}]}
            return {'id':'new-session','messages':[]}
        with patch.object(self.server.provider_config,'status',return_value=status), patch.object(ProductHandler,'api',api):
            code, _, body = self.request('POST','/chat/send',{'message':'My original draft'})
        self.assertEqual(code,400)
        self.assertIn('name="id" value="new-session"',body)
        self.assertIn('>My original draft</textarea>',body)
        with patch.object(self.server.provider_config,'status',return_value=status), patch.object(ProductHandler,'api',side_effect=ValueError('agent_unavailable')):
            code, _, body = self.request('POST','/chat/send',{'id':'existing-session','message':'Retry this draft'})
        self.assertEqual(code,400)
        self.assertIn('name="id" value="existing-session"',body)
        self.assertIn('>Retry this draft</textarea>',body)
