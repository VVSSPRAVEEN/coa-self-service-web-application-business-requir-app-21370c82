import json, os, re, sys
PATTERNS = [
    ('\\beval\\s*\\(', 'use of eval()'),
    ('\\bexec\\s*\\(', 'use of exec()'),
    ('os\\.system\\s*\\(', 'use of os.system()'),
    ('subprocess\\.\\w+\\([^)]*shell\\s*=\\s*True', 'subprocess with shell=True'),
    ('yaml\\.load\\s*\\(', 'yaml.load() without SafeLoader'),
    ('pickle\\.loads?\\s*\\(', 'pickle load'),
    ('(?i)(api_key|secret|password|token)\\s*=\\s*[\\"\'][^\\"\']{6,}[\\"\']', 'hardcoded secret/credential'),
    ('sqlite3\\.connect', 'sqlite usage (prefer postgres)'),
    ('(?i)\\b(?:VITE|NEXT_PUBLIC|PUBLIC|REACT_APP|GATSBY|EXPO_PUBLIC|CLIENT)_(?:[A-Z0-9]+_)*(?:API_?KEY|ACCESS_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_?KEY)(?:_[A-Z0-9]+)*\\s*[:=]\\s*[\\"\']?[^\\"\'\\s,}]{6,}', 'client-bundled secret: VITE_/NEXT_PUBLIC_/PUBLIC_/REACT_APP_ var is shipped to the browser (value redacted)'),
    ('\\bAKIA[0-9A-Z]{16}\\b', 'AWS access key id (value redacted)'),
    ('\\bsk-[A-Za-z0-9_-]{16,}', 'provider API key, sk- prefixed (value redacted)'),
    ('-----BEGIN [A-Z ]*PRIVATE KEY-----', 'embedded PEM private key block (value redacted)'),
    ('(?i)[\\"\'](?:api[_-]?key|apikey|client[_-]?secret|secret|password|passwd|token|access[_-]?token|auth[_-]?token|credentials?)[\\"\']\\s*:\\s*[\\"\'](?!\\$\\{)[^\\"\']{6,}[\\"\']', 'hardcoded credential in quoted JSON/YAML form (value redacted)'),
    ('(?im)^[ \\t]*(?<![\\w-])(?:api[_-]?key|apikey|client[_-]?secret|secret|password|passwd|pwd|token|access[_-]?token|auth[_-]?token|credentials?)[ \\t]*[:=][ \\t]*[\\"\']?([A-Za-z0-9_\\-\\.\\/+=!@%]{6,})[ \\t]*(?:#.*)?$', 'hardcoded credential in unquoted key=value or key:value form (value redacted)'),
]
SUFFIX = {'.cjs', '.css', '.cts', '.env', '.html', '.js', '.json', '.jsx', '.mjs', '.mts', '.py', '.ts', '.tsx', '.yaml', '.yml'}
SKIP_DIRS = {'.venv', 'venv', 'node_modules', '.git', '__pycache__', 'dist', 'build', '.p3', '.ruff_cache', '.pytest_cache', '.mypy_cache', '.cache', '.next', 'coverage'}
root = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.getcwd(), 'security_findings.json')
compiled = [(re.compile(p), d) for p, d in PATTERNS]
findings = []
for dirpath, dirnames, files in os.walk(root):
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith('.egg-info')]
    for name in files:
        if os.path.splitext(name)[1] not in SUFFIX:
            continue
        fp = os.path.join(dirpath, name)
        if name.startswith('_security_audit') or name.startswith('_qa_verdict') or name == 'security_findings.json':
            continue
        try:
            with open(fp, encoding='utf-8', errors='ignore') as f:
                text = f.read()
        except Exception:
            continue
        rel = os.path.relpath(fp, root).replace('\\', '/')
        for pat, desc in compiled:
            for m in pat.finditer(text):
                line_no = text.count('\n', 0, m.start()) + 1
                findings.append({'file': rel, 'line': line_no, 'issue': desc})
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump({'count': len(findings), 'findings': findings}, f, indent=2)
print('AUDIT_SCAN_FINDINGS=' + str(len(findings)))
