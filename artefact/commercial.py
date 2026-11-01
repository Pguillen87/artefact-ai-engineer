"""Approved demonstration rules; callers supply a validated quote and destination."""
from decimal import Decimal, ROUND_HALF_UP


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


def pix_price(price: Decimal, promotion_applied: bool) -> Decimal:
    price = _amount(price)
    if not isinstance(promotion_applied, bool):
        raise ValueError('promotion status must be explicit')
    if promotion_applied:
        return price
    return (price * Decimal('0.95')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
