import unittest
from unittest.mock import Mock
from pathlib import Path
from tempfile import TemporaryDirectory
from artefact.agent import TelegramAdapter
from artefact.state import State

class AuthorizedChatTests(unittest.TestCase):
 def test_unauthorized_private_chat_never_reaches_model_or_sender(self):
  with TemporaryDirectory() as d:
   agent=Mock();send=Mock();state=State(Path(d)/'state.db')
   adapter=TelegramAdapter(agent,send,state=state,allowed_chat_id=123)
   adapter.handle({'update_id':10,'message':{'chat':{'id':999,'type':'private'},'text':'Oi'}})
   agent.answer.assert_not_called();send.assert_not_called()
   self.assertTrue(state.claim_update(10))
 def test_authorized_chat_still_deduplicates_across_restart(self):
  with TemporaryDirectory() as d:
   agent=Mock();agent.answer.return_value='Olá';send=Mock();path=Path(d)/'s.db'
   update={'update_id':11,'message':{'chat':{'id':123,'type':'private'},'text':'Oi'}}
   TelegramAdapter(agent,send,state=State(path),allowed_chat_id=123).handle(update)
   TelegramAdapter(agent,send,state=State(path),allowed_chat_id=123).handle(update)
   self.assertEqual(send.call_count,1);agent.answer.assert_called_once_with('Oi',chat_id=123)

class StartCommandTests(unittest.TestCase):
 def test_start_is_local_greeting_without_model_call(self):
  from artefact.conversation import ConversationAgent
  planner=Mock();agent=ConversationAgent(None,planner)
  self.assertIn('Olá',agent.answer('/start',chat_id=123))
  planner.request.assert_not_called()
class TelegramSecretSafetyTests(unittest.TestCase):
 def test_http_failure_never_exposes_token(self):
  from unittest.mock import patch
  from artefact.telegram import TelegramClient,TelegramError
  secret='fake-token-value'
  with patch('artefact.telegram.urllib.request.urlopen',side_effect=OSError('sensitive '+secret)):
   with self.assertRaises(TelegramError) as caught: TelegramClient(secret).get_updates(0)
  self.assertNotIn(secret,str(caught.exception))
  self.assertIsNone(caught.exception.__cause__)
