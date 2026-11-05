import unittest
from pathlib import Path

class DockerContracts(unittest.TestCase):
    def test_empty_startup_and_local_boundary(self):
        text=Path('compose.yaml').read_text()
        self.assertNotIn('ARTEFACT_SOURCE_DIR',text)
        self.assertNotIn('profiles:',text)
        self.assertIn('127.0.0.1:',text)
        self.assertIn('artefact.chat_api',text)
        self.assertIn('artefact.product_web',text)
        self.assertNotIn('type: bind',text)
        agent=text.split('  admin:')[0]
        self.assertNotIn('ports:',agent)
        self.assertIn('USER 65532:65532',Path('Dockerfile').read_text())
