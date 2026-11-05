"""Origin checks must accept local browser posts without trusting foreign origins."""
from email.message import Message
import unittest

from artefact.web_security import same_origin


class SameOriginTests(unittest.TestCase):
    def headers(self, **overrides):
        headers = Message()
        headers['Host'] = '127.0.0.1:8080'
        for name, value in overrides.items():
            headers[name.replace('_', '-')] = value
        return headers

    def test_explicit_matching_origin(self):
        self.assertTrue(same_origin(self.headers(Origin='http://127.0.0.1:8080')))

    def test_missing_or_null_origin_uses_same_origin_referer(self):
        for origin in (None, 'null'):
            for fetch_site in (None, 'same-origin'):
                with self.subTest(origin=origin, fetch_site=fetch_site):
                    headers = self.headers(Referer='http://127.0.0.1:8080/ai?provider=google')
                    if origin is not None:
                        headers['Origin'] = origin
                    if fetch_site is not None:
                        headers['Sec-Fetch-Site'] = fetch_site
                    self.assertTrue(same_origin(headers))

    def test_missing_or_null_origin_requires_referer_even_with_fetch_metadata(self):
        for origin in (None, 'null'):
            with self.subTest(origin=origin):
                headers = self.headers(Sec_Fetch_Site='same-origin')
                if origin is not None:
                    headers['Origin'] = origin
                self.assertFalse(same_origin(headers))

    def test_foreign_origin_cannot_fall_back_to_local_referer(self):
        self.assertFalse(same_origin(self.headers(
            Origin='http://evil.example', Referer='http://127.0.0.1:8080/ai',
            Sec_Fetch_Site='same-origin')))

    def test_referer_must_match_scheme_host_and_port(self):
        for referer in (
            'http://evil.example/ai', 'https://127.0.0.1:8080/ai',
            'http://127.0.0.1:8081/ai', 'http://localhost:8080/ai',
            'http://127.0.0.1:8080.evil.example/ai',
            'http://127.0.0.1:8080@evil.example/ai',
            'http://user@127.0.0.1:8080/ai', '/ai', '', 'http://[invalid',
            'http://127.0.0.1:\t8080/ai',
        ):
            for origin in (None, 'http://127.0.0.1:8080'):
                with self.subTest(referer=referer, origin=origin):
                    headers = self.headers(Referer=referer)
                    if origin is not None:
                        headers['Origin'] = origin
                    self.assertFalse(same_origin(headers))

    def test_conflicting_fetch_metadata_rejected(self):
        for fetch_site in ('cross-site', 'same-site', 'none', '', 'invalid'):
            for origin in (None, 'null', 'http://127.0.0.1:8080'):
                with self.subTest(fetch_site=fetch_site, origin=origin):
                    headers = self.headers(Referer='http://127.0.0.1:8080/ai',
                                           Sec_Fetch_Site=fetch_site)
                    if origin is not None:
                        headers['Origin'] = origin
                    self.assertFalse(same_origin(headers))

    def test_empty_origin_is_not_treated_as_missing(self):
        self.assertFalse(same_origin(self.headers(
            Origin='', Referer='http://127.0.0.1:8080/ai')))

    def test_duplicate_security_headers_rejected(self):
        for name, value in (
            ('Host', 'evil.example'), ('Origin', 'http://evil.example'),
            ('Referer', 'http://evil.example/'), ('Sec-Fetch-Site', 'cross-site'),
        ):
            with self.subTest(name=name):
                headers = self.headers(Origin='http://127.0.0.1:8080',
                    Referer='http://127.0.0.1:8080/ai', Sec_Fetch_Site='same-origin')
                headers[name] = value
                self.assertFalse(same_origin(headers))

    def test_missing_host_rejected(self):
        headers = Message()
        headers['Origin'] = 'http://'
        self.assertFalse(same_origin(headers))
