"""Approved demonstration rules and deterministic commercial calculations."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re


def _amount(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError('amount must be a finite nonnegative Decimal')
    return value


def shipping_quote(total: Decimal, metropolitan_area: bool) -> Decimal | None:
    total = _amount(total)
    if not isinstance(metropolitan_area, bool):
        raise ValueError('destination must be confirmed')
    if not metropolitan_area:
        return None
    return Decimal('35') if total < Decimal('500') else Decimal('0')


def installments(total: Decimal) -> tuple[int, ...]:
    total = _amount(total)
    return tuple(n for n in range(1, 13)
                 if total >= n * (Decimal('50') if n <= 3 else Decimal('80') if n <= 6 else Decimal('100')))


@dataclass(frozen=True)
class InstallmentQuote:
    amount: Decimal
    count: int
    installments: tuple[Decimal, ...]
    minimum_per_installment: Decimal
    eligible: bool
    eligible_counts: tuple[int, ...]


def quote_installments(total: Decimal, count: int) -> InstallmentQuote:
    """Split a stated amount into exact cents and check the approved per-part floor."""
    total = _amount(total)
    if total != total.quantize(Decimal('0.01')):
        raise ValueError('installment amount must be expressed in cents')
    if type(count) is not int or not 1 <= count <= 12:
        raise ValueError('installment count must be between 1 and 12')
    cents = int(total * 100)
    quotient, remainder = divmod(cents, count)
    parts = tuple(Decimal(quotient + (index < remainder)) / Decimal(100) for index in range(count))
    minimum = Decimal('50') if count <= 3 else Decimal('80') if count <= 6 else Decimal('100')
    eligible_counts = installments(total)
    return InstallmentQuote(total, count, parts, minimum, count in eligible_counts, eligible_counts)


def pix_price(price: Decimal, promotion_applied: bool) -> Decimal:
    price = _amount(price)
    if not isinstance(promotion_applied, bool):
        raise ValueError('promotion status must be explicit')
    if promotion_applied:
        return price
    return (price * Decimal('0.95')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def promotion_price(price: Decimal, discount_percent: Decimal) -> Decimal:
    price = _amount(price)
    if not isinstance(discount_percent, Decimal) or not discount_percent.is_finite() or not Decimal('0') <= discount_percent <= Decimal('100'):
        raise ValueError('discount must be a finite Decimal between zero and one hundred')
    return (price * (Decimal('100') - discount_percent) / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


_MONEY = re.compile(r'(?i)R\$\s*([0-9][0-9.,]*)')
_COUNT_WORDS = {'um': 1, 'uma': 1, 'dois': 2, 'duas': 2, 'três': 3, 'tres': 3, 'quatro': 4, 'cinco': 5, 'seis': 6, 'sete': 7, 'oito': 8, 'nove': 9, 'dez': 10, 'onze': 11, 'doze': 12}
_COUNT_PATTERN = r'(?:[0-9]{1,2}|um|uma|dois|duas|tr[eê]s|quatro|cinco|seis|sete|oito|nove|dez|onze|doze)'
_INSTALLMENT_COUNT = re.compile(r'(?<![\w.,])(' + _COUNT_PATTERN + r')\s*(?:x\b|vezes\b|parcelas?\b)', re.I)
_ORDER_TOTAL_CONTEXT = re.compile(r'\b(?:pedido|total\s+hist[oó]rico|total\s+registrado|valor\s+do\s+pedido)\b', re.I)
_PER_INSTALLMENT_CONTEXT = re.compile(r'(?i)(?:por\s+parcela|cada\s+parcela|piso\s+de|m[ií]nimo\s+de|parcela\s+m[ií]nima)')


def _parse_brl_token(value: str) -> Decimal | None:
    if not value or not value[0].isdigit():
        return None
    if '.' in value and ',' in value:
        decimal_separator = ',' if value.rfind(',') > value.rfind('.') else '.'
        grouping_separator = '.' if decimal_separator == ',' else ','
        integer, fraction = value.rsplit(decimal_separator, 1)
        groups = integer.split(grouping_separator)
        if not 1 <= len(fraction) <= 2 or not groups[0].isdigit() or not 1 <= len(groups[0]) <= 3:
            return None
        if any(not group.isdigit() or len(group) != 3 for group in groups[1:]):
            return None
        normalized = ''.join(groups) + '.' + fraction
    elif '.' in value or ',' in value:
        separator = '.' if '.' in value else ','
        parts = value.split(separator)
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            integer, fraction = parts
            if not integer.isdigit() or not fraction.isdigit():
                return None
            normalized = integer + '.' + fraction
        elif len(parts) >= 2 and 1 <= len(parts[0]) <= 3 and parts[0].isdigit() and all(p.isdigit() and len(p) == 3 for p in parts[1:]):
            normalized = ''.join(parts)
        else:
            return None
    else:
        if not value.isdigit():
            return None
        normalized = value
    try:
        result = Decimal(normalized)
    except InvalidOperation:
        return None
    if not result.is_finite() or result < 0 or result != result.quantize(Decimal('0.01')):
        return None
    return result


def parse_brl_mention(value: str) -> Decimal | None:
    """Remove sentence punctuation only when the preceding numeric token is complete."""
    if value.endswith('.') and not value.endswith('..'):
        value = value[:-1]
    elif re.search(r'[.,]\d{2,3},$', value):
        value = value[:-1]
    return _parse_brl_token(value)


def money_mentions(text: str) -> tuple[Decimal | None, ...]:
    if not isinstance(text, str):
        return ()
    return tuple(parse_brl_mention(match.group(1)) for match in _MONEY.finditer(text))


def has_order_context(text: str) -> bool:
    return bool(_ORDER_TOTAL_CONTEXT.search(text))


def has_budget_context(text: str) -> bool:
    return bool(re.search(r'(?i)\b(?:orçamento|orcamento|para gastar|posso gastar|limite|teto|no m[aá]ximo|menos de|abaixo de)\b|\bat[eé]\s+r\$', text))


def has_explicit_total_intent(text: str) -> bool:
    """Require positive authority for treating an amount as a total or simulation."""
    if has_budget_context(text):
        return False
    if re.search(r'(?i)\bn[aã]o\s+(?:quero\s+(?:mais\s+)?)?simul\w*\b', text):
        return False
    return bool(re.search(r'(?i)\b(?:simul\w*|parcelar|parcele|pagar|pague)\s+(?:(?:um|o)\s+)?(?:total\s+de\s+|valor\s+de\s+)?r\$|\bcompra\s+(?:ficou|custou|custa|[eé])\s+(?:em\s+)?r\$|\btotal\s+(?:da\s+compra\s+)?(?:[eé]|de|ficou)\s+(?:em\s+)?r\$|\bsimula[cç][aã]o\s+(?:puramente\s+)?hipot[eé]tica\s+(?:de|com)\s+r\$|\bsimul\w*\s+hipoteticamente\s+r\$', text))


def extract_installment_count(text: str) -> int | None:
    if not isinstance(text, str) or _ORDER_TOTAL_CONTEXT.search(text):
        return None
    matches = _INSTALLMENT_COUNT.findall(text)
    if len(matches) != 1:
        return None
    raw = matches[0].casefold()
    count = int(raw) if raw.isdigit() else _COUNT_WORDS.get(raw, 0)
    return count if 1 <= count <= 12 else None


@dataclass(frozen=True)
class InstallmentRequest:
    stated_amount: Decimal
    total_amount: Decimal
    per_installment_amount: Decimal | None
    count: int


def parse_installment_request(text: str) -> InstallmentRequest | None:
    """Read one explicit BRL amount and one count; refuse order-history or ambiguous amounts."""
    if not isinstance(text, str) or _ORDER_TOTAL_CONTEXT.search(text):
        return None
    if has_budget_context(text):
        return None
    if not has_explicit_total_intent(text) and not re.search(r'(?i)\d{1,2}\s*(?:x|vezes|parcelas?)\s+de\s*r\$', text):
        return None
    if re.search(r'(?i)\b(?:ou|versus|vs)\b', text) or re.search(r'(?i)R\$\s*[0-9][0-9.,]*\s+e\s+(?:R\$\s*)?[0-9]', text):
        return None
    count = extract_installment_count(text)
    money = list(_MONEY.finditer(text))
    if count is None or len(money) != 1:
        return None
    match = money[0]
    nearby = text[max(0, match.start() - 30):min(len(text), match.end() + 30)]
    if re.search(r'(?i)frete|envio|entrega', nearby) or _PER_INSTALLMENT_CONTEXT.search(nearby):
        return None
    amount = parse_brl_mention(match.group(1))
    if amount is None:
        return None
    before = text[:match.start()]
    per_part = bool(re.search(r'(?i)(?<![\w.,])\d{1,2}\s*(?:x|vezes|parcelas?)\s+de\s*$', before))
    count_match = _INSTALLMENT_COUNT.search(text)
    if not per_part and count_match.start() < match.start():
        return None
    if not per_part and not re.search(r'(?i)\b(?:em|a)\s*' + _COUNT_PATTERN + r'\s*(?:x\b|vezes\b|parcelas?\b)', text):
        return None
    return InstallmentRequest(amount, amount * count if per_part else amount,
                              amount if per_part else None, count)


def extract_installment_request(text: str) -> tuple[Decimal, int] | None:
    request = parse_installment_request(text)
    return (request.total_amount, request.count) if request else None
