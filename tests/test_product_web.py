import tempfile, threading, unittest, http.client, base64
from pathlib import Path
from artefact.product_web import make_product_server

class ProductWebTests(unittest.TestCase):
    def test_empty_authenticated_dashboard_and_six_file_form(self):
        with tempfile.TemporaryDirectory() as d:
            server=make_product_server('127.0.0.1',0,Path(d),'synthetic-password-long')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
                auth='Basic '+base64.b64encode(b'admin:synthetic-password-long').decode()
                conn.request('GET','/',headers={'Authorization':auth})
                response=conn.getresponse(); body=response.read().decode()
                self.assertEqual(response.status,200)
                self.assertIn('Sem dataset ativo',body)
                self.assertIn('Configura\u00e7\u00e3o de IA',body)
                self.assertIn('Chat de teste',body)
                self.assertIn('multiple',body)
                conn.close()
            finally: server.shutdown();server.server_close();thread.join()
    def test_proxied_chat_allows_authenticated_review_callback(self):
        from unittest.mock import patch
        from urllib.parse import urlencode
        from artefact.product_web import ProductHandler
        with tempfile.TemporaryDirectory() as d:
            server=make_product_server('127.0.0.1',0,Path(d),'synthetic-password-long')
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def callback(handler,method,path,payload):
                nested=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
                nested.request('POST','/internal/reviews',body='{}',headers={'Authorization':'Bearer '+server.review_token,'Content-Type':'application/json'})
                response=nested.getresponse();response.read();nested.close()
                self.assertEqual(response.status,400)
                return {'answer':'ok'}
            try:
                with patch.object(ProductHandler,'api',callback):
                    conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
                    auth='Basic '+base64.b64encode(b'admin:synthetic-password-long').decode()
                    conn.request('POST','/chat/send',body=urlencode({'csrf':server.csrf,'id':'synthetic','message':'review'}),headers={'Authorization':auth,'Origin':f'http://127.0.0.1:{server.server_port}','Content-Type':'application/x-www-form-urlencoded'})
                    response=conn.getresponse();response.read();conn.close()
                    self.assertEqual(response.status,303)
            finally:server.shutdown();server.server_close();thread.join()
