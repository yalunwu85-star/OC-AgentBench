"""Credential handling. Raw agent artifacts must remain private."""
import json
import logging
import os
import re
from pathlib import Path
SECRET_NAME = re.compile(r'(KEY|TOKEN|PASSWORD|SECRET|CREDENTIAL)', re.I)
PLACEHOLDER = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')
# Values expanded from model configs are redacted whatever their variable is called.
EXPANDED_VALUES: set[str] = set()

def redact(text: str) -> str:
    secrets = {v for k, v in os.environ.items() if SECRET_NAME.search(k)} | EXPANDED_VALUES
    for value in sorted(secrets, key=len, reverse=True):
        if len(value) >= 4:
            text = text.replace(value, '[REDACTED]')
    return re.sub(r'\b(?:sk-[A-Za-z0-9_-]{16,}|hf_[A-Za-z0-9]{20,})\b', '[REDACTED]', text)

class SecretFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        return True

def load_models_config(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict) or not isinstance(payload.get('providers'), dict):
        raise ValueError('Model config requires a providers object')
    def expand(value, key=''):
        if isinstance(value, dict):
            return {k: expand(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v) for v in value]
        if isinstance(value, str):
            if SECRET_NAME.search(key) and value and not PLACEHOLDER.fullmatch(value):
                raise ValueError('Credentials in model configs must use ${ENV_VAR} references')
            def replace(match):
                if not os.environ.get(match[1]):
                    raise ValueError(f'Required environment variable is empty: {match[1]}')
                if SECRET_NAME.search(key):
                    EXPANDED_VALUES.add(os.environ[match[1]])
                return os.environ[match[1]]
            return PLACEHOLDER.sub(replace, value)
        return value
    return expand(payload)
