"""Validate limited model-written prose around canonical Python-rendered facts."""
import json
import re
import unicodedata


_COMMERCIAL_CLAIM = re.compile(
    r'\b(?:reais?|precos?|valores?|estoque|disponive(?:l|is)|indisponive(?:l|is)|'
    r'promoc(?:ao|oes)|descontos?|pix|parcelas?|parcelamento|juros|frete|entregas?|'
    r'garantias?|trocas?|devolucoes?|prazos?|vigencias?|condicoes?|taxas?|'
    r'cartoes?|parcelad[oa]s?|gratis|gratuito|custa|custam|comprar|compra agora)\b|(?<!\w)r\$|%'
)
_SAFE_COMMERCIAL_QUESTIONS = (
    re.compile(r'^(?:quer|gostaria de) (?:saber|conhecer|ver|consultar)(?: mais)? (?:sobre )?(?:a|as|o|os)?\s*'
               r'(?:condicoes de pagamento|formas de pagamento|parcelamento|politica de frete|frete|garantia|troca)\?$'),
    re.compile(r'^(?:quer|gostaria de) que eu (?:consulte|explique|mostre) (?:a|as|o|os)?\s*'
               r'(?:condicoes de pagamento|formas de pagamento|politica de frete|frete|parcelamento|garantia|troca)\?$'),
)
_DEFAULT_NEXT_STEPS = (
    'Qual faixa de preço você tem em mente?',
    'Qual desses modelos você quer conhecer melhor?',
    'Que tipo de instrumento ou categoria te interessa?',
)
_BUDGET_REASK = re.compile(
    r'\b(?:orcamento|faixa de preco|quanto (?:voce )?(?:quer|pretende|pode) (?:gastar|pagar)|'
    r'quanto para gastar|qual valor (?:pretende|quer) gastar|qual seu limite)\b'
)
_CATEGORY_REASK = re.compile(r'\b(?:qual|que) (?:tipo|categoria|instrumento)\b')
_BRAND_REASK = re.compile(r'\bqual marca\b')
_PRODUCT_REASK = re.compile(r'\b(?:qual|que) (?:modelo|produto|instrumento)\b|\bqual desses modelos\b')


def _response_text(response):
    output = response.get('output') if isinstance(response, dict) else None
    if not isinstance(output, list) or any(
            isinstance(item, dict) and item.get('type') == 'function_call' for item in output):
        return None
    pieces = []
    for item in output:
        if not isinstance(item, dict) or item.get('type') != 'message':
            continue
        content = item.get('content', [])
        if not isinstance(content, list):
            continue
        for part in content:
            if (isinstance(part, dict) and part.get('type') == 'output_text' and
                    isinstance(part.get('text'), str)):
                pieces.append(part['text'])
    return '\n'.join(pieces).strip() or None


def _normalised(text):
    decomposed = unicodedata.normalize('NFKD', text.casefold())
    return ''.join(char for char in decomposed if not unicodedata.combining(char))


def _safe_commercial_question(value):
    normalized = _normalised(value.strip())
    return any(pattern.fullmatch(normalized) for pattern in _SAFE_COMMERCIAL_QUESTIONS)


def _safe_generated_text(value, *, max_length, question=False):
    if (not isinstance(value, str) or len(value) > max_length or
            any(char.isdigit() or char in '\r\n' or unicodedata.category(char) == 'Cc'
                for char in value)):
        return False
    if _COMMERCIAL_CLAIM.search(_normalised(value)) and not (question and _safe_commercial_question(value)):
        return False
    if question:
        return value == '' or (value.endswith('?') and value.count('?') == 1)
    return value == '' or not value.endswith('?')


def _text_rejection(value, *, max_length, question=False):
    if not isinstance(value, str):
        return 'field_not_text', 'field_type'
    if len(value) > max_length:
        return 'field_too_long', 'length'
    if any(char.isdigit() for char in value):
        return 'field_contains_digit', 'digit'
    if any(char in '\r\n' for char in value):
        return 'field_contains_line_break', 'line_break'
    if any(unicodedata.category(char) == 'Cc' for char in value):
        return 'field_contains_control_character', 'control_character'
    if _COMMERCIAL_CLAIM.search(_normalised(value)) and not (question and _safe_commercial_question(value)):
        return 'field_contains_commercial_claim', 'commercial_claim'
    if question and value and (not value.endswith('?') or value.count('?') != 1):
        return 'invalid_question_shape', 'question_shape'
    if not question and value.endswith('?'):
        return 'opening_contains_question', 'question_shape'
    return None, None


def _known_preference_rejection(text, known_context):
    normalized = _normalised(text)
    if known_context.get('budget_already_provided') and _BUDGET_REASK.search(normalized):
        return 'repeats_known_budget'
    if known_context.get('category') and _CATEGORY_REASK.search(normalized):
        return 'repeats_known_category'
    if known_context.get('brand') and _BRAND_REASK.search(normalized):
        return 'repeats_known_brand'
    if known_context.get('specific_product_already_selected') and _PRODUCT_REASK.search(normalized):
        return 'repeats_known_product'
    for feature in known_context.get('features', ()):
        feature_text = _normalised(feature)
        if feature_text and re.search(
                r'\b(?:prefere|quer|gosta|busca)\b.{0,40}\b' + re.escape(feature_text) + r'\b',
                normalized):
            return 'repeats_known_feature'
    return None


def _safe_refs(values):
    if not isinstance(values, list) or len(values) > 16:
        return []
    return [item for item in values if isinstance(item, str) and re.fullmatch(r'E[0-9]{1,3}', item)]


def _safe_product_ids(values):
    if not isinstance(values, (list, tuple)):
        return []
    return [item for item in values[:50] if type(item) is int and 0 < item <= 2**31 - 1]


def validated_final_response_with_diagnostics(response, expected_refs=('E1',), product_ids=(),
                                              known_context=None, require_all_product_ids=False):
    """Return short, noncommercial prose and an optional validated product order.

    The text guards reject common commercial claims, currency, and numeric details.
    They are deliberately narrow and do not prove semantic grounding. An empty
    optional product order means Python keeps the canonical display order.
    """
    raw = _response_text(response)
    diagnostics = {
        'reason_code': None,
        'guard': None,
        'field': None,
        'response_text_present': raw is not None,
        'response_text_length': len(raw) if raw is not None else 0,
        'validated_evidence_refs': [],
        'validated_product_ids': [],
        'product_order_validated': False,
    }

    def reject(reason, guard, field=None):
        diagnostics.update(reason_code=reason, guard=guard, field=field)
        return None, diagnostics

    if raw is None:
        return reject('response_text_missing', 'response_text')
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return reject('invalid_json', 'json_parse')
    if not isinstance(value, dict):
        return reject('invalid_json_object', 'json_shape')
    if set(value) not in ({'opening', 'next_step', 'evidence_refs'},
                          {'opening', 'next_step', 'evidence_refs', 'product_order'}):
        return reject('unexpected_fields', 'schema', 'object')
    refs = list(expected_refs)
    safe_expected_refs = _safe_refs(refs)
    if (value.get('evidence_refs') != refs or not refs or len(refs) > 16 or
            len(safe_expected_refs) != len(refs)):
        return reject('evidence_refs_mismatch', 'evidence_refs', 'evidence_refs')
    diagnostics['validated_evidence_refs'] = safe_expected_refs
    for field, limit, question in (('opening', 180, False), ('next_step', 200, True)):
        reason, guard = _text_rejection(value.get(field), max_length=limit, question=question)
        if reason:
            return reject(f'{field}_{reason.removeprefix("field_")}', guard, field)
    preference_reason = _known_preference_rejection(value.get('next_step', ''), known_context or {})
    if preference_reason:
        return reject(preference_reason, 'known_context', 'next_step')
    allowed_ids = tuple(product_ids)
    product_order = None
    if 'product_order' in value:
        proposed = value['product_order']
        if proposed is not None:
            if not isinstance(proposed, list):
                return reject('product_order_not_list', 'product_order', 'product_order')
            if proposed:
                if len(proposed) > len(allowed_ids):
                    return reject('product_order_too_long', 'product_order', 'product_order')
                if any(type(item) is not int for item in proposed):
                    return reject('product_order_ids_not_integers', 'product_order', 'product_order')
                if len(set(proposed)) != len(proposed):
                    return reject('duplicate_product_order_ids', 'product_order', 'product_order')
                if not set(proposed).issubset(set(allowed_ids)):
                    return reject('unknown_product_order_ids', 'product_order', 'product_order')
                if require_all_product_ids and set(proposed) != set(allowed_ids):
                    return reject('incomplete_product_order', 'product_order', 'product_order')
                product_order = tuple(proposed)
                diagnostics['validated_product_ids'] = _safe_product_ids(proposed)
                diagnostics['product_order_validated'] = True
    return {
        'opening': value['opening'].strip(),
        'next_step': value['next_step'].strip(),
        'product_order': product_order,
    }, diagnostics


def validated_final_response_with_reason(response, expected_refs=('E1',), product_ids=(), known_context=None,
                                         require_all_product_ids=False):
    result, diagnostics = validated_final_response_with_diagnostics(
        response, expected_refs, product_ids, known_context, require_all_product_ids)
    return result, diagnostics['reason_code']


def validated_final_response(response, expected_refs=('E1',), product_ids=(), known_context=None,
                             require_all_product_ids=False):
    result, _diagnostics = validated_final_response_with_diagnostics(
        response, expected_refs, product_ids, known_context, require_all_product_ids)
    return result


def communication_context(current, previous=None):
    """Expose only validated preferences needed to avoid repeating questions."""
    pending = current.pending_search or (previous.pending_search if previous else None)
    known = {}
    if pending is not None:
        for key in ('category', 'brand', 'model'):
            value = getattr(pending, key)
            if value:
                known[key] = value
        if pending.features:
            known['features'] = list(pending.features)
    basis = current.commercial
    known['budget_already_provided'] = bool(
        (pending and (pending.min_price is not None or pending.max_price is not None)) or
        (basis and basis.kind == 'budget'))
    known['specific_product_already_selected'] = bool(current.product_id)
    return known


def _reasks_known_preference(text, known_context):
    normalized = _normalised(text)
    if known_context.get('budget_already_provided') and _BUDGET_REASK.search(normalized):
        return True
    if known_context.get('category') and _CATEGORY_REASK.search(normalized):
        return True
    if known_context.get('brand') and _BRAND_REASK.search(normalized):
        return True
    if known_context.get('specific_product_already_selected') and _PRODUCT_REASK.search(normalized):
        return True
    for feature in known_context.get('features', ()):
        feature_text = _normalised(feature)
        if feature_text and re.search(
                r'\b(?:prefere|quer|gosta|busca)\b.{0,40}\b' + re.escape(feature_text) + r'\b',
                normalized):
            return True
    return False


def apply_final_response(facts, generated):
    """Place approved prose around the unchanged canonical fact block."""
    if (not isinstance(facts, str) or not isinstance(generated, dict) or
            not isinstance(generated.get('opening'), str) or
            not isinstance(generated.get('next_step'), str)):
        raise ValueError('invalid final answer parts')
    body = facts
    next_step = generated['next_step'].strip()
    if next_step:
        replaced = False
        for default in _DEFAULT_NEXT_STEPS:
            suffix = '\n\n' + default
            if body.endswith(suffix):
                body = body[:-len(suffix)]
                replaced = True
                break
        if replaced or '?' not in body:
            body = body.rstrip() + '\n\n' + next_step
    opening = generated['opening'].strip()
    return f'{opening}\n\n{body}' if opening else body
