"""Все тесты PaintWeb без TouchDesigner.

    python paint\\td\\tests\\run_all.py

Проверяет логику рантайма на фейковом TD, прогоняет сам скрипт сборки (создание
дерева, связей, параметров, файлов и отчёта) и декодер PNG, на котором держится
автоопределение ориентации в самопроверке. Возвращает ненулевой код при провале.
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
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
    print('#' * 66)
    print('# 1/6  рантайм на фейковом TouchDesigner')
    print('#' * 66)
    rc = test_paint_runtime.main()
    print('\n' + '#' * 66)
    print('# 2/6  прогон скрипта сборки (build_paint_web.py)')
    print('#' * 66)
    rc |= test_build_paint_web.main()
    print('\n' + '#' * 66)
    print('# 3/6  один шаг run_paint_web.py (сборка + проверка + сводка)')
    print('#' * 66)
    rc |= test_run_paint_web.main()
    print('\n' + '#' * 66)
    print('# 4/6  декодер PNG самопроверки')
    print('#' * 66)
    rc |= test_selftest_png.main()
    print('\n' + '#' * 66)
    print('# 5/6  статическая проверка шейдеров (GLSL)')
    print('#' * 66)
    rc |= test_shaders.main()
    print('\n' + '#' * 66)
    print('# 6/6  веб-клиент в Node (заглушка DOM)')
    print('#' * 66)
    rc |= run_web_harness()
    print('\n' + '=' * 66)
    print('ИТОГ: ' + ('ВСЁ ХОРОШО' if rc == 0 else 'ЕСТЬ ПРОВАЛЫ'))
    print('=' * 66)
    return rc


if __name__ == '__main__':
    sys.exit(main())
