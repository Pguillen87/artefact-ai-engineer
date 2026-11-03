"""Small, per-chat operational references; never stores conversation transcripts."""
from __future__ import annotations
from dataclasses import dataclass, replace
from decimal import Decimal
import re
from typing import Literal

from .commercial import (has_budget_context, has_explicit_total_intent, has_order_context, money_mentions,
                         extract_installment_count, parse_installment_request,
                         InstallmentRequest)
from .intent import normalize, product_reference, mentioned_products, intent_clauses, clause_polarity
from .services import Catalog


@dataclass(frozen=True)
class ConversationContext:
    product_id: int | None = None
    product_name: str | None = None
    turn: int = 0
    origin: str = 'user'
    customer_id: int | None = None
    policy_topic: str | None = None
    commercial: CommercialBasis | None = None


@dataclass(frozen=True)
class MonetaryValue:
    amount: Decimal | None
    role: Literal['budget', 'hypothetical_total', 'declared_total', 'claimed_product_price',
                  'shipping_amount', 'per_installment', 'stated_price', 'ambiguous']


@dataclass(frozen=True)
class CommercialBasis:
    kind: Literal['catalog', 'hypothetical', 'total', 'budget', 'ambiguous', 'restricted', 'none']
    amount: Decimal | None = None
    product: dict | None = None
    catalog_amount: Decimal | None = None
    amount_role: str | None = None
    total_amount: Decimal | None = None
    count: int | None = None
    restriction: str | None = None
    values: tuple[MonetaryValue, ...] = ()
    source: str = 'user'

    @property
    def price_source(self) -> str | None:
        if self.kind == 'catalog':
            return 'catalog_snapshot'
        if self.kind in {'hypothetical', 'total'}:
            return 'explicit_user_total'
        return None

    @property
    def allows_catalog_options(self) -> bool:
        """A budget filters catalog prices; it never becomes their quoted total."""
        return self.kind == 'budget' and self.restriction is None and self.count is not None

    @property
    def quote_allowed(self) -> bool:
        return (self.restriction is None and self.count is not None and
                self.kind in {'catalog', 'hypothetical', 'total'} and
                self.quote_amount is not None)

    @property
    def quote_amount(self) -> Decimal | None:
        return self.catalog_amount if self.kind == 'catalog' else self.total_amount

    def request(self) -> InstallmentRequest | None:
        if not self.quote_allowed:
            return None
        stated = self.amount if self.amount_role == 'per_installment' else self.quote_amount
        return InstallmentRequest(stated, self.quote_amount,
                                  self.amount if self.amount_role == 'per_installment' else None, self.count)

    def mismatch(self) -> dict | None:
        compared = self.total_amount if self.amount_role == 'per_installment' else self.amount
        if (self.product is None or compared is None or self.catalog_amount == compared
                or self.kind in {'hypothetical', 'budget'} or self.amount_role == 'budget_cap'):
            return None
        return {'product_name': self.product['name'], 'reported_amount_brl': compared,
                'reported_per_installment': self.amount_role == 'per_installment',
                'catalog_amount_brl': self.catalog_amount,
                'catalog_quote_authorized': self.kind == 'catalog' and self.restriction is None}


def confirms_catalog_price(text: str) -> bool:
    """Only explicit affirmative confirmation within a single clause grants authority."""
    for clause in intent_clauses(text):
        if not re.search(r'\bcatalogo\b', clause):
            continue
        if clause_polarity(clause) != 'affirmative' or re.search(r'\b(?:incorret\w*|errad\w*|fals\w*)\b', clause):
            continue
        if re.search(r'\b(?:confirme|confirmo|confirmar|utilize|usar|use)\b', clause):
            return True
    return False


def resolve_commercial_basis(text: str, snapshot, previous: ConversationContext | None = None) -> CommercialBasis:
    """Resolve source, meaning and restrictions once, before any tool is selected.

    Unknown language remains unquotable. Context is only inherited by bounded
    follow-ups; an explicit new product request is evaluated on its own merits.
    """
    products = mentioned_products(text, snapshot)
    count_text = normalize(text)
    for product in products:
        count_text = count_text.replace(normalize(product['name']), '')
    count = extract_installment_count(count_text)
    if previous and previous.commercial and (_FOLLOWUP.fullmatch(text) or product_followup(text) or product_installment_followup(text)):
        prior = previous.commercial
        if prior.restriction is None and prior.amount_role == 'stated_price' and re.search(r'\b(?:esse|este|aquele) valor\b', normalize(text)):
            return replace(prior, kind='total', total_amount=prior.amount, count=count, source='session')
        if prior.amount_role == 'per_installment' and prior.kind != 'hypothetical':
            return CommercialBasis('ambiguous', count=count, restriction='ambiguous_reference', source='session')
        catalog = Catalog(snapshot).product(prior.product['product_id']) if prior.product else None
        return replace(prior, count=count,
                       amount=prior.total_amount if prior.amount_role == 'per_installment' else prior.amount,
                       amount_role='total' if prior.amount_role == 'per_installment' else prior.amount_role,
                       catalog_amount=catalog['price_quote'].get('checkout_price_brl') if catalog else None,
                       source='session')

    clauses = intent_clauses(text)
    candidates = [p for p in products if product_reference(text, p['name']) != 'rejected']
    product = candidates[0] if len(candidates) == 1 else products[0] if len(products) == 1 else None
    catalog = Catalog(snapshot).product(product['product_id']) if product else None
    catalog_amount = catalog['price_quote'].get('checkout_price_brl') if catalog else None
    values = []
    financial = []
    hypothetical = False
    restriction = None
    for clause in clauses:
        polarity = clause_polarity(clause)
        clause_products = mentioned_products(clause, snapshot)
        shipping = bool(re.search(r'\b(?:frete|envio|entrega)\b', clause))
        # Scope monetary roles to their clause. Independent shipping amounts
        # never become product totals; inseparable mixed clauses stay ambiguous.
        mixed = shipping and bool(clause_products)
        is_hypothesis = (re.search(r'\bsimul\w*\b', clause) and
                         re.search(r'\bhipotetic\w*\b|\b(?:apenas|somente) como exemplo\b', clause) and
                         polarity == 'affirmative')
        amounts = money_mentions(clause)
        hypothetical = hypothetical or bool(is_hypothesis and amounts and not shipping)
        for amount in amounts:
            role = ('ambiguous' if mixed else 'shipping_amount' if shipping else
                    'budget' if has_budget_context(clause) else
                    'hypothetical_total' if is_hypothesis else
                    'claimed_product_price' if products else
                    'declared_total' if has_explicit_total_intent(clause) else
                    'stated_price' if re.search(r'\binstrumento de r\$', clause) else 'ambiguous')
            per_part = bool(re.search(r'\b(?:cada|por)\s+parcela\b|\b\d{1,2}\s*(?:x|vezes|parcelas?)\s+de\s*r\$', clause))
            if per_part and role not in {'budget', 'shipping_amount'}:
                role = 'per_installment'
            value = MonetaryValue(amount, role)
            values.append(value)
            if role != 'shipping_amount':
                financial.append(value)
        concerns_product = not clause_products or product is None or any(p['product_id'] == product['product_id'] for p in clause_products)
        commercial_clause = concerns_product and (
            bool(re.search(r'\b(?:catalogo|preco|simul\w*|parcel\w*)\b', clause)) or
            (bool(clause_products or amounts) and not shipping))
        if commercial_clause:
            if polarity in {'negative', 'conditional', 'incidental'}:
                restriction = polarity
            elif re.search(r'\b(?:incorret\w*|errad\w*|fals\w*)\b', clause):
                restriction = 'disputed_price'

    relevant = [value for value in financial if value.role != 'budget']
    budgets = [value for value in financial if value.role == 'budget']
    value = relevant[0] if len(relevant) == 1 else budgets[0] if not relevant and len(budgets) == 1 else None
    amount = value.amount if value else None
    role = value.role if value else 'ambiguous' if financial else None
    per_part = role == 'per_installment'
    total = amount * count if amount is not None and per_part and count else amount
    common = dict(amount=amount, product=product, catalog_amount=catalog_amount,
                  amount_role='per_installment' if per_part else 'total' if role == 'declared_total' else
                              'budget_cap' if role == 'budget' else 'stated_price' if role == 'stated_price' else 'ambiguous' if amount is not None else None,
                  total_amount=total, count=count, restriction=restriction, values=tuple(values))
    if restriction:
        return CommercialBasis('ambiguous' if amount is not None else 'restricted', **common)
    if hypothetical and amount is not None and len(relevant) == 1:
        return CommercialBasis('hypothetical', **common)
    if budgets and not relevant:
        return CommercialBasis('catalog' if product else 'budget', **common)
    if products:
        if product is None:
            return CommercialBasis('ambiguous', **common)
        if financial and (len(relevant) != 1 or amount is None or total != catalog_amount):
            return CommercialBasis('catalog' if confirms_catalog_price(text) else 'ambiguous', **common)
        if product_reference(text, product['name']) not in {'affirmative', 'mentioned'}:
            return CommercialBasis('ambiguous', **common)
        if product_reference(text, product['name']) == 'mentioned' and count is None:
            return CommercialBasis('none', **common)
        return CommercialBasis('catalog', **common)
    if amount is not None and role in {'declared_total', 'per_installment'} and not has_order_context(text):
        # Reuse the existing lexical request contract, rather than introducing a
        # permissive second parser for standalone monetary simulations.
        request = parse_installment_request(text)
        if request is not None or (count is None and role == 'declared_total'):
            return CommercialBasis('total', **common)
    return CommercialBasis('ambiguous' if financial else 'none', **common)


_FOLLOWUP = re.compile(
    r'(?i)^\s*(?:e\s+)?(?:(?:quanto\s+(?:ficaria|fica|seria|d[aá])\s+)?)'
    r'(?:(?:esse|este|aquele)\s+valor\s+)?(?:em|a)\s+(?:\d{1,2}|um|uma|duas|dois|tr[eê]s|quatro|cinco|seis|sete|oito|nove|dez|onze|doze)\s*'
    r'(?:x|vezes|parcelas?)(?:\s+sem\s+juros)?\s*[?.!]*\s*$')
_PRODUCT_FOLLOWUP = re.compile(
    r'(?i)^\s*(?:e\s+)?(?:qual\s+)?(?:o\s+)?'
    r'(?:pre[cç]o|produto|promo[cç][aã]o|estoque|disponibilidade)'
    r'(?:\s+(?:dele|dela|desse|deste))?(?:\s+(?:em|a)\s+\d{1,2}\s*(?:x|vezes|parcelas?))?\s*[?.!]*\s*$')


def product_followup(text: str) -> bool:
    return bool(_PRODUCT_FOLLOWUP.fullmatch(text))


def order_followup(text: str) -> bool:
    spoken = normalize(text)
    return bool(re.search(r'\bmeus?\s+pedidos?\b', spoken) and
                not re.search(r'\b(?:meu nome|me chamo|sou|outro cliente)\b', spoken))


def name_in_text(text: str, name: str) -> bool:
    suffix = r'(?=$|[.,!?;:]|\s+(?:e|pedido|pedidos|meu|meus|quero|qual|veja|por favor|pode)\b)'
    spoken = normalize(text)
    if re.search(r'\b(?:nao sou|nao me chamo|outro cliente)\b', spoken):
        return False
    return bool(re.search(r'(?<!\w)' + re.escape(normalize(name)) + suffix, spoken))


def explicit_customer(text: str, snapshot) -> int | None:
    """Store only an ID for an explicit unique self-identification in fictional data."""
    if snapshot is None:
        return None
    spoken = normalize(text)
    if re.search(r'\b(?:nao sou|nao me chamo|outro cliente)\b', spoken):
        return None
    matches = []
    for customer in snapshot.tables['customers']:
        name = normalize(customer['name'])
        suffix = r'(?=$|[.,!?;:]|\s+(?:e|pedido|pedidos|meu|meus|quero|qual|veja|por favor|pode)\b)'
        if len(name.split()) >= 2 and re.search(r'\b(?:meu nome(?: completo)? (?:e|eh)|me chamo|sou)\s+' + re.escape(name) + suffix, spoken):
            matches.append(customer['customer_id'])
    return matches[0] if len(matches) == 1 else None


def product_installment_followup(text: str) -> bool:
    shortened = re.sub(r'\b(?:(?:esse|este|aquele)\s+)?produto\s+', '', normalize(text))
    return bool(_FOLLOWUP.fullmatch(shortened))


def pix_policy_followup(text: str) -> bool:
    spoken = normalize(text)
    return bool(re.search(r'\b(?:posso|pode|podemos)\s+usar\s+(?:os\s+dois|ambos\s+os)\s+descontos?\s*[?.!]*\s*$', spoken))


def explicit_order_id(text: str) -> int | None:
    spoken = normalize(text)
    matches = re.findall(r'\bpedido(?:\s+(?:numero|n[ºo.]?))?\s+(\d+)\b', spoken)
    unique = set(matches)
    return int(next(iter(unique))) if len(unique) == 1 else None


def explicit_product(text: str, snapshot):
    """Resolve a unique affirmative catalog reference, including abbreviations."""
    if snapshot is None:
        return None
    matches = [product for product in mentioned_products(text, snapshot)
               if product_reference(text, product['name']) == 'affirmative']
    return matches[0] if len(matches) == 1 else None


def next_context(text: str, previous: ConversationContext | None, turn: int) -> ConversationContext:
    """Retain only bounded references; commercial meaning belongs to the resolver."""
    if previous and order_followup(text):
        return ConversationContext(turn=turn, customer_id=previous.customer_id)
    if previous and previous.policy_topic == 'pix' and pix_policy_followup(text):
        return ConversationContext(turn=turn, policy_topic='pix')
    if previous and (_FOLLOWUP.fullmatch(text) or product_followup(text) or product_installment_followup(text)):
        return replace(previous, turn=turn)
    return ConversationContext(turn=turn)
