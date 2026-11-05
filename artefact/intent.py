"""Conservative grounding for proposed catalog operations."""
from decimal import Decimal
import re
import unicodedata

from .commercial import has_budget_context, parse_brl_mention


def normalize(value: str) -> str:
    return ' '.join(''.join(ch for ch in unicodedata.normalize('NFKD', value.casefold())
                            if not unicodedata.combining(ch)).split())


def intent_clauses(text: str) -> tuple[str, ...]:
    """Keep decimal tokens intact and separate independently requested operations."""
    return tuple(part.strip() for part in re.split(
        r'\.(?!\d)|[!?;]|\b(?:mas|porem)\b|(?:,\s*(?:e\s+)?|\s+e\s+)(?=(?:prefiro|quero|mostre|use|utilize)\b)|,?\s+e\s+(?=(?:o\s+frete|quais|qual\b|nao\b|tenho\b|recomende\b))',
        normalize(text)) if part.strip())


def clause_polarity(clause: str) -> str:
    """Shared conservative polarity for a single operation, not an entire message."""
    clause = re.sub(r'\bsem juros\b', '', clause)
    # Negated obligation removes a confirmation step, not price authority.
    clause = re.sub(r'\bnao\s+(?:precisa|e necessario)\s+confirmar\b', 'dispensa confirmacao', clause)
    if re.search(r'\b(?:nao|nunca|sem|exceto|evite|dispenso|proibido)\b', clause):
        return 'negative'
    if re.search(r'\b(?:se|caso|talvez|quando|apos|depois|desde que)\b', clause):
        return 'conditional'
    if re.search(r'\b(?:palavra|texto|exemplo de frase|mencionei)\b', clause):
        return 'incidental'
    return 'affirmative'


def requests_catalog_price(clause: str) -> bool:
    return (clause_polarity(clause) == 'affirmative' and 'catalogo' in clause and
            bool(re.search(r'\b(?:confirme|confirmo|confirmar|utilize|usar|use)\b', clause)) and
            not re.search(r'\b(?:incorret\w*|errad\w*|fals\w*)\b', clause))


def requests_options(text: str) -> bool:
    return any(clause_polarity(clause) == 'affirmative' and
               re.search(r'\b(?:opcoes|alternativas|recomende|mostre tambem|outros|outras)\b', clause)
               for clause in intent_clauses(text))


_GENERIC_PRODUCT_WORDS = {'teclado', 'sintetizador', 'arranjador', 'violao', 'guitarra',
                          'baixo', 'bateria', 'ukulele', 'instrumento', 'digital', 'acustico'}


def _product_matches(text: str, name: str):
    """Return maximal catalog phrases, shared by resolution and polarity."""
    words = normalize(name).split()
    matches = {}
    for size in range(len(words), 0, -1):
        for start in range(len(words) - size + 1):
            phrase = words[start:start + size]
            if not any(len(word) >= 3 and word not in _GENERIC_PRODUCT_WORDS for word in phrase):
                continue
            for match in re.finditer(r'(?<!\w)' + re.escape(' '.join(phrase)) + r'(?!\w)', text):
                matches[(match.start(), match.end())] = match
    return sorted((match for (start, end), match in matches.items()
                   if not any(a <= start and end <= b and b - a > end - start for a, b in matches)),
                  key=lambda match: match.start())


def mentioned_products(text: str, snapshot) -> list[dict]:
    if snapshot is None:
        return []
    spoken = normalize(text)
    references = [(product, match) for product in snapshot.tables['products']
                  for match in _product_matches(spoken, product['name'])]
    found = {}
    for product, match in references:
        full = match.group() == normalize(product['name'])
        if not any(other.start() <= match.start() and match.end() <= other.end() and
                   (len(other.group()) > len(match.group()) or
                    (not full and other.group() == normalize(row['name'])))
                   for row, other in references):
            found[product['product_id']] = product
    return list(found.values())


def product_reference(text: str, name: str) -> str:
    """Classify the latest scoped reference; a mention alone grants no selection."""
    spoken = normalize(text)
    for clause in reversed(intent_clauses(text)):
        matches = _product_matches(clause, name)
        if not matches:
            continue
        match = matches[-1]
        prefix, tail = clause[:match.start()], clause[match.end():]
        polarity = clause_polarity(prefix)
        if polarity == 'negative':
            # Rejecting a price operation is not rejecting the product itself.
            return 'restricted' if re.search(r'\b(?:catalogo|preco|simul\w*|parcel\w*)\b', clause) else 'rejected'
        if polarity in {'conditional', 'incidental'}:
            return polarity
        # A trailing revocation can refer to the same product without repeating it.
        suffix = spoken[spoken.rfind(match.group()) + len(match.group()):]
        if re.search(r'\b(?:mas|porem)\s+(?:eu\s+)?(?:nao|nunca|dispenso)\b', suffix):
            return 'rejected'
        if re.search(r'\b(?:compar\w*|versus|vs)\b', spoken):
            return 'comparison'
        if re.match(r'\s*(?:custa|vale|tem|qual|quanto|preco|valor|disponibilidade|'
                    r'esta\s+disponivel|tem\s+disponivel|tem\s+estoque|'
                    r'esta\s+em\s+estoque|ha\s+estoque|estoque|promocao|garantia|'
                    r'foi\s+anunciad[oa])\b', tail):
            return 'affirmative'
        if re.search(r'\b(?:quero|prefiro|escolho|escolhi|escolha|mostre|procuro|comprar|adquirir|preco|valor|custa|tem|qual|quanto|disponibilidade|estoque|promocao)\b', prefix):
            return 'affirmative'
        return 'mentioned'
    return 'unmentioned'


def _words(value: str) -> set[str]:
    return set(re.findall(r'[a-z0-9]+', normalize(value)))


def _category_terms(value: str) -> set[str]:
    terms = set()
    for word in _words(value):
        if word in {'instrumentos', 'instrumento', 'cordas', 'corda', 'madeiras', 'metais'} or len(word) < 5:
            continue
        if word == 'violoes':
            word = 'violao'
        elif word in {'sintetizador', 'sintetizadores', 'arranjador', 'arranjadores'}:
            word = 'teclado'
        elif word.endswith('s'):
            word = word[:-1]
        terms.add(word)
    return terms


def mentioned_category_ids(text: str, snapshot) -> frozenset[int]:
    mentioned_categories = set()
    for clause in intent_clauses(text):
        if clause_polarity(clause) != 'affirmative':
            continue
        spoken_terms = _category_terms(clause)
        clause_categories = {c['category_id'] for c in snapshot.tables['categories']
                             if _category_terms(c['name']).intersection(spoken_terms)}
        if 'sopro' in spoken_terms:
            spoken = _words(clause)
            qualifier = ('metais' if spoken.intersection({'metal', 'metais'}) else
                         'madeiras' if spoken.intersection({'madeira', 'madeiras'}) else None)
            if qualifier:
                clause_categories = {c['category_id'] for c in snapshot.tables['categories']
                                     if c['category_id'] in clause_categories and qualifier in _words(c['name'])}
        mentioned_categories.update(clause_categories)
    return frozenset(mentioned_categories)


def is_catalog_discovery(text: str, snapshot) -> bool:
    """Recognize a clear, unfiltered question about the store's product range."""
    if not isinstance(text, str) or snapshot is None:
        return False
    spoken = normalize(text)
    if mentioned_category_ids(text, snapshot) or mentioned_products(text, snapshot):
        return False
    if re.search(r'\b(?:pedido\w*|frete|entrega|garantia|troca|pix|parcel\w*|politica|horario|clima)\b', spoken):
        return False
    return bool(re.search(
        r'\b(?:o que|que que|quais?|qual)\s+(?:(?:voces?|a loja)\s+)?'
        r'(?:produtos?\s+)?(?:tem|ha|vende\w*|disponibiliza\w*|trabalha\w*)\b', spoken
    ) or re.search(r'\b(?:quais?|qual)\s+produtos?\s+(?:disponiveis?|em estoque)\b', spoken)
      or re.search(r'\b(?:catalogo|produtos?\s+da\s+loja)\b.*\b(?:tem|mostr\w*|ver|conhecer)\b', spoken))


def direct_catalog_plan(text: str, snapshot):
    """Return only an unambiguous discovery/category listing shortcut."""
    if is_catalog_discovery(text, snapshot):
        return 'catalog_overview', {}
    categories = mentioned_category_ids(text, snapshot)
    spoken = normalize(text)
    if re.search(r'\b(?:mais|outras?)\s+opcoes\b', spoken):
        return None
    bare_category = len(categories) == 1 and _category_terms(spoken) == {
        term for row in snapshot.tables['categories'] if row['category_id'] in categories
        for term in _category_terms(row['name'])
    }
    if (len(categories) != 1 or
            (not bare_category and not re.search(r'\b(?:quais?|que que|o que|tem|vende\w*|opcoes|modelos?|instrumentos?)\b', spoken)) or
            re.search(r'\b(?:nao|nunca|sem|exceto|preco|orcamento|ate|abaixo|acima|entre|nylon|aco|marca)\b', spoken)):
        return None
    category_id = next(iter(categories))
    category = next((row for row in snapshot.tables['categories'] if row['category_id'] == category_id), None)
    if category is None:
        return None
    category_words = _words(category['name']) | _category_terms(category['name'])
    generic_words = {
        'a', 'as', 'de', 'do', 'da', 'dos', 'das', 'e', 'em', 'para', 'por', 'o', 'os', 'voce',
        'um', 'uma', 'uns', 'umas', 'que', 'quais', 'qual', 'voces', 'loja', 'tem', 'temos',
        'ha', 'vende', 'vender', 'disponivel', 'disponiveis', 'estoque', 'venda', 'opcoes',
        'modelos', 'instrumentos', 'instrumento', 'produtos', 'produto',
    }
    if _words(spoken) - category_words - generic_words:
        return None
    return 'search_products', {'category': category['name'], 'available_only': True}


def has_independent_catalog_request(text: str, snapshot) -> bool:
    """Keep a catalog request in the planner when a message also asks about policy."""
    for clause in intent_clauses(text):
        if (is_catalog_discovery(clause, snapshot) or
                (direct_catalog_plan(clause, snapshot) is not None and re.search(
                    r'\b(?:quais?|que que|o que|tem|vende\w*|opcoes|modelos?|instrumentos?)\b',
                    normalize(clause)))):
            return True
        if clause_polarity(clause) != 'affirmative':
            continue
        categories = mentioned_category_ids(clause, snapshot)
        products = mentioned_products(clause, snapshot)
        if not (products or categories):
            continue
        request = re.sub(r'\b(?:quero|gostaria de)\s+saber\b', '', clause)
        if re.search(r'\b(?:quero|gostaria de|procuro|busco|comprar|adquirir|'
                     r'mostre|recomende|indique|opcoes|alternativas)\b', request):
            return True
        listing_request = bool(
            re.search(r'\b(?:quais?|que|tem|vende\w*|disponiveis?|estoque|modelos?|instrumentos?|produtos?)\b', request)
            and re.search(r'\b(?:opcoes?|modelos?|instrumentos?|produtos?|disponiveis?|estoque|quais?)\b', request))
        if categories and (listing_request or has_budget_context(request)):
            return True
    return False


def direct_product_status_plan(text: str, snapshot):
    """Use the identified product record for a clear purchase or stock question."""
    if not isinstance(text, str) or snapshot is None:
        return None
    spoken = normalize(text)
    if not re.search(
        r'\b(?:posso|consigo|da para|tem como)\s+(?:comprar|adquirir)\b|'
        r'\b(?:quero|gostaria de|pretendo|vou)\s+(?:comprar|adquirir)\b|'
        r'\b(?:esta disponivel|tem disponivel|tem estoque|esta em estoque|ha estoque)\b', spoken):
        return None
    products = [product for product in mentioned_products(text, snapshot)
                if product_reference(text, product['name']) == 'affirmative']
    if len(products) != 1:
        return None
    return 'product_detail', {'product_id': products[0]['product_id']}


def normalize_catalog_args(name, args, text, snapshot):
    """Resolve a category alias only when both proposal and customer ground it.

    Names, budgets and negation still go through the original validator. A
    broad term such as 'sopro' cannot silently choose between two categories.
    """
    if name not in {'search_products', 'recommend_products'}:
        return args
    args = dict(args)
    requested = mentioned_category_ids(text, snapshot)
    if len(requested) != 1:
        return args
    category = next(row for row in snapshot.tables['categories'] if row['category_id'] in requested)
    aliases = {normalize(category['name'])}
    # Complete phrases only. Token stemming is useful for recognizing a
    # customer's category, but would erase short constraints like aço/7 cordas.
    groups = {'violoes':{'violao','violoes'}, 'guitarras':{'guitarra','guitarras'},
              'baixos':{'baixo','baixos'}, 'baterias':{'bateria','baterias','percussao'},
              'teclados':{'teclado','teclados','piano','pianos','sintetizador','sintetizadores','arranjador','arranjadores'},
              'ukuleles':{'ukulele','ukuleles'}}
    for token, values in groups.items():
        if token in _words(category['name']): aliases.update(values)
    def same_category(value):
        if not isinstance(value, str) or not value.strip():
            return False
        return (normalize(value) in aliases and
                mentioned_category_ids(value, snapshot) == requested)
    if same_category(args.get('category')):
        args['category'] = category['name']
    if same_category(args.get('text')) and 'category' not in args:
        args.pop('text')
        args['category'] = category['name']
    if (name == 'search_products' and args.get('category') == category['name'] and
            'available_only' not in args and
            re.search(r'\b(?:quero|procuro|busco|comprar|opcoes|disponivel|disponiveis)\b', normalize(text)) and
            not re.search(r'\b(?:indisponiveis|indisponivel|esgotados?|sem estoque)\b', normalize(text))):
        args['available_only'] = True
    return args


def _non_price_unit(text: str, end: int) -> bool:
    return bool(re.match(r'\s*(?:anos?|meses?|dias?|parcelas?|vezes|teclas?|cordas?|x\b|%)', text[end:]))


def _money_values(text: str) -> set[Decimal]:
    values = set()
    normalized = normalize(text)
    for match in re.finditer(r'(?i)(?:R\$\s*|\b(?:ate|entre|abaixo de|acima de|partir de)\s*)([0-9][0-9.,]*)', normalized):
        if _non_price_unit(normalized, match.end(1)):
            continue
        value = parse_brl_mention(match.group(1))
        if value is not None:
            values.add(value)
    interval = re.search(r'(?i)\bentre\s*(?:r\$\s*)?([0-9][0-9.,]*)\s+e\s+(?:r\$\s*)?([0-9][0-9.,]*)', normalize(text))
    if interval:
        for token in interval.groups():
            value = parse_brl_mention(token)
            if value is not None:
                values.add(value)
    return values


_SCOPE_ACCESSORY_PATTERN = (
    r'\b(?:acessorios?|palhetas?|cabos?|amplificadores?|cases?|pedais?|'
    r'encordoamento\w*|jogos?\s+de\s+cordas?|cordas?\s+(?:para|de)\s+'
    r'(?:viol\w*|guitarra|baixo))\b')
_SCOPE_WEATHER_PATTERN = r'\b(?:previsao do tempo|clima)\b'


def _scope_accessory_mentioned(clause: str) -> bool:
    if re.search(_SCOPE_ACCESSORY_PATTERN, clause):
        return True
    if not re.search(r'\bcordas?\b', clause):
        return False
    instrument_string_count = re.search(
        r'\b(?:viol\w*|guitarra|baixo|ukulele)\s+(?:de|com)\s+'
        r'(?:\d+|um|uma|dois|duas|tres|quatro|cinco|seis|sete|oito|nove|dez|onze|doze)\s+cordas?\b|'
        r'\bquantas?\s+cordas?\b', clause)
    if instrument_string_count:
        return False
    return bool(re.search(
        r'\b(?:vend\w*|comercializa\w*|tem|possui|trabalh\w*|quer\w*|comprar|'
        r'procura\w*|busca\w*)\s+(?:(?:as?|algumas?)\s+)?cordas?\b', clause))


def requested_scope_topics(text: str) -> frozenset[str]:
    """Return the independently requested scope boundaries in a message."""
    topics = set()
    for clause in intent_clauses(text):
        if clause_polarity(clause) != 'affirmative':
            continue
        if _scope_accessory_mentioned(clause):
            topics.add('accessories')
        if re.search(_SCOPE_WEATHER_PATTERN, clause):
            topics.add('weather')
    return frozenset(topics)


_POLICY_TOPIC_PATTERNS = {
    'exchange': r'\b(?:troca\w*|trocar|defeito\w*)\b',
    'warranty': r'\b(?:garantia\w*|fabricante|mau uso|cobertura)\b',
    'regret': r'\b(?:arrepend\w*|desist\w*|devolu\w*|devolver)\b',
    'shipping': r'\b(?:frete|envio|entreg\w*|cheg\w*)\b',
    'installments': r'\b(?:parcel\w*|vezes|crediario)\b',
    'payment_mix': r'\b(?:combinar|dividir|misturar|formas de pagamento|pagamentos)\b',
    'pix': r'\bpix\b',
    'hours': r'\b(?:horario\w*|funcionamento|aberto|abre|fecha)\b',
    'contacts': r'\b(?:contato\w*|telefone|vendedor\w*|ligar|whats?\s*app|wpp|zap)\b',
    'scope': '(?:' + _SCOPE_ACCESSORY_PATTERN + '|' + _SCOPE_WEATHER_PATTERN + ')',
}
_BRAZILIAN_UFS = frozenset({
    'ac', 'al', 'ap', 'am', 'ba', 'ce', 'df', 'es', 'go', 'ma', 'mt', 'ms', 'mg',
    'pa', 'pb', 'pr', 'pe', 'pi', 'rj', 'rn', 'rs', 'ro', 'rr', 'sc', 'sp', 'se', 'to',
})


def requested_policy_topics(text: str) -> frozenset[str]:
    """Return the unnegated policy topics explicitly requested in this message."""
    spoken = normalize(text)
    mentioned = {key for key, pattern in _POLICY_TOPIC_PATTERNS.items()
                 if re.search(pattern, spoken)}
    if requested_scope_topics(text):
        mentioned.add('scope')
    requested = set()
    for topic in mentioned:
        for clause in intent_clauses(text):
            if (not re.search(_POLICY_TOPIC_PATTERNS[topic], clause) and
                    not (topic == 'scope' and _scope_accessory_mentioned(clause))):
                continue
            # A reported negative experience can still ask for the applicable policy.
            request_clause = re.sub(r'\bnao\s+(?:gostei|chegou|recebi|funciona|foi entregue)\b', '', clause)
            polarity = clause_polarity(request_clause)
            # Informational conditions differ from deferred commands.
            if polarity == 'conditional' and not re.search(
                    r'\b(?:consulte|mostre|informe|responda)\b', clause):
                requested.add(topic)
                break
            if polarity == 'affirmative':
                requested.add(topic)
                break
    return frozenset(requested)


def validate_policy_intent(topic: str, text: str) -> bool:
    """Reject policy topics contradicted by an explicit topic in this turn."""
    return topic in requested_policy_topics(text)


def local_shipping_destination(text: str) -> bool:
    """Recognize the store's local Campo Grande/MS destination in delivery context."""
    spoken = normalize(text)
    for destination in re.finditer(
            r'\b(?:para|em|destino|entrega em|envio para)\s+(?:o\s+)?'
            r'([a-z]+(?:\s+[a-z]+){0,2})\s*(?:/|,|-)\s*([a-z]{2})\b', spoken):
        city, state_code = destination.groups()
        if state_code in _BRAZILIAN_UFS and (city != 'campo grande' or state_code != 'ms'):
            return False
    state = re.search(r'\bcampo grande(?:\s*/\s*|\s*,\s*|\s+-\s*|\s+)([a-z]{2})\b', spoken)
    if state and state.group(1) in _BRAZILIAN_UFS and state.group(1) != 'ms':
        return False
    explicit = bool(state and state.group(1) == 'ms')
    local_city = re.search(r'\b(?:para|em|ate)\s+(?:o\s+)?campo grande\b', spoken)
    return bool(explicit or local_city)


def is_shipping_quote_request(text: str, snapshot) -> bool:
    """Separate a freight amount request from a general question about shipping policy."""
    spoken = normalize(text)
    for clause in intent_clauses(text):
        has_freight = bool(re.search(r'\bfrete\b', clause))
        has_delivery = bool(re.search(r'\bentreg\w*\b', clause))
        has_amount_question = bool(re.search(
            r'\b(?:valor|preco|quanto|custa|custo|taxa|cotacao|cotar)\b', clause))
        if re.search(r'\b(?:politica|regras?|condicoes?)\b', clause) and not has_amount_question:
            continue
        if ((has_amount_question and (has_freight or has_delivery)) or
                (has_freight and re.search(r'\bqual\s+(?:o\s+)?frete\b', clause))):
            return True
        if has_freight and (local_shipping_destination(clause) or mentioned_products(clause, snapshot)):
            return True
    return False


def validate_cheapest_intent(text: str, snapshot) -> bool:
    """Only authorize the unfiltered global cheapest-item operation for a clear query."""
    normalized = normalize(text)
    if not re.search(r'\b(?:mais\s+barato|menor\s+(?:preco|valor)|mais\s+em\s+conta)\b', normalized):
        return False
    if not re.search(r'\b(?:estoque|disponivel|disponiveis)\b', normalized):
        return False
    if re.search(r'\bnao\s+(?:quero|mostre|indique|recomende)\s+(?:o\s+)?(?:instrumento\s+)?mais\s+barato\b', normalized):
        return False
    if any(re.search(r'(?<!\w)' + re.escape(normalize(product['name'])) + r'(?!\w)', normalized)
           for product in snapshot.tables['products']):
        return False
    spoken_terms = _category_terms(text)
    if any(_category_terms(category['name']).intersection(spoken_terms)
           for category in snapshot.tables['categories']):
        return False
    return not re.search(r'R\$\s*[0-9]', text)


def validate_catalog_intent(name: str, args: dict, text: str, snapshot, product_id: int | None = None,
                            grounded_category_ids=()) -> bool:
    normalized = normalize(text)
    if name == 'promotion':
        pid = args.get('product_id')
        product = next((p for p in snapshot.tables['products'] if p['product_id'] == pid), None)
        if product is None:
            return False
        if product_reference(text, product['name']) == 'rejected':
            return False
        labelled = re.search(r'\bproduto\s+(?:id\s*)?(\d+)\b', normalized)
        return bool((labelled and int(labelled.group(1)) == pid) or
                    normalize(product['name']) in normalized or product_id == pid)
    if name not in {'search_products', 'recommend_products'}:
        return True
    explicit_products = [p for p in snapshot.tables['products']
                         if re.search(r'(?<!\w)' + re.escape(normalize(p['name'])) + r'(?!\w)', normalized)]
    affirmative_products = [p for p in mentioned_products(text, snapshot)
                            if product_reference(text, p['name']) == 'affirmative']
    contextual_product = next((p for p in snapshot.tables['products']
                                if p['product_id'] == product_id), None)
    selected_product_reference = bool(
        contextual_product and product_reference(text, contextual_product['name']) in
        {'affirmative', 'mentioned'} and
        (not affirmative_products or
         len(affirmative_products) == 1 and affirmative_products[0]['product_id'] == product_id))
    product_installment_lookup = bool(
        name == 'search_products' and type(args.get('installments')) is int and
        product_id is not None and set(args) <= {'text', 'installments', 'available_only'} and
        isinstance(args.get('text'), str) and selected_product_reference and
        normalize(args['text']) == normalize(contextual_product['name']))
    if name == 'search_products' and re.search(r'\bdisponiveis\b', normalized) and args.get('available_only') is not True:
        return False
    if args.get('available_only') is True and re.search(r'\bindisponiveis\b', normalized):
        return False
    if name == 'search_products' and args.get('available_only') is False and not re.search(
            r'\b(?:indisponiveis|indisponivel|esgotados?|sem estoque)\b', normalized) and not product_installment_lookup:
        return False
    negation = (r'(?:nao quero(?:\s+mais)?|nao (?:me )?(?:recomende|recomendar|indique|sugira)|'
                r'nao gosto de|nao o|nao a|sem|exceto)\s+(?:(?:o|a)\s+)?')
    query = args.get('text')
    if query is not None:
        if not isinstance(query, str) or not query.strip():
            return False
        if not _words(query).issubset(_words(text)):
            resolved = mentioned_products(text, snapshot)
            if (len(resolved) != 1 or normalize(query) != normalize(resolved[0]['name']) or
                    product_reference(text, resolved[0]['name']) == 'rejected'):
                return False
        query_products = [p for p in snapshot.tables['products']
                          if _words(query).issubset(_words(p['name']))]
        if any(product_reference(text, product['name']) == 'rejected'
               for product in query_products):
            return False
        if re.search(r'\b' + negation + re.escape(normalize(query)) + r'\b', normalized):
            return False
        if name == 'search_products' and not explicit_products:
            continuation = re.search(r'(?<!\w)' + re.escape(normalize(query)) + r'\s+([a-z][a-z0-9-]*)', normalized)
            ordinary = {'em','ate','com','tem','possui','esta','disponivel','disponiveis','estoque',
                        'por','no','na','de','do','da','e','ou','para','qual','custa','serve',
                        'sai','preco','garantia','barato','baratos','acustico','eletrico'}
            model_words = set().union(*(_words(p['name']) for p in query_products)) if query_products else set()
            if continuation and continuation.group(1) not in ordinary | model_words:
                return False
        terms = {normalize(query)}
        terms.update(normalize(product['name']) for product in snapshot.tables['products']
                     if _words(query).issubset(_words(product['name'])))
        for term in terms:
            if re.search(r'(?<!\w)' + re.escape(term) + r'\s+(?:ou|versus|vs)\s+[a-z]', normalized):
                return False
            if re.search(r'\b[a-z]+\s+(?:ou|versus|vs)\s+' + re.escape(term) + r'(?!\w)', normalized):
                return False
    category = args.get('category')
    mentioned_categories = mentioned_category_ids(text, snapshot)
    if name == 'recommend_products':
        mentioned_categories = mentioned_categories | frozenset(grounded_category_ids)
    if len(mentioned_categories) > 1 and len(explicit_products) != 1 and not product_installment_lookup:
        return False
    if category is not None:
        if not isinstance(category, str) or not category.strip():
            return False
        selected = [c['category_id'] for c in snapshot.tables['categories'] if normalize(c['name']) == normalize(category)]
        if len(selected) != 1:
            return False
        if selected[0] not in mentioned_categories and not (len(explicit_products) == 1 and explicit_products[0]['category_id'] == selected[0]):
            return False
    elif mentioned_categories and not (
            len(explicit_products) == 1 and explicit_products[0]['category_id'] in mentioned_categories or
            product_installment_lookup and contextual_product['category_id'] in mentioned_categories):
        return False
    terms = []
    for key in ('brand', 'model'):
        term = args.get(key)
        if term is not None:
            if not isinstance(term, str) or not term.strip() or len(term) > 80:
                return False
            terms.append(term)
    features = args.get('features', [])
    if (not isinstance(features, list) or len(features) > 8 or
            any(not isinstance(term, str) or not term.strip() or len(term) > 80 for term in features)):
        return False
    terms.extend(features)
    for term in terms:
        normalized_term = normalize(term)
        if normalized_term not in normalized:
            return False
        if not any(normalized_term in clause and clause_polarity(clause) == 'affirmative'
                   for clause in intent_clauses(text)):
            return False
    values = _money_values(text)
    number = r'(?:r\$\s*)?([0-9][0-9.,]*)'
    interval = re.search(r'\bentre\s*' + number + r'\s+e\s+' + number, normalized)
    lower = re.search(r'\b(?:a partir de|acima de|no minimo|mais de)\s*' + number, normalized)
    upper = re.search(r'\b(?:ate|abaixo de|no maximo|menos de)\s*' + number, normalized)
    if lower and _non_price_unit(normalized, lower.end(1)):
        lower = None
    if upper and _non_price_unit(normalized, upper.end(1)):
        upper = None
    required = {}
    for key, raw in (('min_price', interval.group(1) if interval else lower.group(1) if lower else None),
                     ('max_price', interval.group(2) if interval else upper.group(1) if upper else None)):
        if raw is not None:
            value = parse_brl_mention(raw)
            if value is None:
                return False
            required[key] = value
    for key, value in required.items():
        if key not in args or not isinstance(args[key], str) or parse_brl_mention(args[key]) != value:
            return False
    for key in ('min_price', 'max_price'):
        if key in args:
            if key not in required and not (key == 'max_price' and re.search(r'\borcamento\b', normalized)):
                return False
            price = parse_brl_mention(args[key]) if isinstance(args[key], str) else None
            if price is None or price not in values:
                return False
            if key == 'min_price' and re.search(r'\b(?:ate|abaixo|maximo)\b', normalized) and not re.search(r'\b(?:entre|partir|minimo|acima)\b', normalized):
                return False
            if key == 'max_price' and re.search(r'\b(?:partir|minimo|acima)\b', normalized) and not re.search(r'\b(?:entre|ate|maximo|abaixo)\b', normalized):
                return False
    criteria = any(args.get(key) is not None for key in ('text', 'category', 'brand', 'model', 'min_price', 'max_price')) or bool(features)
    if not criteria and args.get('available_only') is True:
        criteria = bool(re.search(r'\b(?:disponivel|disponiveis|estoque)\b', normalized))
    return criteria or (name == 'recommend_products' and bool(re.search(r'\b(?:recomende|recomenda|sugira|sugere|opcoes|alternativas)\b', normalized)))
