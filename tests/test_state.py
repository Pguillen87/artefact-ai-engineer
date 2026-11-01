import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from artefact.state import State, BudgetExhausted

class StateTests(unittest.TestCase):
 def test_budget_persists_and_prevents_another_call(self):
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'state.db';state=State(path)
   state.reserve(estimated_usd=Decimal('0.01'),max_usd=Decimal('0.02'),max_calls=1)
   with self.assertRaises(BudgetExhausted): State(path).reserve(Decimal('0.01'),Decimal('0.02'),1)
   self.assertEqual(State(path).usage()['calls'],1)
 def test_duplicate_update_survives_restart(self):
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'state.db'
   self.assertTrue(State(path).claim_update(123))
   self.assertFalse(State(path).claim_update(123))
 def test_budget_rejects_invalid_or_excess_values(self):
  with tempfile.TemporaryDirectory() as d:
   state=State(Path(d)/'state.db')
   for amount in [Decimal('NaN'),Decimal('-1'),Decimal('1')]:
    with self.assertRaises((BudgetExhausted,ValueError)):state.reserve(amount,Decimal('0.1'),10)

class UsageCliTests(unittest.TestCase):
 def test_usage_command_reports_counts_without_messages(self):
  import subprocess,sys,json
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'state.db';State(path).reserve(Decimal('0.001'),Decimal('1'),10)
   result=subprocess.run([sys.executable,'-m','artefact.usage',str(path)],text=True,capture_output=True,check=True)
   self.assertEqual(json.loads(result.stdout)['calls'],1)
