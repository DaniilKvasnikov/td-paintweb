"""PaintWeb — ОДИН шаг: собрать, проверить, показать адрес.

Запуск в Textport (одна строка):

    exec(open(r"C:\\Users\\DaniilNotebook\\Documents\\PixelFlow\\paint\\td\\run_paint_web.py",
              encoding="utf-8").read())

Что делает по порядку:
  1. собирает компонент `paint_web` (тот же скрипт сборки, что и отдельно);
  2. если сборка прошла без проблем — запускает самопроверку в TD (синтетический
     мазок, пиксели, undo, кодирование, ориентация);
  3. пишет сводку в `paint/tmp/run_summary.txt` и печатает адрес для планшета.

Сводку никуда копировать не нужно: она лежит в рабочей папке проекта.
"""

import os
import time
import traceback


def _ns():
    """Имена, которые Textport даёт исполняемому коду (me в Textport нет)."""
    g = {'print': print}
    for name in ('op', 'project', 'app', 'me', 'parent'):
        try:
            g[name] = eval(name)          # noqa: S307 — eval в namespace Textport
        except Exception:
            pass
    return g


def _run_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        src = f.read()
    ns = _ns()
    ns.setdefault('__name__', '__main__')
    exec(compile(src, path, 'exec'), ns)
    return ns


def _run_file_capture(path):
    """Выполнить скрипт и вернуть то, что он напечатал.

    Нужно для диагностики: её вывод и есть результат, а Textport при запуске из
    скрипта в сводку не попадает.
    """
    lines = []

    def collect(*args):
        lines.append(' '.join(str(a) for a in args))

    with open(path, 'r', encoding='utf-8') as f:
        src = f.read()
    ns = _ns()
    ns['print'] = collect
    ns.setdefault('__name__', '__main__')
    exec(compile(src, path, 'exec'), ns)
    return lines


def _lines(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _resolve_paint_dir(ns_build):
    """Папку paint/ ищем кодом самой сборки — один источник правды."""
    try:
        fn = ns_build.get('resolve_paint_dir')
        if callable(fn):
            d = fn()
            if d:
                return d
    except Exception:
        pass
    try:
        d = os.path.join(project.folder, 'paint')
        return d if os.path.isdir(d) else None
    except Exception:
        return None


def main():
    t0 = time.time()
    print('=' * 66)
    print('PaintWeb: сборка + проверка, один шаг')
    print('=' * 66)

    paint_dir = _resolve_paint_dir({})
    build_py = os.path.join(paint_dir, 'td', 'build_paint_web.py') if paint_dir else None
    if not build_py or not os.path.isfile(build_py):
        print('НЕ НАЙДЕН build_paint_web.py.')
        print('Скорее всего проект сохранён не в папке репозитория PixelFlow.')
        print('Тогда запусти сборку напрямую по полному пути:')
        print(r'  exec(open(r"<путь>\paint\td\build_paint_web.py", encoding="utf-8").read())')
        return

    # ---------------------------------------------------------------- сборка
    ns_build = _run_file(build_py)
    paint_dir = _resolve_paint_dir(ns_build) or paint_dir
    build_report = _lines(ns_build.get('REPORT'))
    build_fail = _lines(ns_build.get('FAIL'))
    # Скрипты сами подтверждают, что дошли до конца: иначе «ноль проблем» могло бы
    # означать «упало на середине, не успев ничего записать» — ложная зелень.
    build_ok = (ns_build.get('COMPLETED') is True) and not build_fail

    # ---------------------------------------------------------------- проверка
    checks = 0
    selftest_fail = []
    selftest_error = None
    st_ok = False
    st_completed = False
    st_lines = []
    if not build_ok:
        print('\nСборка не завершилась или сообщила о проблемах — самопроверку пропускаю.')
    else:
        st_py = os.path.join(paint_dir, 'td', 'selftest_paint_web.py')
        if os.path.isfile(st_py):
            print('\n' + '-' * 66)
            print('самопроверка в TD')
            print('-' * 66)
            try:
                # Вывод самопроверки собираем: по нему видно, ЧТО именно она
                # измерила (пиксели, кадры, снимки), а в сводку попадёт хвост.
                st_lines = _run_file_capture(st_py)
                checks = 0
                for ln in st_lines:
                    if ln.startswith('проверок:'):
                        try:
                            checks = int(ln.split()[1].rstrip(','))
                        except Exception:
                            pass
                selftest_fail = [ln.strip() for ln in st_lines
                                 if ln.strip().startswith('ПРОВАЛ:')]
                st_completed = bool(checks)
                min_checks = 25
                if not st_completed:
                    tail = [l for l in st_lines if l.strip()][-1:]
                    selftest_error = ('самопроверка не дошла до конца: %s'
                                      % (tail[0] if tail else 'причина неизвестна'))
                elif checks < min_checks:
                    selftest_error = ('самопроверка выполнила только %d проверок '
                                      '(ожидалось минимум %d)' % (checks, min_checks))
                else:
                    st_ok = not selftest_fail
                # Полный вывод самопроверки кладём на диск: в сводку попадает
                # только хвост, а разбирать иногда нужно начало (например, какие
                # именно ноды сообщили об ошибках).
                try:
                    with open(os.path.join(paint_dir, 'tmp', 'selftest_output.txt'),
                              'w', encoding='utf-8') as f:
                        f.write('\n'.join(st_lines))
                except Exception:
                    pass
            except Exception:
                selftest_error = traceback.format_exc()
                st_lines = []
        else:
            selftest_error = 'нет файла %s' % st_py

    # ---------------------------------------------------------------- диагностика
    # Отдельный, точечный разбор: самопроверка говорит «мазка нет», но не говорит,
    # на каком шаге он теряется. Эта часть печатает состояние по кадрам и потому
    # прикладывается в сводку — её и достаточно, чтобы понять причину.
    diag_lines = []
    if build_ok:
        diag_py = os.path.join(paint_dir, 'td', 'diag_paint.py')
        if os.path.isfile(diag_py):
            print('\n' + '-' * 66)
            print('диагностика доставки штампов')
            print('-' * 66)
            try:
                diag_lines = _run_file_capture(diag_py)
            except Exception:
                diag_lines = ['диагностика упала: %s'
                              % traceback.format_exc().splitlines()[-1]]

    # ---------------------------------------------------------------- адреса
    urls = [l.split(':', 1)[1].strip() for l in build_report
            if l.startswith('планшет:') or l.startswith('на ПК:')]

    summary = []
    summary.append('=== PaintWeb: сводка одного шага ===')
    summary.append('время: %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    try:
        summary.append('TD: %s' % app.version)
    except Exception:
        pass
    summary.append('папка: %s' % paint_dir)
    summary.append('')
    summary.append('--- сборка: завершена=%s, проблем %d ---'
                   % (ns_build.get('COMPLETED') is True, len(build_fail)))
    summary.extend(build_fail or ['нет'])
    summary.append('')
    summary.append('--- самопроверка: дошла до конца=%s, проверок %d, провалов %d ---'
                   % (st_completed, checks, len(selftest_fail)))
    summary.extend(selftest_fail or ['нет'])
    if selftest_error:
        tail = [l for l in str(selftest_error).strip().splitlines() if l.strip()]
        summary.append(tail[-1] if tail else 'самопроверка не отработала')
    summary.append('')
    summary.append('ВЕРДИКТ: %s' % ('ВСЁ ЧИСТО' if (build_ok and st_ok)
                                   else 'ЕСТЬ ЧТО ИСПРАВЛЯТЬ'))
    summary.append('')
    summary.append('--- адреса ---')
    summary.extend(urls or ['(адрес не определён — смотри ipconfig, порт 9980)'])
    summary.append('')
    summary.append('--- файлы отчётов ---')
    for rel in ('td/last_build_report.txt', 'tmp/paint_web_report.json',
                'tmp/diag_paint.txt', 'tmp/selftest_brush.png', 'tmp/selftest_out.png'):
        p = os.path.join(paint_dir, rel.replace('/', os.sep))
        if os.path.isfile(p):
            summary.append('%s  (%d байт)' % (p, os.path.getsize(p)))
    summary.append('')
    # Входы — то, из-за чего в прошлый раз «из base ничего не выходило»: связи,
    # поставленные коннекторами, молча не вставали. Показываем их прямо в сводке,
    # чтобы не открывать отчёт сборки.
    in_block = []
    for i, l in enumerate(build_report):
        if l.startswith('--- фактические входы'):
            # до следующего заголовка или до конца — иначе в сводку утечёт
            # следующий раздел отчёта сборки
            for l2 in build_report[i + 1:i + 25]:
                if l2.startswith('--- '):
                    break
                in_block.append(l2)
            break
    summary.append('--- фактические входы нод ---')
    summary.extend(in_block or ['(блок не найден — смотри last_build_report.txt)'])

    summary.append('')
    summary.append('--- диагностика штампов (хвост; целиком — в tmp/diag_paint.txt) ---')
    summary.extend((diag_lines or ['(диагностика не запускалась)'])[-45:])
    summary.append('')
    # Хвост вывода самопроверки: в нём видны измерения (яркость снимка, значение
    # композита, что нашлось в патче) — без них провал «мазка нет» ни о чём не
    # говорит.
    summary.append('--- самопроверка, хвост вывода ---')
    summary.extend((st_lines or ['(вывод не пойман)'])[-28:])

    summary.append('')
    summary.append('--- рантайм из paint_web_report.json ---')
    try:
        import json
        with open(os.path.join(paint_dir, 'tmp', 'paint_web_report.json'),
                  encoding='utf-8') as f:
            j = json.load(f)
        rtj = j.get('runtime') or {}
        dg = rtj
        keys = ('up', 'ms', 'ms_start', 'ms_end', 'dabs', 'dab_pushes', 'dab_tex',
                'dab_png', 'dab_png_file', 'dab_png_ms', 'dab_mode', 'patches',
                'proxy', 'strokes', 'queued_points', 'clients', 'undo', 'redo',
                'error_count')
        summary.append(', '.join('%s=%s' % (k, dg[k]) for k in keys if k in dg)
                       or '(диагностики нет)')
        for e in (rtj.get('error_list') or [])[:5]:
            summary.append('ошибка рантайма: %s' % e)
        lg = rtj.get('log') or []
        if lg:
            summary.append('журнал (хвост): %s' % ' ;; '.join(lg[-4:]))
    except Exception as e:
        summary.append('не прочитал: %s' % e)

    summary.append('')
    summary.append('--- что дальше ---')
    summary.append('1) сохрани проект (Ctrl+S) — компонент с автозапуском останется внутри .toe')
    summary.append('2) дальше запуск без команд: двойной щелчок по «Запустить PaintWeb.bat»')
    summary.append('   (TD сам пересоберёт то, что изменилось, и поднимет сервер)')
    summary.append('3) адрес для планшета всегда лежит в DAT paint_web/server/address')
    summary.append('')
    summary.append('--- последние строки отчёта сборки ---')
    summary.extend(build_report[-14:])
    summary.append('')
    summary.append('всего заняло: %.1f с' % (time.time() - t0))

    out_path = None
    try:
        tdir = os.path.join(paint_dir, 'tmp')
        if not os.path.isdir(tdir):
            os.makedirs(tdir)
        out_path = os.path.join(tdir, 'run_summary.txt')
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(summary))
    except Exception:
        traceback.print_exc()

    print('\n' + '\n'.join(summary))
    print('\n' + '=' * 66)
    if build_ok and st_ok:
        print('ИТОГ: сборка и самопроверка чисты — можно рисовать с планшета.')
    else:
        print('ИТОГ: есть что исправлять (подробности выше).')
    print('Сводка (её и достаточно показать): %s' % out_path)
    print('=' * 66)


try:
    main()
except Exception:
    print('run_paint_web упал целиком:')
    print(traceback.format_exc())
