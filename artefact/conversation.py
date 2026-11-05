"""Bounded conversation orchestration for the fictional store."""
from collections import OrderedDict
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import inspect
import json
import re
import time

from .agent import (Agent, CLARIFY, FALLBACK, ORDER_AUTH_CLARIFY, TOOLS, WRITE_ISOLATION, WRITE_UNCERTAIN,
                    prepare_execution_plan, prepared_rejection_result)
from .chat_limits import MAX_ASSISTANT_ANSWER_CHARS, MAX_USER_MESSAGE_CHARS
from .context import (ConversationContext, complete_catalog_list_request, customer_id_for_contact, explicit_order_id,
                      explicit_product, has_customer_contact,
                      local_order_lookup_requested, next_context, order_followup, product_followup,
                      redact_contact_details, redact_customer_names,
                      resolve_commercial_basis, confirms_catalog_price)
from .rendering import render_results
from .sales import (pending_from_plan, pending_search_action, pending_search_arguments,
                    pending_search_grounding_text, displayed_product_ids, record_final_product_order,
                    record_search_page,
                    render_sales_results, render_sales_answer_within_limit,
                    resolve_budget_reply, BUDGET_QUESTION, PRODUCT_PAGE_SIZE)
from .final_response import (apply_final_response, communication_context,
                             validated_final_response_with_diagnostics)
from .intent import (direct_catalog_plan, direct_product_status_plan, is_shipping_quote_request,
                     local_shipping_destination, normalize, product_reference, mentioned_products,
                     has_independent_catalog_request, requested_policy_topics, validate_policy_intent)
from .commercial import money_mentions, parse_brl_mention
from .context import pix_policy_followup
from .services import Catalog, OrderService, MAX_SEARCH_RESULTS
from .telemetry import event, operation_id

_TOOL_NAMES = {item['name'] for item in TOOLS}


def local_social_reply(text):
    spoken = normalize(text.strip()) if isinstance(text, str) else ''
    if re.fullmatch(r'(?:oi|ola|bom dia|boa tarde|boa noite)(?:[, ]+tudo bem)?[?.! ]*', spoken):
        return ('Olá! Tudo bem por aqui. Sou o assistente virtual do Empório da Música. '
                'Posso ajudar com produtos, pedidos e informações da loja.')
    identity_text = re.sub(
        r'^(?:oi|ola|bom dia|boa tarde|boa noite)(?:[,.!? ]+tudo bem)?[,.!? ]+', '', spoken)
    if re.fullmatch(r'(?:com quem eu falo|quem esta falando|quem e voce|qual seu nome)(?: por favor)?[?.! ]*', identity_text):
        return ('Você está falando com o assistente virtual do Empório da Música. '
                'Posso ajudar com produtos, pedidos e dúvidas da loja.')
    if re.fullmatch(r'(?:o que voce faz|quem e voce|como voce pode ajudar)[?! .]*', identity_text):
        return ('Sou o assistente virtual do Empório da Música. Posso ajudar com produtos, pedidos '
                'e políticas da loja.')
    return None


def is_local_social_request(text):
    return local_social_reply(text) is not None


def _shipping_quote_clarification(text, snapshot, basis):
    if (requested_policy_topics(text) != {'shipping'} or
            not is_shipping_quote_request(text, snapshot)):
        return None
    if re.search(r'\bpiano\s+digit(?:al|ais)\b', normalize(text)):
        return ('Para piano digital, o frete e o prazo precisam de cotação especial com a loja. '
                'A taxa e o prazo ainda não estão confirmados; qual é o modelo exato para solicitar a verificação?')
    destination_known = local_shipping_destination(text)
    products = mentioned_products(text, snapshot)
    product = products[0] if len(products) == 1 else None
    if product is not None and product_reference(text, product['name']) not in {'affirmative', 'mentioned'}:
        product = None
    if not destination_known and product is None:
        return ('Para calcular o total, informe o nome ou modelo do produto e o CEP de entrega. '
                'A cotação local automática cobre Campo Grande/MS.')
    if not destination_known:
        return (f'Para cotar o frete de {product["name"]}, qual é o CEP de entrega? '
                'A cotação automática cobre Campo Grande/MS; outros destinos precisam de cotação da loja.')
    if product is None:
        if products:
            return ('Não consegui identificar um produto exato nesta consulta. '
                    'Confirme o nome ou modelo que deseja cotar para Campo Grande/MS.')
        return 'Qual é o nome ou modelo do instrumento que deseja cotar para Campo Grande/MS?'
    if basis.mismatch():
        return (f'O valor informado para {product["name"]} diverge do preço efetivo do catálogo. '
                'Quer que eu calcule com o preço cadastrado?')
    return None


def _direct_plan_for_request(text, snapshot, basis):
    price_mismatch = basis.mismatch()
    policy_topics = requested_policy_topics(text)
    direct_status = direct_product_status_plan(text, snapshot)
    if (price_mismatch is not None or basis.count is not None or
            basis.scope.max_price is not None):
        direct_status = None
    catalog_plan = direct_catalog_plan(text, snapshot)
    if (basis.kind == 'catalog' and basis.product is not None and basis.quote_allowed and
            policy_topics.issubset({'installments'})):
        return ('search_products', {'text': basis.product['name'],
                                    'available_only': False,
                                    'installments': basis.count})
    if (basis.kind in {'hypothetical', 'total'} and basis.product is None and
            basis.quote_allowed and policy_topics.issubset({'installments'})):
        quote_request = basis.request()
        if quote_request is not None:
            return ('installment_quote', {
                'amount_brl': format(quote_request.stated_amount, '.2f'),
                'installments': quote_request.count})
    if (policy_topics == {'pix'} and price_mismatch is None and basis.restriction is None and re.search(
            r'\b(?:pre[cç]o|quanto\s+(?:custa|fica)|valor|custa|promoc\w*|desconto\w*)\b',
            normalize(text))):
        products = [product for product in mentioned_products(text, snapshot)
                    if product_reference(text, product['name']) in {'affirmative', 'mentioned'}]
        if len(products) == 1:
            return ('search_products', {'text': products[0]['name']})
    if direct_status is not None and not policy_topics:
        return direct_status
    if (len(policy_topics) == 1 and price_mismatch is None and basis.restriction is None and
            not direct_status and not has_independent_catalog_request(text, snapshot)):
        return ('policy', {'topic': next(iter(policy_topics))})
    if catalog_plan is not None and not policy_topics:
        return catalog_plan
    return None


def local_only_request(text, snapshot, previous=None):
    """Only greetings and order lookups terminate locally without a provider."""
    return bool(text.strip() == '/start' or is_local_social_request(text) or
                local_order_lookup_requested(text, previous, snapshot))


@dataclass(frozen=True)
class _SinglePlan:
    name: str
    args: dict

    def plan(self, _text):
        return [(self.name, self.args)]


def _confirms_catalog_price(text):
    # Compatibility for historical callers; the decision itself owns authority.
    return confirms_catalog_price(text)


def _add_timing(trace, stage, started):
    elapsed = max(0, int((time.monotonic() - started) * 1000))
    timings = trace.setdefault('timings_ms', {})
    timings[stage] = timings.get(stage, 0) + elapsed
    return elapsed


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
        detail['brand_filter'] = isinstance(args.get('brand'), str) and bool(args['brand'].strip())
        detail['model_filter'] = isinstance(args.get('model'), str) and bool(args['model'].strip())
        if isinstance(args.get('features'), list):
            detail['feature_filter_count'] = len(args['features'])
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
        detail['order_id_filter_present'] = type(args.get('order_id')) is int and args['order_id'] > 0
    return detail


def _empty_search_diagnostics(snapshot, args):
    base = {key: value for key, value in args.items()
            if key in {'text', 'category', 'brand', 'model', 'features',
                       'min_price', 'max_price', 'available_only'}}
    for field in ('min_price', 'max_price'):
        if field in base and isinstance(base[field], str):
            base[field] = parse_brl_mention(base[field])
    if any(base.get(field) is None for field in ('min_price', 'max_price') if field in base):
        return {'candidate_counts_without_filters': {}, 'empty_filter_candidates': ['invalid_filter']}
    catalog = Catalog(snapshot)
    counts = {}
    omissions = []
    for field, label in (('text', 'text'), ('category', 'category'),
                         ('brand', 'brand'), ('model', 'model'), ('features', 'features'),
                         ('min_price', 'price_bounds'), ('max_price', 'price_bounds')):
        if field not in base:
            continue
        omitted = dict(base)
        if label == 'price_bounds':
            omitted.pop('min_price', None)
            omitted.pop('max_price', None)
        else:
            omitted.pop(field, None)
        counts[label] = len(catalog.search(**omitted, limit=MAX_SEARCH_RESULTS, allow_unfiltered=True))
        omissions.append(label)
    without_all = {key: value for key, value in base.items()
                   if key not in {'text', 'category', 'brand', 'model', 'features', 'min_price', 'max_price'}}
    counts['without_search_filters'] = len(catalog.search(**without_all, limit=MAX_SEARCH_RESULTS, allow_unfiltered=True))
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

    def _request_model(self, messages, *, final, stage, operation):
        calls = self.last_trace['model_calls']
        if len(calls) >= 2:
            raise ValueError('per-turn model-call limit exceeded')
        try:
            parameters = inspect.signature(self.provider.request).parameters.values()
            # Opt in only when the provider declares this part of the contract.
            # Generic mocks and legacy adapters often expose **kwargs without
            # implementing a final-response mode.
            supports_final = any(parameter.name == 'final' for parameter in parameters)
        except (TypeError, ValueError):
            supports_final = False
        if final and not supports_final:
            self.last_trace['final_response_status'] = 'unsupported'
            return None
        started = time.monotonic()
        self.last_trace['model_call'] = True
        self.last_trace['stage'] = stage
        try:
            response = (self.provider.request(messages, final=final) if supports_final
                        else self.provider.request(messages))
        except Exception as exc:
            _add_timing(self.last_trace, stage, started)
            failure = (getattr(exc, 'category', None) or
                       getattr(self.provider, 'last_failure', None))
            if not isinstance(failure, str) or not re.fullmatch(r'[a-z_]{1,64}', failure):
                failure = 'provider_failed'
            entry = {'stage': stage, 'status': 'failure', 'error_code': failure,
                     'duration_ms': int((time.monotonic() - started) * 1000)}
            calls.append(entry)
            self.last_trace['model_call_count'] = len(calls)
            event(operation, 'provider', stage, 'provider', 'failure', error=exc,
                  duration_ms=entry['duration_ms'])
            raise
        _add_timing(self.last_trace, stage, started)
        usage = response.get('usage', {}) if isinstance(response, dict) else {}
        sdk_metadata = getattr(self.provider, 'last_response_metadata', {}) if final else {}
        if final and isinstance(sdk_metadata, dict):
            safe_sdk_metadata = {}
            finish_reason = sdk_metadata.get('sdk_finish_reason')
            if isinstance(finish_reason, str) and re.fullmatch(r'[A-Z_]{1,32}', finish_reason):
                safe_sdk_metadata['sdk_finish_reason'] = finish_reason
            if type(sdk_metadata.get('response_text_present')) is bool:
                safe_sdk_metadata['response_text_present'] = sdk_metadata['response_text_present']
            text_length = sdk_metadata.get('response_text_length')
            if type(text_length) is int and 0 <= text_length <= 1_000_000:
                safe_sdk_metadata['response_text_length'] = text_length
            self.last_trace['final_response_sdk'] = safe_sdk_metadata
        entry = {'stage': stage, 'status': 'success',
                 'duration_ms': int((time.monotonic() - started) * 1000)}
        if isinstance(usage, dict):
            for key in ('input_tokens', 'output_tokens', 'thought_tokens'):
                if type(usage.get(key)) is int and usage[key] >= 0:
                    entry[key] = usage[key]
        calls.append(entry)
        self.last_trace['model_call_count'] = len(calls)
        event(operation, 'provider', stage, 'model', 'success', duration_ms=entry['duration_ms'])
        return response

    def _answer_order_request(self, text, prior, snapshot, chat_id, operation):
        explicit_id = explicit_order_id(text)
        if (explicit_id is None and prior and prior.pending_order_lookup and
                re.fullmatch(r'(?:pedido\s*)?#?\s*\d+', normalize(text))):
            explicit_id = int(re.search(r'\d+', text).group())
        is_order_list = bool(re.search(r'\bpedidos\b', normalize(text)))
        pending_id = (explicit_id if explicit_id is not None else
                      None if is_order_list else
                      prior.pending_order_id if prior and prior.pending_order_lookup else None)
        supplied_contact = has_customer_contact(text)
        matched_id = customer_id_for_contact(text, snapshot) if supplied_contact else None
        if supplied_contact and matched_id is None:
            pending_id = None if prior and prior.customer_contact_matched else pending_id
            current = ConversationContext(turn=(prior.turn + 1) if prior else 1,
                                          pending_order_id=pending_id, pending_order_lookup=True)
            answer = ('Não foi possível associar os dados informados a um cadastro da demonstração. '
                      'Confira o e-mail ou telefone cadastrado e tente novamente.')
            status = 'clarification'
            reason = 'contact_not_matched'
        else:
            customer_id = matched_id
            contact_matched = matched_id is not None
            if customer_id is None and prior and prior.customer_contact_matched:
                customer_id = prior.customer_id
                contact_matched = True
            changed_customer = bool(
                supplied_contact and matched_id is not None and prior and prior.customer_contact_matched and
                prior.customer_id != matched_id and explicit_id is None and prior.pending_order_lookup and
                not is_order_list)
            if changed_customer:
                pending_id = None
                current = ConversationContext(turn=prior.turn + 1, customer_id=matched_id,
                                              customer_contact_matched=True, pending_order_lookup=True)
                answer = 'Para localizar o pedido com esses dados, informe novamente o número do pedido.'
                status = 'clarification'
                reason = 'order_reference_cleared'
            elif customer_id is None:
                current = ConversationContext(turn=(prior.turn + 1) if prior else 1,
                                              pending_order_id=pending_id, pending_order_lookup=True)
                answer = (f'Para localizar o pedido {pending_id} nesta demonstração, informe o e-mail ou telefone cadastrado na compra. Qual deles prefere usar?'
                          if pending_id is not None else
                          'Para localizar seus pedidos nesta demonstração, informe o e-mail ou telefone cadastrado na compra. Qual deles prefere usar?')
                status = 'clarification'
                reason = 'contact_required'
            else:
                current = ConversationContext(turn=(prior.turn + 1) if prior else 1,
                                              customer_id=customer_id,
                                              customer_contact_matched=contact_matched)
                spoken = normalize(text)
                compare_catalog = bool(
                    re.search(r'\b(?:hoje|atualmente|agora|atual)\b', spoken) and
                    re.search(r'\b(?:som\w*|totalizam|totalizar)\b', spoken) and
                    re.search(r'\b(?:preco|catalogo|pedido|pedidos)\b', spoken))
                records = OrderService(snapshot).lookup(
                    customer_id, order_id=pending_id, include_catalog_comparison=compare_catalog)
                if records:
                    answer = render_results([{'consulta': 'orders', 'resultado': list(records)}])
                    answer += '\n\nQuer consultar outro pedido?'
                    status = 'success'
                    reason = 'orders_returned'
                elif pending_id is not None:
                    answer = (f'Não encontrei o pedido {pending_id} nesses registros. '
                              'Quer conferir o número do pedido?')
                    status = 'empty'
                    reason = 'order_not_found'
                else:
                    answer = 'Não encontrei pedidos nesses registros para os dados informados.'
                    status = 'empty'
                    reason = 'orders_not_found'
        if len(answer) > MAX_ASSISTANT_ANSWER_CHARS:
            answer = 'A lista de pedidos é extensa. Informe o número do pedido que deseja consultar.'
        self._remember(self.context, chat_id, current)
        self.last_trace.update(status=status, stage='order_lookup', error_code=None,
                               persisted_user_message='Consulta de pedido (dados de identificação omitidos).',
                               selected_tools=['orders'], executed_tools=(['orders'] if status in {'success', 'empty'} else []),
                               order_lookup_reason=reason)
        event(operation, 'conversation', 'order_lookup', 'validation', 'success',
              action='orders', reason_code=reason)
        event(operation, 'conversation', 'complete', 'conversation', 'success', action='orders')
        return answer

    def answer(self, text, chat_id=None):
        self.last_trace = {'status': 'pending', 'stage': 'validation', 'model_call': False,
                           'selected_tools': [], 'executed_tools': [], 'tool_details': [],
                           'tool_executions': [], 'rejected_tools': [], 'plan_overrides': [], 'error_code': None,
                           'model_calls': [], 'model_call_count': 0,
                           'final_response_status': 'not_needed', 'evidence_refs': [],
                           'timings_ms': {'snapshot': 0, 'interpretation': 0, 'execution': 0,
                                          'final_response': 0, 'render': 0, 'persistence': 0}}
        op = operation_id()
        started = time.monotonic()
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_USER_MESSAGE_CHARS:
            self.last_trace.update(status='error', error_code='invalid_message')
            event(op, 'conversation', 'complete', 'validation', 'failure')
            return FALLBACK
        raw_text = text
        text = redact_contact_details(raw_text)
        self.last_trace['persisted_user_message'] = text
        prior = self.context.get(chat_id) if chat_id is not None else None
        if prior and prior.awaiting_budget:
            self._remember(self.context, chat_id, replace(prior, awaiting_budget=False))
        social_reply = local_social_reply(raw_text)
        if raw_text.strip() == '/start' or social_reply is not None:
            self.last_trace.update(status='local', stage='greeting' if raw_text.strip() == '/start' else 'capabilities')
            self.last_trace['persisted_user_message'] = 'Saudação ou pergunta sobre a identidade do assistente.'
            event(op, 'conversation', 'complete', 'conversation', 'success')
            return (social_reply if social_reply is not None else
                    'Olá! Sou o assistente virtual do Empório da Música. Posso ajudar com produtos, pedidos e dúvidas da loja.')

        current = replace(next_context(text, prior, (prior.turn + 1) if prior else 1), awaiting_budget=False)
        if prior and prior.customer_contact_matched:
            current = replace(current, customer_id=prior.customer_id, customer_contact_matched=True)
        self._remember(self.context, chat_id, current)
        previous = self.history.get(chat_id, []) if chat_id is not None else []
        messages = list(previous) + [{'role': 'user', 'content': text}]
        written = False
        stage = 'snapshot'
        try:
            snapshot_started = time.monotonic()
            snapshot = self.snapshot_loader() if self.snapshot_loader else self.snapshot
            _add_timing(self.last_trace, 'snapshot', snapshot_started)
            if local_order_lookup_requested(raw_text, prior, snapshot):
                stage = 'order_lookup'
                answer = self._answer_order_request(raw_text, prior, snapshot, chat_id, op)
                return answer
            text = redact_customer_names(text, snapshot)
            self.last_trace['persisted_user_message'] = text
            messages[-1]['content'] = text
            scoped_text = resolve_budget_reply(text, prior, snapshot)
            if scoped_text != text:
                messages[-1] = {'role':'user','content':scoped_text}
            explicit = explicit_product(text, snapshot)
            comparison = bool(re.search(r'(?i)\b(?:compar\w*|versus|vs)\b', text))
            if comparison:
                explicit = None
            customer_id = prior.customer_id if prior and prior.customer_contact_matched else None
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
                current = replace(current, product_id=explicit['product_id'],
                                  product_name=explicit['name'], origin='user')
            self._remember(self.context, chat_id, current)
            price_mismatch = basis.mismatch()
            shipping_clarification = _shipping_quote_clarification(text, snapshot, basis)
            if shipping_clarification is not None:
                self.last_trace.update(status='clarification', stage='shipping_quote', error_code=None)
                self._remember(self.history, chat_id,
                               (messages + [{'role': 'assistant', 'content': shipping_clarification}])[-6:])
                event(op, 'conversation', 'complete', 'validation', 'success', action='shipping_quote_clarification')
                return shipping_clarification
            direct = _direct_plan_for_request(text, snapshot, basis)
            pending_query = None
            pending_kind = None
            pending_message = None
            pending_action = (pending_search_action(text, prior.pending_search, snapshot)
                              if direct is None and prior and prior.pending_search else None)
            if (direct is None and prior and prior.awaiting_budget and prior.pending_search and
                    basis.kind == 'budget' and basis.scope.max_price is not None):
                pending_action = ('search', replace(
                    prior.pending_search, max_price=basis.scope.max_price,
                    shown_ids=(), page_ids=(), selected_id=None, more_available=False), None, None)
            if pending_action is not None:
                pending_kind, pending_query, pending_product_id, pending_message = pending_action
            forced_plan = None
            forced_product_id = None
            if pending_kind == 'clarify':
                current = replace(current, pending_search=pending_query)
                self._remember(self.context, chat_id, current)
                self.last_trace.update(status='clarification', stage='pending_search', error_code=None)
                event(op, 'conversation', 'complete', 'conversation', 'success')
                return pending_message
            if pending_kind == 'search':
                forced_plan = _SinglePlan('search_products', pending_search_arguments(pending_query))
                self.last_trace['plan_overrides'].append({'reason': 'pending_search_refinement', 'tool': 'search_products'})
            elif pending_kind == 'detail':
                forced_plan = _SinglePlan('product_detail', {'product_id': pending_product_id})
                forced_product_id = pending_product_id
                selected = Catalog(snapshot).product(pending_product_id)
                if selected:
                    current = replace(current, product_id=pending_product_id,
                                      product_name=selected['name'], origin='catalog', pending_search=pending_query)
                    self._remember(self.context, chat_id, current)
                self.last_trace['plan_overrides'].append({'reason': 'pending_product_selection', 'tool': 'product_detail'})
            elif direct is not None:
                forced_plan = _SinglePlan(*direct)
                if forced_plan.name == 'search_products':
                    if not (basis.kind == 'catalog' and basis.product is not None and basis.count is not None):
                        pending_query = pending_from_plan(forced_plan, basis)
            calls = []
            if forced_plan is not None:
                self.last_trace['selected_tools'] = [forced_plan.name]
                if direct is not None and pending_action is None:
                    reason = ('explicit_policy_intent' if forced_plan.name == 'policy' else
                              'identified_product_installment_calculation'
                              if forced_plan.name == 'search_products' and 'installments' in forced_plan.args and
                              basis.kind == 'catalog' else
                              'identified_product_status' if forced_plan.name == 'product_detail' else
                              'explicit_catalog_discovery')
                    self.last_trace['plan_overrides'].append({'reason': reason, 'tool': forced_plan.name})
            else:
                if self.provider is None:
                    self.last_trace.update(status='error', stage='provider',
                                           error_code='provider_unavailable',
                                           error_type='ProviderUnavailable')
                    event(op, 'conversation', 'complete', 'provider', 'failure',
                          reason_code='provider_unavailable')
                    return FALLBACK
                stage = 'provider'
                response = self._request_model(messages, final=False, stage='interpretation', operation=op)
                output = response.get('output')
                if not isinstance(output, list):
                    raise ValueError('invalid provider output')
                if any(not isinstance(item, dict) for item in output):
                    raise ValueError('invalid provider item')
                event(op, 'provider', 'select', 'provider', 'success')
                calls = [item for item in output if isinstance(item, dict) and item.get('type') == 'function_call']
            if forced_plan is None:
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
            mismatch_result = price_mismatch
            if forced_plan is None and basis.source == 'session' and basis.kind == 'catalog' and basis.quote_allowed:
                forced_plan = _SinglePlan('search_products', {'text': basis.product['name'], 'installments': basis.count})
            elif forced_plan is None and prior and prior.policy_topic == 'pix' and pix_policy_followup(text):
                forced_plan = _SinglePlan('policy', {'topic': 'pix'})
            if not calls and forced_plan is None and mismatch_result is None:
                answer = 'Posso ajudar com produtos, pedidos e políticas da loja. O que você gostaria de saber?'
                self.last_trace['final_response_status'] = 'not_needed'
                self.last_trace.update(status='no_tool', stage='complete', error_code=None)
                self._remember(self.history, chat_id, (messages + [{'role': 'assistant', 'content': answer}])[-6:])
                return answer

            stage = 'plan'
            plans = []
            for call in calls:
                name, call_id = call.get('name'), call.get('call_id')
                if not isinstance(name, str) or not isinstance(call_id, str) or not call_id:
                    raise ValueError('invalid call')
                args = json.loads(call.get('arguments', ''))
                if not isinstance(args, dict):
                    raise ValueError('invalid arguments')
                args = {key: value for key, value in args.items() if value is not None}
                plans.append(_SinglePlan(name, args))
            if forced_plan is not None and not plans:
                plans = [forced_plan]
                if direct is None and pending_action is None:
                    self.last_trace['plan_overrides'].append({'reason': 'valid_session_reference', 'tool': forced_plan.name})
            if forced_plan is not None and forced_plan.name == 'policy':
                plans = [forced_plan if plan.name == 'policy' else plan for plan in plans]
            # Context supplies identity only for bounded references. It does not
            # amend the user's current words to manufacture consent.
            validation_text = (pending_search_grounding_text(scoped_text, pending_query)
                               if pending_query is not None and pending_kind in {'search', 'detail'}
                               else scoped_text)
            product_id = (forced_product_id if forced_product_id is not None else
                          explicit['product_id'] if explicit is not None else
                          basis.product['product_id'] if direct is not None and
                          direct[0] == 'search_products' and 'installments' in direct[1] and
                          basis.product is not None else None)
            if prior and basis.source == 'session' and basis.product and not basis.restriction:
                validation_text = text + ' ' + basis.product['name']
                product_id = basis.product['product_id']
            elif prior and prior.product_id is not None and product_followup(text) and not basis.restriction:
                validation_text = text + ' ' + prior.product_name
                product_id = prior.product_id
            proposed_plan = [(plan.name, plan.args) for plan in plans]
            prepared = prepare_execution_plan(
                snapshot, proposed_plan, validation_text,
                commercial_decision=basis, product_id=product_id,
                validation_text=validation_text)
            if isinstance(prepared, str):
                code = 'tool_isolation' if prepared == WRITE_ISOLATION else 'invalid_tool_plan'
                self.last_trace.update(status='error', error_code=code)
                return prepared
            commercial_rejections = []
            for rejection in prepared.rejected:
                reason = rejection.reason
                if reason == 'catalog_filter_not_grounded':
                    invalid_filter = any(
                        not isinstance(rejection.args.get(field), str) or
                        parse_brl_mention(rejection.args[field]) is None
                        for field in ('min_price', 'max_price') if field in rejection.args)
                    reason = ('invalid_price_filter' if invalid_filter else 'incorrect_price_filter'
                              if _incorrect_price_filter(rejection.args, validation_text)
                              else 'catalog_filter_not_grounded')
                self.last_trace['tool_executions'].append(
                    _safe_tool_detail(rejection.name, rejection.args,
                                      status='rejected', reason=reason))
                self.last_trace['rejected_tools'].append(
                    {'name': rejection.name, 'reason': reason})
                commercial_rejections.append(prepared_rejection_result(rejection, basis))
                if mismatch_result is not None:
                    mismatch_result['catalog_quote_authorized'] = False
            plans = [_SinglePlan(call.name, dict(call.args)) for call in prepared.calls]
            for call in prepared.calls:
                if not any(name == call.name and args == dict(call.args)
                           for name, args in proposed_plan):
                    self.last_trace['plan_overrides'].append(
                        {'reason': 'resolved_commercial_basis', 'tool': call.name})
            event(op, 'conversation', 'plan', 'validation', 'success')

            results = ([{'consulta': 'price_mismatch', 'resultado': mismatch_result}]
                       if mismatch_result is not None else [])
            resolved_products = {}
            explicitly_mentioned = [p for p in snapshot.tables['products'] if normalize(p['name']) in normalize(text)]
            clarifications = [] if mismatch_result is not None else commercial_rejections
            if (mismatch_result is not None and validate_policy_intent('shipping', text)
                    and not any(plan.name == 'policy' and plan.args.get('topic') == 'shipping' for plan in plans)):
                clarifications.append({'consulta': 'clarification', 'resultado': {'tool': 'policy', 'topic': 'shipping'}})
            for plan, prepared_call in zip(plans, prepared.calls):
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
                execution_started = time.monotonic()
                try:
                    evidence = Agent(snapshot, plan, self.review_submit).execute_prepared(
                        prepared, selected_call=prepared_call, text=tool_text,
                        product_id=product_id,
                        customer_id=customer_id if customer_id is not None else prior.customer_id if prior and prior.customer_contact_matched and order_followup(text) else None,
                        structured=True, correlation_id=op,
                        excluded_ids=pending_query.excluded_ids if pending_query else ())
                finally:
                    _add_timing(self.last_trace, 'execution', execution_started)
                if isinstance(evidence, str):
                    if evidence == ORDER_AUTH_CLARIFY:
                        current = replace(current, pending_order_id=explicit_order_id(raw_text),
                                          pending_order_lookup=True)
                        self._remember(self.context, chat_id, current)
                        detail.update(status='clarification', reason='contact_required')
                        self.last_trace['tool_executions'].append(detail)
                        self.last_trace.update(status='clarification', stage='order_authorization', error_code=None,
                                               persisted_user_message='Consulta de pedido (dados de identificação omitidos).')
                        event(op, 'conversation', 'complete', 'validation', 'success', action='orders',
                              reason_code='contact_required')
                        return evidence
                    elif evidence == CLARIFY:
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
            search_plans = [plan for plan in plans if plan.name in {'search_products', 'recommend_products'}]
            if len(search_plans) == 1 and len(plans) == 1:
                search_plan = search_plans[0]
                search_result = next((entry for entry in reversed(results)
                                      if entry.get('consulta') == search_plan.name), None)
                if search_result is not None:
                    if basis.kind == 'catalog' and basis.product is not None and basis.count is not None:
                        current = replace(current, pending_search=None)
                    else:
                        pending_query = record_search_page(
                            pending_query, search_plan, basis, search_result, scoped_text)
                        current = replace(current, pending_search=pending_query)
                    self._remember(self.context, chat_id, current)
            elif search_plans:
                current = replace(current, pending_search=None)
                self._remember(self.context, chat_id, current)
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
            render_started = time.monotonic()
            if mismatch_result is not None:
                mismatch_result['catalog_quote_executed'] = any(
                    entry['consulta'] == 'installment_quote' and entry['resultado'].get('status') in {'eligible', 'below_minimum'} or
                    entry['consulta'] == 'search_products' and any(item.get('installment_quote', {}).get('status') in {'eligible', 'below_minimum'}
                                                                 for item in entry['resultado'])
                    for entry in results)
            answer = render_sales_results(results, scoped_text, snapshot)
            _add_timing(self.last_trace, 'render', render_started)
            has_evidence = bool(self.last_trace['executed_tools']) and not results_are_empty
            if has_evidence and self.provider is None:
                self.last_trace['final_response_status'] = 'omitted'
                has_evidence = False
            if has_evidence:
                refs = ['E1']
                self.last_trace['evidence_refs'] = refs
                selectable_ids = displayed_product_ids(results, scoped_text)
                dialogue_context = communication_context(current, prior)
                selectable_products = [
                    {'product_id': product_id, 'name': item['name']}
                    for product_id in selectable_ids
                    if (item := Catalog(snapshot).product(product_id)) is not None
                ]
                prompt = json.dumps({
                    'customer_message': scoped_text,
                    'evidence': [{'id': 'E1', 'validated_facts': answer}],
                    'selectable_products': selectable_products,
                    'conversation_context': dialogue_context,
                    'instructions': (
                        'O bloco factual de Python responde ao pedido; escreva uma abertura muito breve e uma única pergunta '
                        'contextual de esclarecimento ou próximo passo, em campos separados. Use fala natural, clara e curta. '
                        'Não repita orçamento, categoria, marca, modelo ou características presentes em conversation_context. '
                        'A abertura deve ser neutra, sem fatos comerciais. Na pergunta, você pode apenas perguntar se a pessoa quer '
                        'conhecer ou consultar condições de pagamento, frete, parcelamento, garantia ou troca; não declare regras, '
                        'valores, elegibilidade ou resultados. Essas perguntas têm formas permitidas limitadas. Fatos e cálculos '
                        'permanecem no bloco de Python. Sem números, promessas, urgência, escassez ou pressão. '
                        'A pergunta pode ser vazia; faça só uma e não pergunte algo já respondido. '
                        'Referencie todas as evidências recebidas em evidence_refs. Se houver pelo menos cinco '
                        'selectable_products e você incluir product_order, mantenha os cinco IDs da página exatamente '
                        'uma vez; não reduza a lista. Em lista completa explícita, mantenha todos os IDs. '
                        'Use uma ordem útil e não invente IDs. '
                        'Retorne somente JSON com opening, next_step, evidence_refs e, opcionalmente, product_order.'),
                }, ensure_ascii=False, separators=(',', ':'))
                generated = None
                try:
                    final_response = self._request_model(
                        [{'role': 'user', 'content': prompt}], final=True,
                        stage='final_response', operation=op)
                except Exception:
                    final_response = None
                    self.last_trace['final_response_status'] = 'failed'
                if final_response is not None:
                    generated, diagnostics = validated_final_response_with_diagnostics(
                        final_response, refs, selectable_ids, known_context=dialogue_context,
                        require_all_product_ids=(
                            len(selectable_ids) >= PRODUCT_PAGE_SIZE or
                            (bool(selectable_ids) and complete_catalog_list_request(scoped_text))))
                    usage = final_response.get('usage', {}) if isinstance(final_response, dict) else {}
                    if isinstance(usage, dict):
                        for name in ('input_tokens', 'output_tokens', 'thought_tokens'):
                            count = usage.get(name)
                            if type(count) is int and 0 <= count <= 1_000_000_000:
                                diagnostics[name] = count
                    diagnostics.update(self.last_trace.get('final_response_sdk', {}))
                    self.last_trace['final_response_diagnostics'] = diagnostics
                    self.last_trace['final_response_rejection_code'] = diagnostics['reason_code']
                    if generated is not None:
                        if generated['product_order'] is not None:
                            render_started = time.monotonic()
                            answer = render_sales_results(
                                results, scoped_text, snapshot,
                                product_order=generated['product_order'])
                            _add_timing(self.last_trace, 'render', render_started)
                            if current.pending_search is not None:
                                current = replace(
                                    current,
                                    pending_search=record_final_product_order(
                                        current.pending_search, generated['product_order']))
                                self._remember(self.context, chat_id, current)
                        answer = apply_final_response(answer, generated)
                        self.last_trace['final_response_status'] = 'accepted'
                    else:
                        self.last_trace['final_response_status'] = 'invalid'
                if self.last_trace['final_response_status'] in {'failed', 'invalid', 'unsupported'}:
                    event(op, 'conversation', 'final_response', 'model', 'failure',
                          duration_ms=self.last_trace['timings_ms'].get('final_response', 0),
                          reason_code=self.last_trace.get('final_response_rejection_code'),
                          diagnostics=self.last_trace.get('final_response_diagnostics'))
            elif results_are_empty:
                self.last_trace['final_response_status'] = 'not_needed'
            if answer.endswith(BUDGET_QUESTION):
                current = replace(current, awaiting_budget=True)
                self._remember(self.context, chat_id, current)
            if len(answer) > MAX_ASSISTANT_ANSWER_CHARS:
                answer, bounded_ids = render_sales_answer_within_limit(
                    results, scoped_text, snapshot,
                    product_order=(generated.get('product_order') if has_evidence and generated else None),
                )
                if bounded_ids and current.pending_search is not None:
                    current = replace(
                        current,
                        pending_search=record_final_product_order(current.pending_search, bounded_ids),
                    )
                    self._remember(self.context, chat_id, current)
                if len(answer) > MAX_ASSISTANT_ANSWER_CHARS:
                    answer = 'A ficha técnica é extensa. Diga o nome do campo que deseja consultar para receber um bloco completo.'
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
                           'order_lookup': 'order_lookup_failed',
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
