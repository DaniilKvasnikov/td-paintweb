"""Проверить, не испорчена ли кодировка в файлах проекта (ищем двойное кодирование).

    python scan_encoding.py <папка>

Для каждого текстового файла: читается как UTF-8, считаются «мусорные маркеры»
(Ð/Ñ/â€) и наличие осмысленных русских слов. Файл считается испорченным, если
маркеры есть И русских слов не найдено.
"""

import io
import os
import sys

PROBES = ('\u043f\u0443\u0441\u0442\u043e\u0439', '\u043e\u0448\u0438\u0431\u043a\u0430',
          '\u0448\u0435\u0439\u0434\u0435\u0440', '\u043f\u0430\u0440\u0430\u043c\u0435\u0442\u0440',
          '\u0437\u0430\u043f\u0443\u0441\u043a', '\u043a\u043b\u0438\u0435\u043d\u0442')
BAD = ('\u00d0', '\u00d1', '\u00e2\u20ac')
TEXT_EXT = ('.py', '.md', '.js', '.css', '.html', '.glsl', '.txt', '.json')


def main():
    root = sys.argv[1]
    self_path = os.path.abspath(__file__)
    broken, ok, skipped = [], 0, 0
    for dirpath, _dirs, files in os.walk(root):
        if '__pycache__' in dirpath or os.sep + 'tmp' in dirpath:
            continue
        for name in files:
            if not name.lower().endswith(TEXT_EXT):
                continue
            path = os.path.join(dirpath, name)
            if os.path.abspath(path) == self_path:
                continue          # в этом файле маркеры перечислены как данные
            try:
                with io.open(path, encoding='utf-8') as f:
                    text = f.read()
            except Exception as e:
                broken.append((path, 'не читается как UTF-8: %s' % e))
                continue
            bad = sum(text.count(b) for b in BAD)
            good = sum(1 for p in PROBES if p in text)
            if bad and not good:
                broken.append((path, 'мусорных маркеров %d, русских слов 0' % bad))
            elif bad and good:
                broken.append((path, 'подозрительно: маркеров %d, слов %d' % (bad, good)))
            else:
                ok += 1
    print('проверено файлов: %d, чистых: %d, проблемных: %d' % (ok + len(broken), ok,
                                                              len(broken)))
    for path, why in broken:
        print('  ИСПОРЧЕН %s — %s' % (path, why))
    return 1 if broken else 0


if __name__ == '__main__':
    sys.exit(main())
