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
        self.assertIn('href="/?view=files"',body)
        self.assertIn('href="/?view=settings"',body)
        self.assertNotIn('id="datasets"',body)
        self.assertNotIn('id="api-key"',body)
        with patch.object(ProductHandler,'api',side_effect=AssertionError('Settings must not depend on the agent')):
            files = self.request('GET','/?view=files')[2]
            settings = self.request('GET','/?view=settings')[2]
        self.assertIn('multiple',files)
        self.assertNotIn('id="api-key"',files)
        self.assertIn('id="api-key"',settings)
        self.assertNotIn('id="datasets"',settings)
        self.assertIn('class="advanced"',settings)
        self.assertNotIn('class="advanced" open',settings)
        self.assertLess(settings.index('action="/ai/test"'),settings.index('class="advanced"'))
        for removed in ('Importa\u00e7\u00e3o e vers\u00f5es', 'Cat\u00e1logo e revis\u00e3o', '<pre>', 'preserve_legacy', 'name="official"'):
            self.assertNotIn(removed,body + files + settings)
        for path, target in [('/catalog','/?view=files'),('/ai','/?view=settings'),('/chat?id=abc','/?view=chat&id=abc')]:
            self.assertEqual(self.request('GET',path)[1]['Location'],target)

    def test_seller_validation_keeps_form_and_explains_the_problem(self):
        self.login()
        code, _, body = self.request('POST','/seller-contact',{'name':'Test Seller','contact':'bad'})
        self.assertEqual(code,400)
        self.assertIn('id="seller-phone"',body)
        self.assertIn('value="Test Seller"',body)
        self.assertIn('value="bad"',body)
        self.assertIn('Telefone',body)
        self.assertIn('aria-current="page">Configura',body)
        code, _, body = self.request('POST','/seller-contact',{'name':'Test Seller','contact':'(00) 90000-0000'})
        self.assertEqual(code,303)
        self.assertEqual(self.server.admin.seller_contact(),'(00) 90000-0000')

    def test_invalid_ai_settings_keep_nonsecret_fields(self):
        self.login()
        config = self.server.provider_config
        models = config.models()['google']
        config.save('google', models[0], 'synthetic-key')
        code, _, body = self.request('POST','/ai/limits',{'provider':'google','max_calls':'57','max_usd':'-1','input_price':'2','output_price':'7'})
        self.assertEqual(code,400)
        for value in ('57','-1','2','7'):
            self.assertIn('value="'+value+'"',body)
        self.assertEqual(config.status()['providers']['google']['max_calls'],100)
        secret = 'SENSITIVE-' + 'x' * 4096
        code, _, body = self.request('POST','/ai/save',{'provider':'google','model':models[-1],'api_key':secret})
        self.assertEqual(code,400)
        self.assertIn('value="'+models[-1]+'" selected',body)
        self.assertNotIn(secret,body)

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
        imported_html=self.request('GET','/?view=files&notice=imported')[2]
        self.assertEqual(imported_html.count('class="file-state"'),6)
        for name,_ in files:
            self.assertIn(name,imported_html)
        self.assertIn('import-success',imported_html)
        for invalid in (files[:-1], files+[files[0]], [(n,b'bad') if n=='products.csv' else (n,b) for n,b in files]):
            body, content_type = self.multipart(invalid)
            self.assertEqual(self.request('POST','/dataset/import',body=body,headers={'Content-Type':content_type})[0],400)
            self.assertEqual(active,self.server.datasets.active_version())
        body, content_type=self.multipart(files,csrf='wrong')
        self.assertEqual(self.request('POST','/dataset/import',body=body,headers={'Content-Type':content_type})[0],403)
        self.assertEqual(self.request('POST','/seller-contact',{'contact':'(00) 90000-0000'},headers={'Origin':'http://external.test'})[0],403)

    def test_basic_ai_save_preserves_settings_and_never_calls_api(self):
        self.login(); config=self.server.provider_config; model=config.models()['google'][0]
        config.save('google',model,'test-key',max_calls=57,max_usd='3',input_price='2',output_price='7')
        with patch.object(ProductHandler,'api') as api:
            status, _, _=self.request('POST','/ai/save',{'provider':'google','model':model,'api_key':''})
            self.assertEqual(status,303);api.assert_not_called()
        state=config.status()['providers']['google']
        self.assertEqual((state['max_calls'],state['max_usd'],state['input_price'],state['output_price']),(57,'3','2','7'))
        self.assertEqual(config.credentials('google'),'test-key')
        self.assertEqual(self.request('POST','/seller-contact',{'name':'Test Seller','contact':'(00) 90000-0000'})[0],303)
        self.assertEqual(self.server.admin.seller_name(),'Test Seller')

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

    def test_async_chat_contract_and_json_errors(self):
        import json
        headers = {'Accept':'application/json'}
        code, _, body = self.request('POST','/chat/send',{'id':'test','message':'hello'},headers=headers)
        self.assertEqual(code,401)
        self.assertEqual(json.loads(body)['error'],'login_required')
        self.login()
        code, _, body = self.request('POST','/chat/send',{'csrf':'bad','id':'test','message':'hello'},headers=headers)
        self.assertEqual(code,403)
        self.assertEqual(json.loads(body)['error'],'invalid_csrf')
        with patch.object(ProductHandler,'api',return_value={'id':'new-chat'}) as api:
            code, _, body = self.request('POST','/chat/create',headers=headers)
            self.assertEqual(code,201)
            self.assertEqual(json.loads(body)['id'],'new-chat')
            self.assertEqual(api.call_count,1)
        session = {'id':'new-chat','messages':[{'role':'user','content':'hello'},{'role':'assistant','content':'reply'}]}
        with patch.object(ProductHandler,'api',return_value={'answer':'reply','session':session}) as api:
            code, response_headers, body = self.request('POST','/chat/send',{'id':'new-chat','message':'hello'},headers=headers)
            self.assertEqual(code,200)
            self.assertIn('application/json',response_headers['Content-Type'])
            self.assertEqual(json.loads(body)['session'],session)
            self.assertEqual(api.call_count,1)
        with patch.object(ProductHandler,'api',side_effect=ValueError('budget_exhausted')) as api:
            code, _, body = self.request('POST','/chat/send',{'id':'new-chat','message':'hello'},headers=headers)
            self.assertEqual(code,400)
            self.assertEqual(json.loads(body)['id'],'new-chat')
            self.assertEqual(json.loads(body)['error'],'budget_exhausted')
            self.assertEqual(api.call_count,1)
        with patch.object(ProductHandler,'api',side_effect=ValueError('agent_unavailable')):
            code, _, body = self.request('POST','/chat/send',{'id':'new-chat','message':'hello'},headers=headers)
            self.assertEqual(code,503)
            self.assertEqual(json.loads(body)['id'],'new-chat')

    def test_slow_send_does_not_block_settings(self):
        self.login()
        started = threading.Event(); release = threading.Event(); replies = []
        def api(handler, method, path, payload=None):
            started.set()
            release.wait(4)
            return {'answer':'done'}
        def send():
            replies.append(self.request('POST','/chat/send',{'id':'test','message':'slow'},headers={'Accept':'application/json'})[0])
        with patch.object(ProductHandler,'api',api):
            worker = threading.Thread(target=send); worker.start()
            try:
                self.assertTrue(started.wait(2))
                self.assertEqual(self.request('GET','/?view=settings')[0],200)
                self.assertTrue(worker.is_alive())
            finally:
                release.set(); worker.join(5)
        self.assertEqual(replies,[200])

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
