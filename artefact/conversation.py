"""Bounded conversation orchestration for the fictional store."""
from collections import OrderedDict
from dataclasses import dataclass, replace
import json
import re
import time

from .agent import Agent, CLARIFY, FALLBACK, TOOLS, WRITE_ISOLATION, WRITE_UNCERTAIN
from .context import explicit_customer, explicit_product, installment_followup, next_context, order_followup, product_followup, product_installment_followup, product_amount_requires_clarification
from .rendering import render_results
from .intent import normalize, product_reference, validate_catalog_intent
from .commercial import extract_installment_count, has_budget_context, money_mentions, parse_brl_mention
from .context import explicit_order_id, pix_policy_followup
from .services import Catalog
from .telemetry import event, operation_id

_GREETING = re.compile(r'^(oi|ol[aá]|bom dia|boa tarde|boa noite)[!. ]*$', re.I)
_TOOL_NAMES = {item['name'] for item in TOOLS}


@dataclass(frozen=True)
class _SinglePlan:
    name: str
    args: dict

    def plan(self, _text):
        return [(self.name, self.args)]


def _catalog_price_mismatch(text, explicit, snapshot):
    if (explicit is None or has_budget_context(text) or
            not re.search(r'\b(?:custa|preco|anunciad[oa]s?|vale)\b', normalize(text))):
        return None
    amounts = money_mentions(text)
    if len(amounts) != 1 or amounts[0] is None:
        return None
    product = Catalog(snapshot).product(explicit['product_id'])
    if product is None:
        return None
    effective = product['price_quote'].get('checkout_price_brl')
    if effective is not None and effective == amounts[0]:
        return None
    return {
        'product_name': product['name'],
        'reported_amount_brl': amounts[0],
        'catalog_amount_brl': effective,
    }


def _confirms_catalog_price(text):
    normalized = normalize(text)
    return bool('catalogo' in normalized.split() and
                re.search(r'\b(?:confirm\w*|corret\w*|cert\w*|oficial)\b', normalized))


def _safe_tool_detail(name, args, *, status, result_count=None, reason=None, origin=None):
    safe_name = name if name in _TOOL_NAMES else 'unknown'
    detail = {'name': safe_name, 'status': status}
    if result_count is not None:
        detail['result_count'] = result_count
    if reason:
        detail['reason'] = reason
    if origin:
        detail['origin'] = origin
    if safe_name == 'search_products':
        detail['text_filter'] = isinstance(args.get('text'), str) and bool(args['text'].strip())
        detail['category_filter'] = isinstance(args.get('category'), str) and bool(args['category'].strip())
        if isinstance(args.get('available_only'), bool):
            detail['available_only'] = args['available_only']
        for field in ('min_price', 'max_price'):
            if field in args:
                parsed = parse_brl_mention(args[field]) if isinstance(args[field], str) else None
                detail[field + '_brl'] = format(parsed, '.2f') if parsed is not None else None
                detail[field + '_valid'] = parsed is not None
        if type(args.get('installments')) is int:
            detail['installments'] = args['installments']
    elif safe_name == 'policy' and args.get('topic') in {
            'shipping', 'installments', 'payment_mix', 'pix', 'exchange', 'warranty',
            'regret', 'hours', 'contacts', 'scope'}:
        detail['topic'] = args['topic']
    elif safe_name == 'cheapest_product':
        detail['global_catalog_scan'] = True
    elif safe_name == 'orders':
        detail['order_id_filter_present'] = False
    return detail


def _empty_search_diagnostics(snapshot, args):
    base = {key: value for key, value in args.items()
            if key in {'text', 'category', 'min_price', 'max_price', 'available_only'}}
    for field in ('min_price', 'max_price'):
        if field in base and isinstance(base[field], str):
            base[field] = parse_brl_mention(base[field])
    if any(base.get(field) is None for field in ('min_price', 'max_price') if field in base):
        return {'candidate_counts_without_filters': {}, 'empty_filter_candidates': ['invalid_filter']}
    catalog = Catalog(snapshot)
    counts = {}
    omissions = []
    for field, label in (('text', 'text'), ('category', 'category'),
                         ('min_price', 'price_bounds'), ('max_price', 'price_bounds')):
        if field not in base:
            continue
        omitted = dict(base)
        if label == 'price_bounds':
            omitted.pop('min_price', None)
            omitted.pop('max_price', None)
        else:
            omitted.pop(field, None)
        counts[label] = len(catalog.search(**omitted, limit=50, allow_unfiltered=True))
        omissions.append(label)
    without_all = {key: value for key, value in base.items()
                   if key not in {'text', 'category', 'min_price', 'max_price'}}
    counts['without_search_filters'] = len(catalog.search(**without_all, limit=50, allow_unfiltered=True))
    candidates = list(dict.fromkeys(label for label in omissions if counts.get(label, 0) > 0))
    if not candidates and counts['without_search_filters'] == 0:
        candidates = ['no_eligible_products']
    elif not candidates:
        candidates = ['combined_filters']
    return {'candidate_counts_without_filters': counts, 'empty_filter_candidates': candidates}


def _incorrect_price_filter(args, text):
    """Classify a well-formed price bound that does not match the user's request."""
    requested = [amount for amount in money_mentions(text) if amount is not None]
    if not requested:
        return False
    for field in ('min_price', 'max_price'):
        if field not in args:
            continue
        raw = args[field]
        parsed = parse_brl_mention(raw) if isinstance(raw, str) else None
        if parsed is None or parsed not in requested:
            return True
    spoken = normalize(text)
    if 'min_price' in args and re.search(r'\b(?:ate|abaixo de|no maximo|menos de)\b', spoken):
        return True
    if 'max_price' in args and re.search(r'\b(?:a partir de|acima de|no minimo|mais de)\b', spoken):
        return True
    return False


class ConversationAgent:
    def __init__(self, snapshot, provider, *, snapshot_loader=None, review_submit=None):
        self.snapshot = snapshot
        self.provider = provider
        self.snapshot_loader = snapshot_loader
        self.review_submit = review_submit
        self.history = OrderedDict()
        self.context = OrderedDict()
        self.last_trace = {'status': 'idle', 'selected_tools': [], 'executed_tools': [], 'tool_details': [],
                           'tool_executions': [], 'rejected_tools': [], 'plan_overrides': [], 'error_code': None}

    @staticmethod
    def _remember(store, chat_id, value):
        if chat_id is None:
            return
        store[chat_id] = value
        store.move_to_end(chat_id)
        if len(store) > 100:
            store.popitem(last=False)

    def answer(self, text, chat_id=None):
        self.last_trace = {'status': 'pending', 'stage': 'validation', 'model_call': False,
                           'selected_tools': [], 'executed_tools': [], 'tool_details': [],
                           'tool_executions': [], 'rejected_tools': [], 'plan_overrides': [], 'error_code': None}
        op = operation_id()
        started = time.monotonic()
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            self.last_trace.update(status='error', error_code='invalid_message')
            event(op, 'conversation', 'complete', 'validation', 'failure')
            return FALLBACK
        if text.strip() == '/start' or _GREETING.fullmatch(text.strip()):
            self.last_trace.update(status='local', stage='greeting')
            event(op, 'conversation', 'complete', 'conversation', 'success')
            return 'Olá! Como posso ajudar com instrumentos, pedidos ou políticas da loja?'
        if re.fullmatch(r'(o que voc[eê] faz|quem [eé] voc[eê]|como voc[eê] pode ajudar)[?! .]*', text.strip(), re.I):
            self.last_trace.update(status='local', stage='capabilities')
            event(op, 'conversation', 'complete', 'conversation', 'success')
            return 'Posso ajudar com produtos, pedidos fictícios e políticas da loja.'

        prior = self.context.get(chat_id) if chat_id is not None else None
        current = next_context(text, prior, (prior.turn + 1) if prior else 1)
        self._remember(self.context, chat_id, current)
        previous = self.history.get(chat_id, []) if chat_id is not None else []
        messages = list(previous) + [{'role': 'user', 'content': text}]
        written = False
        stage = 'snapshot'
        try:
            snapshot = self.snapshot_loader() if self.snapshot_loader else self.snapshot
            explicit = explicit_product(text, snapshot)
            comparison = bool(re.search(r'(?i)\b(?:compar\w*|versus|vs)\b', text))
            if comparison:
                explicit = None
            if explicit is not None and not validate_catalog_intent('search_products', {'text': explicit['name']}, text, snapshot):
                explicit = None
            customer_id = explicit_customer(text, snapshot)
            if customer_id is not None:
                current = replace(current, customer_id=customer_id)
                self._remember(self.context, chat_id, current)
            if explicit is not None:
                role = current.amount_role
                price = Catalog(snapshot).product(explicit['product_id'])
                effective = price['price_quote'].get('checkout_price_brl') if price else None
                if (current.amount is not None and current.amount_role != 'budget_cap' and
                        (effective is None or effective != current.amount)):
                    role = 'ambiguous'
                current = replace(current, amount_role=role, product_id=explicit['product_id'],
                                  product_name=explicit['name'], origin='user')
                self._remember(self.context, chat_id, current)
            price_mismatch = _catalog_price_mismatch(text, explicit, snapshot)
            stage = 'provider'
            response = self.provider.request(messages)
            self.last_trace.update(model_call=True, stage=stage)
            output = response.get('output')
            if not isinstance(output, list):
                raise ValueError('invalid provider output')
            if any(not isinstance(item, dict) for item in output):
                raise ValueError('invalid provider item')
            event(op, 'provider', 'select', 'provider', 'success')
            calls = [item for item in output if isinstance(item, dict) and item.get('type') == 'function_call']
            self.last_trace['selected_tools'] = [
                item.get('name') if item.get('name') in _TOOL_NAMES else 'unknown'
                for item in calls if isinstance(item.get('name'), str)
            ][:3]
            details = getattr(self.provider, 'last_tool_summaries', [])
            if isinstance(details, list):
                self.last_trace['tool_details'] = [
                    dict(item) if item.get('name') in _TOOL_NAMES else {'name': 'unknown'}
                    for item in details[:3] if isinstance(item, dict) and isinstance(item.get('name'), str)
                ]
            if len(calls) > 3:
                self.last_trace.update(status='error', error_code='invalid_tool_plan')
                event(op, 'conversation', 'plan', 'validation', 'failure')
                event(op, 'conversation', 'complete', 'validation', 'failure')
                return FALLBACK
            if len(calls) > 1 and any(c.get('name') == 'request_technical_review' for c in calls):
                self.last_trace.update(status='error', error_code='tool_isolation')
                event(op, 'conversation', 'plan', 'validation', 'failure')
                event(op, 'conversation', 'complete', 'validation', 'failure')
                return WRITE_ISOLATION

            forced_plan = None
            forced_reason = None
            mismatch_result = None
            if price_mismatch is not None:
                count = extract_installment_count(text)
                quote_authorized = count is not None and _confirms_catalog_price(text)
                mismatch_result = dict(price_mismatch, catalog_quote_authorized=quote_authorized)
                args = {'text': explicit['name']}
                if quote_authorized:
                    args['installments'] = count
                forced_plan = _SinglePlan('search_products', args)
                forced_reason = 'catalog_price_authority'
            elif prior and prior.product_id is not None and product_installment_followup(text):
                count = extract_installment_count(text)
                if count is not None:
                    product = Catalog(snapshot).product(prior.product_id)
                    if product is not None:
                        forced_plan = _SinglePlan('search_products', {
                            'text': product['name'], 'installments': count})
                        forced_reason = 'valid_session_product_reference'
            elif prior and prior.policy_topic == 'pix' and pix_policy_followup(text):
                forced_plan = _SinglePlan('policy', {'topic': 'pix'})
                forced_reason = 'valid_session_policy_reference'

            if not calls and forced_plan is None:
                answer = 'Posso ajudar com produtos, pedidos e políticas da loja. O que você gostaria de saber?'
                self.last_trace.update(status='no_tool', stage='complete')
                self._remember(self.history, chat_id, (messages + [{'role': 'assistant', 'content': answer}])[-6:])
                event(op, 'conversation', 'complete', 'conversation', 'success', duration_ms=int((time.monotonic()-started)*1000))
                return answer

            stage = 'plan'
            plans = []
            seen = set()
            for call in calls:
                name, call_id = call.get('name'), call.get('call_id')
                if not isinstance(name, str) or not isinstance(call_id, str) or not call_id:
                    raise ValueError('invalid call')
                args = json.loads(call.get('arguments', ''))
                if not isinstance(args, dict):
                    raise ValueError('invalid arguments')
                args = {key: value for key, value in args.items() if value is not None}
                signature = json.dumps([name, args], sort_keys=True, ensure_ascii=True)
                if signature in seen:
                    raise ValueError('duplicate call')
                seen.add(signature)
                plans.append(_SinglePlan(name, args))
            if forced_plan is not None:
                plans = [forced_plan]
                self.last_trace['plan_overrides'].append({
                    'reason': forced_reason,
                    'tool': forced_plan.name,
                })
            event(op, 'conversation', 'plan', 'validation', 'success')

            results = ([{'consulta': 'price_mismatch', 'resultado': mismatch_result}]
                       if mismatch_result is not None else [])
            resolved_products = {}
            explicitly_mentioned = [p for p in snapshot.tables['products'] if normalize(p['name']) in normalize(text)]
            forbid_parse = current.amount_role == 'ambiguous' or product_amount_requires_clarification(text, snapshot)
            clarifications = []
            for plan in plans:
                stage = 'tool'
                detail = _safe_tool_detail(plan.name, plan.args, status='pending',
                                           origin='context_override' if forced_plan is plan else None)
                if plan.name not in _TOOL_NAMES:
                    detail.update(status='rejected', reason='invalid_tool_plan')
                    self.last_trace['tool_executions'].append(detail)
                    self.last_trace['rejected_tools'].append({'name': 'unknown', 'reason': 'invalid_tool_plan'})
                    if len(plans) > 1:
                        clarifications.append({'consulta': 'clarification', 'resultado': {'tool': 'unknown'}})
                        continue
                    self.last_trace.update(status='error', error_code='invalid_tool_plan')
                    event(op, 'conversation', 'tool', 'tool', 'failure', action='unknown')
                    event(op, 'conversation', 'complete', 'tool', 'failure')
                    return FALLBACK
                validation_text = text
                product_id = explicit['product_id'] if explicit is not None else None
                if prior and prior.product_id is not None and (product_followup(text) or
                        (prior.amount is None and product_installment_followup(text))):
                    validation_text = text + ' ' + prior.product_name
                    product_id = prior.product_id
                if prior and prior.policy_topic == 'pix' and pix_policy_followup(text) and plan.name == 'policy':
                    validation_text = text + ' Pix'
                invalid_filter = any(
                    not isinstance(plan.args.get(field), str) or
                    parse_brl_mention(plan.args.get(field)) is None
                    for field in ('min_price', 'max_price') if field in plan.args)
                if invalid_filter:
                    detail.update(status='rejected', reason='invalid_price_filter')
                    self.last_trace['tool_executions'].append(detail)
                    self.last_trace['rejected_tools'].append({'name': plan.name, 'reason': 'invalid_price_filter'})
                    if len(plans) > 1:
                        clarifications.append({'consulta': 'clarification', 'resultado': {'tool': plan.name}})
                        continue
                    self.last_trace.update(status='clarification', error_code=None)
                    event(op, 'conversation', 'tool', 'validation', 'failure', action=plan.name)
                    event(op, 'conversation', 'complete', 'validation', 'failure')
                    return CLARIFY
                request = installment_followup(text, prior) if plan.name == 'installment_quote' else None
                evidence = Agent(snapshot, plan, self.review_submit).answer(
                    validation_text, installment_request=request, forbid_installment_parse=forbid_parse,
                    forbid_product_installments=comparison or len(explicitly_mentioned) > 1, product_id=product_id,
                    customer_id=customer_id if customer_id is not None else prior.customer_id if prior and order_followup(text) else None,
                    structured=True, correlation_id=op, count_text=text)
                if isinstance(evidence, str):
                    if evidence == CLARIFY:
                        reason = ('incorrect_price_filter'
                                  if plan.name in {'search_products', 'recommend_products'} and
                                  _incorrect_price_filter(plan.args, validation_text)
                                  else 'catalog_filter_not_grounded'
                                  if plan.name in {'search_products', 'recommend_products'}
                                  else 'intent_not_grounded')
                        detail.update(status='rejected', reason=reason)
                        self.last_trace['tool_executions'].append(detail)
                        self.last_trace['rejected_tools'].append({'name': plan.name, 'reason': reason})
                        if len(plans) > 1:
                            clarifications.append({'consulta': 'clarification', 'resultado': {'tool': plan.name}})
                            continue
                        self.last_trace.update(status='clarification', error_code=None)
                        event(op, 'conversation', 'tool', 'validation', 'failure', action=plan.name)
                        event(op, 'conversation', 'complete', 'validation', 'failure')
                        return evidence
                    elif evidence == WRITE_UNCERTAIN:
                        detail.update(status='uncertain', reason='write_result_uncertain')
                        self.last_trace['tool_executions'].append(detail)
                        self.last_trace.update(status='uncertain', error_code='tool_failed')
                    else:
                        detail.update(status='error', reason='tool_failed')
                        self.last_trace['tool_executions'].append(detail)
                        self.last_trace.update(status='tool_error', error_code='tool_failed')
                    category = 'validation' if evidence == CLARIFY else 'integration' if evidence == WRITE_UNCERTAIN else 'tool'
                    event(op, 'conversation', 'tool', category, 'uncertain' if evidence == WRITE_UNCERTAIN else 'failure',
                          side_effect='unknown' if evidence == WRITE_UNCERTAIN else 'none', action=plan.name)
                    event(op, 'conversation', 'complete', category, 'uncertain' if evidence == WRITE_UNCERTAIN else 'failure',
                          side_effect='unknown' if evidence == WRITE_UNCERTAIN else 'none')
                    return evidence
                results.extend(evidence)
                self.last_trace['executed_tools'].append(plan.name)
                result_count = None
                if plan.name in {'search_products', 'recommend_products', 'cheapest_product'}:
                    result_count = len(evidence[0]['resultado'])
                detail.update(status='executed')
                if result_count is not None:
                    detail['result_count'] = result_count
                if plan.name == 'search_products' and result_count == 0:
                    detail['empty_search_diagnostics'] = _empty_search_diagnostics(snapshot, plan.args)
                self.last_trace['tool_executions'].append(detail)
                written = any(item['consulta'] == 'request_technical_review' and
                              isinstance(item['resultado'], dict) and item['resultado'].get('registered') is True
                              for item in evidence)
                event(op, 'agent', 'execute', 'tool', 'success', action=plan.name,
                      side_effect='committed' if written else 'none')
                if plan.name == 'policy' and plan.args.get('topic') in {
                        'shipping', 'installments', 'payment_mix', 'pix', 'exchange', 'warranty',
                        'regret', 'hours', 'contacts', 'scope'}:
                    current = replace(current, policy_topic=plan.args['topic'])
                    self._remember(self.context, chat_id, current)
                if plan.name == 'search_products':
                    products = evidence[0]['resultado']
                    if len(products) == 1:
                        resolved_products[products[0]['product_id']] = products[0]
            if (len(resolved_products) == 1 and len(explicitly_mentioned) <= 1 and not comparison
                    and product_reference(text, next(iter(resolved_products.values()))['name']) == 'affirmative'):
                product = next(iter(resolved_products.values()))
                current = replace(current, product_id=product['product_id'], product_name=product['name'], origin='catalog')
                self._remember(self.context, chat_id, current)
            elif len(resolved_products) > 1:
                current = replace(current, product_id=None, product_name=None)
                self._remember(self.context, chat_id, current)
            if clarifications:
                if not self.last_trace['executed_tools']:
                    self.last_trace.update(status='clarification', error_code=None)
                    event(op, 'conversation', 'complete', 'validation', 'failure')
                    return CLARIFY
                results.extend(clarifications)
            if all(item.get('resultado') in (None, [], (), {}) for item in results):
                self.last_trace.update(status='empty', stage='complete')
                event(op, 'conversation', 'complete', 'tool', 'empty', duration_ms=int((time.monotonic()-started)*1000))
                return 'Não encontrei informações suficientes para essa consulta.'
            stage = 'render'
            answer = render_results(results)
            if len(answer) > 3500:
                event(op, 'conversation', 'complete', 'validation', 'failure', side_effect='committed' if written else 'none', duration_ms=int((time.monotonic()-started)*1000))
                return 'A resposta é longa demais. Por favor, refine sua consulta.'
            self._remember(self.history, chat_id, (messages + [{'role': 'assistant', 'content': answer}])[-6:])
            self.last_trace.update(status='partial' if clarifications else 'success',
                                   stage='complete', error_code=None)
            event(op, 'conversation', 'complete', 'tool', 'success',
                  side_effect='committed' if written else 'none', duration_ms=int((time.monotonic()-started)*1000))
            return answer
        except Exception as exc:
            failure = getattr(self.provider, 'last_failure', None)
            if not failure:
                failure = {'provider': 'provider_failed', 'snapshot': 'data_snapshot_error',
                           'plan': 'invalid_tool_plan', 'tool': 'tool_failed',
                           'render': 'render_failed'}.get(stage, 'agent_failed')
            self.last_trace.update(status='error', stage=stage, error_code=failure,
                                   error_type=type(exc).__name__)
            category = 'provider' if stage == 'provider' else 'persistence' if stage == 'snapshot' else 'validation' if stage == 'plan' else 'execution'
            event(op, 'conversation', stage, category, 'failure', error=exc,
                  side_effect='committed' if written else 'none', duration_ms=int((time.monotonic()-started)*1000))
            event(op, 'conversation', 'complete', category, 'failure',
                  side_effect='committed' if written else 'none', duration_ms=int((time.monotonic()-started)*1000))
            if written:
                return 'Sua solicitação de revisão técnica foi registrada para análise.'
            return FALLBACK
