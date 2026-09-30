#!/usr/bin/env python3
"""Scan publishable files without displaying credential values.

This is a defense-in-depth check, not a proof that arbitrary data is secret-free.
Use --root on a clean checkout/staging directory before upload.
"""
import argparse
from pathlib import Path
import re
import sys
PATTERNS = {
    'provider credential': re.compile(rb'\b(?:sk-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{24,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16})\b'),
    'private key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'personal absolute path': re.compile(rb'/(?:home|Users|media)/[A-Za-z0-9_.-]+/'),
    'credential in URL': re.compile(rb'https?://[^\s/:]+:[^\s/@]+@'),
    'literal credential': re.compile(rb"""(?i)["']?(?:api[_-]?key|apiKey|access[_-]?token|password|secret)["']?\s*[:=]\s*["']([A-Za-z0-9+/=_-]{16,})["']"""),
}
FORBIDDEN_DIRS = {'data','workspace','tasks','skills','output','logs','results','.venv','venv'}
SKIP_DIRS = {'.git','__pycache__','.pytest_cache','.ruff_cache'}

def scan(root):
    issues = []
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        if any(p in SKIP_DIRS for p in rel.parts):
            continue
        if path.is_symlink():
            issues.append((str(rel),'symlink not allowed in release'))
            continue
        if path.is_dir():
            if len(rel.parts)==1 and path.name in FORBIDDEN_DIRS:
                issues.append((str(rel),'private/generated directory in release'))
            continue
        if not path.is_file():
            continue
        if (path.name.startswith('.env') and path.name != '.env.example') or path.name in {'my_api.json','auth.json','auth-profiles.json'} or path.name.endswith(('.local.json','.pem','.key','.zip','.tar','.gz')):
            issues.append((str(rel),'private configuration or archive'))
        if path.stat().st_size > 2_000_000:
            issues.append((str(rel),'unexpected large file'))
            continue
        content = path.read_bytes()
        for label, regex in PATTERNS.items():
            for match in regex.finditer(content):
                line = content[:match.start()].count(b'\n')+1
                issues.append((f'{rel}:{line}',label))
    return issues

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    args=p.parse_args(argv)
    issues=scan(args.root.resolve())
    for path,reason in issues:
        print(f'{path}: {reason}',file=sys.stderr)
    print(f'Release scan: {len(issues)} issue(s)')
    return bool(issues)
if __name__=='__main__':
    raise SystemExit(main())
