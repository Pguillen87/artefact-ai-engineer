import json, unittest
from unittest.mock import Mock
from artefact.review_client import ReviewClient

class ReviewClientTests(unittest.TestCase):
    def test_restrict_endpoint_and_token(self):
        for url in ('http://evil.example/internal/reviews','http://admin:8080/other','https://admin:8080/internal/reviews','http://admin:8080@evil.example/internal/reviews'):
            with self.assertRaises(ValueError): ReviewClient(url,'x'*32)
        with self.assertRaises(ValueError): ReviewClient('http://admin:8080/internal/reviews','short')
    def test_typed_request_and_sanitized_failures(self):
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);response.read.return_value=b'{"id":42}'
        transport=Mock(return_value=response)
        client=ReviewClient('http://admin:8080/internal/reviews','x'*32,transport=transport)
        self.assertEqual(client(2,'specs.keys','inconsistencia_catalogo',1),42)
        request=transport.call_args.args[0];self.assertEqual(json.loads(request.data)['product_id'],2)
        transport.side_effect=OSError('sensitive details')
        with self.assertRaises(OSError) as exc: client(2,'specs.keys','inconsistencia_catalogo',1)
        self.assertNotIn('sensitive',str(exc.exception))
