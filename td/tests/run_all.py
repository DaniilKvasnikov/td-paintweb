"""Все тесты PaintWeb без TouchDesigner.

    python td\\tests\\run_all.py        (репозиторий веб-интерфейса)
    python paint\\td\\tests\\run_all.py  (папка данных проекта)

Проверяет логику рантайма на фейковом TD, прогоняет сам скрипт сборки (создание
дерева, связей, параметров, файлов и отчёта), один шаг, декодер PNG, шейдеры и
веб-клиент в Node. Возвращает ненулевой код при провале.

Отдельно следит за тем, чтобы тесты НЕ трогали рабочие файлы веб-клиента: раньше
прогон в репозитории затирал web/app.js тестовой заглушкой, и клиент приходилось
восстанавливать из git. Теперь файлы сверяются до и после каждого шага, при
изменении возвращаются на место, а прогон говорит, какой шаг это сделал.
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Папка исходников: <paint> или корень репозитория — смотря где лежат тесты
PAINT = os.path.dirname(os.path.dirname(HERE))
WEB = os.path.join(PAINT, 'web')

sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import test_paint_runtime     # noqa: E402
import test_build_paint_web   # noqa: E402
import test_run_paint_web     # noqa: E402
import test_selftest_png      # noqa: E402
import test_shaders           # noqa: E402


def web_snapshot():
    """Снимок рабочих файлов веб-клиента (не тестовых)."""
    snap = {}
    if os.path.isdir(WEB):
        for name in sorted(os.listdir(WEB)):
            path = os.path.join(WEB, name)
            if os.path.isfile(path):
                try:
                    with open(path, 'rb') as f:
                        snap[name] = f.read()
                except Exception:
                    pass
    return snap


def web_restore(snap):
    """Вернуть рабочие файлы клиента, если тест их изменил. False = были правки."""
    now = web_snapshot()
    changed = [n for n in sorted(set(now) | set(snap)) if now.get(n) != snap.get(n)]
    if not changed:
        return True
    for name, data in snap.items():
        if now.get(name) != data:
            try:
                with open(os.path.join(WEB, name), 'wb') as f:
                    f.write(data)
            except Exception as e:
                print('  не удалось вернуть %s: %s' % (name, e))
    for name in changed:
        if name not in snap:
            try:
                os.remove(os.path.join(WEB, name))
            except Exception:
                pass
    print('  ПРОВАЛ: шаг изменил рабочие файлы клиента (%s) — файлы возвращены'
          % ', '.join(changed))
    return False


def step(number, title, func):
    print('\n' + '#' * 66)
    print('# %s  %s' % (number, title))
    print('#' * 66)
    snap = web_snapshot()
    rc = func()
    if not web_restore(snap):
        rc |= 1
    return rc


def run_web_harness():
    """Веб-клиент исполняется в Node — если node есть в системе."""
    node = shutil.which('node')
    if not node:
        print('  node не найден — прогон веб-клиента пропущен')
        return 0
    script = os.path.join(HERE, 'web_harness.js')
    try:
        res = subprocess.run([node, script], capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=120)
    except Exception as e:
        print('  не удалось запустить node: %s' % e)
        return 1
    sys.stdout.write(res.stdout or '')
    if res.stderr:
        sys.stdout.write(res.stderr)
    return res.returncode


def main():
    rc = 0
    rc |= step('1/6', 'рантайм на фейковом TouchDesigner', test_paint_runtime.main)
    rc |= step('2/6', 'прогон скрипта сборки (build_paint_web.py)',
               test_build_paint_web.main)
    rc |= step('3/6', 'один шаг run_paint_web.py (сборка + проверка + сводка)',
               test_run_paint_web.main)
    rc |= step('4/6', 'декодер PNG самопроверки', test_selftest_png.main)
    rc |= step('5/6', 'статическая проверка шейдеров (GLSL)', test_shaders.main)
    rc |= step('6/6', 'веб-клиент в Node (заглушка DOM)', run_web_harness)
    print('\n' + '=' * 66)
    print('ИТОГ: ' + ('ВСЁ ХОРОШО' if rc == 0 else 'ЕСТЬ ПРОВАЛЫ'))
    print('=' * 66)
    return rc


if __name__ == '__main__':
    sys.exit(main())
