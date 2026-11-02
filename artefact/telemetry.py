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


def event(operation: str, component: str, stage: str, category: str,
          result: str, *, error: Exception | None = None,
          side_effect: str = 'none', duration_ms: int | None = None,
          action: str | None = None) -> None:
    record = {'operation_id': operation, 'component': component, 'stage': stage,
              'category': category, 'result': result, 'side_effect': side_effect}
    if error is not None:
        name = type(error).__name__
        record['exception_type'] = name if re.fullmatch(r'[A-Za-z0-9_]{1,80}', name) else 'Exception'
    if duration_ms is not None:
        record['duration_ms'] = max(0, int(duration_ms))
    if action is not None:
        record['action'] = action if action in {'search_products','recommend_products','promotion','installment_quote','policy','orders','request_technical_review'} else 'unknown'
    write = logger.warning if result in ('failure', 'uncertain') else logger.info
    write(json.dumps(record, ensure_ascii=True, sort_keys=True))
