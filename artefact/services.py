"""Read-only services for the authorized fictional demonstration dataset."""
from decimal import Decimal
import re
import unicodedata

from .commercial import pix_price, promotion_price

MAX_SEARCH_RESULTS = 50


def _name(value):
    return ' '.join(unicodedata.normalize('NFC', value).casefold().split())


def _search_text(value):
    return ' '.join(''.join(char for char in unicodedata.normalize('NFKD', value.casefold())
                            if not unicodedata.combining(char)).split())


class Catalog:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def product(self, product_id):
        product = next((p for p in self._snapshot.tables['products'] if p['product_id'] == product_id), None)
        if product is None:
            return None
        conflicts = {i.field for i in self._snapshot.issues if i.product_id == product_id}
        specs = {k: v for k, v in product['specs'].items() if f'specs.{k}' not in conflicts}
        available = product['status'] == 'active' and product['stock_quantity'] > 0
        # A brand or category conflict makes the free-text description unsafe to assert.
        return {'product_id':product_id, 'name':product['name'], 'price_brl':product.get('price_brl'),
                'stock_quantity':product['stock_quantity'], 'status':product['status'],
                'available':available,
                'description':None if conflicts else product['description'], 'specs':specs,
                'review_fields':tuple(sorted(conflicts)), 'freshness':'snapshot_unknown',
                'catalog_version':self._snapshot.catalog_version,
                'price_quote':self._price_quote(product, available)}

    def _price_quote(self, product, available):
        price = product.get('price_brl')
        freshness = 'snapshot_unknown'
        source = {'source':'catalog_snapshot','catalog_version':self._snapshot.catalog_version}
        if not isinstance(price, Decimal) or not price.is_finite() or price < 0 or price != price.quantize(Decimal('0.01')):
            return {'status':'missing_price', 'price_brl':None, 'discount_percent':None,
                    'promotional_price_brl':None, 'pix_price_brl':None, 'checkout_price_brl':None,
                    'freshness':freshness, **source}
        active = [p for p in self._snapshot.tables['promotions']
                  if p['product_id'] == product['product_id'] and p['is_active']]
        result = {'price_brl':price, 'discount_percent':None, 'promotional_price_brl':None,
                  'pix_price_brl':None, 'checkout_price_brl':None, 'freshness':freshness, **source}
        if not available:
            return {'status':'unavailable', **result}
        if len(active) > 1:
            return {'status':'ambiguous', **result}
        if active:
            discount = active[0].get('discount_percent')
            try:
                amount = promotion_price(price, discount)
            except ValueError:
                return {'status':'ambiguous', **result}
            return {'status':'active_in_snapshot', **result, 'discount_percent':discount,
                    'promotional_price_brl':amount, 'checkout_price_brl':amount}
        return {'status':'none_available', **result, 'pix_price_brl':pix_price(price, False),
                'checkout_price_brl':price}

    def search(self, text=None, *, category=None, brand=None, model=None, features=(), min_price=None, max_price=None,
               available_only=True, limit=20, allow_unfiltered=False, excluded_ids=(), product_ids=()):
        for value in (text, category, brand, model):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError('empty search criterion')
        if (not isinstance(features, (list, tuple)) or len(features) > 8 or
                any(not isinstance(value, str) or not value.strip() or len(value) > 80
                    for value in features)):
            raise ValueError('invalid search features')
        if not isinstance(available_only, bool):
            raise ValueError('invalid availability filter')
        if (text is None and category is None and brand is None and model is None and not features and
                min_price is None and max_price is None and not allow_unfiltered):
            raise ValueError('search criterion required')
        query = _name(text) if text is not None else None
        brand_query = _search_text(brand) if brand is not None else None
        model_query = _search_text(model) if model is not None else None
        feature_terms = tuple(_search_text(value) for value in features)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_SEARCH_RESULTS:
            raise ValueError(f'limit must be between 1 and {MAX_SEARCH_RESULTS}')
        for value in (min_price, max_price):
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or value < 0):
                raise ValueError('invalid price')
        if min_price is not None and max_price is not None and min_price > max_price:
            raise ValueError('invalid price range')
        categories = {c['category_id']: _name(c['name']) for c in self._snapshot.tables['categories']}
        found = []
        for p in self._snapshot.tables['products']:
            if p['product_id'] in excluded_ids or (product_ids and p['product_id'] not in product_ids):
                continue
            if (query is not None and query not in _name(p['name'])) or (category is not None and categories[p['category_id']] != _name(category)):
                continue
            item = self.product(p['product_id'])
            searchable = ' '.join((
                _search_text(item['name']),
                _search_text(item['description'] or ''),
                *(_search_text(str(value)) for value in item['specs'].values()
                  if isinstance(value, (str, int, Decimal)) and not isinstance(value, bool)),
            ))
            if (brand_query is not None and brand_query not in searchable) or (
                    model_query is not None and model_query not in _search_text(item['name'])) or any(
                    term not in searchable for term in feature_terms):
                continue
            effective = item['price_quote']['checkout_price_brl']
            if (min_price is not None or max_price is not None) and effective is None:
                continue
            if min_price is not None and effective < min_price or max_price is not None and effective > max_price:
                continue
            if not available_only or item['available']:
                found.append(item)
        if available_only:
            found.sort(key=lambda item: (item['price_quote']['checkout_price_brl'] is None,
                                         -(item['price_quote']['checkout_price_brl'] or Decimal('0')),
                                         item['product_id']))
            if len(found)>limit:
                ranked = sorted(
                    found,
                    key=lambda item: (
                        item['price_quote']['checkout_price_brl'] is None,
                        item['price_quote']['checkout_price_brl'] or Decimal('0'),
                        item['product_id']))
                selected = {item['product_id'] for item in ranked[:limit]}
                return tuple(item for item in found if item['product_id'] in selected)
        return tuple(found[:limit])

    def overview(self, *, example_limit=3):
        """Return bounded, saleable category counts and one priced example per category."""
        if type(example_limit) is not int or not 1 <= example_limit <= 3:
            raise ValueError('example limit must be between one and three')
        categories = {row['category_id']: row for row in self._snapshot.tables['categories']}
        counts = {category_id: 0 for category_id in categories}
        available = []
        for product in self._snapshot.tables['products']:
            item = self.product(product['product_id'])
            if not item or not item['available']:
                continue
            category_id = product['category_id']
            counts[category_id] += 1
            if item['price_quote'].get('checkout_price_brl') is not None:
                available.append((category_id, item))

        visible_categories = [
            {'category_id': category_id, 'name': category['name'], 'available_count': counts[category_id]}
            for category_id, category in sorted(categories.items(), key=lambda pair: _name(pair[1]['name']))
            if counts[category_id] > 0
        ]
        examples = []
        seen = set()
        for category_id, item in sorted(available, key=lambda pair: (pair[0], pair[1]['product_id'])):
            if category_id in seen:
                continue
            category = categories[category_id]
            examples.append({**item, 'category_id': category_id, 'category_name': category['name']})
            seen.add(category_id)
            if len(examples) == example_limit:
                break
        return {'categories': tuple(visible_categories), 'examples': tuple(examples)}

    def cheapest_available(self):
        """Return the global lowest effective snapshot price for a saleable item."""
        candidates = []
        for product in self._snapshot.tables['products']:
            item = self.product(product['product_id'])
            if not item or not item['available']:
                continue
            quote = item['price_quote']
            effective = quote.get('checkout_price_brl')
            if effective is None or quote.get('status') not in {'none_available', 'active_in_snapshot'}:
                continue
            candidates.append(item)
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item['price_quote']['checkout_price_brl'],
                                                 item['product_id']))

    def promotion(self, product_id):
        product = self.product(product_id)
        if product is None:
            return None
        quote = dict(product['price_quote'])
        if quote['status']=='unavailable':
            quote['status']='none_available'
        quote.update({'product_id':product_id, 'name':product['name'], 'available':product['available'],
                      'stock_quantity':product['stock_quantity']})
        return quote


class OrderService:
    """Fictional dataset lookup scoped to a server-matched customer ID."""
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def lookup(self, customer_id, *, order_id=None, include_catalog_comparison=False):
        if (type(customer_id) is not int or customer_id <= 0 or
                (order_id is not None and (type(order_id) is not int or order_id <= 0)) or
                not isinstance(include_catalog_comparison, bool)):
            return ()
        results = []
        products = {p['product_id']: p for p in self._snapshot.tables['products']}
        for order in self._snapshot.tables['orders']:
            if order['customer_id'] != customer_id or (order_id is not None and order['order_id'] != order_id):
                continue
            order_items = [i for i in self._snapshot.tables['order_items'] if i['order_id'] == order['order_id']]
            items = tuple({'product_id': i['product_id'], 'name': products[i['product_id']]['name'],
                           'quantity': i['quantity']} for i in order_items)
            result = {'order_id': order['order_id'], 'status': order['status'], 'total_brl': order['total_brl'],
                      'payment_method': order['payment_method'], 'tracking_code': order['tracking_code'] or None,
                      'estimated_delivery': order['estimated_delivery'] or None, 'delivered_at': None,
                      'payment_confirmed': None, 'items': items}
            if include_catalog_comparison:
                comparison_items = []
                catalog_total = Decimal('0')
                complete = True
                for item in order_items:
                    product = products[item['product_id']]
                    price = product.get('price_brl')
                    if not isinstance(price, Decimal) or not price.is_finite():
                        complete = False
                    else:
                        catalog_total += price * item['quantity']
                    comparison_items.append({'product_id': item['product_id'],
                                             'quantity': item['quantity'],
                                             'catalog_list_price_brl': price})
                catalog_total = catalog_total if complete else None
                result['catalog_comparison'] = {
                    'basis': 'catalog_list_price',
                    'items': tuple(comparison_items),
                    'catalog_total_brl': catalog_total,
                    'difference_brl': catalog_total - order['total_brl'] if catalog_total is not None else None,
                }
            results.append(result)
        return tuple(results)


# Approved demo decisions, not a reproduction of the restricted manual.
_POLICIES = {
    'shipping': {'summary':'Frete metropolitano: R$35 abaixo de R$500; gratuito a partir de R$500. Para Campo Grande, a previsão normal é de 1 a 3 dias úteis, sem garantia de data exata. Outros destinos requerem cotação; instrumentos pesados, como piano digital, podem exigir cotação especial, a confirmar com a loja.', 'local_delivery':{'destination':'Campo Grande/MS','minimum_business_days':1,'maximum_business_days':3,'date_guaranteed':False}, 'source':'ARTEFACT-11, decisão humana da demonstração; ARTEFACT-12 F04–F06 e F08'},
    'installments': {'summary':'Parcelas sem juros: piso de R$50 em 1–3x, R$80 em 4–6x e R$100 em 7–12x.', 'source':'ARTEFACT-11, decisão humana da demonstração'},
    'payment_mix': {'summary':'É permitido combinar Pix e cartão somente em compras acima de R$ 2.000,00; exatamente R$ 2.000,00 não se qualifica. A possibilidade acima do limite não define a composição específica do pagamento, que deve ser confirmada com a loja.', 'source':'ARTEFACT-12 E07'},
    'pix': {'summary':'Desconto de 5% no Pix não acumula com promoção.', 'source':'ARTEFACT-11, decisão humana da demonstração'},
    'defect': {'summary':'Troca comercial por defeito até 30 dias da compra; depois orientar garantia e fabricante, sem negar a garantia legal de 90 dias a partir do recebimento.', 'source':'ARTEFACT-11, decisão humana da demonstração'},
    'exchange': {'summary':'Troca comercial por defeito até 30 dias da compra, sujeita à avaliação. Depois, orientar o acionamento do fabricante e a possível intermediação da loja, sem negar a garantia legal de 90 dias a partir do recebimento. Troca por preferência segue o prazo e as condições do manual, depende da disponibilidade e pode envolver diferença de preço. Produtos personalizados podem ter restrição para troca por preferência. Venda final não elimina direitos por defeito nem a avaliação de garantia.', 'source':'ARTEFACT-11, decisão humana da demonstração; ARTEFACT-12 H07–H09'},
    'warranty': {'summary':'Há garantia legal de 90 dias a partir do recebimento e podem existir garantias adicionais do fabricante. O prazo específico da garantia contratual depende do certificado correspondente ao produto; sem essa evidência não afirmo dois anos de cobertura. O fim de um prazo comercial não elimina direitos legais aplicáveis. Danos por mau uso não têm cobertura automática; se a causa não estiver clara, o produto precisa de avaliação conforme as condições do fabricante.', 'source':'ARTEFACT-11, decisão humana da demonstração; ARTEFACT-12 H06 e H10'},
    'regret': {'summary':'Para compras online, é possível solicitar o arrependimento em até 7 dias corridos após o recebimento, conforme a regra aprovada para esta demonstração. Esse prazo é para solicitar orientação; não confirma que uma devolução foi aprovada ou iniciada. A data de recebimento e as condições do caso precisam ser confirmadas com a loja.', 'source':'ARTEFACT-12 H01 e interpretação aprovada pelo usuário nesta missão'},
    'hours': {'summary':'No sábado, expediente até 13h; domingos e feriados fechados. Demais horários não confirmados neste resumo.', 'source':'ARTEFACT-12, casos A03 e A04'},
    'contacts': {'summary':'Os dados de contato da demonstração são divergentes; confirme o canal oficial com a loja.', 'source':'Operator confirmation required'},
    'scope': {'summary':'A loja atende instrumentos musicais do catálogo e não comercializa acessórios como palhetas, cabos ou amplificadores. Previsão do tempo e pesquisa climática estão fora do escopo do atendimento.', 'source':'ARTEFACT-12 A05–A06'},
}


def get_policy(topic):
    policy = _POLICIES.get(topic)
    return dict(policy) if policy else None
