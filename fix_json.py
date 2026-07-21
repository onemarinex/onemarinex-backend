with open('app/api/v1/routes_ports.py', 'r') as f:
    code = f.read()

import re

# We will inject a helper function at the top
helper = '''
def safe_parse_json(val, default_val):
    if isinstance(val, list) or isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            import json
            return json.loads(val)
        except Exception:
            pass
    return default_val
'''

if 'def safe_parse_json' not in code:
    code = code.replace('import json\n', 'import json\n' + helper + '\n')

# Fix get_port_rules
code = re.sub(
    r'"rules": rules\.rules if isinstance\(rules\.rules, list\) else \(json\.loads\(rules\.rules\) if isinstance\(rules\.rules, str\) else \[\]\),',
    r'"rules": safe_parse_json(rules.rules, []),',
    code
)

# Fix update_port_rules
code = re.sub(
    r'"rules": port_rules\.rules if isinstance\(port_rules\.rules, list\) else \(json\.loads\(port_rules\.rules\) if isinstance\(port_rules\.rules, str\) else \[\]\),',
    r'"rules": safe_parse_json(port_rules.rules, []),',
    code
)

with open('app/api/v1/routes_ports.py', 'w') as f:
    f.write(code)
