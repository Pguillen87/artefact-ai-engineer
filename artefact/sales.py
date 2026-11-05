"""Consultative presentation; all commercial facts still come from tool output.

Few comparable choices, answer first, one useful follow-up. No invented
scarcity, social proof, customer intent or unsupported product benefits.
"""
from decimal import Decimal
from dataclasses import replace
import re

from .context import (PendingCatalogSearch, pending_catalog_followup,
                      complete_catalog_list_request, more_catalog_list_request)
from .intent import normalize
from .commercial import parse_brl_mention
from .chat_limits import MAX_ASSISTANT_ANSWER_CHARS
from .rendering import render_results, _money
from .services import Catalog, MAX_SEARCH_RESULTS

BUDGET_QUESTION = 'Qual faixa de preço você tem em mente?'
PRODUCT_PAGE_SIZE = 5


def _product_list(results):
    if not results or any(entry.get('consulta') not in {'search_products', 'recommend_products'} for entry in results):
        return ()
    products = {}
    for entry in results:
        for item in entry.get('resultado', ()):
            if isinstance(item, dict) and type(item.get('product_id')) is int:
                products[item['product_id']] = item
    return tuple(products.values())


def _simple_listing(products, text):
    return bool(products) and not any(
        item.get('price_withheld') or item.get('installment_quote') or not item.get('available') or
        not isinstance(item.get('price_quote'), dict) or
        item['price_quote'].get('status') not in {'none_available', 'active_in_snapshot'}
        for item in products
    ) and not any(normalize(item['name']) in normalize(text) for item in products)


def displayed_product_ids(results, text):
    """Return the same first five IDs the customer sees, in display order."""
    products = _product_list(results)
    mentioned = normalize(text)
    explicitly_named = sum(normalize(item['name']) in mentioned for item in products)
    if explicitly_named < 2:
        products = tuple(sorted(products, key=_effective_price_key))
    limit = len(products) if complete_catalog_list_request(text) else PRODUCT_PAGE_SIZE
    return tuple(item['product_id'] for item in products[:limit])


def _effective_price_key(item):
    quote = item.get('price_quote')
    value = quote.get('checkout_price_brl') if isinstance(quote, dict) else None
    amount = Decimal(str(value)) if value is not None else Decimal('Infinity')
    return amount, item['product_id']


def pending_from_plan(plan, decision, previous=None):
    args = plan.args
    def price(field):
        value = args.get(field)
        return parse_brl_mention(value) if isinstance(value, str) else None
    retained = previous if isinstance(previous, PendingCatalogSearch) else None
    exclusions = set(decision.scope.excluded_ids)
    if retained:
        exclusions.update(retained.excluded_ids)
    features = args.get('features', ())
    return PendingCatalogSearch(
        category=args.get('category'), text=args.get('text'), brand=args.get('brand'),
        model=args.get('model'), features=tuple(features),
        min_price=price('min_price'), max_price=price('max_price'),
        available_only=args.get('available_only', True) is not False,
        excluded_ids=tuple(sorted(exclusions)),
        shown_ids=retained.shown_ids if retained else (),
        page_ids=retained.page_ids if retained else (),
        no_result_count=retained.no_result_count if retained else 0,
    )


def record_search_page(pending, plan, decision, result, text):
    pending = pending_from_plan(plan, decision, pending)
    products = result.get('resultado', [])
    product_ids = tuple(dict.fromkeys(
        item['product_id'] for item in products
        if isinstance(item, dict) and type(item.get('product_id')) is int))
    visible_ids = displayed_product_ids([result], text)
    shown_ids = tuple(dict.fromkeys((*pending.shown_ids, *visible_ids)))
    no_result_count = pending.no_result_count + 1 if not product_ids else 0
    if not product_ids and isinstance(result.get('search_context'), dict):
        result['search_context']['no_result_count'] = no_result_count
    return replace(
        pending, shown_ids=shown_ids, page_ids=visible_ids,
        more_available=(len(product_ids) > len(visible_ids) or
                        len(product_ids) >= MAX_SEARCH_RESULTS),
        no_result_count=no_result_count)


def record_final_product_order(pending, product_ids):
    """Align ordinal references with the model-approved set/order actually shown."""
    if not isinstance(pending, PendingCatalogSearch):
        return pending
    selected = tuple(product_ids)
    page_ids = tuple(pending.page_ids)
    if (not selected or len(set(selected)) != len(selected) or
            not set(selected).issubset(set(page_ids))):
        raise ValueError('final product order is outside the current page')
    hidden = set(page_ids) - set(selected)
    shown_ids = tuple(product_id for product_id in pending.shown_ids if product_id not in hidden)
    return replace(
        pending, shown_ids=shown_ids, page_ids=selected,
        more_available=pending.more_available or bool(hidden))


def pending_search_action(text, pending: PendingCatalogSearch, snapshot):
    """Resume a previously grounded search from a short filter or page command."""
    if not isinstance(pending, PendingCatalogSearch) or not pending_catalog_followup(text):
        return None
    spoken = normalize(text)
    if complete_catalog_list_request(text):
        page_exclusions = set(pending.shown_ids)
        retained_exclusions = tuple(product_id for product_id in pending.excluded_ids
                                    if product_id not in page_exclusions)
        return ('search', replace(pending, excluded_ids=retained_exclusions, shown_ids=(), page_ids=(),
                                  selected_id=None, more_available=False), None, None)

    if more_catalog_list_request(text):
        if not pending.more_available:
            return ('clarify', pending, None,
                    'Já mostrei todas as opções encontradas com esses filtros. Qual filtro você gostaria de ajustar?')
        excluded = tuple(dict.fromkeys((*pending.excluded_ids, *pending.shown_ids)))
        return ('search', replace(pending, excluded_ids=excluded, selected_id=None, page_ids=(),
                                  more_available=False), None, None)

    ordinal = re.fullmatch(
        r'(?:o|a)\s+(primeir[oa]|segund[oa]|terceir[oa]|quart[oa]|quint[oa]|sext[oa]|'
        r'setim[oa]|oitav[oa]|non[oa]|decim[oa])', spoken)
    if ordinal:
        index = {'primeiro': 0, 'primeira': 0, 'segundo': 1, 'segunda': 1,
                 'terceiro': 2, 'terceira': 2, 'quarto': 3, 'quarta': 3,
                 'quinto': 4, 'quinta': 4, 'sexto': 5, 'sexta': 5,
                 'setimo': 6, 'setima': 6, 'oitavo': 7, 'oitava': 7,
                 'nono': 8, 'nona': 8, 'decimo': 9, 'decima': 9}[ordinal.group(1)]
        page_ids = pending.page_ids or pending.shown_ids
        if index >= len(page_ids):
            return ('clarify', pending, None, 'Qual das opções que mostrei você quer conhecer melhor?')
        product_id = page_ids[index]
        return ('detail', replace(pending, selected_id=product_id), product_id, None)

    if re.fullmatch(r'(?:nao quero esse|nao quero este|tem outro|tem outra|algum outro|alguma outra|prefiro outro|prefiro outra)(?:\s+tem\s+outro)?', spoken):
        selected = pending.selected_id
        page_ids = pending.page_ids or pending.shown_ids
        if selected is None and len(page_ids) == 1:
            selected = page_ids[0]
        if selected is None:
            labels = []
            for product_id in page_ids:
                product = next((row for row in snapshot.tables['products'] if row['product_id'] == product_id), None)
                if product:
                    labels.append(product['name'])
            options = ', '.join(labels[:PRODUCT_PAGE_SIZE])
            return ('clarify', pending, None,
                    'Qual destas opções você quer deixar de fora?' + (f' Foram mostradas: {options}.' if options else ''))
        excluded = tuple(dict.fromkeys((*pending.excluded_ids, *pending.shown_ids, selected)))
        return ('search', replace(pending, excluded_ids=excluded, shown_ids=(), page_ids=(), selected_id=None,
                                  more_available=False), None, None)

    amount = re.fullmatch(r'(?:ate|no maximo)\s*(?:r\$\s*)?([0-9][0-9.,]*)(?:\s+reais)?[.!]?', spoken)
    if amount:
        value = parse_brl_mention(amount.group(1))
        if value is not None:
            return ('search', replace(pending, max_price=value, shown_ids=(), page_ids=(), selected_id=None,
                                      more_available=False), None, None)

    terms = [word for word in spoken.split() if word not in {'de', 'do', 'da', 'dos', 'das', 'e', 'com', 'para'}]
    searchable = []
    spec_terms = set()
    catalog = Catalog(snapshot)
    for product in snapshot.tables['products']:
        item = catalog.product(product['product_id'])
        if not item:
            continue
        title = normalize(item['name'])
        description = normalize(item['description'] or '')
        searchable.append(' '.join((title, description,
                                    *(normalize(str(value)) for value in item['specs'].values()
                                      if isinstance(value, (str, int, Decimal)) and not isinstance(value, bool)))))
        spec_terms.update(description.split())
        for value in item['specs'].values():
            if isinstance(value, (str, int, Decimal)) and not isinstance(value, bool):
                spec_terms.update(normalize(str(value)).split())
    if terms and all(any(term in haystack for haystack in searchable) for term in terms):
        brand_terms = {normalize(row['name']).split()[0] for row in snapshot.tables['products']
                       if normalize(row['name']).split()}
        brand_parts = [term for term in terms if term in brand_terms]
        feature_parts = [term for term in terms if term in spec_terms]
        model_parts = [term for term in terms if term not in brand_parts and term not in feature_parts]
        brand = ' '.join(brand_parts) if brand_parts else pending.brand
        model = ' '.join(model_parts) if model_parts else pending.model
        features = tuple(dict.fromkeys((*pending.features, *feature_parts)))
        return ('search', replace(pending, brand=brand, model=model, features=features,
                                  shown_ids=(), page_ids=(), selected_id=None,
                                  more_available=False), None, None)
    return None


def pending_search_arguments(pending: PendingCatalogSearch):
    args = {}
    for key in ('category', 'text'):
        value = getattr(pending, key)
        if value is not None:
            args[key] = value
    if pending.brand is not None:
        args['brand'] = pending.brand
    if pending.model is not None:
        args['model'] = pending.model
    if pending.features:
        args['features'] = list(pending.features)
    if pending.min_price is not None:
        args['min_price'] = format(pending.min_price, '.2f')
    if pending.max_price is not None:
        args['max_price'] = format(pending.max_price, '.2f')
    args['available_only'] = pending.available_only
    return args


def pending_search_grounding_text(text, pending: PendingCatalogSearch):
    parts = [text.strip()]
    if pending.category:
        parts.append('Quero continuar a busca na categoria ' + pending.category)
    if pending.text:
        parts.append('Mantendo o nome de produto ' + pending.text)
    if pending.brand:
        parts.append('Mantendo a marca ' + pending.brand)
    if pending.model:
        parts.append('Mantendo o modelo ' + pending.model)
    parts.extend('Mantendo a característica ' + feature for feature in pending.features)
    if pending.min_price is not None:
        parts.append('Mantendo o preço mínimo de R$ ' + format(pending.min_price, '.2f'))
    if pending.max_price is not None:
        parts.append('Mantendo o preço máximo de R$ ' + format(pending.max_price, '.2f'))
    if not pending.available_only:
        parts.append('Mantendo a consulta por produtos indisponíveis')
    return '. '.join(part for part in parts if part)


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


def render_sales_results(results, text, snapshot, product_order=None):
    if not results or any(entry['consulta'] not in {'search_products','recommend_products'} for entry in results):
        return render_results(results)
    if product_order is not None:
        positions = {product_id: index for index, product_id in enumerate(product_order)}
        reordered = []
        for entry in results:
            copy = dict(entry)
            original = list(entry['resultado'])
            selected = [(index, item) for index, item in enumerate(original)
                        if item.get('product_id') in positions]
            copy['resultado'] = [item for _, item in sorted(
                selected, key=lambda pair: (positions[pair[1]['product_id']], pair[0]))]
            reordered.append(copy)
        results = reordered
    products = _product_list(results)
    if not products:
        return render_results(results)
    spoken = normalize(text)
    asks_for_unavailable = bool(re.search(
        r'\b(?:indisponiveis|indisponivel|esgotados?|sem estoque)\b', spoken))
    names_product = any(normalize(item['name']) in spoken for item in products)
    if not asks_for_unavailable and not names_product:
        available_ids = {item['product_id'] for item in products if item.get('available') is True}
        if len(available_ids) != len(products):
            filtered = []
            for entry in results:
                copy = dict(entry)
                copy['resultado'] = [item for item in entry['resultado']
                                     if item.get('product_id') in available_ids]
                filtered.append(copy)
            results = filtered
            products = _product_list(results)
            if not products:
                return render_results(results)
    total_product_count = len(products)
    if not _simple_listing(products, text):
        shown_order = tuple(product_order) if product_order is not None else displayed_product_ids(results, text)
        positions = {product_id: index for index, product_id in enumerate(shown_order)}
        limited = []
        for entry in results:
            copy = dict(entry)
            copy['resultado'] = sorted(
                (item for item in entry['resultado'] if item.get('product_id') in positions),
                key=lambda item: positions[item['product_id']])
            limited.append(copy)
        answer = render_results(limited)
        if total_product_count > len(shown_order):
            answer += '\n\nHá mais opções no catálogo. Diga “mais opções” para continuar sem repetir as que já mostrei.'
        return answer
    page_limit = len(products) if complete_catalog_list_request(text) else PRODUCT_PAGE_SIZE
    options = (products[:page_limit] if product_order is not None else
               sorted(products, key=_effective_price_key)[:page_limit])
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
        lines.append(f"• {item['name']}: {_money(quote['checkout_price_brl'])}{label}.")
    if total_product_count > len(options):
        lines.append('Há mais opções no catálogo. Diga “mais opções” para continuar sem repetir as que já mostrei.')
    lines.append('Preços e o estoque registrado vêm da base importada; confirme as condições atuais com o vendedor.')
    if any(item['price_quote']['status']=='active_in_snapshot' for item in options):
        lines[-1] += ' O desconto Pix não acumula com a promoção; a vigência atual não foi confirmada.'
    has_budget = bool(re.search(r'\b(?:ate|entre|orcamento|no maximo|abaixo de)\b|r\$',normalize(text)))
    lines.append('Qual desses modelos você quer conhecer melhor?' if has_budget else
                 BUDGET_QUESTION)
    return '\n\n'.join([lines[0], '\n'.join(lines[1:-2]), lines[-2], lines[-1]])


def render_sales_answer_within_limit(results, text, snapshot, product_order=None,
                                    max_chars=MAX_ASSISTANT_ANSWER_CHARS):
    """Keep complete product blocks and add a continuation when a list exceeds its cap."""
    answer = render_sales_results(results, text, snapshot, product_order=product_order)
    if len(answer) <= max_chars:
        return answer, None
    if (len(results) == 1 and results[0].get('consulta') == 'product_detail' and
            isinstance(results[0].get('resultado'), dict)):
        compact_result = dict(results[0]['resultado'])
        compact_result['specs'] = {}
        compact = render_results([{'consulta': 'product_detail', 'resultado': compact_result}])
        compact += '\n\nAs especificações são extensas; peça um campo técnico para continuar.'
        return (compact, None) if len(compact) <= max_chars else (answer, None)
    if not results or any(entry.get('consulta') not in {'search_products', 'recommend_products'}
                          for entry in results):
        return answer, None
    product_ids = tuple(product_order) if product_order is not None else displayed_product_ids(results, text)
    continuation = 'Há mais opções no catálogo. Diga “mais opções” para receber o próximo bloco completo.'
    for count in range(len(product_ids) - 1, 0, -1):
        selected = product_ids[:count]
        selected_set = set(selected)
        subset = []
        for entry in results:
            copy = dict(entry)
            copy['resultado'] = [item for item in entry['resultado']
                                 if item.get('product_id') in selected_set]
            subset.append(copy)
        subset_order = tuple(item for item in product_ids if item in selected_set)
        candidate = render_sales_results(
            subset, text, snapshot,
            product_order=subset_order if product_order is not None else None)
        candidate = _insert_continuation(candidate, continuation)
        if len(candidate) <= max_chars:
            return candidate, selected

    first_id = product_ids[0] if product_ids else None
    first_item = next((item for entry in results for item in entry.get('resultado', ())
                       if isinstance(item, dict) and item.get('product_id') == first_id), None)
    if first_item is None:
        return answer, None
    compact = render_sales_results(
        [{'consulta': 'search_products', 'resultado': [first_item]}],
        'pix' if re.search(r'\bpix\b', normalize(text)) else '', snapshot)
    compact = _insert_continuation(
        compact, 'Os detalhes técnicos são extensos; peça um campo específico para continuar.')
    if len(compact) <= max_chars:
        return compact, (first_id,)
    return answer, None


def _insert_continuation(answer, continuation):
    for question in (BUDGET_QUESTION, 'Qual desses modelos você quer conhecer melhor?',
                     'Qual dessas opções você quer conhecer melhor?'):
        suffix = '\n\n' + question
        if answer.endswith(suffix):
            return answer[:-len(suffix)] + '\n\n' + continuation + suffix
    return answer + '\n\n' + continuation
