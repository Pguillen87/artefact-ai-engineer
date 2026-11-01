import unittest
from decimal import Decimal
from artefact.commercial import shipping_quote, installments, pix_price

class CommercialRulesTests(unittest.TestCase):
 def test_shipping_boundary_and_other_destinations(self):
  self.assertEqual(shipping_quote(Decimal('499.99'), True), Decimal('35'))
  self.assertEqual(shipping_quote(Decimal('500'), True), Decimal('0'))
  self.assertIsNone(shipping_quote(Decimal('500'), False))
 def test_installment_tiers(self):
  self.assertIn(5, installments(Decimal('450')))
  self.assertNotIn(6, installments(Decimal('450')))
  self.assertNotIn(1, installments(Decimal('49.99')))
  self.assertIn(12, installments(Decimal('1200')))
 def test_pix_does_not_accumulate_with_promotion(self):
  self.assertEqual(pix_price(Decimal('100.00'), False), Decimal('95.00'))
  self.assertEqual(pix_price(Decimal('90.00'), True), Decimal('90.00'))
 def test_negative_and_non_finite_are_rejected(self):
  for value in (Decimal('-1'),Decimal('NaN'),Decimal('Infinity')):
   with self.subTest(value=value), self.assertRaises(ValueError): shipping_quote(value,True)
