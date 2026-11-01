"""Read-only services for the authorized fictional demonstration dataset."""
from decimal import Decimal
import unicodedata


def _name(value):
    return ' '.join(unicodedata.normalize('NFC', value).casefold().split())


class Catalog:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def product(self, product_id):
        product = next((p for p in self._snapshot.tables['products'] if p['product_id'] == product_id), None)
        if product is None:
            return None
        conflicts = {i.field for i in self._snapshot.issues if i.product_id == product_id}
        specs = {k: v for k, v in product['specs'].items() if f'specs.{k}' not in conflicts}
        # A brand or category conflict makes the free-text description unsafe to assert.
        return {'product_id':product_id, 'name':product['name'], 'price_brl':product['price_brl'],
                'stock_quantity':product['stock_quantity'], 'status':product['status'],
                'available':product['status'] == 'active' and product['stock_quantity'] > 0,
                'description':None if conflicts else product['description'], 'specs':specs,
                'review_fields':tuple(sorted(conflicts)), 'freshness':'snapshot_unknown'}

    def search(self, text):
        query = _name(text)
        if not query:
            return ()
        return tuple(self.product(p['product_id']) for p in self._snapshot.tables['products'] if query in _name(p['name']))

    def promotion(self, product_id):
        product = self.product(product_id)
        if product is None:
            return None
        active = [p for p in self._snapshot.tables['promotions'] if p['product_id'] == product_id and p['is_active']]
        if len(active) > 1:
            return {'status':'ambiguous', 'freshness':'snapshot_unknown'}
        if not active or not product['available']:
            return {'status':'none_available', 'freshness':'snapshot_unknown'}
        return {'status':'active_in_snapshot','discount_percent':active[0]['discount_percent'],
                'freshness':'snapshot_unknown'}


class OrderService:
    """Fictional dataset only: exact unique name identifies a record, not a real person."""
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def lookup(self, full_name):
        name = _name(full_name)
        if len(name.split()) < 2:
            return ()
        matches = [c for c in self._snapshot.tables['customers'] if _name(c['name']) == name]
        if len(matches) != 1:
            return ()
        customer_id = matches[0]['customer_id']
        results = []
        for order in self._snapshot.tables['orders']:
            if order['customer_id'] != customer_id:
                continue
            items = tuple({'product_id':i['product_id'],'quantity':i['quantity']} for i in self._snapshot.tables['order_items'] if i['order_id'] == order['order_id'])
            results.append({'order_id':order['order_id'],'status':order['status'],'total_brl':order['total_brl'],
                            'payment_method':order['payment_method'],'tracking_code':order['tracking_code'] or None,
                            'estimated_delivery':order['estimated_delivery'] or None,'items':items})
        return tuple(results)


# Approved demo decisions, not a reproduction of the restricted manual.
_POLICIES = {
    'shipping': {'summary':'Frete metropolitano: R$35 abaixo de R$500; gratuito a partir de R$500. Outros destinos requerem cotação.', 'source':'ARTEFACT-11, decisão humana da demonstração'},
    'installments': {'summary':'Parcelas sem juros: piso de R$50 em 1–3x, R$80 em 4–6x e R$100 em 7–12x.', 'source':'ARTEFACT-11, decisão humana da demonstração'},
    'pix': {'summary':'Desconto de 5% no Pix não acumula com promoção.', 'source':'ARTEFACT-11, decisão humana da demonstração'},
    'defect': {'summary':'Troca comercial por defeito até 30 dias da compra; depois orientar garantia e fabricante, sem negar a garantia legal de 90 dias a partir do recebimento.', 'source':'ARTEFACT-11, decisão humana da demonstração'},
}


def get_policy(topic):
    policy = _POLICIES.get(topic)
    return dict(policy) if policy else None
