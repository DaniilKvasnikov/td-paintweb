"""Проверка декодера PNG, на котором держится автоопределение ориентации.

Самопроверка в TD решает вопрос «перевёрнут ли мазок» по PNG-файлу (в PNG строки
идут сверху вниз по формату — значит, это честная точка отсчёта, в отличие от
догадок о конвенциях TD). Поэтому декодер обязан быть верным на всех пяти типах
фильтров строк, которые может выдать TD.

Запуск:  python paint/td/tests/test_selftest_png.py
"""

import os
import struct
import sys
import types
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
PAINT = os.path.dirname(os.path.dirname(HERE))

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

RESULTS = []


def check(name, cond, extra=''):
    RESULTS.append((bool(cond), name, extra))
    print('  %s %s%s' % ('ok  ' if cond else 'ПРОВАЛ', name,
                         ('   ' + str(extra)) if extra and not cond else ''))


def load_selftest_module():
    """Загрузить функции самопроверки, не запуская её работу с TD.

    Подсовываем op(), который всегда возвращает None: main() тогда просто
    сообщит, что компонента нет, и завершится — а png_read/row_profile/pixel
    останутся доступны. Его болтовню в консоль подавляем.
    """
    import contextlib
    import io
    path = os.path.join(PAINT, 'td', 'selftest_paint_web.py')
    with open(path, 'r', encoding='utf-8') as f:
        src = f.read()
    m = types.ModuleType('selftest_probe')
    m.__dict__['__file__'] = path
    m.__dict__['op'] = lambda _p: None
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(src, path, 'exec'), m.__dict__)
    return m


# --- запись PNG с выбранным фильтром (чтобы проверить все ветки декодера) ---

def _filter_line(ft, line, prev, nch):
    out = bytearray(len(line))
    for i in range(len(line)):
        a = line[i - nch] if i >= nch else 0
        b = prev[i]
        c = prev[i - nch] if i >= nch else 0
        x = line[i]
        if ft == 0:
            v = x
        elif ft == 1:
            v = x - a
        elif ft == 2:
            v = x - b
        elif ft == 3:
            v = x - ((a + b) >> 1)
        else:
            pa = abs(b - c)
            pb = abs(a - c)
            pc = abs(a + b - 2 * c)
            pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            v = x - pr
        out[i] = v & 255
    return bytes(out)


def write_png(path, w, h, nch, rows, filter_type):
    ctype = {1: 0, 2: 4, 3: 2, 4: 6}[nch]
    raw = bytearray()
    prev = bytes(w * nch)
    for line in rows:
        raw.append(filter_type)
        raw += _filter_line(filter_type, line, prev, nch)
        prev = line

    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))

    blob = b'\x89PNG\r\n\x1a\n'
    blob += chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, ctype, 0, 0, 0))
    blob += chunk(b'IDAT', zlib.compress(bytes(raw), 6))
    blob += chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(blob)


def make_rows(w, h, nch, band_row, band_value):
    """Картинка с яркой горизонтальной полосой в известной строке."""
    rows = []
    for y in range(h):
        val = band_value if y == band_row else 0
        line = bytearray()
        for _x in range(w):
            for _c in range(nch):
                line.append(val)
        rows.append(bytes(line))
    return rows


def main():
    print('\n=== декодер PNG из самопроверки ===============================')
    m = load_selftest_module()
    tmp = os.path.join(HERE, '_tmp_png')
    if not os.path.isdir(tmp):
        os.makedirs(tmp)

    W, H, BAND = 64, 40, 9
    for nch in (1, 3, 4):
        for ft in (0, 1, 2, 3, 4):
            path = os.path.join(tmp, 'p_%d_%d.png' % (nch, ft))
            write_png(path, W, H, nch, make_rows(W, H, nch, BAND, 200), ft)
            try:
                w, h, nch2, rows = m.png_read(path)
            except Exception as e:
                check('декод nch=%d фильтр=%d' % (nch, ft), False, e)
                continue
            prof = m.row_profile(rows, nch2, w)
            best = prof.index(max(prof))
            ok = (w == W and h == H and nch2 == nch and best == BAND)
            check('декод nch=%d фильтр=%d (полоса в строке %d)' % (nch, ft, BAND),
                  ok, 'w=%s h=%s nch=%s строка=%s' % (w, h, nch2, best))

    # пиксельный доступ и ориентация «сверху вниз»
    path = os.path.join(tmp, 'pixel.png')
    rows = make_rows(8, 6, 4, 2, 128)
    write_png(path, 8, 6, 4, rows, 0)
    w, h, nch, dec = m.png_read(path)
    check('pixel() читает верхнюю строку как 0',
          m.pixel(dec, nch, 0, 2)[3] == 128 and m.pixel(dec, nch, 0, 0)[3] == 0,
          (m.pixel(dec, nch, 0, 2), m.pixel(dec, nch, 0, 0)))
    check('не-PNG отвергается', _raises(m.png_read, os.path.join(tmp, 'x.txt')))

    # ---------------------------------------------------------------- вердикт
    # Ключевая логика самопроверки: она вправе переключить Flipy ТОЛЬКО когда
    # мазок действительно зеркален. Ошибочный 'flip' сломал бы верную настройку.
    print('\n--- вердикт по ориентации ---')
    H = 200
    TOL = 20
    exp = 50

    def prof_at(rows_list):
        p = [0] * H
        for r in rows_list:
            p[r] = 1000
        return p

    check('мазок на месте → ok', m.decide_orientation(prof_at([exp]), exp, TOL)[0] == 'ok')
    check('мазок зеркален → flip',
          m.decide_orientation(prof_at([H - 1 - exp]), exp, TOL)[0] == 'flip')
    check('мазок в стороне → fail, Flipy не трогаем',
          m.decide_orientation(prof_at([120]), exp, TOL)[0] == 'fail',
          m.decide_orientation(prof_at([120]), exp, TOL))
    check('пустая картинка → fail', m.decide_orientation([0] * H, exp, TOL)[0] == 'fail')
    check('пустой профиль → fail', m.decide_orientation([], exp, TOL)[0] == 'fail')
    check('край допуска ещё ok',
          m.decide_orientation(prof_at([exp + TOL]), exp, TOL)[0] == 'ok')
    check('на пиксель дальше допуска — уже не ok',
          m.decide_orientation(prof_at([exp + TOL + 1]), exp, TOL)[0] != 'ok')
    check('симметричный профиль не считается перевёрнутым',
          m.decide_orientation(prof_at([exp, H - 1 - exp]), exp, TOL)[0] == 'ok')
    check('вердикт возвращает найденную строку',
          m.decide_orientation(prof_at([H - 1 - exp]), exp, TOL)[1] == H - 1 - exp)

    print('\n===============================================================')
    bad = [r for r in RESULTS if not r[0]]
    print('проверок: %d, провалов: %d' % (len(RESULTS), len(bad)))
    for _, name, extra in bad:
        print('  ПРОВАЛ: %s   %s' % (name, extra))
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


def _raises(fn, arg):
    try:
        fn(arg)
        return False
    except Exception:
        return True


if __name__ == '__main__':
    sys.exit(main())
