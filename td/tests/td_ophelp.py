"""Вывести справку по параметрам оператора TD из локального TDParameterHelp.json.

    python td_ophelp.py dattoCHOP chopToTOP

Нужно, чтобы не угадывать семантику параметров (например, что делает firstrow
у DAT to CHOP и какой у него формат вывода).
"""

import json
import os
import re
import sys

HELP = r'C:\Program Files\Derivative\TouchDesigner\Config\TDParameterHelp.json'


def block_for(text, opname):
    key = '"%s"' % opname
    idx = text.find(key)
    if idx < 0:
        return None
    start = text.find('{', idx)
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def main():
    with open(HELP, 'r', encoding='utf-8') as f:
        text = f.read()
    for name in sys.argv[1:]:
        blk = block_for(text, name)
        print('\n===== %s =====' % name)
        if not blk:
            print('  не найдено')
            continue
        pars = re.search(r'"parameters"\s*:\s*\{', blk)
        if not pars:
            print('  нет параметров')
            continue
        sub = block_for(blk, 'parameters') if False else None
        # собственные параметры: ключ -> label/summary/parType
        for m in re.finditer(
                r'"([a-zA-Z0-9_]+)"\s*:\s*\{\s*"label"\s*:\s*"([^"]*)"'
                r'(?:\s*,\s*"summary"\s*:\s*"((?:[^"\\]|\\.)*)")?'
                r'(?:\s*,\s*"parType"\s*:\s*"([^"]*)")?', blk):
            key, label, summary, ptype = m.groups()
            if key in ('summary', 'label'):
                continue
            s = (summary or '').replace('\\n', ' ').replace('\\t', ' ')
            if len(s) > 130:
                s = s[:130] + '…'
            print('  %-16s %-26s %-10s %s' % (key, label, ptype or '', s))


if __name__ == '__main__':
    main()
