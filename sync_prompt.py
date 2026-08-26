# -*- coding: utf-8 -*-
import re, io, importlib.util
spec = importlib.util.spec_from_file_location('t', 'i2va_test.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
app = io.open('app.html', encoding='utf-8').read()
sp = m.SYSTEM_PROMPT.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')
new_sp = 'const SYSTEM_PROMPT = `' + sp + '`;'
app2, n = re.subn(r'const SYSTEM_PROMPT = `.*?`;', lambda _: new_sp, app, count=1, flags=re.S)
assert n == 1, 'SYSTEM_PROMPT block not found'
io.open('app.html', 'w', encoding='utf-8').write(app2)
print('system prompt replaced OK')
