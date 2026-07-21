with open('app/api/v1/routes_ports.py', 'r') as f:
    code = f.read()

import re

# Fix working_days in get_port_rules
old_block1 = '''    working_days = rules.working_days
    if isinstance(working_days, str):
        try:
            working_days = json.loads(working_days)
        except Exception:
            working_days = [d.strip() for d in working_days.split(",") if d.strip()]'''
new_block1 = '''    working_days = rules.working_days
    if isinstance(working_days, str):
        try:
            parsed = json.loads(working_days)
            if isinstance(parsed, list):
                working_days = parsed
            else:
                working_days = [d.strip() for d in working_days.split(",") if d.strip()]
        except Exception:
            working_days = [d.strip() for d in working_days.split(",") if d.strip()]'''
code = code.replace(old_block1, new_block1)

# Fix working_days in update_port_rules
old_block2 = '''    working_days = port_rules.working_days
    if isinstance(working_days, str):
        try:
            working_days = json.loads(working_days)
        except Exception:
            working_days = [d.strip() for d in working_days.split(",") if d.strip()]'''
new_block2 = '''    working_days = port_rules.working_days
    if isinstance(working_days, str):
        try:
            parsed = json.loads(working_days)
            if isinstance(parsed, list):
                working_days = parsed
            else:
                working_days = [d.strip() for d in working_days.split(",") if d.strip()]
        except Exception:
            working_days = [d.strip() for d in working_days.split(",") if d.strip()]'''
code = code.replace(old_block2, new_block2)

with open('app/api/v1/routes_ports.py', 'w') as f:
    f.write(code)
