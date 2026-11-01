import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from decimal import Decimal
from test_data import write_dataset
from artefact.data import load_dataset
from artefact.services import Catalog, OrderService, get_policy

class ServiceTests(unittest.TestCase):
 def setUp(self):
  self.temp=TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
  root=Path(self.temp.name);write_dataset(root);self.snapshot=load_dataset(root)
 def test_catalog_preserves_commercial_data_and_masks_conflicting_details(self):
  item=Catalog(self.snapshot).product(1)
  self.assertEqual(item['price_brl'],Decimal('1127.50'))
  self.assertTrue(item['available'])
  self.assertIsNone(item['description'])
  self.assertNotIn('keys',item['specs'])
  self.assertEqual(Catalog(self.snapshot).search('korg')[0]['product_id'],1)
 def test_promotion_is_snapshot_not_freshness_claim(self):
  result=Catalog(self.snapshot).promotion(1)
  self.assertEqual(result['discount_percent'],Decimal('10'))
  self.assertEqual(result['freshness'],'snapshot_unknown')
 def test_order_matches_full_name_exactly_and_returns_recorded_total(self):
  service=OrderService(self.snapshot)
  self.assertEqual(service.lookup(' synthetic   CUSTOMER ')[0]['total_brl'],Decimal('1127.50'))
  self.assertEqual(service.lookup('Synthetic'),())
  self.assertEqual(service.lookup('fake@example.invalid'),())
 def test_ambiguous_name_never_discloses_orders(self):
  with TemporaryDirectory() as d:
   p=Path(d);write_dataset(p,extra={'customers':['2','Synthetic Customer','001','other@example.invalid','Test']})
   self.assertEqual(OrderService(load_dataset(p)).lookup('Synthetic Customer'),())
 def test_policy_is_versioned_and_unknown_topics_not_invented(self):
  self.assertIn('500',get_policy('shipping')['summary'])
  self.assertIsNone(get_policy('made_up'))

class MoreServiceTests(unittest.TestCase):
 def test_duplicate_active_promotion_is_ambiguous(self):
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root,extra={'promotions':['2','1','20','other','1']})
   self.assertEqual(Catalog(load_dataset(root)).promotion(1)['status'],'ambiguous')
 def test_unavailable_product_does_not_offer_promotion(self):
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root,{'products':{'status':'coming_soon','stock_quantity':'5'}})
   service=Catalog(load_dataset(root))
   self.assertFalse(service.product(1)['available'])
   self.assertEqual(service.promotion(1)['status'],'none_available')
