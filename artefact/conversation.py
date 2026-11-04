"""Bounded conversation orchestration for the fictional store."""
from collections import OrderedDict
from dataclasses import dataclass, replace
import json
import re
import time

from .agent import Agent, CLARIFY, FALLBACK, TOOLS, WRITE_ISOLATION, WRITE_UNCERTAIN, prepare_commercial_call, validate_execution_plan
from .context import explicit_customer, explicit_product, next_context, order_followup, product_followup, resolve_commercial_basis, confirms_catalog_price
from .rendering import render_results
from .sales import render_sales_results, resolve_budget_reply, BUDGET_QUESTION
from .intent import normalize, product_reference, validate_catalog_intent, validate_policy_intent
from .commercial import money_mentions, parse_brl_mention
from .context import pix_policy_followup
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


def _confirms_catalog_price(text):
    # Compatibility for historical callers; the decision itself owns authority.
    return confirms_catalog_price(text)


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
    elif safe_name == 'installment_quote':
        amount = parse_brl_mention(args['amount_brl']) if isinstance(args.get('amount_brl'), str) else None
        detail['amount_brl'] = format(amount, '.2f') if amount is not None else None
        if type(args.get('installments')) is int:
            detail['installments'] = args['installments']
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
        prior = self.context.get(chat_id) if chat_id is not None else None
        if prior and prior.awaiting_budget:
            self._remember(self.context, chat_id, replace(prior, awaiting_budget=False))
        if text.strip() == '/start' or _GREETING.fullmatch(text.strip()):
            self.last_trace.update(status='local', stage='greeting')
            event(op, 'conversation', 'complete', 'conversation', 'success')
            return 'Olá! Bem-vindo ao Empório da Música. Qual instrumento você está procurando? Também posso ajudar com um pedido ou uma dúvida sobre a loja.'
        if re.fullmatch(r'(o que voc[eê] faz|quem [eé] voc[eê]|como voc[eê] pode ajudar)[?! .]*', text.strip(), re.I):
            self.last_trace.update(status='local', stage='capabilities')
            event(op, 'conversation', 'complete', 'conversation', 'success')
            return 'Posso ajudar com produtos, pedidos fictícios e políticas da loja.'

        current = replace(next_context(text, prior, (prior.turn + 1) if prior else 1), awaiting_budget=False)
        self._remember(self.context, chat_id, current)
        previous = self.history.get(chat_id, []) if chat_id is not None else []
        messages = list(previous) + [{'role': 'user', 'content': text}]
        written = False
        stage = 'snapshot'
        try:
            snapshot = self.snapshot_loader() if self.snapshot_loader else self.snapshot
            scoped_text = resolve_budget_reply(text, prior, snapshot)
            if scoped_text != text:
                messages[-1] = {'role':'user','content':scoped_text}
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
            basis = resolve_commercial_basis(scoped_text, snapshot, prior)
            self.last_trace['commercial_basis'] = {
                'kind': basis.kind, 'amount_role': basis.amount_role,
                'amount_brl': format(basis.amount, '.2f') if basis.amount is not None else None,
                'catalog_amount_brl': format(basis.catalog_amount, '.2f') if basis.catalog_amount is not None else None,
                'total_amount_brl': format(basis.total_amount, '.2f') if basis.total_amount is not None else None,
                'restriction': basis.restriction, 'source': basis.source,
                'price_source': basis.price_source,
                'authorized_amount_brl': str(basis.quote_amount) if basis.quote_allowed else None,
                'product_id': basis.product['product_id'] if basis.product else None,
                'values': [{'role': value.role, 'amount_brl': str(value.amount)} for value in basis.values]}
            current = replace(current, commercial=basis)
            if explicit is not None:
                current = replace(current, product_id=explicit['product_id'], product_name=explicit['name'], origin='user')
            self._remember(self.context, chat_id, current)
            price_mismatch = basis.mismatch()
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
                return WRITE_ISOLATION

            forced_plan = None
            mismatch_result = price_mismatch
            if basis.source == 'session' and basis.kind == 'catalog' and basis.quote_allowed:
                forced_plan = _SinglePlan('search_products', {'text': basis.product['name'], 'installments': basis.count})
            elif prior and prior.policy_topic == 'pix' and pix_policy_followup(text):
                forced_plan = _SinglePlan('policy', {'topic': 'pix'})
            if not calls and forced_plan is None and mismatch_result is None:
                answer = 'Posso ajudar com produtos, pedidos e políticas da loja. O que você gostaria de saber?'
                self.last_trace.update(status='no_tool', stage='complete')
                self._remember(self.history, chat_id, (messages + [{'role': 'assistant', 'content': answer}])[-6:])
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
            if forced_plan is not None and not plans:
                plans = [forced_plan]
                self.last_trace['plan_overrides'].append({'reason': 'valid_session_reference', 'tool': forced_plan.name})
            if forced_plan is not None and forced_plan.name == 'policy':
                plans = [forced_plan if plan.name == 'policy' else plan for plan in plans]
            # Context supplies identity only for bounded references. It does not
            # amend the user's current words to manufacture consent.
            validation_text = scoped_text
            product_id = explicit['product_id'] if explicit is not None else None
            if prior and basis.source == 'session' and basis.product and not basis.restriction:
                validation_text = text + ' ' + basis.product['name']
                product_id = basis.product['product_id']
            elif prior and prior.product_id is not None and product_followup(text) and not basis.restriction:
                validation_text = text + ' ' + prior.product_name
                product_id = prior.product_id
            normalized_plans = []
            commercial_rejections = []
            for plan in plans:
                try:
                    name, args = prepare_commercial_call(plan.name, plan.args, validation_text, snapshot, basis, product_id)
                except ValueError as exc:
                    reason = 'conflicting_price_basis' if mismatch_result is not None else str(exc)
                    if reason == 'ungrounded_catalog':
                        invalid_filter = any(not isinstance(plan.args.get(field), str) or parse_brl_mention(plan.args[field]) is None
                                             for field in ('min_price', 'max_price') if field in plan.args)
                        reason = ('invalid_price_filter' if invalid_filter else 'incorrect_price_filter'
                                  if _incorrect_price_filter(plan.args, validation_text) else 'catalog_filter_not_grounded')
                    if plan.name == 'installment_quote' and reason in {'invalid_quote_amount', 'invalid_quote_count'}:
                        commercial_rejections.append({'consulta': 'execution_clarification', 'resultado': {'tool': plan.name}})
                    elif plan.name == 'installment_quote':
                        commercial_rejections.append({'consulta': 'installment_quote', 'resultado': {'status': 'clarification'}})
                    elif reason in {'invalid_price_filter', 'incorrect_price_filter', 'catalog_filter_not_grounded'}:
                        commercial_rejections.append({'consulta': 'clarification', 'resultado': {'tool': plan.name}})
                    else:
                        commercial_rejections.append({'consulta': 'commercial_clarification', 'resultado': {'reason': reason}})
                    self.last_trace['tool_executions'].append(_safe_tool_detail(plan.name, plan.args, status='rejected', reason=reason))
                    self.last_trace['rejected_tools'].append({'name': plan.name, 'reason': reason})
                    # Invalid arguments cannot authorize a mismatch quote either.
                    if mismatch_result is not None:
                        mismatch_result['catalog_quote_authorized'] = False
                    continue
                if name != plan.name:
                    self.last_trace['plan_overrides'].append({'reason': 'resolved_commercial_basis', 'tool': name})
                normalized_plans.append(_SinglePlan(name, args))
            plans = normalized_plans
            # The executor receives the final whole plan, including contextual
            # overrides, so its write isolation cannot be bypassed by per-call execution.
            final_plan = [(plan.name, plan.args) for plan in plans]
            if final_plan:
                invalid = validate_execution_plan(final_plan)
                if invalid:
                    self.last_trace.update(status='error', error_code='tool_isolation')
                    return invalid
            event(op, 'conversation', 'plan', 'validation', 'success')

            results = ([{'consulta': 'price_mismatch', 'resultado': mismatch_result}]
                       if mismatch_result is not None else [])
            resolved_products = {}
            explicitly_mentioned = [p for p in snapshot.tables['products'] if normalize(p['name']) in normalize(text)]
            clarifications = [] if mismatch_result is not None else commercial_rejections
            if (mismatch_result is not None and validate_policy_intent('shipping', text)
                    and not any(plan.name == 'policy' and plan.args.get('topic') == 'shipping' for plan in plans)):
                clarifications.append({'consulta': 'clarification', 'resultado': {'tool': 'policy', 'topic': 'shipping'}})
            for plan in plans:
                stage = 'tool'
                detail = _safe_tool_detail(plan.name, plan.args, status='pending',
                                           origin='context_override' if forced_plan is plan else None)
                if plan.name not in _TOOL_NAMES:
                    detail.update(status='rejected', reason='invalid_tool_plan')
                    self.last_trace['tool_executions'].append(detail)
                    self.last_trace['rejected_tools'].append({'name': 'unknown', 'reason': 'invalid_tool_plan'})
                    if len(plans) > 1 or results:
                        clarifications.append({'consulta': 'clarification', 'resultado': {'tool': 'unknown'}})
                        continue
                    self.last_trace.update(status='error', error_code='invalid_tool_plan')
                    event(op, 'conversation', 'tool', 'tool', 'failure', action='unknown')
                    event(op, 'conversation', 'complete', 'tool', 'failure')
                    return FALLBACK
                tool_text = validation_text
                if prior and prior.policy_topic == 'pix' and pix_policy_followup(text) and plan.name == 'policy':
                    tool_text = text + ' Pix'
                invalid_filter = any(
                    not isinstance(plan.args.get(field), str) or
                    parse_brl_mention(plan.args.get(field)) is None
                    for field in ('min_price', 'max_price') if field in plan.args)
                if invalid_filter:
                    detail.update(status='rejected', reason='invalid_price_filter')
                    self.last_trace['tool_executions'].append(detail)
                    self.last_trace['rejected_tools'].append({'name': plan.name, 'reason': 'invalid_price_filter'})
                    if len(plans) > 1 or results:
                        clarifications.append({'consulta': 'clarification', 'resultado': {'tool': plan.name}})
                        continue
                    self.last_trace.update(status='clarification', error_code=None)
                    event(op, 'conversation', 'tool', 'validation', 'failure', action=plan.name)
                    event(op, 'conversation', 'complete', 'validation', 'failure')
                    return render_results(clarifications)
                evidence = Agent(snapshot, plan, self.review_submit).answer(
                    tool_text, product_id=product_id,
                    customer_id=customer_id if customer_id is not None else prior.customer_id if prior and order_followup(text) else None,
                    structured=True, correlation_id=op, commercial_decision=basis, execution_plan=final_plan)
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
                        if len(plans) > 1 or results:
                            need = {'tool': plan.name}
                            if plan.name == 'policy' and validate_policy_intent('shipping', text):
                                need['topic'] = 'shipping'
                            clarifications.append({'consulta': 'clarification', 'resultado': need})
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
                if not self.last_trace['executed_tools'] and not results:
                    self.last_trace.update(status='clarification', error_code=None)
                    event(op, 'conversation', 'complete', 'validation', 'failure')
                    return render_results(clarifications)
                results.extend(clarifications)
            has_empty_catalog_result = any(
                entry.get('consulta') in {'search_products', 'recommend_products'} and
                entry.get('resultado') == [] and entry.get('search_context') is not None
                for entry in results)
            results_are_empty = all(item.get('resultado') in (None, [], (), {}) for item in results)
            if results_are_empty and not has_empty_catalog_result:
                self.last_trace.update(status='empty', stage='complete')
                event(op, 'conversation', 'complete', 'tool', 'empty', duration_ms=int((time.monotonic()-started)*1000))
                return 'Não encontrei informações suficientes para essa consulta.'
            stage = 'render'
            if mismatch_result is not None:
                mismatch_result['catalog_quote_executed'] = any(
                    entry['consulta'] == 'installment_quote' and entry['resultado'].get('status') in {'eligible', 'below_minimum'} or
                    entry['consulta'] == 'search_products' and any(item.get('installment_quote', {}).get('status') in {'eligible', 'below_minimum'}
                                                                 for item in entry['resultado'])
                    for entry in results)
            answer = render_sales_results(results, scoped_text, snapshot)
            if answer.endswith(BUDGET_QUESTION):
                current = replace(current, awaiting_budget=True)
                self._remember(self.context, chat_id, current)
            if len(answer) > 3500:
                event(op, 'conversation', 'complete', 'validation', 'failure', side_effect='committed' if written else 'none', duration_ms=int((time.monotonic()-started)*1000))
                return 'A resposta é longa demais. Por favor, refine sua consulta.'
            self._remember(self.history, chat_id, (messages + [{'role': 'assistant', 'content': answer}])[-6:])
            status = ('empty' if results_are_empty and has_empty_catalog_result else
                      'clarification' if not self.last_trace['executed_tools'] else
                      'partial' if clarifications or mismatch_result is not None else 'success')
            self.last_trace.update(status=status,
                                   stage='complete', error_code=None)
            event(op, 'conversation', 'complete', 'tool', 'empty' if status == 'empty' else 'success',
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
