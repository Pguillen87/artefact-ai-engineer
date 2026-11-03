"""Conservative grounding for proposed catalog operations."""
from decimal import Decimal
import re
import unicodedata

from .commercial import parse_brl_mention


def normalize(value: str) -> str:
    return ' '.join(''.join(ch for ch in unicodedata.normalize('NFKD', value.casefold())
                            if not unicodedata.combining(ch)).split())


def intent_clauses(text: str) -> tuple[str, ...]:
    """Keep decimal tokens intact and separate independently requested operations."""
    return tuple(part.strip() for part in re.split(
        r'\.(?!\d)|[!?;]|\b(?:mas|porem)\b|,?\s+e\s+(?=(?:o\s+frete|quais|qual\b|nao\b|tenho\b|recomende\b))',
        normalize(text)) if part.strip())


def clause_polarity(clause: str) -> str:
    """Shared conservative polarity for a single operation, not an entire message."""
    clause = re.sub(r'\bsem juros\b', '', clause)
    if re.search(r'\b(?:nao|nunca|sem|exceto|evite|dispenso|proibido)\b', clause):
        return 'negative'
    if re.search(r'\b(?:se|caso|talvez|quando|apos|depois|desde que)\b', clause):
        return 'conditional'
    if re.search(r'\b(?:palavra|texto|exemplo de frase|mencionei)\b', clause):
        return 'incidental'
    return 'affirmative'


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
            return 'rejected'
        if polarity in {'conditional', 'incidental'}:
            return polarity
        # A trailing revocation can refer to the same product without repeating it.
        suffix = spoken[spoken.rfind(match.group()) + len(match.group()):]
        if re.search(r'\b(?:mas|porem)\s+(?:eu\s+)?(?:nao|nunca|dispenso)\b', suffix):
            return 'rejected'
        if re.search(r'\b(?:compar\w*|versus|vs)\b', spoken):
            return 'comparison'
        if re.match(r'\s*(?:custa|vale|tem|qual|quanto|preco|valor|disponibilidade|estoque|promocao|garantia|foi\s+anunciad[oa])\b', tail):
            return 'affirmative'
        if re.search(r'\b(?:quero|prefiro|escolho|escolhi|escolha|mostre|procuro|preco|valor|custa|tem|qual|quanto|disponibilidade|estoque|promocao)\b', prefix):
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


def validate_policy_intent(topic: str, text: str) -> bool:
    """Reject policy topics contradicted by an explicit topic in this turn."""
    spoken = normalize(text)
    patterns = {
        'exchange': r'\b(?:troca\w*|trocar|defeito\w*)\b',
        'warranty': r'\b(?:garantia\w*|fabricante|mau uso|cobertura)\b',
        'regret': r'\b(?:arrepend\w*|desist\w*|devolu\w*|devolver)\b',
        'shipping': r'\b(?:frete|envio|entrega|entregar|cheg\w*)\b',
        'installments': r'\b(?:parcel\w*|vezes|crediario)\b',
        'payment_mix': r'\b(?:combinar|dividir|misturar|formas de pagamento|pagamentos)\b',
        'pix': r'\bpix\b',
        'hours': r'\b(?:horario\w*|funcionamento|aberto|abre|fecha)\b',
        'contacts': r'\b(?:contato\w*|telefone|vendedor\w*|ligar)\b',
        'scope': r'\b(?:acessorios?|palhetas?|cabos|amplificadores|previsao do tempo|tempo|clima|vendem|vende)\b',
    }
    mentioned = {key for key, pattern in patterns.items() if re.search(pattern, spoken)}
    if topic not in mentioned:
        return False
    for clause in intent_clauses(text):
        if not re.search(patterns[topic], clause):
            continue
        # Negated experiences can motivate a policy question; they are not
        # refusals of the consultation. Every other negative stays negative,
        # including unfamiliar directives and pronouns ("não me informe").
        request_clause = re.sub(r'\bnao\s+(?:gostei|chegou|recebi|funciona|foi entregue)\b', '', clause)
        polarity = clause_polarity(request_clause)
        # An informational condition ("se der R$500, o frete é grátis?") is
        # different from deferring a command ("se eu pedir, consulte frete").
        if polarity == 'conditional' and not re.search(r'\b(?:consulte|mostre|informe|responda)\b', clause):
            return True
        if polarity == 'affirmative':
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


def validate_catalog_intent(name: str, args: dict, text: str, snapshot, product_id: int | None = None) -> bool:
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
    if name == 'search_products' and re.search(r'\bdisponiveis\b', normalized) and args.get('available_only') is not True:
        return False
    if args.get('available_only') is True and re.search(r'\bindisponiveis\b', normalized):
        return False
    explicit_products = [p for p in snapshot.tables['products']
                         if re.search(r'(?<!\w)' + re.escape(normalize(p['name'])) + r'(?!\w)', normalized)]
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
    spoken_terms = _category_terms(text)
    mentioned_categories = {c['category_id'] for c in snapshot.tables['categories']
                            if _category_terms(c['name']).intersection(spoken_terms)}
    if 'sopro' in spoken_terms:
        spoken = _words(text)
        qualifier = 'metais' if spoken.intersection({'metal','metais'}) else 'madeiras' if spoken.intersection({'madeira','madeiras'}) else None
        if qualifier:
            mentioned_categories = {c['category_id'] for c in snapshot.tables['categories']
                                    if c['category_id'] in mentioned_categories and qualifier in _words(c['name'])}
    if len(mentioned_categories) > 1 and len(explicit_products) != 1:
        return False
    if category is not None:
        if not isinstance(category, str) or not category.strip():
            return False
        selected = [c['category_id'] for c in snapshot.tables['categories'] if normalize(c['name']) == normalize(category)]
        if len(selected) != 1:
            return False
        if selected[0] not in mentioned_categories and not (len(explicit_products) == 1 and explicit_products[0]['category_id'] == selected[0]):
            return False
    elif mentioned_categories and not (len(explicit_products) == 1 and explicit_products[0]['category_id'] in mentioned_categories):
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
    criteria = any(args.get(key) is not None for key in ('text', 'category', 'min_price', 'max_price'))
    if not criteria and args.get('available_only') is True:
        criteria = bool(re.search(r'\b(?:disponivel|disponiveis|estoque)\b', normalized))
    return criteria or (name == 'recommend_products' and bool(re.search(r'\b(?:recomende|recomenda|sugira|sugere|opcoes|alternativas)\b', normalized)))
