"""Small, per-chat operational references; never stores conversation transcripts."""
from dataclasses import dataclass
from decimal import Decimal
import re

from .commercial import (has_budget_context, has_order_context, money_mentions,
                         extract_installment_count, parse_installment_request,
                         InstallmentRequest)
from .intent import normalize


@dataclass(frozen=True)
class ConversationContext:
    amount: Decimal | None = None
    amount_role: str | None = None
    product_id: int | None = None
    product_name: str | None = None
    turn: int = 0
    origin: str = 'user'
    customer_id: int | None = None


_FOLLOWUP = re.compile(
    r'(?i)^\s*(?:e\s+)?(?:(?:quanto\s+(?:ficaria|fica|seria|d[aá])\s+)?)'
    r'(?:(?:esse|este|aquele)\s+valor\s+)?(?:em|a)\s+(?:\d{1,2}|um|uma|duas|dois|tr[eê]s|quatro|cinco|seis|sete|oito|nove|dez|onze|doze)\s*'
    r'(?:x|vezes|parcelas?)(?:\s+sem\s+juros)?\s*[?.!]*\s*$')
_PRODUCT_FOLLOWUP = re.compile(
    r'(?i)^\s*(?:e\s+)?(?:qual\s+)?(?:o\s+)?'
    r'(?:pre[cç]o|produto|promo[cç][aã]o|estoque|disponibilidade)'
    r'(?:\s+(?:dele|dela|desse|deste))?(?:\s+(?:em|a)\s+\d{1,2}\s*(?:x|vezes|parcelas?))?\s*[?.!]*\s*$')


def installment_followup(text: str, context: ConversationContext | None) -> InstallmentRequest | None:
    if not context or context.amount is None or context.amount_role != 'total' or not _FOLLOWUP.fullmatch(text):
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
    return bool(re.fullmatch(r'(?i)\s*(?:e\s+)?(?:em|a)\s+\d{1,2}\s*(?:x|vezes|parcelas?)\s*[?.!]*\s*', text))


def explicit_product(text: str, snapshot):
    """Resolve only a unique, complete catalog name explicitly present in the turn."""
    if snapshot is None:
        return None
    spoken = normalize(text)
    matches = [product for product in snapshot.tables['products']
               if re.search(r'(?<!\w)' + re.escape(normalize(product['name'])) + r'(?!\w)', spoken)]
    return matches[0] if len(matches) == 1 else None


def next_context(text: str, previous: ConversationContext | None, turn: int) -> ConversationContext:
    if previous and order_followup(text):
        return ConversationContext(turn=turn, customer_id=previous.customer_id)
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
                    else 'per_installment' if re.search(r'\bparcela\b', spoken) else 'total')
            return ConversationContext(amount, role, turn=turn)
    return ConversationContext(turn=turn)
