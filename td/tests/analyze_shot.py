"""Разбор снимка буфера слоя (paint/tmp/selftest_brush.png).

    python paint/td/tests/analyze_shot.py [путь к png]

Печатает размер, профиль яркости по строкам, положение мазка и значения в
ключевых точках. Нужен, чтобы по снимку из TD понять: нарисовала ли кисть,
где именно оказался мазок и не перевёрнут ли он по вертикали (для этого
сравниваются строка мазка и её зеркало).
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PAINT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        PAINT, 'tmp', 'selftest_brush.png')
    if not os.path.isfile(path):
        print('нет файла %s' % path)
        return 1
    import test_selftest_png
    m = test_selftest_png.load_selftest_module()

    print('файл: %s (%d байт)' % (path, os.path.getsize(path)))
    w, h, nch, rows = m.png_read(path)
    print('размер: %dx%d, каналов: %d' % (w, h, nch))

    prof = m.row_profile(rows, nch, w)
    total = sum(prof)
    top = max(prof) if prof else 0
    best = prof.index(top) if prof else -1
    print('суммарная «яркость» по всем строкам: %d' % total)
    print('самая яркая строка: %d (значение %d), зеркало: %d' % (best, top, h - 1 - best))

    exp = int(h * 0.25)
    verdict, row = m.decide_orientation(prof, exp, 60)
    print('ожидаемая строка мазка при y=25%%: %d' % exp)
    print('вердикт ориентации: %s (строка %d)' % (verdict, row))

    if nch >= 4:
        for label, y in (('центр мазка (ожидание)', exp),
                         ('зеркальная строка', h - 1 - exp),
                         ('далеко внизу', min(h - 2, exp + int(h * 0.4)))):
            print('  %-24s y=%4d  RGBA=%s' % (label, y, m.pixel(rows, nch, w // 2, y)))
        band = [(y, prof[y]) for y in range(h) if prof[y] > top * 0.5]
        if band:
            print('  строки с мазком (ярче половины пика): %d..%d (всего %d)'
                  % (band[0][0], band[-1][0], len(band)))
    else:
        print('  каналов меньше четырёх — альфы нет, смотрю яркость')
        print('  центр y=%d -> %s' % (exp, m.pixel(rows, nch, w // 2, exp)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
