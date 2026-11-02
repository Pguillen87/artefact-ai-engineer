import unittest
from decimal import Decimal
from artefact.commercial import shipping_quote, installments, pix_price, promotion_price, quote_installments, extract_installment_request, extract_installment_count

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
  self.assertEqual(promotion_price(Decimal('359.00'),Decimal('10')),Decimal('323.10'))
  self.assertEqual(pix_price(Decimal('1127.50'),False),Decimal('1071.13'))
 def test_negative_and_non_finite_are_rejected(self):
  for value in (Decimal('-1'),Decimal('NaN'),Decimal('Infinity')):
   with self.subTest(value=value), self.assertRaises(ValueError): shipping_quote(value,True)

 def test_installment_quote_is_exact_and_applies_each_tier_floor(self):
  from artefact import commercial
  quote = getattr(commercial, 'quote_installments', None)
  self.assertIsNotNone(quote, 'commercial installment quoting must be part of the deterministic domain layer')
  cases = [
   ('150.00',2,True,('75.00','75.00')),
   ('90.00',2,False,('45.00','45.00')),
   ('480.00',6,True,('80.00',)*6),
   ('479.99',6,False,('80.00','80.00','80.00','80.00','80.00','79.99')),
   ('1200.00',12,True,('100.00',)*12),
   ('1199.88',12,False,('99.99',)*12),
  ]
  for amount,count,eligible,values in cases:
   with self.subTest(amount=amount,count=count):
    result=quote(Decimal(amount),count)
    self.assertEqual(result.eligible,eligible)
    self.assertEqual(result.installments,tuple(Decimal(value) for value in values))
    self.assertEqual(sum(result.installments),Decimal(amount))

 def test_installment_quote_distributes_cents_without_rounding_total(self):
  result=quote_installments(Decimal('151.00'),3)
  self.assertTrue(result.eligible)
  self.assertEqual(result.installments,(Decimal('50.34'),Decimal('50.33'),Decimal('50.33')))
  self.assertEqual(sum(result.installments),Decimal('151.00'))

 def test_installment_count_and_cent_precision_are_validated(self):
  for count in (0,13,True):
   with self.subTest(count=count),self.assertRaises(ValueError): quote_installments(Decimal('150.00'),count)
  with self.assertRaises(ValueError): quote_installments(Decimal('150.001'),2)

 def test_explicit_amount_parser_accepts_brazilian_and_international_grouping(self):
  for amount in ('R$ 1.000,00','R$ 1,000.00','R$ 1.000','R$ 1,000'):
   with self.subTest(amount=amount):
    self.assertEqual(extract_installment_request(f'Parcelar {amount} em 2x?'),(Decimal('1000.00'),2))
  self.assertEqual(extract_installment_request('R$250,00 em 2 vezes?'),(Decimal('250.00'),2))

 def test_explicit_amount_parser_rejects_ambiguous_freight_and_history(self):
  for text in (
   'Parcelar R$150 ou R$90 em 2x?',
   'Posso dividir o pedido 20 em 2x?',
   'Reparcelar total histórico de R$1.400 em 2x?',
   'Frete de R$35 em 2x?',
   'Qual o piso de R$50 por parcela em 2x?',
   'Parcelar R$150 em 2x ou 3x?',
  ):
   with self.subTest(text=text): self.assertIsNone(extract_installment_request(text))
  self.assertIsNone(extract_installment_count('Reparcelar o pedido 20 em 2x?'))
