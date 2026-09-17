"""PaintWeb — диагностика. Запуск в Textport:

    exec(open(r"C:\\Users\\DaniilNotebook\\Documents\\PixelFlow\\paint\\td\\diagnose_paint_web.py",
              encoding="utf-8").read())

Печатает всё, что нужно, чтобы понять, почему не работает: состояние сервера,
ошибки и предупреждения нод, значения параметров, состояние рантайма, размеры
буферов и последние строки журнала. Вывод можно целиком скопировать в чат.
"""

import json
import os
import traceback

BASE = '/project1/paint_web'


def show(title):
    print('\n--- %s %s' % (title, '-' * max(0, 60 - len(title))))


def as_list(x):
    """TD отдаёт сообщения то строкой, то списком — приводим к списку строк."""
    if x is None:
        return []
    if isinstance(x, str):
        return [x] if x.strip() else []
    try:
        return [str(i) for i in x]
    except Exception:
        return [str(x)]


def rt_sig(paint_dir):
    """Отпечаток исходников — тем же кодом, что и рантайм/сборка."""
    dat = op(BASE + '/server/runtime')
    if dat is None or not paint_dir:
        return None
    ns = {'__name__': 'sig'}
    exec(compile(dat.text, 'paint_runtime.py', 'exec'), ns)
    return ns['sources_signature'](paint_dir)


def main():
    print('=== PaintWeb: диагностика ======================================')
    base = op(BASE)
    if base is None:
        print('НЕТ компонента %s — сначала запустите build_paint_web.py' % BASE)
        return
    show('параметры')
    for name in ('Canvasw', 'Canvash', 'Port', 'Patchhz', 'Proxyfps', 'Undodepth',
                 'Srcfile', 'Srcvisible', 'Srcopacity', 'Fitmode',
                 'Paintvisible', 'Paintopacity', 'Useext', 'Datadir'):
        try:
            print('  %-14s = %r' % (name, base.par[name].eval()))
        except Exception as e:
            print('  %-14s = НЕТ (%s)' % (name, e))

    show('сервер')
    ws = base.op('server/ws')
    if ws is None:
        print('  нет server/ws')
    else:
        try:
            print('  active   = %s' % ws.par.active.eval())
            print('  port     = %s' % ws.par.port.eval())
            print('  callbacks= %s' % ws.par.callbacks.eval())
            info = ws.info
            for ch in ('server_running', 'websocket_connections'):
                try:
                    print('  %-16s = %s' % (ch, info[ch].eval()))
                except Exception:
                    pass
            conns = getattr(ws, 'webSocketConnections', None)
            print('  соединения WS = %s' % (conns,))
        except Exception as e:
            print('  ошибка чтения: %s' % e)

    show('ноды: ошибки и предупреждения')
    bad = 0
    for o in base.findChildren():
        try:
            errs = as_list(o.errors())
            warns = as_list(o.warnings())
        except Exception:
            continue
        if errs or warns:
            bad += 1
            print('  %s' % o.path)
            for e in errs:
                print('      ошибка: %s' % e)
            for w in warns:
                print('      предупр: %s' % w)
    if not bad:
        print('  чисто')

    show('размеры')
    for rel in ('movie', 'fit', 'proxy', 'dabpng', 'brush',
                'buf', 'snap', 'patchin', 'sw',
                'src_level', 'paint_level', 'paint_mask',
                'over', 'mask', 'mode', 'out1', 'out1'):
        o = base.op(rel)
        if o is None:
            print('  %-16s НЕТ' % rel)
            continue
        try:
            print('  %-16s %sx%s  format=%s  lock=%s'
                  % (rel, int(o.width), int(o.height),
                     o.par.format.eval() if hasattr(o.par, 'format') else '-',
                     getattr(o, 'lock', '-')))
        except Exception as e:
            print('  %-16s ошибка чтения: %s' % (rel, e))

    show('шейдеры')
    for rel in ('shaders/paint', 'shaders/restore'):
        d = base.op(rel)
        print('  %-20s %s' % (rel, 'нет DAT' if d is None else '%d строк' % d.numRows))
    for rel in ('brush', 'restore'):
        o = base.op(rel)
        if o is None:
            continue
        try:
            print('  %-20s pixeldat=%s' % (rel, o.par.pixeldat.eval()))
            for i in range(4):
                nm = o.par['vec%dname' % i].eval()
                vals = [o.par['vec%dvalue%s' % (i, c)].eval() for c in 'xyzw']
                if nm:
                    print('        %s = %s' % (nm, ['%.3f' % v for v in vals]))
        except Exception as e:
            print('  %-20s ошибка: %s' % (rel, e))

    show('рантайм')
    try:
        # Берём ТОТ ЖЕ объект рантайма, что используют колбэки и кадровый цикл:
        # загрузчик читаем из pw_boot.py, иначе увидим пустое отдельное состояние.
        import types
        datadir = str(base.par.Datadir.eval())
        boot_path = os.path.join(datadir, 'td', 'runtime', 'pw_boot.py')
        with open(boot_path, 'r', encoding='utf-8') as f:
            bsrc = f.read()
        boot = types.ModuleType('paintweb_boot')
        boot.__dict__['op'] = op
        exec(compile(bsrc, boot_path, 'exec'), boot.__dict__)
        rt = boot.attach(base, op)
        print(json.dumps(rt.status(), indent=2, ensure_ascii=False, default=str))
        rp = rt.write_report('diagnose')
        print('\n  машиночитаемый отчёт: %s' % rp)
    except Exception:
        print(traceback.format_exc())

    show('автозапуск (без Textport)')
    try:
        rt = boot.attach(base, op)
        datadir = rt.paint_dir()
        print('  папка данных: %s' % datadir)
        cur = rt_sig(datadir)
        have = rt.stored_signature()
        print('  отпечаток файлов на диске: %s' % cur)
        print('  отпечаток собранного:      %s' % (have or 'нет'))
        print('  итог: %s' % ('сборка не нужна — при открытии проекта ничего не '
                               'пересобирается' if cur == have else
                               'ФАЙЛЫ ИЗМЕНИЛИСЬ — TD пересоберёт компонент сам '
                               '(или нажми «Пересобрать» на странице)'))
        ad = base.op('server/address')
        print('  адрес (DAT server/address):')
        for line in str(ad.text if ad is not None else '').splitlines()[:6]:
            print('    %s' % line)
    except Exception:
        print(traceback.format_exc())

    show('журнал (последнее из DAT server/log)')
    d = base.op('server/log')
    if d is not None:
        lines = d.text.split('\n')
        for line in lines[-25:]:
            print('  ' + line)

    print('\n--- как отдать этот отчёт ---')
    print('Скопируйте весь вывод Textport. Полный файл отчёта сборки:')
    print('  paint/td/last_build_report.txt')
    print('===============================================================')


main()
