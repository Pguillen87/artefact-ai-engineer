"""Strict read-only CSV snapshot and conservative, field-local quality checks."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

SCHEMAS = {
    'categories': ('category_id','name','description'),
    'customers': ('customer_id','name','phone','email','city'),
    'products': ('product_id','price_brl','name','category_id','description','stock_quantity','status','specs','created_at'),
    'orders': ('order_id','customer_id','order_date','status','total_brl','payment_method','tracking_code','estimated_delivery','notes'),
    'order_items': ('order_id','quantity','product_id'),
    'promotions': ('promotion_id','product_id','discount_percent','description','is_active'),
}
HASHES = {
    'categories':'7a7901e16c8c6357a956031ada45a6fd6a4cc9bd7bc1edc05448cc55b01b0a9e',
    'customers':'1cd724dc86eac6258913cc890abf23d3893c57a99c9325ebfb91dfad3c7b0f37',
    'order_items':'7656eb681821d48e0c932f61d3188309b955c25446578235f2d8e69604b0f0bc',
    'orders':'deb9c2480605d8d22117d569f0045fd156c5e9a58ffeebb847ff61b3b9512fd5',
    'products':'78d911d96ee891abb163b6c858e895025aa530ab27d0290540f05d5098377c12',
    'promotions':'cbe08c7e91b00437c0c00a7ef1486deba73de52d23e3f97f51a7dbd557d759e0',
}
PREFIX = 'desafio_tecnico_ai_eng - '
INTS = {'category_id','customer_id','product_id','order_id','promotion_id','stock_quantity','quantity'}
MONEY = {'price_brl','total_brl','discount_percent'}
DATES = {'created_at','order_date','estimated_delivery'}
KEYS = {'categories':('category_id',), 'customers':('customer_id',), 'products':('product_id',), 'orders':('order_id',), 'order_items':('order_id','product_id'), 'promotions':('promotion_id',)}

class StructuralError(ValueError):
    """Input cannot be exposed as a validated snapshot; details exclude row contents."""

@dataclass(frozen=True)
class QualityIssue:
    product_id: int
    field: str
    rule: str
    evidence: str
    confidence: str
    severity: str = 'review'

@dataclass(frozen=True)
class Snapshot:
    tables: Mapping[str, tuple[Mapping, ...]]
    issues: tuple[QualityIssue, ...]
    hashes: Mapping[str, str]
    seller_contact: str = '(67) 3341-4444'
    catalog_version: int = 0


def _normalize(text: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold()) if not unicodedata.combining(c))


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('invalid JSON constant')


def _parse(table: str, raw: bytes) -> list[dict]:
    try:
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig'), newline=''), strict=True)
        if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)) or set(reader.fieldnames) != set(SCHEMAS[table]):
            raise StructuralError(f'{table}: invalid header')
        result = []
        seen = set()
        for line, row in enumerate(reader, 2):
            if None in row or any(v is None for v in row.values()):
                raise StructuralError(f'{table}:{line}: malformed row')
            try:
                if table in {'categories', 'customers', 'products'} and not row['name'].strip():
                    raise ValueError('name')
                for field in INTS.intersection(row):
                    value = row[field]
                    if not re.fullmatch(r'[0-9]+', value): raise ValueError(field)
                    row[field] = int(value)
                    if row[field] < (0 if field == 'stock_quantity' else 1): raise ValueError(field)
                for field in MONEY.intersection(row):
                    value = Decimal(row[field])
                    if not value.is_finite() or value < 0 or (field == 'discount_percent' and value > 100): raise ValueError(field)
                    row[field] = value
                for field in DATES.intersection(row):
                    if row[field]: row[field] = date.fromisoformat(row[field])
                    elif field != 'estimated_delivery': raise ValueError(field)
                if table == 'products':
                    if row['status'] not in {'active','discontinued','coming_soon'}: raise ValueError('status')
                    row['specs'] = json.loads(row['specs'], object_pairs_hook=_json_object, parse_constant=_invalid_constant)
                    if not isinstance(row['specs'], dict): raise ValueError('specs')
                if table == 'orders' and row['status'] not in {'pending','confirmed','shipped','delivered','cancelled'}: raise ValueError('status')
                if table == 'promotions':
                    if row['is_active'] not in {'0','1'}: raise ValueError('is_active')
                    row['is_active'] = row['is_active'] == '1'
                key = tuple(row[f] for f in KEYS[table])
                if key in seen: raise ValueError('duplicate key')
                seen.add(key)
            except (ValueError, InvalidOperation, json.JSONDecodeError) as exc:
                raise StructuralError(f'{table}:{line}: invalid typed field or duplicate key') from exc
            result.append(row)
        return result
    except (UnicodeError, csv.Error) as exc:
        raise StructuralError(f'{table}: invalid encoding or CSV') from exc


def _check_relations(tables):
    links = [('products','category_id','categories','category_id'), ('orders','customer_id','customers','customer_id'), ('order_items','order_id','orders','order_id'), ('order_items','product_id','products','product_id'), ('promotions','product_id','products','product_id')]
    for source, field, target, target_field in links:
        ids = {r[target_field] for r in tables[target]}
        if any(r[field] not in ids for r in tables[source]):
            raise StructuralError(f'{source}.{field}: missing reference to {target}')


def _scan(products, categories):
    issues = []
    brands = ('music man','fender','ibanez','yamaha','pearl','tama','korg','roland','gibson','casio','epiphone','cort')
    categories_by_id = {c['category_id']: _normalize(c['name']) for c in categories}
    groups = {'teclado': 'teclados', 'sintetizador':'teclados', 'guitarra':'guitarras', 'contrabaixo':'baixos', 'bateria':'baterias', 'violao':'violoes', 'ukulele':'ukuleles'}
    for product in products:
        name = _normalize(product['name']); desc = _normalize(product['description']); specs = product['specs']; pid = product['product_id']
        def matches(text):
            return {b for b in brands if re.search(r'(?<!\w)' + re.escape(b) + r'(?!\w)', text)}
        title_brands = matches(name); desc_brands = matches(desc)
        if len(title_brands) == len(desc_brands) == 1 and title_brands != desc_brands:
            issues.append(QualityIssue(pid,'brand','brand_conflict',f"name={next(iter(title_brands))}; description={next(iter(desc_brands))}",'high'))
        for spec, word in [('keys','teclas'),('strings','cordas')]:
            found = re.findall(r'\b(\d+)\s+' + word + r'\b', desc)
            value = str(specs.get(spec,''))
            if len(set(found)) == 1 and value.isdigit() and found[0] != value:
                issues.append(QualityIssue(pid,f'specs.{spec}','numeric_conflict',f'description={found[0]}; specs={value}','high'))
        for word, expected in groups.items():
            if re.search(r'\b'+word+r'\b',name) and expected not in categories_by_id[product['category_id']]:
                issues.append(QualityIssue(pid,'category_id','category_conflict',f'name term={word}; category does not match','medium'))
    return tuple(issues)


def load_dataset(directory: Path, *, verify_baseline: bool = False) -> Snapshot:
    """Read all six inputs and either return one immutable snapshot or raise."""
    directory = Path(directory)
    tables = {}; hashes = {}
    for table in SCHEMAS:
        path = directory / ((PREFIX if verify_baseline else '') + table + '.csv')
        try: raw = path.read_bytes()
        except OSError as exc: raise StructuralError(f'{table}: file unavailable') from exc
        hashes[table] = hashlib.sha256(raw).hexdigest()
        if verify_baseline and hashes[table] != HASHES[table]: raise StructuralError(f'{table}: source hash mismatch')
        tables[table] = _parse(table, raw)
    _check_relations(tables)
    issues = _scan(tables['products'], tables['categories'])
    return Snapshot(_freeze(tables), issues, MappingProxyType(hashes))
