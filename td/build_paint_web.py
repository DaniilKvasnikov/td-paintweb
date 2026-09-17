"""PaintWeb — сборка компонента `paint_web` внутри ОТКРЫТОГО проекта TouchDesigner.

Запуск (Textport, Dialogs -> Textport and DATs):

    exec(open(r"C:\\Users\\DaniilNotebook\\Documents\\PixelFlow\\paint\\td\\build_paint_web.py",
              encoding="utf-8").read())

Обычно запускать отдельно не нужно: есть `run_paint_web.py`, который делает
сборку, самопроверку и сводку за один шаг.

Скрипт идемпотентный: повторный запуск обновляет существующие ноды, DAT-ы и
значения параметров, не удаляя компонент. Внешние подключения сохраняются: вход
картинки задаётся параметром `Externalsrc` (путь к своему TOP), а выход берётся
с разъёма компонента `paint_web` (его выставляет outTOP `out`).

Что делает:
  1. создаёт/обновляет иерархию нод компонента `paint_web`;
  2. заливает в DAT-ы код рантайма и шейдеры из папки paint/td/runtime;
  3. заводит пользовательские параметры на базовом компоненте;
  4. делает папки web/uploads/media/tmp, тестовую картинку и пустые текстуры штампов;
  5. проверяет, компилируются ли шейдеры (и печатает лог TD через Info DAT);
  6. проверяет, что сервер отвечает, и печатает адрес для планшета с командой файрвола;
  7. сохраняет tox/paint_web.tox и пишет отчёт paint/td/last_build_report.txt.
"""

import os
import struct
import time
import traceback
import zlib

REPORT = []
FAIL = []
COMPLETED = False      # True только если сборка дошла до конца отчёта


def rep(msg):
    REPORT.append(str(msg))


def fail(msg):
    FAIL.append(str(msg))
    print('PaintWeb СБОЙ: ' + str(msg))


# --------------------------------------------------------------------- утилиты

def resolve_paint_dir():
    """Папка paint/ этого репозитория."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if os.path.basename(os.path.dirname(here)) == 'paint':
            return os.path.dirname(here)
    except Exception:
        pass
    try:
        cand = os.path.join(project.folder, 'paint')
        if os.path.isdir(cand):
            return cand
    except Exception:
        pass
    return None


def ensure(parent, optype, name):
    """Найти или создать ноду нужного типа (лишние не дублируются)."""
    o = parent.op(name)
    if o is not None:
        try:
            if str(o.type).lower() != optype.lower():
                o.destroy()
                o = None
        except Exception:
            o = None
    if o is None:
        o = parent.create(optype, name)
        rep('создан %s (%s)' % (o.path, optype))
    return o


def get_par(o, name):
    """Параметр или None.

    Важно: в TD `op.par['нет такого']` возвращает None, а НЕ бросает исключение.
    Раньше из-за этого «самопроверка параметров» рапортовала, что все имена на
    месте, хотя пользовательские параметры не создались вовсе.
    """
    if o is None:
        return None
    try:
        return o.par[name]
    except Exception:
        return None


def setp(o, **kw):
    for k, v in kw.items():
        if o is None:
            continue
        par = get_par(o, k)
        if par is None:
            fail('у %s нет параметра %s' % (o.path, k))
            continue
        try:
            par.val = v
        except Exception as e:
            fail('%s.%s = %r: %s' % (o.path, k, v, e))


def set_menu(o, name, *want):
    """Меню-параметр по токену или по куску подписи. Возвращает выбранный токен."""
    par = get_par(o, name)
    if par is None:
        fail('у %s нет параметра %s' % (o.path if o else '?', name))
        return None
    try:
        names = [str(x) for x in par.menuNames]
        labels = [str(x) for x in par.menuLabels]
    except Exception:
        names, labels = [], []
    for w in want:
        if w in names:
            par.val = w
            return w
    for w in want:
        for i, lab in enumerate(labels):
            if w.lower() in lab.lower() and i < len(names):
                par.val = names[i]
                return names[i]
    if names:
        rep('!!! %s.%s: не нашёл %s, оставил %r (варианты: %s)'
            % (o.path, name, want, par.eval(), ', '.join(names[:10])))
    return None


def runtime_signature(runtime_src, paint_dir):
    """Отпечаток исходников — тем же кодом, что и в рантайме.

    Функция `sources_signature` живёт в paint_runtime.py (её же зовёт рантайм при
    каждом старте). Здесь мы исполняем текст рантайма, который только что
    записали в DAT, и берём функцию оттуда: если считать отпечаток двумя разными
    способами, сборка и рантайм будут вечно считать друг друга устаревшими и
    пересобираться по кругу.
    """
    ns = {'__name__': 'paintweb_sig'}
    exec(compile(runtime_src, 'paint_runtime.py', 'exec'), ns)
    return ns['sources_signature'](paint_dir)


def set_inputs(o, sources):
    """Задать входы оператора одним вызовом и ПРОВЕРИТЬ, что они встали.

    Почему не `inputConnectors[i].connect()`: у части операторов в этой сборке TD
    входы динамические, и через connectors связи молча не вставали (у selectTOP
    вообще не было входа 0, у levelTOP и glslmultiTOP межкомпонентные связи не
    регистрировались — отсюда «Not enough sources specified» и отсутствие
    sTD2DInputs). `setInputs` задаёт весь набор входов сразу, а `OP.inputs`
    позволяет сразу же увидеть, что реально подключилось.
    """
    if o is None:
        return
    want = [x.path if x is not None else None for x in sources]
    try:
        o.setInputs(list(sources))
    except Exception as e:
        fail('setInputs для %s: %s' % (o.path, e))
        return
    try:
        got = [x.path if x is not None else None for x in o.inputs]
    except Exception:
        got = None
    if got is None:
        rep('%s: входы заданы, проверить не удалось' % o.path)
    elif got[:len(want)] != want:
        fail('входы %s не совпали: хотели %s, получили %s' % (o.path, want, got))


def connect(dst, index, src):
    """Подключить один вход (оставлено для мест, где так проще)."""
    if dst is None:
        return
    try:
        conn = dst.inputConnectors[index]
    except Exception:
        fail('у %s нет входа %d' % (dst.path, index))
        return
    try:
        conn.disconnect()
    except Exception:
        pass
    if src is None:
        return
    try:
        conn.connect(src)
    except Exception as e:
        fail('соединение %s[%d] <- %s: %s' % (dst.path, index, src.path, e))


def put_text(datop, text):
    if datop is None:
        return
    try:
        datop.text = text
    except Exception as e:
        fail('не смог записать в %s: %s' % (datop.path, e))


_place_failed = [False]


def place(o, x, y):
    """Положить ноду в сети: nodeX/nodeY — свойства, а не параметры.

    Молча глотать ошибку нельзя: если расставить ноды не получится, сеть снова
    станет нечитаемой, и по отчёту это должно быть видно.
    """
    if o is None:
        return False
    try:
        o.nodeX = x
        o.nodeY = y
        return True
    except Exception as e:
        if not _place_failed[0]:
            _place_failed[0] = True
            fail('не смог расставить ноды (nodeX/nodeY недоступны): %s' % e)
        return False


def lan_addresses():
    """IPv4-адреса этой машины — чтобы сразу дать точный адрес для планшета."""
    out = []
    try:
        import socket
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out and not ip.startswith('127.'):
                out.append(ip)
    except Exception:
        pass
    if not out:
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(('8.8.8.8', 80))
                ip = s.getsockname()[0]
                if ip and not ip.startswith('127.'):
                    out.append(ip)
            finally:
                s.close()
        except Exception:
            pass
    return out


def port_listening(port, host='127.0.0.1', timeout=0.4):
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((host, int(port)))
            return True
        finally:
            s.close()
    except Exception:
        return False


def as_list(x):
    """TD отдаёт сообщения то строкой, то списком — приводим к списку строк.

    Однажды строка была итерирована по символам, и в отчёт вместо «The GLSL Shader
    has compile errors...» попал массив отдельных букв.
    """
    if x is None:
        return []
    if isinstance(x, str):
        return [x] if x.strip() else []
    try:
        return [str(i) for i in x]
    except Exception:
        return [str(x)]


def node_messages(o):
    """Ошибки и предупреждения ноды одним списком строк."""
    out = []
    for getter in ('errors', 'warnings'):
        try:
            out.extend(as_list(getattr(o, getter)()))
        except Exception:
            pass
    return out


def read_file(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        fail('не читается %s: %s' % (path, e))
        return ''


def _png_chunk(tag, data):
    return (struct.pack('>I', len(data)) + tag + data
            + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))


def make_zero_rgba_png(path, w, h):
    """Пустая RGBA-картинка (все нули): стартовая текстура штампов."""
    stride = w * 4
    raw = bytearray()
    for _y in range(h):
        raw.append(0)                     # фильтр None
        raw += bytes(stride)
    blob = b'\x89PNG\r\n\x1a\n'
    blob += _png_chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
    blob += _png_chunk(b'IDAT', zlib.compress(bytes(raw), 1))
    blob += _png_chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(blob)


def make_png(path, w, h):
    """Простая тестовая картинка: градиент, сетка, пятна. Без сторонних библиотек."""
    px = bytearray()
    for y in range(h):
        px.append(0)
        for x in range(w):
            u = x / float(w - 1)
            v = y / float(h - 1)
            r = int(24 + 60 * u + 30 * (1.0 - v))
            g = int(18 + 40 * (1.0 - u) + 50 * v)
            b = int(40 + 120 * (1.0 - v) * (0.4 + 0.6 * u))
            if x % 120 == 0 or y % 120 == 0:
                r = min(255, r + 26)
                g = min(255, g + 26)
                b = min(255, b + 26)
            d = ((x - w * 0.28) ** 2 + (y - h * 0.34) ** 2) ** 0.5
            if d < 190:
                k = 1.0 - d / 190.0
                r = min(255, int(r + 150 * k))
                g = min(255, int(g + 90 * k))
                b = min(255, int(b + 40 * k))
            d2 = ((x - w * 0.74) ** 2 + (y - h * 0.68) ** 2) ** 0.5
            if d2 < 140:
                k = 1.0 - d2 / 140.0
                r = min(255, int(r + 40 * k))
                g = min(255, int(g + 120 * k))
                b = min(255, int(b + 160 * k))
            px += bytes((max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))))
    blob = b'\x89PNG\r\n\x1a\n'
    blob += _png_chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
    blob += _png_chunk(b'IDAT', zlib.compress(bytes(px), 6))
    blob += _png_chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(blob)


# ------------------------------------------------------------------ параметры

CANVAS_W = 1920
CANVAS_H = 1080
PORT = 9980
MAXDABS = 128
DAB_TEX_W = MAXDABS * 4       # ширина PNG штампов: по 4 тексела RGBA8 на штамп
FMT_HDR = ('rgba16float', 'rgba16f', '16bitfloat', '16-bit float')


def check_pars(base):
    """Сверить, что все параметры, на которые рассчитывает рантайм, реально есть.

    Рантайм обращается к ним по именам каждый кадр; если имя в этой сборке TD
    называется иначе, лучше узнать об этом из отчёта, а не из тишины в вебе.
    """
    need = {
        '.': ('Canvasw', 'Canvash', 'Patchhz', 'Proxyfps', 'Undodepth', 'Srcfile',
              'Srcvisible', 'Srcopacity', 'Paintvisible', 'Paintopacity', 'Fitmode',
              'Srcint', 'Paintint', 'Maskint', 'Colorint', 'Colortemp',
              'Useext', 'Externalsrc', 'Fullframe', 'Fullfps', 'Fulljpeg',
              'Flipy', 'Datadir'),
        'server/ws': ('port', 'active', 'callbacks'),
        'tick': ('active', 'framestart', 'frameend', 'start'),
        'server/version': (),
        'server/address': (),
        'movie': ('file', 'reloadpulse', 'play', 'speed'),
        'ext': ('top',),
        'src': ('index',),
        'fit': ('fit', 'justifyh', 'justifyv', 'outputresolution', 'resolutionw'),
        'proxy': ('outputresolution', 'resolutionw', 'resolutionh'),
        'dabpng': ('file', 'reloadpulse', 'play', 'inputcolorspace',
                   'premultrgbbyalpha'),
        'fb': ('top',),
        'brush': ('pixeldat', 'vec0name', 'vec0valuex', 'vec0valuew',
                  'outputresolution', 'resolutionw', 'resolutionh',
                  'premultrgbbyalpha'),
        'restore': ('pixeldat', 'vec0name', 'vec0valuew', 'outputresolution'),
        'sw': ('index',),
        'patchin': ('file', 'reloadpulse', 'play'),
        'crop': ('cropleft', 'cropright', 'croptop', 'cropbottom', 'cropleftunit'),
        'cropundo': ('cropleft', 'cropright', 'croptop', 'cropbottom'),
        'cropsnap': ('cropleft', 'cropright', 'croptop', 'cropbottom'),
        'fbm': ('top',),
        'brushm': ('pixeldat', 'vec0name', 'vec0valuex', 'vec0valuew',
                   'outputresolution', 'resolutionw', 'resolutionh',
                   'premultrgbbyalpha'),
        'restorem': ('pixeldat', 'vec0name', 'vec0valuew', 'outputresolution'),
        'swm': ('index',),
        'patchinm': ('file', 'reloadpulse', 'play'),
        'cropm': ('cropleft', 'cropright', 'croptop', 'cropbottom', 'cropleftunit'),
        'cropsnapm': ('cropleft', 'cropright', 'croptop', 'cropbottom'),
        'src_level': ('opacity',),
        'paint_level': ('opacity',),
        'mask_level': ('opacity',),
        'color_src': ('colorr', 'colorg', 'colorb', 'alpha'),
        'colapply': ('pixeldat', 'outputresolution', 'resolutionw', 'resolutionh'),
        'maskapply': ('pixeldat', 'outputresolution'),
        'base_layer': (),
        'over': (),
        'out': (),
        'outpaint': (),
        'outmask': (),
    }
    missing = 0
    for rel, pars in need.items():
        o = base if rel == '.' else base.op(rel)
        if o is None:
            fail('нет оператора %s' % rel)
            missing += 1
            continue
        for name in pars:
            if get_par(o, name) is None:
                fail('у %s нет параметра %s (рантайм на него рассчитывает)'
                     % (o.path, name))
                missing += 1
    if not missing:
        rep('самопроверка параметров: все %d имён на месте'
            % sum(len(v) for v in need.values()))


def build():
    paint_dir = resolve_paint_dir()
    if paint_dir is None:
        fail('не нашёл папку paint/ — сохрани проект в папку репозитория PixelFlow '
             'или поправь resolve_paint_dir()')
        return
    rt_dir = os.path.join(paint_dir, 'td', 'runtime')
    rep('папка проекта: %s' % paint_dir)

    root = op('/project1')
    if root is None:
        kids = [c for c in op('/').children if c.isCOMP]
        root = kids[0] if kids else op('/')
    rep('корневой компонент: %s' % root.path)

    boot = read_file(os.path.join(rt_dir, 'pw_boot.py'))
    src_rt = read_file(os.path.join(rt_dir, 'paint_runtime.py'))
    src_cb = read_file(os.path.join(rt_dir, 'pw_callbacks.py'))
    src_ex = read_file(os.path.join(rt_dir, 'pw_execute.py'))
    gl_brush = read_file(os.path.join(rt_dir, 'brush.glsl'))
    gl_rest = read_file(os.path.join(rt_dir, 'restore.glsl'))
    gl_unpre = read_file(os.path.join(rt_dir, 'unpremult.glsl'))
    gl_maskapply = read_file(os.path.join(rt_dir, 'maskapply.glsl'))
    gl_colapply = read_file(os.path.join(rt_dir, 'colapply.glsl'))
    for name, text in (('paint_runtime.py', src_rt), ('pw_callbacks.py', src_cb),
                       ('pw_execute.py', src_ex), ('brush.glsl', gl_brush),
                       ('restore.glsl', gl_rest), ('unpremult.glsl', gl_unpre),
                       ('maskapply.glsl', gl_maskapply),
                       ('colapply.glsl', gl_colapply),
                       ('pw_boot.py', boot)):
        if not text:
            fail('пустой или нечитаемый %s' % name)
    # Синтаксис проверяем ДО того, как что-то записано в ноды. Иначе достаточно
    # одного сохранения файла «на середине правки», чтобы в компонент уехал
    # неработающий код: автопересборка не спрашивает разрешения, а потом в TD
    # падает уже всё, включая веб-сервер (ровно так и случилось).
    #
    # Одного синтаксиса мало: файл может компилироваться и падать на первом же
    # кадре. Так вышло с FIT_MODES — ссылка на константу появилась раньше самой
    # константы, сборка собрала этот момент, и кадр падал с NameError. Проверяем
    # и имена констант — тем же кодом, что и рантайм (он лежит в paint_runtime.py,
    # поэтому берём функцию из него, а не пишем вторую копию).
    names_check = None
    try:
        rt_ns = {'__name__': 'paint_runtime_check'}
        exec(compile(src_rt, 'paint_runtime.py', 'exec'), rt_ns)
        names_check = rt_ns.get('check_source_names')
    except Exception as e:
        rep('проверка имён констант недоступна: %s' % e)
    for name, text in (('paint_runtime.py', src_rt), ('pw_callbacks.py', src_cb),
                       ('pw_execute.py', src_ex), ('pw_boot.py', boot)):
        if not text:
            continue
        try:
            compile(text, name, 'exec')
        except SyntaxError as e:
            fail('%s не компилируется (строка %s): %s — ноды НЕ тронуты, '
                 'исправь файл и запусти снова' % (name, e.lineno, e.msg))
            continue
        if names_check is not None:
            bad = names_check(text, name)
            if bad:
                fail('%s — ноды НЕ тронуты, исправь файл и запусти снова' % bad)
    if FAIL:
        return

    # ---------------------------------------------------------------- ноды
    #
    # ВАЖНО ПРО АРХИТЕКТУРУ (это выяснилось на живом TD, см. last_build_report.txt):
    # TouchDesigner НЕ соединяет операторы, которые лежат в РАЗНЫХ компонентах.
    # Данные между сетями ходят только через In/Out-операторы компонента. Сборка
    # раньше раскладывала TOP-ы по под-компонентам (in/, paint/, comp/), и все
    # межкомпонентные связи молча не вставали: у comp/src_level и comp/paint_level
    # входов не появлялось, композит падал с «Not enough sources specified», из
    # base ничего не выходило. И connect(), и setInputs() в этом случае не дают
    # ни ошибки, ни связи — просто ничего.
    #
    # Поэтому весь граф TOP-ов живёт в ОДНОЙ сети — прямо в paint_web. В
    # под-компонентах остались только DAT-ы (server/, shaders/): их никто не
    # соединяет проводами, ссылки на них идут по пути, а это работает везде.
    # Наружу данные выходят через outTOP `out` — он и есть выход компонента.
    base = ensure(root, 'baseCOMP', 'paint_web')
    server = ensure(base, 'baseCOMP', 'server')
    shaders = ensure(base, 'baseCOMP', 'shaders')

    # ------------------------------------------------------- уборка старых имён
    # Сборка идемпотентна и переиспользует ноды, но ноды от ПРЕДЫДУЩИХ вариантов
    # остаются жить: у них бывают ошибки компиляции, и они портят отчёт и путают
    # самопроверку. Удаляем ровно те имена, которые мы сами когда-то создавали.
    # in/, paint/, comp/ — старые под-компоненты с TOP-ами: теперь TOP-ы плоские,
    # и эти контейнеры только запутывали бы (и держали мёртвые копии нод).
    # paint_mask / mask / mode — композит маски из levelTOP+multiplyTOP+switch:
    # убран, потому что levelTOP не переносит альфу в RGB и маска в TD не работала.
    for rel, otype in (('comp/comp', None), ('paint/dabs', None),
                       ('shaders/compose', None), ('paint/fb_dummy', None),
                       ('paint_mask', 'levelTOP'), ('mask', 'multiplyTOP'),
                       ('mode', 'switchTOP'),
                       ('in', 'baseCOMP'), ('paint', 'baseCOMP'),
                       ('comp', 'baseCOMP')):
        dead = base.op(rel)
        if dead is None:
            continue
        if otype and dead.type != otype:
            rep('оставляю %s (%s) — не наше' % (rel, dead.type))
            continue
        try:
            dead.destroy()
            rep('удалён устаревший %s' % rel)
        except Exception as e:
            rep('не удалось удалить %s: %s' % (rel, e))

    ws = ensure(server, 'webserverDAT', 'ws')
    cb = ensure(server, 'textDAT', 'callbacks')
    runtime = ensure(server, 'textDAT', 'runtime')
    logdat = ensure(server, 'textDAT', 'log')
    # Отпечаток исходников, из которых собран компонент: по нему рантайм решает,
    # нужна ли пересборка при следующем открытии проекта (или сразу, если файлы
    # изменились на диске). Это и есть «запуск без команд».
    version = ensure(server, 'textDAT', 'version')
    # Адрес для планшета — в DAT, который видно прямо в сети нод.
    address = ensure(server, 'textDAT', 'address')

    # Источник. `ext` — selectTOP, а не inTOP: он берёт ЛЮБОЙ TOP проекта по
    # параметру Top, а параметры (в отличие от проводов) между компонентами
    # работают. Путь к своему TOP задаётся параметром Externalsrc на компоненте.
    ext = ensure(base, 'selectTOP', 'ext')
    movie = ensure(base, 'moviefileinTOP', 'movie')
    srcsel = ensure(base, 'switchTOP', 'src')
    fit = ensure(base, 'fitTOP', 'fit')
    proxy = ensure(base, 'nullTOP', 'proxy')

    # Текстура штампов: не scriptTOP с numpy (его колбэки в сборке пользователя
    # не вызывались), а крошечный PNG, который пишет Python и читает moviefileinTOP.
    dabpng = ensure(base, 'moviefileinTOP', 'dabpng')
    fb = ensure(base, 'feedbackTOP', 'fb')
    brush = ensure(base, 'glslmultiTOP', 'brush')
    restore = ensure(base, 'glslmultiTOP', 'restore')
    patchin = ensure(base, 'moviefileinTOP', 'patchin')
    sw = ensure(base, 'switchTOP', 'sw')
    buf = ensure(base, 'nullTOP', 'buf')
    snap = ensure(base, 'nullTOP', 'snap')
    crop = ensure(base, 'cropTOP', 'crop')
    cropundo = ensure(base, 'cropTOP', 'cropundo')
    cropsnap = ensure(base, 'cropTOP', 'cropsnap')
    # Снятие премультипликации для картинок, которые уезжают в браузер: слой в TD
    # премультиплицирован, а PNG браузер читает как straight alpha.
    unpremult = ensure(base, 'glslmultiTOP', 'unpremult')

    # Второй слой — МАСКА источника. Она живёт своим буфером (та же петля
    # обратной связи, свой шейдер кисти и своё восстановление для undo), потому
    # что маска и краска — разные вещи: в браузере слой «Источник» рисует маску,
    # слой «Краска» — краску, и одно не должно превращаться в другое.
    fbm = ensure(base, 'feedbackTOP', 'fbm')
    brushm = ensure(base, 'glslmultiTOP', 'brushm')
    restorem = ensure(base, 'glslmultiTOP', 'restorem')
    patchinm = ensure(base, 'moviefileinTOP', 'patchinm')
    swm = ensure(base, 'switchTOP', 'swm')
    bufm = ensure(base, 'nullTOP', 'bufm')
    snapm = ensure(base, 'nullTOP', 'snapm')
    cropm = ensure(base, 'cropTOP', 'cropm')
    cropsnapm = ensure(base, 'cropTOP', 'cropsnapm')

    # Композит: источник, умноженный на маску, и краска поверх него. Раньше здесь
    # стояли levelTOP + multiplyTOP, но levelTOP не переносит альфу в RGB: маска
    # умножалась на константу, и в TouchDesigner картинка оставалась нетронутой
    # (в браузере маска при этом работала — расхождение TD и страницы).
    src_level = ensure(base, 'levelTOP', 'src_level')
    paint_level = ensure(base, 'levelTOP', 'paint_level')
    # Интенсивность маски — своим уровнем: она уходит и в композит, и на
    # отдельный выход компонента.
    mask_level = ensure(base, 'levelTOP', 'mask_level')
    maskapply = ensure(base, 'glslmultiTOP', 'maskapply')
    # Слой монотонного цвета, который проявляется ТОЙ ЖЕ маской: constantTOP даёт
    # ровный цвет, а второй экземпляр шейдера маски умножает его на альфу маски.
    # Цвет считает рантайм (в том числе по цветовой температуре).
    color_src = ensure(base, 'constantTOP', 'color_src')
    colapply = ensure(base, 'glslmultiTOP', 'colapply')
    base_layer = ensure(base, 'overTOP', 'base_layer')   # цвет поверх источника с маской
    comp_over = ensure(base, 'overTOP', 'over')
    # Вывод наружу. out1 — обычный nullTOP: на него удобно смотреть и с него
    # брать картинку внутри сети. out — outTOP: он выставляет композит на
    # ВЫХОДНОЙ разъём компонента, и только так картинку можно утащить в другую
    # сеть (провод внутри одной сети в другую не идёт — это правило TD).
    out1 = ensure(base, 'nullTOP', 'out1')
    out = ensure(base, 'outTOP', 'out')
    # Отдельные выходы компонента: краска и маска — чтобы в другом проекте брать
    # их напрямую, а не вырезать из композита. Каждый Out TOP — свой разъём.
    outpaint = ensure(base, 'outTOP', 'outpaint')
    outmask = ensure(base, 'outTOP', 'outmask')

    sh_paint = ensure(shaders, 'textDAT', 'paint')
    sh_rest = ensure(shaders, 'textDAT', 'restore')
    sh_unpre = ensure(shaders, 'textDAT', 'unpremult')
    sh_maskapply = ensure(shaders, 'textDAT', 'maskapply')
    sh_colapply = ensure(shaders, 'textDAT', 'colapply')

    # Страница, стили и клиентский скрипт кладём ВНУТРЬ компонента текстовыми
    # DAT-ами: иначе .tox несамодостаточен — в другом проекте папки paint/web нет,
    # и сервер отдал бы 404. Рантайм при старте раскладывает их из DAT-ов в свою
    # папку данных (см. materialize_web) и умеет отдавать прямо из DAT-ов.
    web_comp = ensure(base, 'baseCOMP', 'web')
    web_index = ensure(web_comp, 'textDAT', 'index')
    web_app = ensure(web_comp, 'textDAT', 'app')
    web_style = ensure(web_comp, 'textDAT', 'style')

    tick = ensure(base, 'executeDAT', 'tick')

    # ---------------------------------------------------------------- связи
    # Все связи — внутри ОДНОЙ сети (см. комментарий про архитектуру выше).
    # 0 = файл (movie), 1 = внешний TOP, заданный параметром Externalsrc.
    set_inputs(srcsel, [movie, ext])
    set_inputs(fit, [srcsel])
    set_inputs(proxy, [fit])
    set_inputs(brush, [fb, dabpng])
    set_inputs(restore, [fb, patchin])
    set_inputs(fb, [buf])
    set_inputs(sw, [brush, restore])
    set_inputs(buf, [sw])
    set_inputs(snap, [buf])
    set_inputs(crop, [buf])
    set_inputs(cropundo, [buf])
    set_inputs(cropsnap, [snap])
    set_inputs(unpremult, [crop])
    # Цепочка маски — зеркало слоя краски: своя петля, свой шейдер кисти,
    # своё восстановление области для undo.
    set_inputs(brushm, [fbm, dabpng])
    set_inputs(restorem, [fbm, patchinm])
    set_inputs(fbm, [bufm])
    set_inputs(swm, [brushm, restorem])
    set_inputs(bufm, [swm])
    set_inputs(snapm, [bufm])
    set_inputs(cropm, [bufm])
    set_inputs(cropsnapm, [snapm])
    set_inputs(src_level, [fit])
    set_inputs(paint_level, [buf])
    set_inputs(mask_level, [bufm])
    set_inputs(maskapply, [src_level, mask_level])
    set_inputs(color_src, [])
    set_inputs(colapply, [color_src, mask_level])
    set_inputs(base_layer, [colapply, maskapply])     # цвет поверх источника с маской
    set_inputs(comp_over, [paint_level, base_layer])  # первый вход — верхний слой
    set_inputs(out1, [comp_over])
    set_inputs(out, [comp_over])
    # Отдельные выходы: краска и маска (со своей интенсивностью).
    set_inputs(outpaint, [paint_level])
    set_inputs(outmask, [mask_level])

    # ---------------------------------------------------------------- данные
    put_text(runtime, src_rt)
    put_text(cb, boot + '\n\n' + src_cb)
    put_text(tick, boot + '\n\n' + src_ex)
    # В части сборок TD скрипт Execute DAT берётся из дочернего DAT-а (параметр DAT).
    # Пишем код в сам executeDAT и, если такой параметр есть, дублируем в дочерний.
    try:
        tick.par['dat']
        tick_script = ensure(tick, 'textDAT', 'script')
        put_text(tick_script, boot + '\n\n' + src_ex)
        setp(tick, dat=tick_script)
        rep('скрипт executeDAT продублирован в %s' % tick_script.path)
    except Exception:
        pass
    put_text(sh_paint, gl_brush)
    put_text(sh_rest, gl_rest)
    put_text(sh_unpre, gl_unpre)
    put_text(sh_maskapply, gl_maskapply)
    put_text(sh_colapply, gl_colapply)
    for d in (cb, runtime, tick, sh_paint, sh_rest, sh_unpre, sh_maskapply,
              sh_colapply):
        try:
            d.par.language = 'python'
        except Exception:
            pass

    # ---------------------------------------------------------------- шейдеры
    for t, dat in ((brush, sh_paint), (restore, sh_rest), (unpremult, sh_unpre),
                   (brushm, sh_paint), (restorem, sh_rest),
                   (maskapply, sh_maskapply), (colapply, sh_colapply)):
        setp(t, pixeldat=dat)
    for t, vecs in ((brush, (('vec0name', 'uRes'), ('vec1name', 'uCount'),
                             ('vec2name', 'uColor'), ('vec3name', 'uRect'))),
                    (brushm, (('vec0name', 'uRes'), ('vec1name', 'uCount'),
                              ('vec2name', 'uColor'), ('vec3name', 'uRect'))),
                    (restore, (('vec0name', 'uRes'), ('vec1name', 'uRect'))),
                    (restorem, (('vec0name', 'uRes'), ('vec1name', 'uRect')))):
        for parname, uni in vecs:
            setp(t, **{parname: uni})
    for t in (brush, brushm, restore, restorem, sw, swm, fb, fbm, buf, bufm,
              snap, snapm):
        picked = set_menu(t, 'format', *FMT_HDR)
        if picked:
            rep('%s.format = %s' % (t.name, picked))

    # ---------------------------------------------------------------- параметры
    setp(ws, port=PORT, active=1)
    setp(ws, callbacks=cb)
    # start=1 — чтобы onStart срабатывал при открытии проекта: именно оттуда
    # рантайм запускает автопроверку и, если нужно, пересборку. playstatechange —
    # подстраховка на случай, если проект открылся на паузе (тогда onStart не
    # зовут, а кадров нет; как только нажмут Play — проверимся и пересоберёмся).
    setp(tick, active=1, framestart=1, frameend=1, start=1, playstatechange=1)
    # Источник обратной связи задаём И входом (set_inputs(fb, [buf])), И
    # параметром Target TOP: это одно и то же — предыдущий кадр слоя, но разные
    # сборки TD используют то один, то другой механизм (документация Feedback TOP
    # описывает именно Target TOP, а вход работает как «проходное» изображение).
    # TD при этом предупреждает «Cook dependency loop detected» — это
    # предупреждение, а не ошибка; зато петля работает при любой трактовке.
    # Рантайм дополнительно проверяет и восстанавливает эту связь каждый кадр
    # (Runtime._ensure_wiring), чтобы старая сборка не оставила мёртвый слой.
    setp(fb, top=buf.path)
    # Маска — та же петля, только со своим буфером.
    setp(fbm, top=bufm.path)
    setp(patchin, play=0)
    setp(patchinm, play=0)
    setp(movie, play=0, speed=1)
    # ext — selectTOP: берёт TOP по пути. Пусто = внешнего источника нет, тогда
    # switch src берёт файл. Рантайм каждый кадр выставляет этот путь из параметра
    # Externalsrc (см. _apply_source) — провода между компонентами TD не проводит.
    setp(srcsel, index=0)
    setp(src_level, opacity=1.0)
    setp(paint_level, opacity=1.0)
    setp(mask_level, opacity=1.0)
    # Слой цвета: ровный цвет во всё полотно, маску к нему применяет colapply.
    setp(color_src, colorr=1.0, colorg=1.0, colorb=1.0, alpha=1.0)
    for t in (brush, brushm, restore, restorem, fit, proxy, maskapply, colapply):
        set_menu(t, 'outputresolution', 'custom', 'customresolution')
    # Размеры буферов ставим сразу: иначе до первого кадра в сети висели бы 1280x720.
    for t in (brush, brushm, restore, restorem, fit, maskapply, colapply):
        setp(t, resolutionw=CANVAS_W, resolutionh=CANVAS_H)
    setp(proxy, resolutionw=max(64, CANVAS_W // 2), resolutionh=max(36, CANVAS_H // 2))
    for t in (brush, brushm, restore, restorem):
        # Слой хранится премультиплицированным (rgb = color * a — так выходит из
        # смешивания штампа с прозрачным фоном), и шейдеры ждут именно такое
        # «предыдущее» состояние. Поэтому просим TD не пересчитывать альфу:
        # любой автоматический премульт/анпремульт на входе сломал бы формулу.
        picked = set_menu(t, 'premultrgbbyalpha', 'none', 'off', 'no', 'nopremultiply')
        if picked:
            rep('%s.premultrgbbyalpha = %s' % (t.name, picked))

    # Формат кодируемых картинок держим как у слоя (16-битный float): браузер
    # такие PNG читает нормально (проверено на живом TD: полная синхронизация
    # тем же форматом отображается верно), а перевод в 8 бит в этой сборке
    # доступен только как ЛИНЕЙНЫЙ (sRGB-варианта в меню нет) — это сдвинуло бы
    # цвета. Премультипликацию по-прежнему выключаем.
    for rel, parname, tokens in (('unpremult', 'format', FMT_HDR),
                                 ('unpremult', 'premultrgbbyalpha',
                                  ('none', 'off', 'no', 'nopremultiply'))):
        o = base.op(rel)
        if o is not None:
            picked = set_menu(o, parname, *tokens)
            if picked:
                rep('%s.%s = %s' % (rel, parname, picked))

    # ---------------------------------------------------------------- вёрстка
    # Раскладываем ноды по колонкам «слева направо = от источника к выходу».
    # Раньше позиции не задавались вообще, и TD валил все новые ноды в одну
    # точку — сеть была нечитаемой. Здесь позиция каждой ноды задана явно, а
    # начало координат уводим от 0,0, чтобы колонки не наезжали на другие ноды
    # проекта.
    STEP_X, STEP_Y = 240, 150
    grid = (
        # (колонка, строка, нода) — строки сверху вниз внутри колонки
        (0, 0, movie), (0, 1, ext), (0, 2, srcsel),
        (1, 2, fit), (1, 3, proxy),
        (2, 0, dabpng), (2, 1, patchin), (2, 2, fb), (2, 3, patchinm), (2, 4, fbm),
        (3, 2, brush), (3, 3, restore), (3, 4, brushm), (3, 5, restorem),
        (4, 2, sw), (4, 3, snap), (4, 4, swm), (4, 5, snapm),
        (5, 2, buf), (5, 4, bufm),
        (6, 0, crop), (6, 1, cropundo), (6, 2, cropsnap), (6, 3, unpremult),
        (6, 4, cropm), (6, 5, cropsnapm),
        (7, 0, src_level), (7, 1, paint_level), (7, 3, maskapply),
        (7, 4, mask_level), (7, 5, color_src), (7, 6, colapply),
        (8, 0, comp_over), (8, 1, base_layer),
        (9, 2, out1), (9, 3, outpaint), (9, 4, out), (9, 5, outmask),
    )
    for col, row, o in grid:
        if not place(o, 200 + col * STEP_X, -260 + row * STEP_Y):
            break
    placed = sum(1 for _c, _r, o in grid if o is not None)
    rep('вёрстка: %d нод расставлены по колонкам (шаг %dx%d)' % (placed, STEP_X, STEP_Y))
    # DAT-ы — своей группой слева сверху, чтобы не мешались в потоке картинки.
    place(ws, -360, -260)
    place(cb, -360, -400)
    place(runtime, -360, -540)
    place(logdat, -360, -680)
    place(version, -360, -820)
    place(address, -360, -960)
    place(tick, 200, -560)
    place(sh_paint, 200, -700)
    place(sh_rest, 440, -700)
    try:
        out1.viewer = True                   # сюда смотреть: готовый композит
    except Exception:
        pass
    try:
        address.viewer = True                # адрес для планшета виден в сети нод
    except Exception:
        pass
    # Подписи групп: textDAT с включённым вьюером виден прямо в сети и читается
    # без открытия параметров. annotateCOMP не используем: в этой сборке TD его
    # параметр с текстом не подтверждён (в стабах его нет), а молча не сработать
    # тут хуже, чем не подписать.
    for i, (title, col, text) in enumerate((
            ('0. ИСТОЧНИК', 0,
             'movie — файл/видео (Srcfile, загрузка со страницы)\n'
             'ext — ВНЕШНИЙ TOP: укажи путь в параметре Externalsrc\n'
             'src — переключатель: файл (0) или внешний (1, Useext)\n'
             'fit — вписать в полотно, proxy — картинка для планшета'),
            ('1. СЛОЙ КРАСКИ', 2,
             'fb + brush + sw + buf — петля обратной связи: пиксели живут в TD\n'
             'dabpng — текстура штампов (Python пишет PNG, TD читает)\n'
             'patchin + restore — восстановление области для undo\n'
             'crop* — вырезать область в PNG для патчей и снимков undo'),
            ('2. СЛОЙ МАСКИ', 2,
             'fbm + brushm + swm + bufm — отдельный буфер маски источника\n'
             'рисуется, когда в браузере выбран слой «Источник»\n'
             'patchinm + restorem — восстановление области для undo маски\n'
             'маска хранится белым: RGB = альфа'),
            ('3. СБОРКА КАРТИНКИ', 7,
             'src_level / paint_level / mask_level — интенсивность слоёв\n'
             'maskapply — источник, умноженный на альфу маски\n'
             'color_src + colapply — слой монотонного цвета по маске\n'
             '       (цвет и температура — параметры Colorint и Colortemp)\n'
             'base_layer — цвет поверх источника\n'
             'over — краска поверх всего\n'
             'out1 — смотреть здесь; out — композит наружу,\n'
             'outpaint — отдельный выход краски, outmask — отдельный выход маски'))):
        try:
            note = ensure(base, 'textDAT', 'note_%d' % i)
            put_text(note, '%s\n%s' % (title, text))
            try:
                note.viewer = True
            except Exception:
                pass
            place(note, 200 + col * STEP_X, -260 + 4 * STEP_Y + 40)
        except Exception as e:
            rep('подпись note_%d не создана: %s' % (i, e))

    # ---------------------------------------------------------------- свои параметры
    try:
        for p in list(base.customPages):
            if p.name == 'Paint':
                base.removeCustomPage(p)
    except Exception as e:
        rep('не смог очистить старые страницы параметров: %s' % e)
    try:
        page = base.appendCustomPage('Paint')

        def addpar(kind, name, label, default=None):
            """Создать свой параметр и вернуть его (или None).

            TD требует подпись ИМЕНОВАННЫМ аргументом: appendInt('X', 'Подпись')
            падает с «Single name argument expected» (проверено на живом TD).
            Заодно проверяем, что параметр реально появился: раньше здесь молча
            терялись ВСЕ параметры, а отчёт рапортовал, что всё на месте.
            """
            fn = getattr(page, 'append' + kind, None)
            if fn is None:
                fail('в этой сборке TD нет append%s' % kind)
                return None
            par = None
            last = None
            for call in (lambda: fn(name, label=label), lambda: fn(name)):
                try:
                    par = call()
                    break
                except Exception as e:
                    last = e
            if par is None:
                fail('append%s(%s): %s' % (kind, name, last))
                return None
            if default is not None:
                try:
                    par.val = default
                except Exception as e:
                    fail('значение по умолчанию для %s: %s' % (name, e))
            if get_par(base, name) is None:
                fail('параметр %s создан, но не виден на компоненте' % name)
                return None
            return par

        addpar('Int', 'Canvasw', 'Ширина полотна', CANVAS_W)
        addpar('Int', 'Canvash', 'Высота полотна', CANVAS_H)
        addpar('Int', 'Port', 'Порт веб-сервера', PORT)
        addpar('Float', 'Patchhz', 'Частота патчей (Гц)', 30.0)
        addpar('Float', 'Proxyfps', 'Частота прокси видео (кадр/с)', 2.0)
        addpar('Int', 'Undodepth', 'Глубина undo', 24)
        addpar('File', 'Srcfile', 'Файл источника (картинка или видео)')
        addpar('Toggle', 'Srcvisible', 'Слой источника виден', 1)
        addpar('Float', 'Srcopacity', 'Непрозрачность источника', 1.0)
        addpar('Toggle', 'Paintvisible', 'Слой краски виден', 1)
        addpar('Float', 'Paintopacity', 'Непрозрачность слоя краски', 1.0)
        # Интенсивность слоёв: множитель к прозрачности каждого слоя. Крутится и
        # здесь, и со страницы (ползунок «Интенсивность» в строке слоя).
        addpar('Float', 'Srcint', 'Интенсивность источника', 1.0)
        addpar('Float', 'Paintint', 'Интенсивность краски', 1.0)
        addpar('Float', 'Maskint', 'Интенсивность маски', 1.0)
        addpar('Float', 'Colorint', 'Интенсивность слоя цвета (0 — выключен)', 0.0)
        # Цветовая температура слоя цвета в Кельвинах: 2000 — тёплый, 6500 —
        # нейтральный, 10000 — холодный. Рантайм переводит её в RGB.
        addpar('Float', 'Colortemp', 'Цветовая температура слоя цвета (К)', 6500.0)
        addpar('Toggle', 'Useext', 'Источник = внешний TOP (Externalsrc)', 0)
        addpar('Str', 'Externalsrc', 'Путь к своему TOP, например /project1/render1')
        # Как источник вписывается в полотно — то же меню, что у fitTOP:
        # 0 заполнить, 1 по ширине, 2 по высоте, 3 вписать, 4 заполнить с
        # обрезкой, 5 как есть (native). Именно число, а не меню: у кастомного
        # меню в этой сборке TD имена пунктов не применились («name1» в отчёте).
        # Выбирать удобно со страницы — там это обычный выпадающий список.
        addpar('Int', 'Fitmode',
               'Вставка источника: 0=заполнить, 1=по ширине, 2=по высоте, '
               '3=вписать, 4=с обрезкой, 5=как есть', 3)
        # Как отдавать нарисованное клиенту: патчами (маленькие области, экономно)
        # или целым кадром (проще и надёжнее, но тяжелее по трафику).
        # Именно тумблер, а не меню: у кастомного меню в этой сборке TD имена
        # пунктов не применились (в отчёте значение осталось «name1»), а тумблер
        # работает предсказуемо.
        addpar('Toggle', 'Fullframe', 'Отправлять целый кадр вместо патчей', 0)
        addpar('Float', 'Fullfps', 'Частота целых кадров (кадр/с)', 6.0)
        addpar('Toggle', 'Fulljpeg', 'Целые кадры в JPEG (легче, с потерями)', 0)
        addpar('Toggle', 'Flipy', 'Инвертировать вертикаль (если мазок/undo перевёрнут)', 0)
        # Папка данных ЭТОГО проекта. В другом проекте такой путь не существует,
        # и рантайм сам перейдёт на <проект>/paint (см. Runtime.dirs) — поэтому
        # компонент из .tox работает и на чужом проекте.
        addpar('Str', 'Datadir', 'Папка данных; пусто = <проект>/paint', paint_dir)
    except Exception as e:
        fail('создание своих параметров: %s' % e)

    sample = os.path.join(paint_dir, 'uploads', 'sample.png')
    # значения ещё раз, на случай если параметры создались без значений по умолчанию
    setp(base, Canvasw=CANVAS_W, Canvash=CANVAS_H, Port=PORT, Patchhz=30.0,
         Proxyfps=2.0, Undodepth=24, Srcvisible=1, Srcopacity=1.0,
         Paintvisible=1, Paintopacity=1.0, Useext=0, Flipy=0,
         Datadir=paint_dir, Srcfile=sample, Externalsrc='',
         Fullfps=6.0, Fulljpeg=0, Fullframe=0, Fitmode=3,
         Srcint=1.0, Paintint=1.0, Maskint=1.0, Colorint=0.0, Colortemp=6500.0)

    # ---------------------------------------------------------------- папки
    for sub in ('web', 'uploads', 'media', 'tmp'):
        d = os.path.join(paint_dir, sub)
        if not os.path.isdir(d):
            os.makedirs(d)
            rep('создана папка %s' % d)
    if not os.path.isfile(sample):
        try:
            # Тестовая картинка — только заглушка источника: держим её небольшой,
            # чтобы первая сборка не ждала питон-цикл по 2 млн пикселей.
            sw_w = min(CANVAS_W, 960)
            sw_h = max(2, int(round(sw_w * CANVAS_H / float(CANVAS_W))))
            make_png(sample, sw_w, sw_h)
            rep('сделана тестовая картинка uploads/sample.png (%dx%d)' % (sw_w, sw_h))
        except Exception as e:
            fail('тестовая картинка: %s' % e)
    if not os.listdir(os.path.join(paint_dir, 'web')):
        fail('папка paint/web пуста — веб-клиент не отдастся '
             '(должны быть index.html, app.js, style.css)')
    # ...и те же три файла кладём в компонент, чтобы .tox работал без папки web
    web_files = (('index.html', web_index), ('app.js', web_app),
                 ('style.css', web_style))
    for fname, dat in web_files:
        try:
            with open(os.path.join(paint_dir, 'web', fname), 'r',
                      encoding='utf-8') as f:
                text = f.read()
        except Exception as e:
            fail('страница %s не читается: %s' % (fname, e))
            continue
        if not text:
            fail('страница %s пустая — в компонент нечего положить' % fname)
            continue
        put_text(dat, text)
        rep('страница %s (%d символов) уложена в %s' % (fname, len(text), dat.path))
    try:
        web_comp.display = True
    except Exception:
        pass

    # Пустые текстуры штампов: пока в кадре нет мазков, шейдер их не читает, но
    # файлы должны существовать. Рантайм пишет их по очереди (dabs_a / dabs_b) —
    # смена пути гарантирует, что moviefileinTOP перечитает картинку.
    dabs_png = os.path.join(paint_dir, 'tmp', 'dabs_a.png')
    for name in ('dabs_a.png', 'dabs_b.png'):
        p = os.path.join(paint_dir, 'tmp', name)
        if not os.path.isfile(p):
            try:
                make_zero_rgba_png(p, DAB_TEX_W, 1)
                rep('сделана пустая текстура штампов tmp/%s (%dx1)' % (name, DAB_TEX_W))
            except Exception as e:
                fail('текстура штампов %s: %s' % (name, e))
    setp(dabpng, file=dabs_png, play=0)
    # ВАЖНО: dabpng несёт НЕ картинку, а двоичные данные (координаты, радиус,
    # цвет штампов, упакованные в байты). moviefileinTOP по умолчанию считает
    # файл цветом: переводит из sRGB в рабочее пространство (гамма-кривая!) и
    # может премультиплицировать RGB на альфу. Для картинки это правильно, а для
    # данных — смерть: на полотне вместо линии появляются точки не там, где
    # рисуешь, а патч в браузер приходит пустой. Поэтому просим «не трогать»:
    # линейное пространство, без премульта, линейный 8-битный формат.
    for parname, tokens in (('inputcolorspace', ('linear', 'lin')),
                            ('premultrgbbyalpha', ('none', 'off', 'no', 'nopremult')),
                            ('format', ('rgba8fixed', 'rgba8'))):
        picked = set_menu(dabpng, parname, *tokens)
        rep('dabpng.%s = %s (данные, а не цвет)' % (parname, picked or 'НЕ НАШЁЛ'))

    # ---------------------------------------------------------------- уборка
    for o in (crop, cropundo, cropsnap):
        set_menu(o, 'cropleftunit', 'pixels', 'pixel', 'native')
        set_menu(o, 'croprightunit', 'pixels', 'pixel', 'native')
        set_menu(o, 'cropbottomunit', 'pixels', 'pixel', 'native')
        set_menu(o, 'croptopunit', 'pixels', 'pixel', 'native')
        setp(o, cropleft=0, cropright=0, croptop=0, cropbottom=0)
        picked = get_par(o, 'cropleftunit')
        if picked is not None:
            rep('%s.cropleftunit = %s' % (o.name, picked.eval()))
    for c in (base, server, shaders):
        try:
            c.layoutChildren()
        except Exception:
            pass
    for o in (out, out1, comp_over):
        try:
            o.viewer = True
        except Exception:
            pass
    try:
        out1.display = True                  # смотреть готовый композит — на out1
    except Exception:
        pass

    # ---------------------------------------------------------------- что применилось
    # Токены меню в разных сборках TD могут называться иначе, поэтому печатаем
    # ФАКТИЧЕСКИЕ значения: по этому отчёту видно, встал ли 'pixels' у cropTOP
    # и какой формат буфера реально выбран.
    rep('--- фактические значения ключевых параметров ---')
    for rel, pars in (
            ('server/ws', ('port', 'active', 'callbacks')),
            ('tick', ('active', 'framestart', 'frameend', 'start')),
            ('dabpng', ('file', 'play')),
            ('fb', ('format', 'top')),
            ('brush', ('format', 'outputresolution', 'resolutionw', 'resolutionh')),
            ('restore', ('format', 'outputresolution')),
            ('sw', ('format',)),
            ('buf', ('format',)),
            ('fbm', ('format', 'top')),
            ('brushm', ('format', 'outputresolution', 'resolutionw', 'resolutionh')),
            ('restorem', ('format', 'outputresolution')),
            ('swm', ('format',)),
            ('bufm', ('format',)),
            ('crop', ('cropleftunit', 'format')),
            ('cropm', ('cropleftunit', 'format')),
            ('src_level', ('opacity',)),
            ('paint_level', ('opacity',)),
            ('maskapply', ('outputresolution', 'pixeldat')),
            ('src', ('index',)),
            ('fit', ('fit', 'outputresolution')),
            ('proxy', ('outputresolution', 'resolutionw'))):
        o = base.op(rel)
        if o is None:
            rep('%s: НЕТ ОПЕРАТОРА' % rel)
            continue
        vals = []
        for name in pars:
            par = get_par(o, name)
            if par is None:
                vals.append('%s=НЕТ' % name)
                continue
            try:
                vals.append('%s=%s' % (name, par.eval()))
            except Exception as e:
                vals.append('%s=ОШИБКА(%s)' % (name, e))
        rep('%s: %s' % (rel, ', '.join(vals)))

    # ---------------------------------------------------------------- входы
    # Печатаем ФАКТИЧЕСКИЕ входы каждой ключевой ноды. Это то, из-за чего в
    # прошлый раз «из base ничего не выходило»: связи, поставленные коннекторами,
    # молча не вставали, и по отчёту этого не было видно. Теперь видно.
    rep('--- фактические входы (пусто = связи нет) ---')
    for rel in ('src', 'fit', 'fb', 'brush', 'restore', 'sw', 'buf', 'snap',
                'crop', 'cropundo', 'cropsnap',
                'fbm', 'brushm', 'restorem', 'swm', 'bufm', 'snapm',
                'cropm', 'cropsnapm', 'src_level', 'paint_level', 'maskapply',
                'over', 'out1', 'out'):
        o = base.op(rel)
        if o is None:
            rep('%s: НЕТ ОПЕРАТОРА' % rel)
            continue
        try:
            got = [x.path if x is not None else None for x in o.inputs]
        except Exception as e:
            rep('%s: входы прочитать не удалось (%s)' % (rel, e))
            continue
        shown = [x for x in got if x]
        rep('%s: %s' % (rel, ', '.join(shown) if shown else 'ПУСТО'))
        if not shown:
            fail('у %s нет ни одного входа — он не сработает' % rel)

    # ---------------------------------------------------------------- доступ
    # Сервер стартует асинхронно: сразу после включения Active порт ещё не
    # слушается, поэтому пробуем несколько раз.
    listening = False
    for _ in range(10):
        if port_listening(PORT):
            listening = True
            break
        time.sleep(0.2)
    rep('--- как открыть ---')
    rep('порт %d отвечает: %s' % (PORT, 'да' if listening else
        'пока нет (сервер поднимается асинхронно — проверь http://127.0.0.1:%d/ '
        'через пару секунд)' % PORT))
    addrs = lan_addresses()
    if addrs:
        rep('на ПК:   http://127.0.0.1:%d/  (проверь сначала здесь)' % PORT)
        for ip in addrs:
            rep('планшет: http://%s:%d/' % (ip, PORT))
        rep('если с планшета не открывается — почти всегда файрвол. Запусти '
            'PowerShell от администратора и вставь:')
        rep('  New-NetFirewallRule -DisplayName "TD PaintWeb %d" -Direction Inbound '
            '-LocalPort %d -Protocol TCP -Action Allow -Profile Private' % (PORT, PORT))
    else:
        rep('не смог определить IP — посмотри ipconfig, порт %d' % PORT)
        rep('на ПК проверь http://127.0.0.1:%d/ — если тут открывается, дело в сети '
            'или файрволе' % PORT)
    rep('подтверждение, что сервер действительно поднялся, ищи в '
        'paint/tmp/paint_web_report.json (строка «сервер запущен»)')
    # Тот же адрес — в DAT внутри компонента: видно в сети нод, не нужно искать
    # Textport. Рантайм обновляет этот DAT сам при каждом старте проекта.
    try:
        lines = ['PaintWeb — как открыть:',
                 '  на этом ПК:  http://127.0.0.1:%d/' % PORT]
        if addrs:
            lines.append('  на планшете: http://%s:%d/' % (addrs[0], PORT))
            for ip in addrs[1:]:
                lines.append('               http://%s:%d/' % (ip, PORT))
        else:
            lines.append('  на планшете: IP не определился — смотри ipconfig')
        lines.append('')
        lines.append('Файрвол (один раз, от администратора):')
        lines.append('  New-NetFirewallRule -DisplayName "TD PaintWeb %d" '
                     '-Direction Inbound -LocalPort %d -Protocol TCP -Action Allow '
                     '-Profile Private' % (PORT, PORT))
        put_text(address, '\n'.join(lines))
        try:
            address.viewer = True
        except Exception:
            pass
    except Exception as e:
        rep('не смог записать DAT с адресом: %s' % e)

    # ---------------------------------------------------------------- шейдеры
    # Компилируются ли шейдеры — главный вопрос, который снаружи TD не проверить.
    # К этому моменту все входы подключены, поэтому заставляем перекомпилировать,
    # кукаем и читаем сообщения: ошибка компиляции видна как предупреждение ноды,
    # а подробности TD кладёт в Info DAT.
    for t, dat, src in ((brush, sh_paint, gl_brush),
                        (restore, sh_rest, gl_rest)):
        try:
            t.par.pixeldat = None
            t.par.pixeldat = dat
        except Exception:
            put_text(dat, src)          # смена текста тоже вызывает перекомпиляцию
        try:
            # Обязательно кукаем: во время скрипта TD ничего не готовит сам, и без
            # этого шага проверка рапортовала «скомпилировался», хотя компиляции
            # ещё не было (а на первом кадре шейдер падал).
            t.cook(force=True)
        except Exception as e:
            rep('не смог прогнать кук %s: %s' % (t.name, e))
    time.sleep(0.3)
    for t in (brush, restore):
        msgs = node_messages(t)
        bad = [m for m in msgs if ('compile' in m.lower() or 'error' in m.lower())]
        info_txt = ''
        try:
            idat = ensure(base.op('shaders'), 'infoDAT', 'shaderinfo')
            setp(idat, op=t)
            try:
                t.cook(force=True)
            except Exception:
                pass
            info_txt = str(getattr(idat, 'text', '') or '').strip()
        except Exception as e:
            info_txt = 'Info DAT недоступен: %s' % e
        if bad:
            fail('%s: ШЕЙДЕР НЕ КОМПИЛИРУЕТСЯ — %s' % (t.path, ' | '.join(bad)))
        elif msgs:
            rep('%s: предупреждения ноды: %s' % (t.name, ' | '.join(msgs)))
        else:
            rep('%s: сообщений от шейдера нет' % t.name)
        if info_txt:
            rep('--- Info DAT для %s ---\n%s' % (t.name, info_txt[:1200]))

    # ---------------------------------------------------------------- ошибки
    check_pars(base)

    for o in base.findChildren():
        for m in node_messages(o):
            if 'compile' in m.lower() or 'error' in m.lower():
                fail('%s: %s' % (o.path, m))

    # ---------------------------------------------------------------- tox
    try:
        tox = os.path.join(paint_dir, '..', 'tox', 'paint_web.tox')
        tox = os.path.normpath(tox)
        base.save(tox)
        rep('сохранён %s' % tox)
    except Exception as e:
        rep('tox не сохранён: %s' % e)

    # ------------------------------------------------- отпечаток для автозапуска
    # Пишем ПОСЛЕ всего: если сборка упала на середине, отпечаток обновлять
    # нельзя, иначе рантайм решит, что всё в порядке, и не пересоберёт.
    if not FAIL:
        try:
            sig = runtime_signature(src_rt, paint_dir)
            put_text(version, '%s\n# отпечаток исходников (правится автоматически): '
                              'если файлы изменились, TD пересоберёт компонент сам\n'
                              '# время сборки: %s\n'
                     % (sig, time.strftime('%Y-%m-%d %H:%M:%S')))
            rep('отпечаток сборки: %s' % sig)
        except Exception as e:
            fail('не смог записать отпечаток сборки: %s' % e)
    else:
        rep('отпечаток сборки НЕ обновлён: сборка сообщила о проблемах')


def main():
    print('=== PaintWeb: сборка ===========================================')
    try:
        build()
    except Exception:
        fail('сборка упала целиком:\n' + traceback.format_exc())
    print('\n--- что сделано ---')
    for line in REPORT:
        print('  ' + line)
    if FAIL:
        print('\n--- ПРОБЛЕМЫ (%d) ---' % len(FAIL))
        for line in FAIL:
            print('  ' + line)
    else:
        print('\nПроблем не обнаружено.')
    print('\nАдрес для планшета — в блоке «--- как открыть ---» выше')
    print('и в отчёте paint/td/last_build_report.txt.')
    print('Если страница не открывается — проверь файрвол (см. paint/README.md).')
    print('===============================================================')
    try:
        paint_dir = resolve_paint_dir()
        if paint_dir:
            rp = os.path.join(paint_dir, 'td', 'last_build_report.txt')
            with open(rp, 'w', encoding='utf-8') as f:
                f.write('=== что сделано ===\n')
                f.write('\n'.join(REPORT))
                f.write('\n\n=== проблемы ===\n')
                f.write('\n'.join(FAIL) if FAIL else 'нет')
            print('отчёт записан: %s' % rp)
    except Exception:
        pass
    globals()['COMPLETED'] = True          # дошло до конца — можно верить отчёту


main()
