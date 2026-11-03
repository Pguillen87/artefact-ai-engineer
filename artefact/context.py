"""Small, per-chat operational references; never stores conversation transcripts."""
from dataclasses import dataclass
from decimal import Decimal
import re

from .commercial import (has_budget_context, has_explicit_total_intent, has_order_context, money_mentions,
                         extract_installment_count, parse_installment_request,
                         InstallmentRequest)
from .intent import normalize, product_reference
from .services import Catalog


@dataclass(frozen=True)
class ConversationContext:
    amount: Decimal | None = None
    amount_role: str | None = None
    product_id: int | None = None
    product_name: str | None = None
    turn: int = 0
    origin: str = 'user'
    customer_id: int | None = None
    policy_topic: str | None = None


@dataclass(frozen=True)
class CommercialBasis:
    kind: str
    amount: Decimal | None = None
    product: dict | None = None
    catalog_amount: Decimal | None = None
    amount_role: str | None = None
    total_amount: Decimal | None = None


def confirms_catalog_price(text: str) -> bool:
    """Only explicit affirmative confirmation within a single clause grants authority."""
    for clause in re.split(r'\.(?!\d)|[!?;]', normalize(text)):
        if not re.search(r'\bcatalogo\b', clause):
            continue
        clause = re.sub(r'\bsem juros\b', '', clause)
        if re.search(r'\b(?:nao|nunca|sem|evite|proibido|se|caso|talvez|incorret\w*|errad\w*|fals\w*)\b', clause):
            continue
        if re.search(r'\b(?:confirme|confirmo|confirmar|utilize|usar|use)\b', clause):
            return True
    return False


def resolve_commercial_basis(text: str, snapshot) -> CommercialBasis:
    """Choose a price source independently of the provider's proposed tool."""
    spoken = normalize(text)
    amounts = money_mentions(text)
    amount = amounts[0] if len(amounts) == 1 else None
    per_installment = bool(re.search(r'\b(?:cada|por)\s+parcela\b|\b\d{1,2}\s*(?:x|vezes|parcelas?)\s+de\s*r\$', spoken))
    parsed = parse_installment_request(text)
    total = parsed.total_amount if parsed else None
    role = ('per_installment' if per_installment else 'total' if amount is not None and has_explicit_total_intent(text)
            else 'ambiguous' if amount is not None else None)
    if has_budget_context(text):
        if len(amounts) > 1:
            return CommercialBasis('ambiguous', amount_role='ambiguous')
        return CommercialBasis('budget', amount, amount_role='budget_cap')
    products = []
    exact_products = []
    product_mentioned = False
    if snapshot is not None:
        for product in snapshot.tables['products']:
            name = normalize(product['name'])
            first = name.split()[0]
            exact = re.search(r'(?<!\w)' + re.escape(name) + r'(?!\w)', spoken)
            mentioned = exact or (len(first) >= 3 and re.search(r'(?<!\w)' + re.escape(first) + r'(?!\w)', spoken))
            if mentioned:
                product_mentioned = True
                if product_reference(text, product['name']) != 'rejected':
                    products.append(product)
                    if exact:
                        exact_products.append(product)
    if exact_products:
        products = exact_products
    product = products[0] if len(products) == 1 else None
    catalog = Catalog(snapshot).product(product['product_id']) if product is not None else None
    catalog_amount = catalog['price_quote'].get('checkout_price_brl') if catalog else None
    hypothetical = any(
        re.search(r'\bsimul\w*\b', clause) and re.search(r'\bhipotetic\w*\b', clause)
        and not re.search(r'\bnao\s+(?:quero\s+)?simul\w*\b', clause)
        and not re.search(r'\b(?:nao|nunca|sem)\b.{0,60}\bhipotetic\w*\b', clause)
        for clause in re.split(r'\.(?!\d)|[!?;]', spoken))
    if hypothetical and amount is not None:
        return CommercialBasis('hypothetical', amount, product, catalog_amount, role, total)
    if product_mentioned and not products and amounts:
        return CommercialBasis('ambiguous', amount, amount_role=role, total_amount=total)
    if products and amounts:
        comparison_amount = total if per_installment else amount
        if product is None or comparison_amount is None or catalog_amount is None or comparison_amount != catalog_amount:
            return CommercialBasis('catalog' if product and confirms_catalog_price(text) else 'ambiguous',
                                   amount, product, catalog_amount, role, total)
        return CommercialBasis('catalog', amount, product, catalog_amount, role, total)
    if product is not None:
        return CommercialBasis('catalog', None, product, catalog_amount)
    if amount is not None and has_explicit_total_intent(text):
        return CommercialBasis('total', amount, amount_role=role, total_amount=total)
    return CommercialBasis('none', amount, amount_role=role, total_amount=total)


_FOLLOWUP = re.compile(
    r'(?i)^\s*(?:e\s+)?(?:(?:quanto\s+(?:ficaria|fica|seria|d[aá])\s+)?)'
    r'(?:(?:esse|este|aquele)\s+valor\s+)?(?:em|a)\s+(?:\d{1,2}|um|uma|duas|dois|tr[eê]s|quatro|cinco|seis|sete|oito|nove|dez|onze|doze)\s*'
    r'(?:x|vezes|parcelas?)(?:\s+sem\s+juros)?\s*[?.!]*\s*$')
_PRODUCT_FOLLOWUP = re.compile(
    r'(?i)^\s*(?:e\s+)?(?:qual\s+)?(?:o\s+)?'
    r'(?:pre[cç]o|produto|promo[cç][aã]o|estoque|disponibilidade)'
    r'(?:\s+(?:dele|dela|desse|deste))?(?:\s+(?:em|a)\s+\d{1,2}\s*(?:x|vezes|parcelas?))?\s*[?.!]*\s*$')


def installment_followup(text: str, context: ConversationContext | None) -> InstallmentRequest | None:
    if not context or context.amount is None or not _FOLLOWUP.fullmatch(text):
        return None
    if context.amount_role not in {'total', 'hypothetical'} and not (context.amount_role == 'stated_price' and re.search(r'(?i)\b(?:esse|este|aquele)\s+valor\b', text)):
        return None
    count = extract_installment_count(text)
    if count is None:
        names = {'um': 1, 'uma': 1, 'dois': 2, 'duas': 2, 'tres': 3, 'quatro': 4,
                 'cinco': 5, 'seis': 6, 'sete': 7, 'oito': 8, 'nove': 9,
                 'dez': 10, 'onze': 11, 'doze': 12}
        count = next((value for word, value in names.items()
                      if re.search(r'\b' + word + r'\b', normalize(text))), None)
    if count is None:
        return None
    return InstallmentRequest(context.amount, context.amount, None, count)


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
    return bool(re.fullmatch(r'(?i)\s*(?:e\s+)?(?:(?:quanto\s+(?:fica|ficaria|seria)\s+)?(?:esse|este|aquele)\s+produto\s+)?(?:em|a)\s+\d{1,2}\s*(?:x|vezes|parcelas?)\s*[?.!]*\s*', text))


def pix_policy_followup(text: str) -> bool:
    spoken = normalize(text)
    return bool(re.search(r'\b(?:posso|pode|podemos)\s+usar\s+(?:os\s+dois|ambos\s+os)\s+descontos?\s*[?.!]*\s*$', spoken))


def explicit_order_id(text: str) -> int | None:
    spoken = normalize(text)
    matches = re.findall(r'\bpedido(?:\s+(?:numero|n[ºo.]?))?\s+(\d+)\b', spoken)
    unique = set(matches)
    return int(next(iter(unique))) if len(unique) == 1 else None


def explicit_product(text: str, snapshot):
    """Resolve only a unique, complete catalog name explicitly present in the turn."""
    if snapshot is None:
        return None
    spoken = normalize(text)
    exact = [product for product in snapshot.tables['products']
             if re.search(r'(?<!\w)' + re.escape(normalize(product['name'])) + r'(?!\w)', spoken)]
    candidates = exact if exact else snapshot.tables['products']
    matches = [product for product in candidates if product_reference(text, product['name']) == 'affirmative']
    return matches[0] if len(matches) == 1 else None


def product_amount_requires_clarification(text: str, snapshot) -> bool:
    """A mentioned product does not establish that a stated amount is its price."""
    return resolve_commercial_basis(text, snapshot).kind == 'ambiguous'


def next_context(text: str, previous: ConversationContext | None, turn: int) -> ConversationContext:
    if previous and order_followup(text):
        return ConversationContext(turn=turn, customer_id=previous.customer_id)
    if previous and previous.policy_topic == 'pix' and pix_policy_followup(text):
        return ConversationContext(turn=turn, policy_topic='pix')
    if previous and ((product_followup(text) and previous.product_id is not None) or
                     (_FOLLOWUP.fullmatch(text) and (previous.amount is not None or previous.product_id is not None))):
        return ConversationContext(previous.amount, previous.amount_role, previous.product_id,
                                   previous.product_name, turn, previous.origin)
    request = parse_installment_request(text)
    if request:
        role = 'per_installment' if request.per_installment_amount is not None else 'total'
        return ConversationContext(request.total_amount, role, turn=turn)
    if has_order_context(text) or re.search(r'(?i)\b(?:frete|envio|entrega)\b', text):
        return ConversationContext(turn=turn)
    if re.search(r'(?i)\b(?:ou|versus|vs)\b', text) or re.search(r'(?i)R\$\s*[0-9][0-9.,]*\s+e\s+(?:R\$\s*)?[0-9]', text):
        return ConversationContext(turn=turn)
    amounts = money_mentions(text)
    if len(amounts) == 1:
        amount = amounts[0]
        if amount is not None:
            spoken = normalize(text)
            role = ('budget_cap' if has_budget_context(text)
                    else 'per_installment' if re.search(r'\bparcela\b', spoken)
                    else 'total' if has_explicit_total_intent(text)
                    else 'stated_price' if re.search(r'(?i)\binstrumento\s+de\s+r\$', text) else 'unknown')
            return ConversationContext(amount, role, turn=turn)
    return ConversationContext(turn=turn)
