"""Bound conversation payloads before JSON encoding or SQLite binding."""

import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re

LOG = logging.getLogger(__name__)
DATA_URI = re.compile(r"data:[\w.+/-]+(?:;[\w=.-]+)*;base64,[A-Za-z0-9+/=\s]+")
BASE64 = re.compile(r"[A-Za-z0-9+/]{4096,}={0,2}")


def configure_size_logging(database_path):
    """Persist size-only diagnostics beside the database with bounded disk usage."""
    target = str(Path(database_path).with_name('conversation_sizes.log'))
    for handler in list(LOG.handlers):
        if isinstance(handler, RotatingFileHandler):
            if handler.baseFilename == target:
                return
            LOG.removeHandler(handler)
            handler.close()
    try:
        handler = RotatingFileHandler(target, maxBytes=1048576, backupCount=2, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        LOG.addHandler(handler)
        LOG.setLevel(logging.INFO)
    except OSError:
        LOG.warning('conversation ledger size log unavailable', exc_info=True)


def byte_size(value):
    """Count UTF-8 bytes without allocating a second large payload."""
    if isinstance(value, str):
        return sum(len(value[i:i + 8192].encode('utf-8', errors='replace'))
                   for i in range(0, len(value), 8192))
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    if isinstance(value, dict):
        return sum(byte_size(key) + byte_size(val) for key, val in value.items())
    if isinstance(value, (list, tuple)):
        return sum(byte_size(val) for val in value)
    return len(str(value).encode('utf-8', errors='replace'))


def clip_text(value, budget):
    """Keep a valid UTF-8 prefix and an explicit truncation marker."""
    marker = '\n[ledger: truncated]'
    if byte_size(value) <= budget:
        return value
    prefix = value[:budget].encode('utf-8', errors='replace')
    return prefix[:max(0, budget - len(marker.encode()))].decode('utf-8', errors='ignore') + marker


def safe_payload(value, budget=65536):
    """Remove inline media and bound nested objects before serialization."""
    remaining = [256]

    def clean(item, depth=0):
        """Limit nodes, depth and individual leaves, including embeddings."""
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 8:
            return '[ledger: structure omitted]'
        if isinstance(item, (bytes, bytearray, memoryview)):
            return {'omitted': 'binary', 'bytes': len(item),
                    'sha256': hashlib.sha256(item).hexdigest()}
        if isinstance(item, str):
            size = byte_size(item)
            if size > budget:
                LOG.warning('conversation ledger oversized leaf bytes=%s budget=%s', size, budget)
            # Scan only a bounded prefix; the rest is never serialized.
            prefix = item[:budget]
            prefix = DATA_URI.sub('[ledger: inline media omitted]', prefix)
            prefix = BASE64.sub('[ledger: base64-like payload omitted]', prefix)
            if len(item) > budget:
                prefix += '\n[ledger: truncated]'
            return clip_text(prefix, budget)
        if isinstance(item, dict):
            result = {}
            for index, (key, val) in enumerate(item.items()):
                if index >= 64 or remaining[0] <= 0:
                    result['_ledger_omitted'] = 'remaining fields'
                    break
                result[clip_text(str(key), 128)] = clean(val, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            result = [clean(val, depth + 1) for val in item[:64] if remaining[0] > 0]
            if len(item) > len(result):
                result.append({'omitted_items': len(item) - len(result)})
            return result
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return clip_text(str(item), budget)

    cleaned = clean(value)
    if isinstance(cleaned, str):
        return cleaned
    encoded = json.dumps(cleaned, ensure_ascii=False, default=str)
    if byte_size(encoded) > budget:
        return {'omitted': 'oversized JSON', 'preview': clip_text(encoded, max(64, budget // 2))}
    return cleaned


def prepare_fields(fields, sqlite_limit):
    """Log every bound field size and bound the entire row below SQLite's limit."""
    budget = min(65536, max(128, sqlite_limit // 4))
    sizes = {key: byte_size(val) for key, val in fields.items()}
    LOG.info('conversation ledger input field_bytes=%s sqlite_limit=%s', sizes, sqlite_limit)
    for key in ('content', 'metadata_json'):
        fields[key] = clip_text(fields[key], budget) if key == 'content' else fields[key]
    if byte_size(fields['metadata_json']) > budget:
        fields['metadata_json'] = json.dumps({'omitted': 'oversized metadata', 'bytes': sizes['metadata_json']})
    for key, val in fields.items():
        if key not in ('content', 'metadata_json', 'sequence') and byte_size(val) > min(4096, budget // 16):
            LOG.error('conversation ledger rejected field=%s bytes=%s', key, sizes[key])
            raise ValueError(f'conversation ledger identity field too large: {key} ({sizes[key]} bytes)')
    stored = {key: byte_size(val) for key, val in fields.items()}
    if sum(stored.values()) + 128 >= sqlite_limit:
        raise ValueError('conversation ledger row exceeds SQLite length budget')
    if sizes != stored:
        LOG.warning('conversation ledger bounded input=%s stored=%s', sizes, stored)
    LOG.info('conversation ledger stored field_bytes=%s', stored)
    return fields
