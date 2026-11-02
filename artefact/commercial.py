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
_INSTALLMENT_COUNT = re.compile(r'(?<![\w.,])([0-9]{1,2})\s*(?:x\b|vezes\b|parcelas?\b)', re.I)
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


def extract_installment_count(text: str) -> int | None:
    if not isinstance(text, str) or _ORDER_TOTAL_CONTEXT.search(text):
        return None
    matches = _INSTALLMENT_COUNT.findall(text)
    if len(matches) != 1:
        return None
    count = int(matches[0])
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
    count = extract_installment_count(text)
    money = list(_MONEY.finditer(text))
    if count is None or len(money) != 1:
        return None
    match = money[0]
    nearby = text[max(0, match.start() - 30):min(len(text), match.end() + 30)]
    if re.search(r'(?i)frete|envio|entrega', nearby) or _PER_INSTALLMENT_CONTEXT.search(nearby):
        return None
    token = match.group(1)
    if token.endswith('.') and not token.endswith('..'):
        token = token[:-1]
    amount = _parse_brl_token(token)
    if amount is None:
        return None
    before = text[:match.start()]
    per_part = bool(re.search(r'(?i)(?<![\w.,])\d{1,2}\s*(?:x|vezes|parcelas?)\s+de\s*$', before))
    if not per_part and not re.search(r'(?i)\b(?:em|parcelar|parcelamento|dividir|pagar)\b', text):
        return None
    return InstallmentRequest(amount, amount * count if per_part else amount,
                              amount if per_part else None, count)


def extract_installment_request(text: str) -> tuple[Decimal, int] | None:
    request = parse_installment_request(text)
    return (request.total_amount, request.count) if request else None
