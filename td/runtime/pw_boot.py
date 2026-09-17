"""PaintWeb — загрузчик рантайма.

Этот кусок скрипт сборки подставляет В НАЧАЛО каждого DAT-а рантайма
(webserver-callbacks, executeDAT, scriptTOP), чтобы они все работали с одним и
тем же экземпляром модуля paint_runtime.

ВАЖНО: модуль кэшируется в sys.modules под общим именем, а не в переменной DAT-а.
Иначе каждый DAT получил бы СВОЮ копию модуля, а значит своё состояние: колбэки
складывали бы точки в одну очередь, а кадровый цикл читал бы другую — рисование
не работало бы вовсе. Общий модуль в sys.modules разделяет состояние между всеми
DAT-ами процесса.

Правки здесь применяются при следующем запуске paint/td/build_paint_web.py.
"""

import sys
import types
import traceback

SHARED_MODULE = 'paintweb_runtime_shared'


def _plog(tag):
    try:
        print('PaintWeb %s:\n%s' % (tag, traceback.format_exc()))
    except Exception:
        pass


def _pw_find_base(dat):
    o = dat
    while o is not None:
        try:
            if o.op('server/runtime') is not None:
                return o
        except Exception:
            pass
        o = o.parent()
    return None


def _pw_load(base):
    """Общий модуль рантайма для всего процесса (перезагрузка при правке DAT)."""
    dat_rt = base.op('server/runtime')
    src = dat_rt.text
    sig = (base.path, len(src), hash(src))
    m = sys.modules.get(SHARED_MODULE)
    if m is None or getattr(m, '_PW_SIG', None) != sig:
        m = types.ModuleType(SHARED_MODULE)
        m.__dict__['__file__'] = dat_rt.path
        m.__dict__['_PW_SIG'] = sig
        exec(compile(src, dat_rt.path, 'exec'), m.__dict__)
        sys.modules[SHARED_MODULE] = m
    return m


def _pw(dat):
    base = _pw_find_base(dat)
    if base is None:
        raise RuntimeError('PaintWeb: не найден server/runtime — '
                           'DAT рантайма лежит вне компонента paint_web?')
    return _pw_load(base).bind(base.path, globals())


def attach(base, opfunc):
    """Для внешних скриптов (диагностика, самопроверка).

    Возвращает ТОТ ЖЕ объект рантайма, с которым работают колбэки и кадровый цикл,
    иначе проверка гоняла бы отдельное пустое состояние и ничего не проверяла.
    """
    g = {'op': opfunc}
    # `project` нужен рантайму, чтобы найти папку данных рядом с проектом
    # (<проект>/paint). В TD он есть в глобальных, в тестах подставляется свой;
    # если его нет вовсе — рантайм просто честно скажет, что папку не нашёл.
    try:
        g['project'] = project
    except NameError:
        pass
    return _pw_load(base).bind(base.path, g)
