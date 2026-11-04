"""Sanitized structured operational events; message content is never logged."""
import json
import logging
import re
from contextvars import ContextVar
from uuid import uuid4

logger = logging.getLogger('artefact.operations')
_active_operation = ContextVar('artefact_operation', default=None)


def configure_logging() -> None:
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def operation_id() -> str:
    return _active_operation.get() or uuid4().hex


def bind_operation(value: str):
    return _active_operation.set(value)


def reset_operation(token) -> None:
    _active_operation.reset(token)


def _safe_diagnostics(value):
    if not isinstance(value, dict):
        return {}
    safe = {}
    finish_reason = value.get('sdk_finish_reason')
    if isinstance(finish_reason, str) and re.fullmatch(r'[A-Z_]{1,32}', finish_reason):
        safe['sdk_finish_reason'] = finish_reason
    for key in ('input_tokens', 'output_tokens', 'thought_tokens', 'response_text_length'):
        number = value.get(key)
        if type(number) is int and 0 <= number <= 1_000_000_000:
            safe[key] = number
    for key in ('response_text_present', 'product_order_validated'):
        flag = value.get(key)
        if type(flag) is bool:
            safe[key] = flag
    for key in ('guard', 'field'):
        label = value.get(key)
        if isinstance(label, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', label):
            safe[key] = label
    refs = value.get('validated_evidence_refs')
    if isinstance(refs, list) and len(refs) <= 16 and all(
            isinstance(item, str) and re.fullmatch(r'E[0-9]{1,3}', item) for item in refs):
        safe['validated_evidence_refs'] = refs
    product_ids = value.get('validated_product_ids')
    if isinstance(product_ids, list) and len(product_ids) <= 50 and all(
            type(item) is int and 0 < item <= 2**31 - 1 for item in product_ids):
        safe['validated_product_ids'] = product_ids
    return safe


def event(operation: str, component: str, stage: str, category: str,
          result: str, *, error: Exception | None = None,
          side_effect: str = 'none', duration_ms: int | None = None,
          action: str | None = None, reason_code: str | None = None,
          diagnostics: dict | None = None) -> None:
    record = {'operation_id': operation, 'component': component, 'stage': stage,
              'category': category, 'result': result, 'side_effect': side_effect}
    if error is not None:
        name = type(error).__name__
        record['exception_type'] = name if re.fullmatch(r'[A-Za-z0-9_]{1,80}', name) else 'Exception'
    if duration_ms is not None:
        record['duration_ms'] = max(0, int(duration_ms))
    if action is not None:
        record['action'] = action if action in {'search_products','recommend_products','promotion','installment_quote','policy','orders','request_technical_review'} else 'unknown'
    if isinstance(reason_code, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', reason_code):
        record['reason_code'] = reason_code
    record.update(_safe_diagnostics(diagnostics))
    write = logger.warning if result in ('failure', 'uncertain') else logger.info
    write(json.dumps(record, ensure_ascii=True, sort_keys=True))
