"""Прогон run_paint_web.py на фейковом TD: один шаг не должен падать.

    python paint/td/tests/test_run_paint_web.py

Пикселей в заглушке нет (их считает GPU), поэтому проверки про мазок и композит
в ней обязаны провалиться — и это ровно то, что здесь проверяется: скрипт находит
папку, собирает компонент, доходит до конца самопроверки, ЧЕСТНО сообщает о
провалах (а не «всё чисто»), пишет сводку на диск и печатает адреса.
"""

import os
import shutil
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PAINT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fake_td  # noqa: E402

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

RESULTS = []


def check(name, cond, extra=''):
    RESULTS.append((bool(cond), name, extra))
    print('  %s %s%s' % ('ok  ' if cond else 'ПРОВАЛ', name,
                         ('   ' + str(extra)) if extra and not cond else ''))


def main():
    print('\n=== run_paint_web на фейковом TD ==============================')
    if not fake_td.stubs_available():
        print('  стабы TD не найдены — тест пропущен')
        return 0

    tmp = os.path.join(HERE, '_tmp_run')
    shutil.rmtree(tmp, ignore_errors=True)
    paint_dir = os.path.join(tmp, 'paint')
    os.makedirs(os.path.join(paint_dir, 'td'))
    os.makedirs(os.path.join(paint_dir, 'web'))
    os.makedirs(os.path.join(paint_dir, 'uploads'))
    shutil.copytree(os.path.join(PAINT, 'td', 'runtime'),
                    os.path.join(paint_dir, 'td', 'runtime'))
    for name in ('build_paint_web.py', 'selftest_paint_web.py', 'diag_paint.py'):
        shutil.copy2(os.path.join(PAINT, 'td', name), os.path.join(paint_dir, 'td', name))
    with open(os.path.join(paint_dir, 'web', 'index.html'), 'w', encoding='utf-8') as f:
        f.write('<html>test</html>')
    with open(os.path.join(paint_dir, 'web', 'app.js'), 'w', encoding='utf-8') as f:
        f.write('// test client')
    with open(os.path.join(paint_dir, 'web', 'style.css'), 'w', encoding='utf-8') as f:
        f.write('/* test styles */')
    with open(os.path.join(paint_dir, 'uploads', 'sample.png'), 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n')

    fake_td.FakeOp.REG.clear()
    fake_td.FakeOp('/', 'baseCOMP')
    fake_td.FakeOp('/project1', 'baseCOMP')

    runner = os.path.join(PAINT, 'td', 'run_paint_web.py')
    with open(runner, 'r', encoding='utf-8') as f:
        src = f.read()
    ns = {'op': fake_td.make_op_func(),
          'project': fake_td.FakeProject(tmp),
          'print': print,
          '__name__': '__main__'}
    err = None
    try:
        exec(compile(src, runner, 'exec'), ns)
    except Exception as e:
        err = e
    check('раннер не упал целиком', err is None, err and str(err))

    base = fake_td.FakeOp.REG.get('/project1/paint_web')
    check('компонент собран через раннер', base is not None)
    summary = os.path.join(paint_dir, 'tmp', 'run_summary.txt')
    check('сводка записана на диск', os.path.isfile(summary), summary)
    if os.path.isfile(summary):
        with open(summary, 'r', encoding='utf-8') as f:
            body = f.read()
        check('в сводке есть блок сборки', '--- сборка: завершена=True' in body, body[:300])
        check('в сводке есть блок самопроверки', '--- самопроверка: дошла до конца=' in body)
        check('в сводке есть адреса или причина', '--- адреса ---' in body)
        # Ключевое: в заглушке пиксели нарисовать нечем (это делает GPU), поэтому
        # проверки про мазок и композит обязаны провалиться, а вердикт — быть
        # честным. Если бы сводка сказала «всё чисто», это была бы ложная зелень.
        check('ложной зелени нет: вердикт честный',
              'ВЕРДИКТ: ЕСТЬ ЧТО ИСПРАВЛЯТЬ' in body, body[-600:])
        check('самопроверка дошла до конца (а не упала на середине)',
              'дошла до конца=True' in body,
              [l for l in body.splitlines() if 'самопроверка:' in l])
        check('в сводке перечислены провалы самопроверки',
              'провалов 0' not in body and 'мазок вообще есть' in body,
              [l for l in body.splitlines() if 'провал' in l.lower()][:3])
        # Диагностика доставки штампов должна отработать целиком и без падения:
        # её вывод — единственное место, где видно, что именно уехало в кисть.
        check('в сводке есть блок диагностики штампов',
              '--- диагностика штампов' in body)
        check('диагностика не упала',
              'диагностика упала' not in body,
              [l for l in body.splitlines() if 'упала' in l][:2])
        check('диагностика дошла до файла отчёта',
              'отчёт диагностики' in body,
              [l for l in body.splitlines() if 'диагност' in l][:3])
    diag_txt = os.path.join(paint_dir, 'tmp', 'diag_paint.txt')
    check('файл диагностики записан', os.path.isfile(diag_txt), diag_txt)
    if os.path.isfile(diag_txt):
        with open(diag_txt, 'r', encoding='utf-8') as f:
            dg = f.read()
        check('в диагностике есть разбор по кадрам', 'uniform' in dg, dg[:200])
        check('в диагностике есть решающие опыты', 'опыты' in dg, dg[-400:])
    check('сборка действительно прошла (tox сохранён)',
          any(p.endswith('paint_web.tox') for p in (base.saved if base else [])),
          base.saved if base else None)

    print('\n===============================================================')
    bad = [r for r in RESULTS if not r[0]]
    print('проверок: %d, провалов: %d' % (len(RESULTS), len(bad)))
    for _, name, extra in bad:
        print('  ПРОВАЛ: %s   %s' % (name, extra))
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
