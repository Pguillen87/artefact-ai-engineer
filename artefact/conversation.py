"""Bounded conversation orchestration for the fictional store."""
from collections import OrderedDict
from dataclasses import dataclass, replace
import json
import re
import time

from .agent import Agent, CLARIFY, FALLBACK, WRITE_ISOLATION, WRITE_UNCERTAIN
from .context import explicit_customer, explicit_product, installment_followup, next_context, order_followup, product_followup, product_installment_followup
from .rendering import render_results
from .telemetry import event, operation_id

_GREETING = re.compile(r'^(oi|ol[aá]|bom dia|boa tarde|boa noite)[!. ]*$', re.I)


@dataclass(frozen=True)
class _SinglePlan:
    name: str
    args: dict

    def plan(self, _text):
        return [(self.name, self.args)]


class ConversationAgent:
    def __init__(self, snapshot, provider, *, snapshot_loader=None, review_submit=None):
        self.snapshot = snapshot
        self.provider = provider
        self.snapshot_loader = snapshot_loader
        self.review_submit = review_submit
        self.history = OrderedDict()
        self.context = OrderedDict()

    @staticmethod
    def _remember(store, chat_id, value):
        if chat_id is None:
            return
        store[chat_id] = value
        store.move_to_end(chat_id)
        if len(store) > 100:
            store.popitem(last=False)

    def answer(self, text, chat_id=None):
        op = operation_id()
        started = time.monotonic()
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            event(op, 'conversation', 'complete', 'validation', 'failure')
            return FALLBACK
        if text.strip() == '/start' or _GREETING.fullmatch(text.strip()):
            event(op, 'conversation', 'complete', 'conversation', 'success')
            return 'Olá! Como posso ajudar com instrumentos, pedidos ou políticas da loja?'
        if re.fullmatch(r'(o que voc[eê] faz|quem [eé] voc[eê]|como voc[eê] pode ajudar)[?! .]*', text.strip(), re.I):
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
            customer_id = explicit_customer(text, snapshot)
            if customer_id is not None:
                current = replace(current, customer_id=customer_id)
                self._remember(self.context, chat_id, current)
            if explicit is not None:
                role = current.amount_role
                if current.amount is not None and explicit.get('price_brl') != current.amount:
                    role = 'ambiguous'
                current = replace(current, amount_role=role, product_id=explicit['product_id'],
                                  product_name=explicit['name'], origin='user')
                self._remember(self.context, chat_id, current)
            stage = 'provider'
            response = self.provider.request(messages)
            output = response.get('output')
            if not isinstance(output, list):
                raise ValueError('invalid provider output')
            if any(not isinstance(item, dict) for item in output):
                raise ValueError('invalid provider item')
            event(op, 'provider', 'select', 'provider', 'success')
            calls = [item for item in output if isinstance(item, dict) and item.get('type') == 'function_call']
            if not calls:
                answer = 'Posso ajudar com produtos, pedidos e políticas da loja. O que você gostaria de saber?'
                self._remember(self.history, chat_id, (messages + [{'role': 'assistant', 'content': answer}])[-6:])
                event(op, 'conversation', 'complete', 'conversation', 'success', duration_ms=int((time.monotonic()-started)*1000))
                return answer
            if len(calls) > 3:
                event(op, 'conversation', 'plan', 'validation', 'failure')
                event(op, 'conversation', 'complete', 'validation', 'failure')
                return FALLBACK
            if len(calls) > 1 and any(c.get('name') == 'request_technical_review' for c in calls):
                event(op, 'conversation', 'plan', 'validation', 'failure')
                event(op, 'conversation', 'complete', 'validation', 'failure')
                return WRITE_ISOLATION

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
            event(op, 'conversation', 'plan', 'validation', 'success')

            results = []
            for plan in plans:
                stage = 'tool'
                validation_text = text
                product_id = explicit['product_id'] if explicit is not None else None
                if prior and prior.product_id is not None and (product_followup(text) or
                        (prior.amount is None and product_installment_followup(text))):
                    validation_text = text + ' ' + prior.product_name
                    product_id = prior.product_id
                request = installment_followup(text, prior) if plan.name == 'installment_quote' else None
                evidence = Agent(snapshot, plan, self.review_submit).answer(
                    validation_text, installment_request=request, product_id=product_id,
                    customer_id=customer_id if customer_id is not None else prior.customer_id if prior and order_followup(text) else None,
                    structured=True, correlation_id=op, count_text=text)
                if isinstance(evidence, str):
                    category = 'validation' if evidence == CLARIFY else 'integration' if evidence == WRITE_UNCERTAIN else 'tool'
                    event(op, 'conversation', 'tool', category, 'uncertain' if evidence == WRITE_UNCERTAIN else 'failure',
                          side_effect='unknown' if evidence == WRITE_UNCERTAIN else 'none', action=plan.name)
                    event(op, 'conversation', 'complete', category, 'uncertain' if evidence == WRITE_UNCERTAIN else 'failure',
                          side_effect='unknown' if evidence == WRITE_UNCERTAIN else 'none')
                    return evidence
                results.extend(evidence)
                written = any(item['consulta'] == 'request_technical_review' and
                              isinstance(item['resultado'], dict) and item['resultado'].get('registered') is True
                              for item in evidence)
                event(op, 'agent', 'execute', 'tool', 'success', action=plan.name,
                      side_effect='committed' if written else 'none')
                if plan.name == 'search_products':
                    products = evidence[0]['resultado']
                    if len(products) == 1:
                        price = products[0].get('price_brl')
                        checkout = (products[0].get('price_quote') or {}).get('checkout_price_brl')
                        role = current.amount_role
                        if current.amount is not None and any(value is not None and value != current.amount for value in (price, checkout)):
                            role = 'ambiguous'
                        current = replace(current, amount_role=role, product_id=products[0]['product_id'], product_name=products[0]['name'], origin='catalog')
                        self._remember(self.context, chat_id, current)
            if all(item.get('resultado') in (None, [], (), {}) for item in results):
                event(op, 'conversation', 'complete', 'tool', 'empty', duration_ms=int((time.monotonic()-started)*1000))
                return 'Não encontrei informações suficientes para essa consulta.'
            stage = 'render'
            answer = render_results(results)
            if len(answer) > 3500:
                event(op, 'conversation', 'complete', 'validation', 'failure', side_effect='committed' if written else 'none', duration_ms=int((time.monotonic()-started)*1000))
                return 'A resposta é longa demais. Por favor, refine sua consulta.'
            self._remember(self.history, chat_id, (messages + [{'role': 'assistant', 'content': answer}])[-6:])
            event(op, 'conversation', 'complete', 'tool', 'success',
                  side_effect='committed' if written else 'none', duration_ms=int((time.monotonic()-started)*1000))
            return answer
        except Exception as exc:
            category = 'provider' if stage == 'provider' else 'persistence' if stage == 'snapshot' else 'validation' if stage == 'plan' else 'execution'
            event(op, 'conversation', stage, category, 'failure', error=exc,
                  side_effect='committed' if written else 'none', duration_ms=int((time.monotonic()-started)*1000))
            event(op, 'conversation', 'complete', category, 'failure',
                  side_effect='committed' if written else 'none', duration_ms=int((time.monotonic()-started)*1000))
            if written:
                return 'Sua solicitação de revisão técnica foi registrada para análise.'
            return FALLBACK
