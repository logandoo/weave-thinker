# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

_INVISIBLE_CHARS = {
    '\u200b', '\u200c', '\u200d', '\u2060', '\ufeff',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
}

_MEMORY_THREAT_PATTERNS = [
    (r'ignore\s+(previous|all|above|prior)\s+instructions', "prompt_injection"),
    (r'you\s+are\s+now\s+', "role_hijack"),
    (r'do\s+not\s+tell\s+the\s+user', "deception_hide"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules|guidelines)', "disregard_rules"),
    (r'curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)', "exfil_curl"),
    (r'cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass|\.npmrc)', "read_secrets"),
    (r'authorized_keys', "ssh_backdoor"),
]

_PII_PATTERNS = [
    (r'1[3-9]\d{9}', '[PHONE]'),
    (r'\d{17}[\dXx]', '[ID_CARD]'),
    (r'\b[\w.+-]+@[\w-]+\.[\w.]+\b', '[EMAIL]'),
    (r'\d{16,19}', '[BANK_CARD]'),
    (r'(sk-|ak-|api[_-]?key[=:]\s*)\S+', '[API_KEY]'),
]


def scan_injection(content: str) -> Optional[str]:
    for char in _INVISIBLE_CHARS:
        if char in content:
            return f"Blocked: content contains invisible unicode character U+{ord(char):04X}"
    for pattern, pid in _MEMORY_THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return f"Blocked: content matches threat pattern '{pid}'"
    return None


def scrub_pii(text: str) -> tuple[str, list[str]]:
    hits = []
    result = text
    for pattern, replacement in _PII_PATTERNS:
        if re.search(pattern, result):
            hits.append(replacement)
            result = re.sub(pattern, replacement, result)
    return result, hits


def pii_hit_labels(result: str, hits: list[str]) -> list[str]:
    if not hits:
        return []
    return sorted(set(hits))
