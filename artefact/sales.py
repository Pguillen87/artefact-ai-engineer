"""Consultative presentation; all commercial facts still come from tool output.

Few comparable choices, answer first, one useful follow-up. No invented
scarcity, social proof, customer intent or unsupported product benefits.
"""
from decimal import Decimal
import re

from .intent import normalize, mentioned_category_ids
from .rendering import render_results, _money

BUDGET_QUESTION = 'Qual faixa de preço você tem em mente?'


def resolve_budget_reply(text, previous, snapshot):
    """Only interpret a bare amount after this assistant explicitly asked it."""
    if not previous or not previous.awaiting_budget or not previous.commercial:
        return text
    ids = previous.commercial.scope.category_ids
    if len(ids) != 1:
        return text
    match = re.fullmatch(r'\s*(?:at[eé]\s*)?(?:R\$\s*)?([0-9][0-9.,]*)(?:\s*reais)?[.!]?\s*',text,re.I)
    if not match:
        return text
    category = next((row for row in snapshot.tables['categories'] if row['category_id'] in ids),None)
    if category is None:
        return text
    return 'Quero ' + category['name'] + ' até R$ ' + match.group(1)


def render_sales_results(results, text, snapshot):
    # Complex quotes, restrictions, mixed queries and policies retain their
    # exhaustive validated presentation. This profile only simplifies browsing.
    if (not results or any(entry['consulta'] not in {'search_products','recommend_products'} for entry in results)
            or not mentioned_category_ids(text, snapshot)):
        return render_results(results)
    products = [item for entry in results for item in entry['resultado']]
    if (not products or any(item.get('price_withheld') or item.get('installment_quote') or
                           not item.get('available') or not isinstance(item.get('price_quote'), dict) or
                           item['price_quote'].get('status') not in {'none_available','active_in_snapshot'}
                           for item in products)):
        return render_results(results)
    # A model-specific request should keep the full quote, not turn into a list.
    if any(normalize(item['name']) in normalize(text) for item in products):
        return render_results(results)
    unique = {item['product_id']:item for item in products}
    def price(item):
        value = item['price_quote']['checkout_price_brl']
        return Decimal(str(value))
    options = sorted(unique.values(), key=lambda item:(price(item), item['product_id']))[:3]
    lines = ['Alternativas disponíveis no catálogo da loja:' if any(entry['consulta']=='recommend_products' for entry in results)
             else 'Encontrei estas opções disponíveis no catálogo da loja:']
    for item in options:
        quote = item['price_quote']
        label = (' — de ' + _money(quote['price_brl']) + '; ' + format(Decimal(str(quote['discount_percent'])), 'f') + '% de desconto registrado'
                 if quote['status'] == 'active_in_snapshot' else '')
        if item.get('review_fields'):
            label += ' — detalhes técnicos a confirmar com o vendedor'
        if re.search(r'\bpix\b',normalize(text)) and quote.get('pix_price_brl') is not None:
            label += '; no Pix: ' + _money(quote['pix_price_brl'])
        lines.append(f"• {item['name']}: {_money(price(item))}{label}.")
    if len(unique) > len(options):
        lines.append('Separei três opções para facilitar a comparação.')
    lines.append('Preços e o estoque registrado vêm da base importada; confirme as condições atuais com o vendedor.')
    if any(item['price_quote']['status']=='active_in_snapshot' for item in options):
        lines[-1] += ' O desconto Pix não acumula com a promoção; a vigência atual não foi confirmada.'
    has_budget = bool(re.search(r'\b(?:ate|entre|orcamento|no maximo|abaixo de)\b|r\$',normalize(text)))
    lines.append('Qual desses modelos você quer conhecer melhor?' if has_budget else
                 BUDGET_QUESTION)
    return '\n\n'.join([lines[0], '\n'.join(lines[1:-2]), lines[-2], lines[-1]])
