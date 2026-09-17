"""PaintWeb — рантайм на стороне TouchDesigner.

Этот файл лежит в репозитории и загружается в DAT `paint_web/server/runtime`
скриптом сборки `paint/td/build_paint_web.py`. Правки вносить ЗДЕСЬ и потом
один раз запускать сборку (скрипт перезапишет DAT).

Разделение ответственности:
  * TouchDesigner владеет пикселями (слои, кисть, undo, композит).
  * Браузер — тонкий клиент: мгновенное превью мазка + серверные патчи.
  * HTTP/WS-колбэки только складывают входящее в очереди; вся работа по кадрам
    идёт здесь, на главном потоке, в onFrameStart/onFrameEnd.

Модуль намеренно не пользуется неявными именами TD (`op`, `me`, `project`):
они передаются через bind(), потому что код исполняется через exec().
"""

import ast
import json
import math
import os
import struct
import subprocess
import threading
import time
import traceback
import zlib

PROTO_VER = 1
MAXDABS = 128                 # столько штампов максимум за кадр (см. brush.glsl)
DAB_TEX_W = MAXDABS * 4       # ширина PNG штампов: по 4 тексела RGBA8 на штамп

# Насколько давление стилуса меняет РАДИУС штампа.
# 0.0 — радиус постоянный (size/2), давление влияет только на плотность/альфу:
#       ровно так рисует веб-клиент, поэтому превью на планшете совпадает с TD.
# Подняв значение (например 0.45), получишь кисть «толще от нажатия», но превью
# на клиенте будет чуть тоньше серверного до прихода патча.
PRESS_SIZE = 0.0

IMG_EXT = ('.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.exr', '.hdr',
           '.tga', '.dds', '.webp', '.gif')

# Как источник вписывается в полотно — то же меню, что у Fit TOP (fitTOP).
# Порядок задаёт значение параметра Fitmode на компоненте, поэтому менять его
# можно только вместе с подписью параметра в build_paint_web.py.
# Токены — имена пунктов меню TD; если сборка назовёт их иначе, рантайм пробует
# подписи (см. _set_menu_par) и пишет в отчёт, что реально встало.
FIT_MODES = (
    ('fill', 'Заполнить', ('fill',)),
    ('horizontal', 'По ширине', ('horizontal', 'hor', 'horiz', 'width')),
    ('vertical', 'По высоте', ('vertical', 'vert', 'height')),
    ('best', 'Вписать', ('fit', 'best', 'bestfit')),
    ('outside', 'Заполнить с обрезкой', ('outside', 'overfill', 'crop')),
    ('nativeres', 'Как есть (native)', ('nativeres', 'native', 'native resolution',
                                        'pixel', '1:1')),
)

# Значения по умолчанию для всех настроек рантайма. Нужны, чтобы провал чтения
# параметров не оставил пустой tun: иначе одна ошибка чтения превращалась в
# «KeyError: 'proxyfps'» по всему кадру (так и было, когда компонент собрался из
# файла, сохранённого на середине правки).
DEFAULT_TUN = {
    'w': 1920, 'h': 1080, 'port': 9980,
    'patchhz': 30.0, 'proxyfps': 2.0, 'undodepth': 24,
    'srcfile': '', 'srcvisible': 1.0, 'srcopacity': 1.0,
    'paintvisible': 1.0, 'paintopacity': 1.0, 'paintmask': False,
    'srcint': 1.0, 'paintint': 1.0, 'maskint': 1.0,
    'colorint': 0.0, 'colortemp': 6500.0,
    'fitmode': 3, 'useext': False, 'externalsrc': '',
    'patchmode': 'patches', 'fullfps': 6.0, 'fulljpeg': False, 'flipy': False,
    'datadir': '',
}


def kelvin_rgb(kelvin):
    """Цветовая температура в Кельвинах -> RGB 0..1.

    Приближение Таннера Хелланда: 2000 К — тёплый оранжевый, 6500 К — почти
    белый, 10000 К — холодный голубой. Считаем в Python, а не шейдером: одно и
    то же число уходит и в TD (цвет constantTOP), и в браузер, поэтому
    расходиться им негде.
    """
    try:
        k = float(kelvin)
    except Exception:
        k = 6500.0
    t = _clip(k, 1000.0, 40000.0) / 100.0

    def ch(v):
        return _clip(v, 0.0, 255.0) / 255.0

    if t <= 66.0:
        r = 255.0
        g = 99.4708025861 * math.log(t) - 161.1195681661
    else:
        r = 329.698727446 * math.pow(t - 60.0, -0.1332047592)
        g = 288.1221695283 * math.pow(t - 60.0, -0.0755148492)
    if t >= 66.0:
        b = 255.0
    elif t <= 19.0:
        b = 0.0
    else:
        b = 138.5177312231 * math.log(t - 10.0) - 305.0447927307
    return (ch(r), ch(g), ch(b))
VID_EXT = ('.mp4', '.mov', '.m4v', '.mkv', '.webm', '.avi', '.mpg', '.mpeg',
           '.wmv', '.ts', '.m2ts', '.h264', '.h265', '.r3d', '.braw')

MIME = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.mjs': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
    '.ico': 'image/x-icon',
    '.txt': 'text/plain; charset=utf-8',
}

DEFAULT_TOOL = {
    'tool': 'brush',        # brush | eraser | pan
    'color': '#ff4d6d',
    'size': 48.0,
    'hardness': 0.7,
    'flow': 0.9,
    'spacing': 0.15,
    'target': 1,            # id слоя, в который рисуем
}

# Файлы, из которых собирается компонент. По их отпечатку решается, нужна ли
# пересборка: открыл проект — TD сам собрал то, что изменилось, и поднял сервер,
# без Textport и без команд.
SOURCES = (
    'td/build_paint_web.py',
    'td/selftest_paint_web.py',
    'td/run_paint_web.py',
    'td/runtime/paint_runtime.py',
    'td/runtime/pw_boot.py',
    'td/runtime/pw_callbacks.py',
    'td/runtime/pw_execute.py',
    'td/runtime/brush.glsl',
    'td/runtime/restore.glsl',
    'web/index.html',
    'web/app.js',
    'web/style.css',
)


def lan_addresses(port):
    """Адреса, по которым машина видна из локальной сети.

    Глобальный адрес (100.125.41.73 — это Tailscale) не отфильтровываем: по нему
    тоже ходят, и он часто единственный рабочий с планшета.
    """
    hosts = []
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            hosts.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        import socket
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in hosts:
                hosts.append(ip)
    except Exception:
        pass
    return ['http://%s:%d/' % (ip, port) for ip in hosts
            if ip and not ip.startswith('127.')]


def check_source_names(text, name):
    """Найти обращения к КОНСТАНТАМ, которых в файле нет.

    Полуготовый файл (сохранённый «на середине правки») компилируется, поэтому
    раньше спокойно уезжал в компонент: NameError всплывал уже в живом TD, на
    каждом кадре, и компонент выглядел мёртвым. Ровно так вышло с FIT_MODES:
    ссылка на константу появилась в файле раньше самой константы, автопересборка
    успела собрать этот момент, и кадр падал с «name 'FIT_MODES' is not defined».

    Проверяем только ИМЕНА В ВЕРХНЕМ РЕГИСТРЕ длиннее двух букв — это константы
    уровня модуля. Локальные переменные (`W`, `H`, `o`) не трогаем: имена, которые
    где-то в файле присваиваются, считаются определёнными.

    Возвращает текст проблемы или None, если всё на месте.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None                     # про синтаксис скажет отдельная проверка
    defined = set()
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node.ctx, ast.Load) and node.id.isupper() and len(node.id) > 2:
                used.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
            for a in list(node.args.args) + list(node.args.kwonlyargs):
                defined.add(a.arg)
            if node.args.vararg is not None:
                defined.add(node.args.vararg.arg)
            if node.args.kwarg is not None:
                defined.add(node.args.kwarg.arg)
        elif isinstance(node, ast.ClassDef):
            defined.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                defined.add(a.asname or a.name.split('.')[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
    defined.update(('Ellipsis', 'NotImplemented'))
    missing = sorted(n for n in used if n not in defined)
    if missing:
        return ('%s ссылается на несуществующие константы: %s — похоже, файл '
                'сохранён на середине правки' % (name, ', '.join(missing)))
    return None


def sources_compile(paint_dir):
    """Проверить, что исходники вообще компилируются.

    Зачем: автопересборка запускается сама и не спрашивает разрешения. Если файл
    сохранён «на середине правки», в компонент уедет неработающий код — и потом в
    TD падает всё, включая веб-сервер (а веб-сервером же кнопку пересборки и
    нажимают). Поэтому перед пересборкой проверяем синтаксис: дешевле не
    пересобираться, чем остаться без рабочего компонента.

    Кроме синтаксиса проверяем имена констант (см. check_source_names): файл может
    компилироваться и при этом падать на первом же кадре.

    Возвращает (ok, текст).
    """
    for rel in SOURCES:
        if not rel.endswith('.py'):
            continue
        p = os.path.join(paint_dir, rel.replace('/', os.sep))
        if not os.path.isfile(p):
            continue
        try:
            with open(p, 'r', encoding='utf-8') as f:
                src = f.read()
        except Exception as e:
            return False, '%s не читается: %s' % (rel, e)
        try:
            compile(src, rel, 'exec')
        except SyntaxError as e:
            return False, ('%s: строка %s: %s' % (rel, e.lineno, e.msg))
        bad = check_source_names(src, rel)
        if bad:
            return False, bad
    return True, ''


def sources_signature(paint_dir):
    """Отпечаток исходников: размер и время правки каждого файла.

    Функция модульного уровня, потому что её зовёт ещё и сборка (см.
    build_paint_web.py): отпечаток должен считаться ОДНИМ кодом там и здесь,
    иначе сборка и рантайм будут вечно считать друг друга устаревшими.
    """
    import hashlib
    parts = []
    for rel in SOURCES:
        p = os.path.join(paint_dir, rel.replace('/', os.sep))
        try:
            st = os.stat(p)
            parts.append('%s:%d:%.4f' % (rel, st.st_size, st.st_mtime))
        except Exception:
            parts.append('%s:нет' % rel)
    return hashlib.md5('\n'.join(parts).encode('utf-8')).hexdigest()[:12]

# ---------------------------------------------------------------- инфраструктура

_INSTANCES = {}
_OP = None
_PROJECT = None
_APP = None
_DAT = None
_RUN = None


def bind(base_path, g):
    """Вызывается из DAT-ов рантайма: отдаёт им доступ к именам TD."""
    global _OP, _PROJECT, _APP, _DAT, _RUN
    if g:
        # Берём имена через g[name], а не через `name in g`: в живом TD часть
        # глобальных имён (`app`, `project`) отдаётся не элементом словаря, а
        # лениво, поэтому проверка «есть ли ключ» их не находит, и в отчёте
        # вместо версии TD стоял «?».
        for name, cur in (('op', _OP), ('project', _PROJECT), ('app', _APP),
                          ('me', _DAT), ('run', _RUN)):
            try:
                val = g[name]
            except Exception:
                continue
            if val is None:
                continue
            if name == 'op':
                _OP = val
            elif name == 'project':
                _PROJECT = val
            elif name == 'app':
                _APP = val
            elif name == 'run':
                _RUN = val
            else:
                _DAT = val
    return get(base_path)


def get(base_path):
    rt = _INSTANCES.get(base_path)
    if rt is None:
        rt = Runtime(base_path)
        _INSTANCES[base_path] = rt
    return rt


def set_menu(par, *want):
    """Поставить меню-параметр по токену или по куску подписи. Возвращает токен."""
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
    return None


def _clip(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def _pack16(v):
    """Значение в два байта с шагом 1/8 — ровно так его распаковывает brush.glsl."""
    n = int(round(_clip(float(v), 0.0, 8000.0) * 8.0))
    if n < 0:
        n = 0
    if n > 65535:
        n = 65535
    return (n >> 8) & 0xFF, n & 0xFF


def _pack8(v):
    n = int(round(_clip(float(v), 0.0, 1.0) * 255.0))
    return 0 if n < 0 else (255 if n > 255 else n)


def _write_png_rgba8(path, w, h, data):
    """Минимальный PNG 8 бит RGBA без фильтров: быстро и предсказуемо."""
    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)                            # фильтр None
        raw += data[y * stride:(y + 1) * stride]

    def chunk(tag, payload):
        return (struct.pack('>I', len(payload)) + tag + payload
                + struct.pack('>I', zlib.crc32(tag + payload) & 0xffffffff))

    blob = b'\x89PNG\r\n\x1a\n'
    blob += chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 6, 0, 0, 0))
    blob += chunk(b'IDAT', zlib.compress(bytes(raw), 1))
    blob += chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(blob)


def _as_list(x):
    """Сообщения TD: строка или список — всегда возвращаем список строк."""
    if x is None:
        return []
    if isinstance(x, str):
        return [x] if x.strip() else []
    try:
        return [str(i) for i in x]
    except Exception:
        return [str(x)]


# ------------------------------------------------------------------- рантайм

class Runtime(object):

    def __init__(self, base_path):
        self.base = base_path
        self.mu = threading.Lock()
        self.frame = 0
        self.errors = {}
        self.tool = dict(DEFAULT_TOOL)
        self.clients = {}
        self.cmds = []
        self.bins = []
        self.strokes = {}
        self.stroke_seq = 0
        self.frame_dabs = []
        self.frame_rect = None
        # Второй слой — маска источника: у неё свои штампы, своя область и свой
        # буфер. Смешивать пачки нельзя: текстура штампов одна на обе кисти.
        self.frame_dabs_m = []
        self.frame_rect_m = None
        # Доставка штампов идёт через файл: Python пишет PNG, moviefileinTOP его
        # читает. Чтение не мгновенное — в том кадре, где файл подменили, шейдер
        # ещё видит прежнюю текстуру (в живом TD это выглядело как «нарисовал, а
        # на полотне ничего»). Поэтому пачка штампов не рисуется в том же кадре:
        # сначала она уезжает в текстуру (dab_pending -> dab_ready), и только
        # следующим кадром её рисует кисть, когда текстура уже точно загружена.
        self.dab_pending = {'paint': [], 'mask': []}
        self.dab_pending_rect = {'paint': None, 'mask': None}
        self.dab_pending_mode = {'paint': 0.0, 'mask': 1.0}
        self.dab_ready = None          # (слой, сколько штампов, режим) — ждёт рисования
        self.dab_ready_rect = None
        self.mask_filled = False       # маска залита белым: источник виден целиком
        self.paint_rect = {'paint': None, 'mask': None}   # что изменено в кадре
        self.send_rect = None
        self.send_rect_m = None        # область для патча слоя маски
        self.force_patch = False
        self.clear_frames = 0
        self.snap_depth = 0
        self.last_patch_t = 0.0
        self.next_proxy_t = 0.0
        self.restore_req = None        # {'rect', 'path', 'frame', 'layer'}
        self.switch_until = -1         # кадр, до которого держим sw=1
        self.switch_layer = 'paint'    # чей переключатель держим: краска или маска
        self.seq = 0
        self.undo = []
        self.redo = []
        self.log_lines = []
        self._log_flush = 0.0
        self.src_path = None
        self.src_kind = 'image'
        self.paint_dirty = False
        self.sizes = (0, 0)
        self.tun = {}
        self.t0 = time.time()
        self.report_done = False
        self._rep_t = time.time()
        # -- автозапуск без Textport
        self._sig = ''                 # отпечаток исходников, который видели
        self._sig_checked = 0.0        # когда проверяли в прошлый раз
        self._sig_since = 0.0          # с какого времени отпечаток «чужой»
        self._rebuild_t = 0.0          # когда просили пересборку в прошлый раз
        self._rebuild_sig = ''         # отпечаток, на котором пересборка провалилась
        # Кроп в этой сборке TD может зеркалить область (размер при этом верный,
        # поэтому проверка размера такое не ловит). Флаги ставит
        # check_crop_orientation по эталонной картинке.
        self.crop_flip_v = False
        self.crop_flip_h = False
        self._proxy_try = 0.0          # когда в прошлый раз пытались отдать постер
        self._proxy_fail_t = 0.0       # когда кодировщик вернул пусто
        self._stale_check = 0.0        # когда проверяли молчащих клиентов
        self.autostart_done = False
        self._ready_logged = False
        self.autostart_reason = ''
        self.diag = {'ms': 0.0, 'dabs': 0, 'patches': 0, 'proxy': 0,
                     'up': int(time.time())}

    # -- автозапуск: без Textport, без команд ---------------------------------
    def paint_dir(self):
        """Папка данных (`paint/`): из параметра Datadir, иначе — рядом с проектом."""
        try:
            d = str(self.o('').par['Datadir'].eval() or '').strip()
        except Exception:
            d = ''
        if d and os.path.isdir(d):
            return os.path.normpath(d)
        # запасной путь: искать paint/ рядом с .toe
        try:
            folder = str(_PROJECT.folder) if _PROJECT is not None else ''
        except Exception:
            folder = ''
        for cand in (os.path.join(folder, 'paint'), folder):
            # Признак папки исходников: скрипт сборки или сам рантайм. В репозитории
            # веб-интерфейса исходники лежат в корне, в проекте — в paint/, поэтому
            # проверяем оба варианта и оба маркера.
            if not cand:
                continue
            if (os.path.isfile(os.path.join(cand, 'td', 'build_paint_web.py'))
                    or os.path.isfile(os.path.join(cand, 'td', 'runtime',
                                                   'paint_runtime.py'))):
                return os.path.normpath(cand)
        return os.path.normpath(d) if d else ''

    def stored_signature(self):
        d = self.try_o('server/version')
        if d is None:
            return ''
        try:
            for line in str(d.text or '').splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    return line
        except Exception:
            pass
        return ''

    def ensure_server(self):
        """Сервер обязан быть включён: иначе страница просто не откроется."""
        ws = self.try_o('server/ws')
        if ws is None:
            return False
        try:
            if not int(ws.par['active'].eval()):
                ws.par['active'] = 1
                self.log('включил веб-сервер (Active=1)')
            ws.par['port'] = int(self.tun.get('port') or 9980)
            return True
        except Exception:
            self.err('server-on', traceback.format_exc())
            return False

    def addresses(self):
        port = int(self.tun.get('port') or 9980)
        out = ['http://127.0.0.1:%d/' % port]
        return out, lan_addresses(port)

    def publish_address(self):
        """Адрес для планшета — в DAT на видном месте, а не только в Textport."""
        local, remote = self.addresses()
        self.diag['url'] = local[0]
        self.diag['urls'] = list(local) + list(remote)
        lines = ['PaintWeb — как открыть:',
                 '  на этом ПК:  %s' % local[0]]
        if remote:
            lines.append('  на планшете: %s' % remote[0])
            for extra in remote[1:]:
                lines.append('               %s' % extra)
        else:
            lines.append('  на планшете: не смог определить IP — смотри ipconfig')
        lines.append('')
        lines.append('Если с планшета не открывается — почти всегда файрвол:')
        lines.append('  New-NetFirewallRule -DisplayName "TD PaintWeb %d" '
                     '-Direction Inbound -LocalPort %d -Protocol TCP -Action Allow '
                     '-Profile Private' % (int(self.tun.get('port') or 9980),
                                           int(self.tun.get('port') or 9980)))
        text = '\n'.join(lines)
        # Ссылка видна и в параметрах компонента (`Page`), кнопка рядом —
        # `Openpage`: её нажатие открывает эту же страницу в браузере на ПК.
        # Параметра может не быть в старой сборке — это не ошибка, но факт записи
        # кладём в отчёт: иначе «кнопка не работает» неотличимо от «адрес пустой».
        try:
            self.o('').par['Page'].val = local[0]
            self.diag['page_par'] = True
        except Exception:
            self.diag['page_par'] = False
        d = self.try_o('server/address')
        if d is not None:
            try:
                d.text = text
            except Exception:
                pass
        return text

    def _exec_script(self, path):
        """Исполнить файл-скрипт (сборку или самопроверку) с именами TD."""
        with open(path, 'r', encoding='utf-8') as f:
            src = f.read()
        ns = {'__name__': '__main__', 'print': print}
        if _OP is not None:
            ns['op'] = _OP
            ns['me'] = _OP(self.base)
        if _PROJECT is not None:
            ns['project'] = _PROJECT
        if _APP is not None:
            ns['app'] = _APP
        exec(compile(src, path, 'exec'), ns)
        return ns

    def schedule(self, func, *args):
        """Выполнить на главном потоке, не разрывая текущий кадр.

        Пересборка переписывает DAT-ы, поэтому её нельзя делать прямо посреди
        onFrameStart: просим TD выполнить это следующим кадром (run с задержкой).
        Если run недоступен (в тестах), вызываем сразу.
        """
        if _RUN is not None:
            try:
                base = _OP(self.base) if _OP is not None else None
                _RUN(func, *args, fromOP=base, delayMilliSeconds=60)
                return 'run'
            except Exception:
                self.err('schedule', traceback.format_exc())
        func(*args)
        return 'direct'

    def autostart(self, force=False, reason='запуск проекта'):
        """Собрать (если надо), поднять сервер и показать адрес.

        Это и есть «запуск без команды»: при открытии проекта и дальше раз в
        пару секунд сверяется отпечаток исходников, и если файлы изменились —
        компонент пересобирается сам.
        """
        d = self.paint_dir()
        self.autostart_reason = reason
        if not self.tun:
            try:
                self.read_pars()          # onStart может прийти раньше первого кадра
            except Exception:
                pass
        if not d:
            self.err('autostart', 'не нашёл папку paint (параметр Datadir пуст)')
            return False
        if not self.sources_present():
            # Компонент вставили из .tox в другой проект: исходников рядом нет,
            # пересобирать нечего. Сервер при этом поднимается как обычно, а
            # страница отдаётся из DAT-ов компонента.
            self.diag['autostart'] = 'исходников рядом нет — компонент из .tox'
            self.autostart_done = True
            if not self._ready_logged:
                self._ready_logged = True
                self.log('компонент вставлен из .tox: пересборка не нужна, '
                         'страница отдаётся из компонента')
            try:
                self.ensure_server()
                self.publish_address()
            except Exception:
                self.err('autostart', traceback.format_exc())
            return False
        bpath = os.path.join(d, 'td', 'build_paint_web.py')
        if not os.path.isfile(bpath):
            self.err('autostart', 'нет файла сборки %s' % bpath)
            return False
        self.ensure_server()
        self.publish_address()
        sig = sources_signature(d)
        have = self.stored_signature()
        self.diag['sig'] = sig
        self.diag['sig_stored'] = have or 'нет'
        if not force and have == sig:
            self.diag['autostart'] = 'сборка не нужна'
            self.autostart_done = True
            if not self._ready_logged:
                self._ready_logged = True
                self.log('PaintWeb готов: %s' % self.diag.get('url', ''))
            return False
        if (time.time() - self._rebuild_t) < 5.0:
            self.diag['autostart'] = 'пересборка уже запрошена'
            return False
        self.autostart_done = True
        self.request_rebuild(reason + (' (отпечаток %s → %s)' % (have or 'пусто', sig)))
        return True

    def request_rebuild(self, reason='по кнопке', throttle=True):
        """Пересобрать компонент из файлов на диске.

        throttle=True — не чаще раза в секунду (защита от лавины при автослежении).
        Для нажатия кнопки throttle=False: человек нажал — значит надо сейчас.
        """
        d = self.paint_dir()
        bpath = os.path.join(d, 'td', 'build_paint_web.py')
        if not os.path.isfile(bpath):
            self.err('rebuild', 'нет файла сборки %s' % bpath)
            return False
        now = time.time()
        if throttle and (now - self._rebuild_t) < 1.0:
            return False
        self._rebuild_t = now
        self._rebuild_sig = sources_signature(d)
        self.diag['rebuild'] = reason
        self.log('пересборка компонента: %s' % reason)
        how = self.schedule(self._exec_script, bpath)
        if how == 'direct':
            # при прямом вызове отпечаток уже другой: обновляем, чтобы не
            # пересобирать по кругу
            self._sig_since = 0.0
        return True

    def request_selftest(self, reason='по кнопке'):
        d = self.paint_dir()
        spath = os.path.join(d, 'td', 'selftest_paint_web.py')
        if not os.path.isfile(spath):
            self.err('selftest', 'нет файла самопроверки %s' % spath)
            return False
        self.log('самопроверка: %s' % reason)
        self.schedule(self._exec_script, spath)
        return True

    def open_page(self, reason=''):
        """Открыть страницу в браузере на этом ПК.

        Так работают кнопка «Открыть на ПК» на странице и кнопка-параметр
        `Openpage` на самом компоненте. Двойное срабатывание (колбэк
        parameterExecuteDAT плюс покадровая подстраховка) гасим здесь: два окна
        браузера от одного нажатия не нужны.
        """
        now = time.time()
        if (now - getattr(self, '_opened_at', 0.0)) < 1.0:
            return False
        self._opened_at = now
        url = self.addresses()[0][0]
        try:
            import webbrowser
            webbrowser.open(url)
            self.log('открываю в браузере: %s%s'
                     % (url, (' (%s)' % reason) if reason else ''))
            return True
        except Exception:
            self.err('open-page', traceback.format_exc())
            return False

    def open_button_check(self):
        """Подстраховка кнопки «Открыть интерфейс» на компоненте.

        Штатно нажатие обрабатывает parameterExecuteDAT (нода `onpulse`). Но в
        части сборок TD колбэк может не прийти — тогда ловим нажатие сами: у
        Pulse-параметра значение это счётчик нажатий, и он растёт. Второй раз
        окно не откроется: open_page() гасит повтор меньше чем за секунду.
        """
        par = None
        try:
            par = self.o('').par['Openpage']
        except Exception:
            return
        try:
            cnt = int(par.eval())
        except Exception:
            return
        prev = getattr(self, '_open_pulses', None)
        self._open_pulses = cnt
        if prev is not None and cnt > prev:
            self.open_page(reason='кнопка на компоненте')

    def watch_sources(self):
        """Периодическая проверка: изменились ли файлы — надо пересобрать.

        Дебаунс в две секунды: файлы правятся пачкой, и пересобирать на каждое
        промежуточное сохранение нельзя. Во время рисования пересборка тоже
        откладывается — она рвёт соединение и перезагружает модуль.
        """
        now = time.time()
        if (now - self._sig_checked) < 2.0:
            return
        self._sig_checked = now
        if not self.sources_present():
            # Компонент из .tox: следить не за чем (и пересобирать нечего).
            return
        d = self.paint_dir()
        if not d:
            return
        sig = sources_signature(d)
        have = self.stored_signature()
        if sig == have:
            self._sig_since = 0.0
            self._sig = sig
            return
        if self._sig != sig:
            self._sig = sig
            self._sig_since = now
            return                         # первый раз увидели — ждём
        if (now - self._sig_since) < 2.0:
            return                         # ещё правится
        if self.strokes:
            return                         # не мешаем рисовать
        if (now - self._rebuild_t) < 10.0:
            return                         # только что пересобирали
        # Если на этом самом отпечатке сборка уже не удалась (в отчёте будут
        # провалы), не долбим её каждые десять секунд: ждём либо новой правки
        # файлов, либо минуты.
        if sig == self._rebuild_sig and (now - self._rebuild_t) < 60.0:
            self.diag['rebuild_skip'] = 'та же неудачная сборка, ждём правки файлов'
            return
        # Синтаксис проверяем ДО пересборки: иначе полуготовый файл уедет в
        # компонент и убьёт рантайм (вместе с кнопкой пересборки).
        ok, why = sources_compile(d)
        if not ok:
            self.diag['compile_skip'] = why
            if self.diag.get('compile_warned') != why:
                self.diag['compile_warned'] = why
                self.log('файлы не компилируются, пересборку пропускаю: %s' % why)
            return
        self.request_rebuild('файлы изменились на диске (отпечаток %s → %s)'
                             % (have or 'пусто', sig))

    # -- пути ---------------------------------------------------------------
    def o(self, rel):
        """Оператор внутри компонента.

        `self.o('server/ws')` — нода компонента, `self.o('')` — сам компонент.
        Раньше в коде было `self.o(self.base)`: путь склеивался дважды
        (`<base>/<base>`), TD возвращал None, и чтение параметров базового
        компонента молча падало — например `Datadir` не читался никогда.
        """
        if not rel:
            return _OP(self.base)
        return _OP(self.base + '/' + rel)

    def try_o(self, rel):
        try:
            return _OP(self.base + '/' + rel)
        except Exception:
            return None

    def dirs(self):
        """Папки данных.

        Папка берётся из параметра Datadir, но только если она СУЩЕСТВУЕТ.
        Компонент вставляют из .tox в другие проекты, и записанный там абсолютный
        путь из чужого проекта был бы неверным — тогда берём <проект>/paint и
        создаём его. Пустой параметр означает то же самое. Так один и тот же
        компонент работает и в своём проекте, и в любом другом.
        """
        d = str(self.tun.get('datadir') or '')
        if d and not os.path.isdir(d):
            self.log('папка данных %s не найдена — беру папку этого проекта' % d)
            d = ''
        if not d:
            try:
                root = _PROJECT.folder
            except Exception:
                root = ''
            cand = os.path.join(root, 'paint') if root else ''
            if cand and not os.path.isdir(cand):
                try:
                    os.makedirs(cand)
                except Exception:
                    cand = ''
            if not cand:
                # В корень проекта данные не пишем: там может лежать своя папка
                # web/ и файлы пользователя. Если paint/ создать не удалось,
                # уходим во временную папку системы и честно пишем об этом.
                import tempfile
                cand = os.path.join(tempfile.gettempdir(), 'paintweb')
                try:
                    os.makedirs(cand)
                except Exception:
                    pass
                self.log('папку paint рядом с проектом создать не удалось — '
                         'данные во временной папке %s' % cand)
            d = cand
            if d and not getattr(self, '_dirs_logged', False):
                self._dirs_logged = True
                self.log('папка данных: %s' % d)
        out = {
            'root': d,
            'web': os.path.join(d, 'web'),
            'uploads': os.path.join(d, 'uploads'),
            'media': os.path.join(d, 'media'),
            'tmp': os.path.join(d, 'tmp'),
        }
        # Подпапки создаём на месте: компонент из .tox может оказаться в проекте,
        # где папки данных ещё нет, и загрузка картинки/патчи не должны падать.
        if d and not getattr(self, '_dirs_made', False):
            self._dirs_made = True
            for key in ('web', 'uploads', 'media', 'tmp'):
                try:
                    if not os.path.isdir(out[key]):
                        os.makedirs(out[key])
                except Exception:
                    pass
        return out

    def sources_present(self):
        """Есть ли рядом файлы исходников (td/**, web/**).

        Если компонент вставили из .tox в другой проект, исходников там нет — и
        автопересборка не нужна и невозможна: она бы каждую минуту пыталась
        собрать то, чего нет, и засоряла журнал.
        """
        try:
            root = self.dirs()['root']
        except Exception:
            return False
        if not root:
            return False
        return os.path.isfile(os.path.join(root, 'td', 'runtime', 'paint_runtime.py'))

    # -- журнал -------------------------------------------------------------
    def log(self, msg):
        line = '%s  %s' % (time.strftime('%H:%M:%S'), msg)
        self.log_lines.append(line)
        if len(self.log_lines) > 400:
            del self.log_lines[:200]

    def err(self, tag, tb=None):
        text = str(tb or '')
        key = tag + '|' + text[:200]
        now = time.time()
        last = self.errors.get(key, 0)
        if now - last > 5.0:                 # не спамить одинаковым
            self.errors[key] = now
            self.log('ОШИБКА [%s] %s' % (tag, text.replace('\n', ' | ')[:400]))
            print('PaintWeb [%s]: %s' % (tag, text))

    # -- параметры ----------------------------------------------------------
    def read_pars(self):
        """Прочитать свои параметры компонента.

        Значения по умолчанию кладём СРАЗУ: если чтение почему-то сорвётся,
        `self.tun` всё равно останется полным словарём. Раньше при сбое чтения
        он оставался пустым, и дальше по кадру сыпалось «KeyError: 'proxyfps'» —
        одна ошибка превращалась в лавину, а компонент выглядел мёртвым.
        """
        b = _OP(self.base)
        t = dict(DEFAULT_TUN)
        try:
            self._read_pars_into(b, t)
        except Exception:
            self.err('read_pars', traceback.format_exc())
            t['parse_failed'] = True
        self.tun = t
        return t

    def _read_pars_into(self, b, t):
        def g(name, dflt):
            try:
                return b.par[name].eval()
            except Exception:
                return dflt

        t['w'] = max(16, int(g('Canvasw', 1920)))
        t['h'] = max(16, int(g('Canvash', 1080)))
        t['port'] = int(g('Port', 9980))
        t['patchhz'] = max(1.0, float(g('Patchhz', 30.0)))
        t['proxyfps'] = max(0.0, float(g('Proxyfps', 2.0)))
        t['undodepth'] = max(0, int(g('Undodepth', 24)))
        t['srcfile'] = str(g('Srcfile', '') or '')
        t['srcvisible'] = _clip(float(g('Srcvisible', 1)), 0.0, 1.0)
        t['srcopacity'] = _clip(float(g('Srcopacity', 1)), 0.0, 1.0)
        t['paintvisible'] = _clip(float(g('Paintvisible', 1)), 0.0, 1.0)
        t['paintopacity'] = _clip(float(g('Paintopacity', 1)), 0.0, 1.0)
        # Интенсивность слоёв и цветовая температура слоя цвета: параметры есть и
        # на компоненте, и на странице (ползунки в строке слоя).
        t['srcint'] = _clip(float(g('Srcint', 1)), 0.0, 4.0)
        t['paintint'] = _clip(float(g('Paintint', 1)), 0.0, 4.0)
        t['maskint'] = _clip(float(g('Maskint', 1)), 0.0, 4.0)
        t['colorint'] = _clip(float(g('Colorint', 0)), 0.0, 1.0)
        t['colortemp'] = _clip(float(g('Colortemp', 6500.0)), 1000.0, 40000.0)
        t['paintmask'] = False         # поле больше не используется: маска — свой слой
        t['fitmode'] = int(_clip(float(g('Fitmode', 3)), 0, len(FIT_MODES) - 1))
        t['useext'] = float(g('Useext', 0)) > 0.5
        t['externalsrc'] = str(g('Externalsrc', '') or '').strip()
        t['patchmode'] = 'fullframe' if float(g('Fullframe', 0)) > 0.5 else 'patches'
        t['fullfps'] = max(0.0, float(g('Fullfps', 6.0)))
        t['fulljpeg'] = float(g('Fulljpeg', 0)) > 0.5
        t['flipy'] = float(g('Flipy', 0)) > 0.5
        t['datadir'] = str(g('Datadir', '') or '')

    # -- клиенты ------------------------------------------------------------
    def ws_open(self, client, uri):
        with self.mu:
            self.clients[client] = {
                'w': 0, 'h': 0, 'dpr': 1.0, 'ready': False,
                'need_poster': True, 'need_sync': True,
                'last_patch': 0.0, 'patches': 0, 'since': time.time(),
                'last_seen': time.time(),
            }
        self.log('WS открыт: %s' % client)

    def ws_close(self, client):
        with self.mu:
            self.clients.pop(client, None)
        self.log('WS закрыт: %s' % client)

    def _touch_client(self, client):
        """Клиент, которого рантайм не знает, — знакомимся заново.

        Рантайм пересобирается на ходу (правки в файлах подхватываются
        автоматически), и при перезагрузке модуля список клиентов пустеет, а
        открытые WS-соединения остаются. Без этого после пересборки браузер
        перестал бы получать патчи, хотя связь живая.
        """
        if client not in self.clients:
            self.ws_open(client, None)
        else:
            with self.mu:
                if client in self.clients:
                    self.clients[client]['last_seen'] = time.time()

    def _drop_stale_clients(self):
        """Выкинуть клиентов, от которых давно нет вестей.

        TD не всегда сообщает о закрытии сокета (перезагрузка страницы, сон
        планшета), и «мёртвые» клиенты копились: им слались и патчи, и постеры
        источника. Считаем живым того, кто подавал голос за последнюю минуту.
        """
        now = time.time()
        if (now - self._stale_check) < 10.0:
            return
        self._stale_check = now
        with self.mu:
            dead = [c for c, st in self.clients.items()
                    if (now - float(st.get('last_seen') or st.get('since') or now)) > 60.0]
            for c in dead:
                self.clients.pop(c, None)
        if dead:
            self.log('отбросил молчащих клиентов: %d' % len(dead))

    def ws_text(self, client, data):
        try:
            msg = json.loads(data)
            if isinstance(msg, dict):
                self._touch_client(client)
                with self.mu:
                    self.cmds.append((client, msg))
        except Exception as e:
            self.log('битый JSON от %s: %s' % (client, e))

    def ws_binary(self, client, data):
        self._touch_client(client)
        with self.mu:
            if client in self.clients:
                self.clients[client]['last_seen'] = time.time()
            self.bins.append((client, bytes(data)))
        if len(self.bins) > 2000:
            del self.bins[:1000]

    def _send(self, client, obj):
        try:
            self.o('server/ws').webSocketSendText(client, json.dumps(obj))
            return True
        except Exception as e:
            if client in self.clients:
                self.log('не смог отправить %s: %s' % (client, e))
            return False

    def _send_bin(self, client, head, blob):
        if not self._send(client, head):
            return False
        try:
            self.o('server/ws').webSocketSendBinary(client, blob)
            return True
        except Exception as e:
            self.log('не смог отправить бинарь %s: %s' % (client, e))
            return False

    def _broadcast(self, obj, only=None):
        with self.mu:
            targets = list(self.clients.keys())
        for c in targets:
            if only is None or c in only:
                self._send(c, obj)

    def _ready_clients(self):
        with self.mu:
            return [c for c, st in self.clients.items() if st.get('ready')]

    # -- состояние ----------------------------------------------------------
    def layers(self):
        t = self.tun
        name = os.path.basename(self.src_path) if self.src_path else ''
        return [
            # «drawInto» — в какой буфер TD уедут мазки, если выбран этот слой.
            # Слой «Источник» рисует МАСКУ источника (буфер маски = слой 2), слой
            # «Краска» — краску (буфер краски = слой 1). Так в интерфейсе нет
            # отдельного переключателя «рисовать / маска»: режим определяется тем,
            # какой слой выбран, и одно не превращается в другое.
            {'id': 0, 'name': 'Источник', 'kind': 'source',
             'visible': 1 if t['srcvisible'] > 0.001 else 0,
             'opacity': t['srcopacity'], 'drawInto': 2,
             'fit': self.fit_name(),
             'fitModes': [{'key': k, 'label': lab} for k, lab, _tk in FIT_MODES],
             'srcType': self.src_kind, 'srcName': name, 'srcPath': self.src_path or ''},
            {'id': 1, 'name': 'Краска', 'kind': 'paint',
             'visible': 1 if t['paintvisible'] > 0.001 else 0,
             'opacity': t['paintopacity'], 'drawInto': 1},
            # Буфер маски нужен браузеру, чтобы показывать источник, обрезанный
            # маской, но своей строки в панели слоёв у него нет: маска — часть
            # слоя «Источник».
            {'id': 2, 'name': 'Маска источника', 'kind': 'mask',
             'visible': 1, 'opacity': 1.0, 'ui': 0},
        ]

    def history(self):
        return {'undo': len(self.undo), 'redo': len(self.redo)}

    def status(self):
        d = dict(self.diag)
        d['clients'] = len(self.clients)
        d['undo'] = len(self.undo)
        d['redo'] = len(self.redo)
        d['strokes'] = len(self.strokes)
        d['queued_points'] = sum(len(s.get('queue', ())) for s in self.strokes.values())
        d['src'] = self.src_path
        d['canvas'] = (self.tun.get('w'), self.tun.get('h'))
        d['log'] = list(self.log_lines[-40:])
        # Ошибки рантайма жили только в журнале, а журнал не всегда доходит.
        # В отчёте на диске они нужны как отдельный список.
        d['error_list'] = [k.split('|', 1)[-1].strip().replace('\n', ' | ')[:300]
                           for k in list(self.errors.keys())[-10:]]
        d['error_count'] = len(self.errors)
        return d

    # -- команды ------------------------------------------------------------
    def _drain(self):
        with self.mu:
            cmds, self.cmds = self.cmds, []
            bins, self.bins = self.bins, []
        for client, msg in cmds:
            try:
                self._cmd(client, msg)
            except Exception:
                self.err('cmd', traceback.format_exc())
        for client, data in bins:
            try:
                self._points(data)
            except Exception:
                self.err('points', traceback.format_exc())

    def _cmd(self, client, m):
        t = m.get('t')
        if t == 'hello':
            self._hello(client, m)
        elif t == 'tool':
            self._set_tool(m.get('tool') or {})
            self._broadcast({'t': 'tool', 'tool': self.tool})
        elif t == 'layer':
            self._set_layer(m)
        elif t == 'src':
            self._set_src(m.get('path'))
        elif t == 'sources':
            self._send(client, {'t': 'sources', 'list': self.source_list()})
        elif t == 'clear':
            self._cmd_clear()
        elif t == 'undo':
            self._cmd_history(-1)
        elif t == 'redo':
            self._cmd_history(1)
        elif t == 'ping':
            self._send(client, {'t': 'pong', 'ts': m.get('ts')})
        elif t in ('bye', 'log'):
            pass
        else:
            self.log('неизвестная команда: %r' % (t,))

    def _hello(self, client, m):
        with self.mu:
            st = self.clients.setdefault(client, {})
            st.update({'w': int(m.get('w') or 0), 'h': int(m.get('h') or 0),
                       'dpr': float(m.get('dpr') or 1.0), 'ready': True,
                       'need_poster': True, 'need_sync': True,
                       'since': st.get('since', time.time())})
        t = self.tun
        self._send(client, {
            't': 'welcome', 'ver': PROTO_VER,
            'canvas': {'w': t['w'], 'h': t['h']},
            'layers': self.layers(), 'tool': self.tool,
            'patchHz': t['patchhz'], 'proxyFps': t['proxyfps'],
            'history': self.history(),
            'srcLiveProxy': bool(t['proxyfps'] > 0 and self.src_kind == 'video'),
        })
        self._send(client, {'t': 'sources', 'list': self.source_list()})
        self.log('hello от %s (%sx%s)' % (client, m.get('w'), m.get('h')))

    def _set_tool(self, part):
        for k in ('tool', 'color', 'size', 'hardness', 'flow', 'spacing', 'target'):
            if k in part and part[k] is not None:
                self.tool[k] = part[k]
        self.tool['size'] = _clip(float(self.tool['size']), 1.0, 400.0)
        self.tool['hardness'] = _clip(float(self.tool['hardness']), 0.0, 1.0)
        self.tool['flow'] = _clip(float(self.tool['flow']), 0.01, 1.0)
        self.tool['spacing'] = _clip(float(self.tool['spacing']), 0.02, 1.0)
        if self.tool['tool'] not in ('brush', 'eraser', 'pan'):
            self.tool['tool'] = 'brush'

    def _set_layer(self, m):
        lid = int(m.get('id', -1))
        prop = m.get('prop')
        val = m.get('value')
        b = _OP(self.base)
        try:
            if lid == 0:
                if prop == 'visible':
                    b.par.Srcvisible = 1 if float(val) else 0
                elif prop == 'opacity':
                    b.par.Srcopacity = _clip(float(val), 0.0, 1.0)
                elif prop == 'fit':
                    # Режим вставки источника выбирается на странице: в TD это
                    # число (Fitmode), а имени пункта меню соответствует своя
                    # цифра — см. FIT_MODES.
                    self.set_fit(val)
            elif lid == 1:
                if prop == 'visible':
                    b.par.Paintvisible = 1 if float(val) else 0
                elif prop == 'opacity':
                    b.par.Paintopacity = _clip(float(val), 0.0, 1.0)
                # prop == 'mode' больше не существует: в интерфейсе нет переключателя
                # «рисовать / маска». Куда попадёт мазок, решает выбранный слой
                # (см. layers(): у каждого слоя есть drawInto).
        except Exception:
            self.err('set_layer', traceback.format_exc())
        for lay in self.layers():
            if lay['id'] == lid:
                self._broadcast({'t': 'layer', 'op': 'prop', 'id': lid, 'layer': lay})

    def _set_src(self, path):
        if not path:
            return
        p = str(path)
        if not os.path.isabs(p):
            p = os.path.join(self.dirs()['root'], p)
        p = os.path.normpath(p)
        if not os.path.exists(p):
            self.log('источник не найден: %s' % p)
            self._broadcast({'t': 'error', 'msg': 'Файл не найден: %s' % os.path.basename(p)})
            return
        self.log('источник: %s' % p)
        try:
            _OP(self.base).par.Srcfile = p
        except Exception:
            self.err('set_src', traceback.format_exc())

    def source_list(self):
        d = self.dirs()
        out = []
        for key in ('uploads', 'media'):
            folder = d[key]
            if not os.path.isdir(folder):
                continue
            for name in sorted(os.listdir(folder)):
                ext = os.path.splitext(name)[1].lower()
                if ext in IMG_EXT or ext in VID_EXT:
                    full = os.path.join(folder, name)
                    out.append({
                        'name': name,
                        'path': os.path.relpath(full, d['root']).replace('\\', '/'),
                        'type': 'video' if ext in VID_EXT else 'image',
                    })
        return out

    # -- мазки --------------------------------------------------------------
    def layer_target(self, layer_id):
        """В какой буфер TD попадёт мазок, адресованный этому слою.

        Слой 1 (краска) — буфер краски. Слой 2 — маска, и туда же идут мазки,
        адресованные слою 0 («Источник»): в интерфейсе маска — это часть слоя
        источника, отдельного переключателя режима больше нет.
        """
        try:
            lid = int(layer_id)
        except Exception:
            return 'paint'
        return 'mask' if lid in (0, 2) else 'paint'

    def _points(self, data):
        if len(data) < 9:
            return
        opcode, sid, flags, layer, count = struct.unpack_from('<BIBBH', data, 0)
        if opcode != 1:
            self.log('неизвестный бинарный пакет op=%d' % opcode)
            return
        st = self.strokes.get(sid)
        if st is None or (flags & 1):
            st = {'queue': [], 'last': None, 'dist': 0.0, 'bbox': None,
                  'layer': layer, 'target': self.layer_target(layer),
                  'eraser': str(self.tool.get('tool')) == 'eraser',
                  'end': False, 'cancel': False,
                  'snap': self.snapshot_start(self.layer_target(layer)),
                  'start_frame': self.frame,
                  'last_t': time.time()}
            self.strokes[sid] = st
        if flags & 4:
            st['cancel'] = True
            st['end'] = True
        if flags & 2:
            st['end'] = True
        if count <= 0:
            return                      # пустая пачка: важны только флаги (конец/отмена)
        st['last_t'] = time.time()
        t = self.tun
        W, H = float(t['w']), float(t['h'])
        off = 9
        n = len(data)
        for _ in range(count):
            if off + 8 > n:
                break
            x, y, pr, pflags = struct.unpack_from('<HHBB', data, off)
            off += 8
            if pflags & 1:
                st['last'] = None
                st['dist'] = 0.0
            st['queue'].append((x / 65535.0 * W, y / 65535.0 * H,
                                max(0.05, pr / 255.0 if pr else 0.5)))
        if len(st['queue']) > 20000:            # защита от лавины
            del st['queue'][:10000]

    def _make_dab(self, x, y, pressure):
        size = float(self.tool['size'])
        rad = 0.5 * size * (1.0 - PRESS_SIZE + PRESS_SIZE * pressure)
        r, g, b = self._rgb()
        flow = float(self.tool['flow']) * pressure
        return (x, y, rad, float(self.tool['hardness']), flow, r, g, b)

    def _rgb(self):
        c = str(self.tool.get('color') or '#ffffff').lstrip('#')
        if len(c) == 3:
            c = ''.join(ch * 2 for ch in c)
        try:
            return (int(c[0:2], 16) / 255.0, int(c[2:4], 16) / 255.0, int(c[4:6], 16) / 255.0)
        except Exception:
            return (1.0, 1.0, 1.0)

    def _build_dabs(self):
        """Точки мазков -> штампы с равномерным шагом по длине пути.

        Штампы раскладываются по двум пачкам: краска и маска. В один кадр кисть
        рисует только из одной пачки (текстура штампов одна на обе), поэтому
        смешивать их нельзя — вторая доедет следующим кадром.
        """
        W, H = float(self.tun['w']), float(self.tun['h'])
        spacing_px = max(1.0, float(self.tool['size']) * float(self.tool['spacing']))
        # Бюджет учитывает и штампы этого кадра, и уже накопленные пачки: иначе
        # пачка переполнит текстуру, и часть штампов потеряется.
        used = (len(self.frame_dabs) + len(self.frame_dabs_m)
                + len(self.dab_pending['paint']) + len(self.dab_pending['mask']))
        budget = MAXDABS - used
        if budget <= 0:
            return
        for sid, st in self.strokes.items():
            tgt = 'mask' if st.get('target') == 'mask' else 'paint'
            # Режим кисти для этой пачки. Раньше он НЕ проставлялся, и ластик
            # работал как кисть: значение по умолчанию для слоя краски — 0
            # («краска»), для маски — 1 («проявлять»). Отсюда «ластик красит
            # вместо стирания», а на слое «Источник» — «вообще ничего не делает».
            self.dab_pending_mode[tgt] = (2.0 if st.get('eraser')
                                          else (1.0 if tgt == 'mask' else 0.0))
            q = st['queue']
            i = 0
            while i < len(q) and budget > 0:
                x, y, pr = q[i]
                last = st['last']
                if last is None:
                    pt = self._make_dab(x, y, pr)
                    self._add_dab(tgt, pt)
                    self._grow_bbox(st, pt)
                    st['last'] = (x, y, pr)
                    st['dist'] = 0.0
                    budget -= 1
                    i += 1
                    continue
                lx, ly, lp = last
                seg = math.hypot(x - lx, y - ly)
                if seg < 1e-4:
                    i += 1
                    continue
                pos = spacing_px - st['dist']
                placed = None
                while pos <= seg and budget > 0:
                    f = pos / seg
                    ix = lx + (x - lx) * f
                    iy = ly + (y - ly) * f
                    ipr = lp + (pr - lp) * f
                    pt = self._make_dab(ix, iy, ipr)
                    self._add_dab(tgt, pt)
                    self._grow_bbox(st, pt)
                    placed = pos
                    budget -= 1
                    pos += spacing_px
                if budget <= 0 and pos <= seg:
                    # бюджет кадра кончился посреди сегмента: продолжаем его в следующем
                    # кадре от последнего поставленного штампа (точку не съедаем)
                    if placed is not None:
                        f = placed / seg
                        st['last'] = (lx + (x - lx) * f, ly + (y - ly) * f,
                                      lp + (pr - lp) * f)
                        st['dist'] = 0.0
                    break
                st['dist'] = seg - placed if placed is not None else st['dist'] + seg
                st['last'] = (x, y, pr)
                i += 1
            if i:
                del q[:i]
        self.diag['dabs'] = len(self.frame_dabs) + len(self.frame_dabs_m)

    def _add_dab(self, target, pt):
        if target == 'mask':
            self.frame_dabs_m.append(pt)
        else:
            self.frame_dabs.append(pt)
        x, y, rad = pt[0], pt[1], pt[2]
        r = (x - rad - 2.0, y - rad - 2.0, x + rad + 2.0, y + rad + 2.0)
        if target == 'mask':
            self.frame_rect_m = _union(self.frame_rect_m, r)
        else:
            self.frame_rect = _union(self.frame_rect, r)

    def _grow_rect(self, pt):
        """Область штампов (для uRect и области патча)."""
        x, y, rad = pt[0], pt[1], pt[2]
        r = (x - rad - 2.0, y - rad - 2.0, x + rad + 2.0, y + rad + 2.0)
        self.frame_rect = _union(self.frame_rect, r)

    def _grow_bbox(self, st, pt):
        x, y, rad = pt[0], pt[1], pt[2]
        r = (x - rad - 2.0, y - rad - 2.0, x + rad + 2.0, y + rad + 2.0)
        st['bbox'] = _union(st['bbox'], r)

    # -- undo / redo --------------------------------------------------------
    def _crop_for(self, layer):
        """Кроп, из которого снимается слой для undo: краска или маска."""
        return 'cropsnapm' if layer == 'mask' else 'cropsnap'

    def _capture(self, crop_rel, rect, tag):
        c = self.o(crop_rel)
        self._set_crop(c, rect)
        c.cook(force=True)
        d = self.dirs()
        if not os.path.isdir(d['tmp']):
            os.makedirs(d['tmp'])
        self.seq += 1
        path = os.path.join(d['tmp'], 'pw_%s_%04d.png' % (tag, self.seq))
        c.save(path)
        return path

    def check_crop(self, force=False):
        """Один раз проверить, что кроп вырезает именно запрошенную область.

        Проверяем на заведомо НЕПОЛНОМ прямоугольнике: при путанице «положение
        краёв» ↔ «сколько отрезать» и при неприменившихся единицах измерения он
        получается другого размера (в живом TD выходило 1x1 вместо 645x52).
        """
        if not force and self.diag.get('crop_test'):
            return True
        crop = self.try_o('crop')
        if crop is None or not self.tun:
            return False
        W, H = int(self.tun['w']), int(self.tun['h'])
        l, tp, w, h = 37, 29, min(211, W - 37), min(143, H - 29)
        try:
            self._set_crop(crop, (l, tp, l + w, tp + h))
        except Exception:
            self.diag['crop_test'] = 'ошибка: %s' % traceback.format_exc().splitlines()[-1]
            return False
        got = self._crop_out_size(crop)
        if got is None:
            self.diag['crop_test'] = 'размер не читается'
            return True
        if abs(got[0] - w) <= 1 and abs(got[1] - h) <= 1:
            self.diag['crop_test'] = 'ок: просили %dx%d, получили %dx%d' % (w, h, got[0], got[1])
            return True
        self.diag['crop_test'] = ('НЕ ВЕРНО: просили %dx%d, получили %dx%d в точке (%d,%d)'
                                  % (w, h, got[0], got[1], l, tp))
        self.err('crop-test', self.diag['crop_test'])
        return False

    def _crop_probe_png(self, path):
        """Эталонная картинка 64x64: четыре квадранта разных цветов.

        По ней видно не только размер области, но и КУДА она берётся: если
        квадранты приходят не на своих местах, кроп зеркалит по вертикали или
        горизонтали, и патч «вставляется не туда» (а полный кадр при этом
        корректен — поэтому картинка приходила в норму после перезагрузки).
        """
        cw, ch = 64, 64
        data = bytearray()
        for y in range(ch):
            for x in range(cw):
                if y < ch // 2:
                    c = (255, 0, 0, 255) if x < cw // 2 else (0, 255, 0, 255)
                else:
                    c = (0, 0, 255, 255) if x < cw // 2 else (255, 255, 255, 255)
                data += bytes(c)
        _write_png_rgba8(path, cw, ch, bytes(data))
        return cw, ch

    def _crop_probe(self, crop, rect, img_w, img_h):
        """Выставить кроп и вернуть цвет пикселя — с учётом ТЕКУЩИХ поправок.

        Размеры берём от картинки-эталона, а не от полотна: вход кропа — она.
        """
        W, H = int(img_w), int(img_h)
        l, tp, w, h = _rect_int(rect, W, H)
        left, right = float(l), float(l + w)
        top, bottom = float(H - tp), float(H - (tp + h))
        if getattr(self, 'crop_flip_v', False):
            top, bottom = bottom, top
        if getattr(self, 'crop_flip_h', False):
            left, right = right, left
        try:
            for parname in ('cropleftunit', 'croprightunit', 'croptopunit',
                            'cropbottomunit'):
                set_menu(crop.par[parname], 'pixels', 'pixel', 'native')
            crop.par.cropleft = left
            crop.par.cropright = right
            crop.par.croptop = top
            crop.par.cropbottom = bottom
            crop.cook(force=True)
        except Exception:
            return None
        try:
            return crop.sample(x=max(0, w // 2), y=max(0, h // 2))
        except Exception:
            return None

    def _crop_quadrants(self, crop, cw, ch):
        """Какие цвета вернулись из четырёх квадрантов эталона."""
        got = []
        for (qx, qy) in ((0, 0), (cw // 2, 0), (0, ch // 2), (cw // 2, ch // 2)):
            s = self._crop_probe(crop, (qx, qy, cw // 2, ch // 2), cw, ch)
            got.append(tuple(self._b255(v) for v in s[:3]) if s else None)

        def dom(c):
            if c is None:
                return '?'
            r, g, b = c
            if r > 200 and g > 200 and b > 200:
                return 'white'
            if r > g and r > b:
                return 'red'
            if g > r and g > b:
                return 'green'
            if b > r and b > g and b > 40:
                return 'blue'
            return 'dark'
        return [dom(c) for c in got]

    # как должны прийти квадранты эталона: слева-сверху, справа-сверху,
    # слева-снизу, справа-снизу
    CROP_QUADRANTS = ['red', 'green', 'blue', 'white']

    def check_crop_orientation(self, force=False):
        """Проверить, КУДА кроп реально берёт область, и починить зеркало.

        Зачем: зеркальная область имеет ровно тот же размер, поэтому проверка
        размера такое не ловит, а снаружи это выглядит как «патч вставляется не
        туда» — при этом полный кадр (после перезагрузки страницы) корректен,
        потому что он берёт весь слой целиком.

        Механика: подсовываем во вход кропа картинку из четырёх квадрантов и
        перебираем четыре варианта ориентации, пока квадранты не встанут на свои
        места. Найденный вариант запоминается и применяется ко всем кропам.
        """
        if not force and self.diag.get('crop_orient'):
            return str(self.diag['crop_orient']).startswith('ок')
        crop = self.try_o('crop')
        mv = self.try_o('patchin')
        if crop is None or mv is None or not self.tun:
            return False
        dirs = self.dirs()
        if not dirs.get('tmp'):
            return False
        path = os.path.join(dirs['tmp'], 'crop_probe.png')
        try:
            cw, ch = self._crop_probe_png(path)
        except Exception:
            self.err('crop-orient', traceback.format_exc())
            return False
        prev_inputs = None
        try:
            prev_inputs = [x for x in crop.inputs if x is not None]
        except Exception:
            pass
        prev_file = ''
        try:
            prev_file = str(mv.par['file'].eval() or '')
        except Exception:
            pass
        want = list(self.CROP_QUADRANTS)
        found = None
        last = []
        try:
            mv.par.file = path
            mv.par.reloadpulse.pulse()
            try:
                mv.cook(force=True)
            except Exception:
                pass
            crop.setInputs([mv])
            for fv in (False, True):
                for fh in (False, True):
                    self.crop_flip_v, self.crop_flip_h = fv, fh
                    names = self._crop_quadrants(crop, cw, ch)
                    last = names
                    if names == want:
                        found = (fv, fh, names)
                        break
                if found:
                    break
        finally:
            try:
                if prev_file:
                    mv.par.file = prev_file
                    mv.par.reloadpulse.pulse()
            except Exception:
                pass
            try:
                crop.setInputs(prev_inputs or [self.o('buf')])
            except Exception:
                pass
        if found:
            fv, fh, names = found
            self.crop_flip_v, self.crop_flip_h = fv, fh
            self.diag['crop_probe'] = ','.join(names)
            if fv or fh:
                self.diag['crop_orient'] = ('ок, с поправкой: вертикаль=%s, '
                                            'горизонталь=%s' % (fv, fh))
                self.log('кроп брал область зеркально — поправил '
                         '(вертикаль=%s, горизонталь=%s)' % (fv, fh))
            else:
                self.diag['crop_orient'] = 'ок'
            return True
        self.crop_flip_v = False
        self.crop_flip_h = False
        self.diag['crop_probe'] = ','.join(last)
        self.diag['crop_orient'] = 'НЕ ВЫШЛО: квадранты пришли как %s (ждали %s)' % (
            ','.join(last), ','.join(want))
        self.err('crop-orient', self.diag['crop_orient'])
        return False

    def _crop_out_size(self, crop):
        try:
            return int(crop.width), int(crop.height)
        except Exception:
            return None

    def _check_crop_content(self, crop, l, tp, want):
        """Проверить, что вырезанная область — это ТОТ ЖЕ кусок слоя.

        Размер может совпасть, а содержимое приехать из другого места или из
        другого оператора: снаружи это выглядит как «в местах, где рисую, какие-то
        квадраты с картинками». Сравниваем пиксель из центра области с пикселем
        из слоя по тем же координатам (в осях TD: по вертикали отсчёт снизу).
        """
        buf = self.try_o('buf')
        if buf is None or not want or want[0] < 2 or want[1] < 2:
            return
        # Сверяем только с тем кропом, который смотрит в сам слой: у снимков undo
        # вход — замороженный snap, и содержимое там заведомо другое.
        try:
            src = [x for x in crop.inputs if x is not None]
        except Exception:
            return
        if not src or src[0].path != buf.path:
            return
        H = int(self.tun['h'])
        # берём несколько точек: одна может попасть в пустое место и ничего не
        # покажет, а мазок в области всё равно есть
        pts = [(want[0] // 2, want[1] // 2),
               (want[0] // 4, want[1] // 4),
               (want[0] * 3 // 4, want[1] // 2),
               (want[0] // 2, want[1] * 3 // 4),
               (want[0] * 3 // 4, want[1] * 3 // 4)]
        worst = 0.0
        worst_pair = None
        busy = False
        for cx, cy in pts:
            try:
                got = crop.sample(x=cx, y=cy)
                want_px = buf.sample(x=l + cx, y=H - (tp + want[1]) + cy)
            except Exception:
                return
            if max(float(v) for v in want_px) > 0.02:
                busy = True
            diff = max(abs(float(got[i]) - float(want_px[i])) for i in range(4))
            if diff > worst:
                worst = diff
                worst_pair = (tuple(round(float(v), 3) for v in got),
                              tuple(round(float(v), 3) for v in want_px))
        if worst <= 0.01:
            self.diag['crop_content'] = 'ок' if busy else 'ок (область пустая)'
            return
        self.diag['crop_content'] = ('РАСХОЖДЕНИЕ %.3f: в области %s, в слое %s'
                                     % (worst, worst_pair[0], worst_pair[1]))
        self.err('crop-content', 'кроп берёт не тот кусок: ' + self.diag['crop_content'])

    def _crop_real_units(self, crop):
        """Какие единицы РЕАЛЬНО в силе у кропа — по каждой оси отдельно.

        В живом TD бывает так, что «pixels» принимается для левого/правого края,
        а для верхнего/нижнего остаётся «fraction» (тогда значения в пикселях
        читаются как доли, зажимаются в 1.0, и область выходит пустой — высота
        1 пиксель). Поэтому не надеемся на то, что мы выставили, а СПРАШИВАЕМ
        параметр и считаем значения в тех единицах, которые он реально отдаёт.
        """
        def tok(name):
            try:
                v = str(crop.par[name].eval()).lower()
            except Exception:
                v = 'fraction'
            return 'pixels' if v.startswith(('pix', 'nat')) else 'fraction'
        return tok('cropleftunit'), tok('croptopunit')

    def _set_crop(self, crop, rect):
        """Вырезать ровно заданный прямоугольник — и ПРОВЕРИТЬ, что вышло.

        Подводные камни cropTOP, из-за которых в браузер уходили пустые патчи:
        1. Параметры задают ПОЛОЖЕНИЕ краёв изображения, а не «сколько отрезать»
           (документация Crop TOP: «Crop Right — Positions the right edge of the
           image»). То есть cropright — координата правого края (l + w), а не
           W - (l + w). Со «отрезанием» область выходила пустой: right = left = 0,
           и TD отдавал картинку 1x1.
        2. Единицы измерения задаются четырьмя отдельными параметрами, и они
           применяются не обязательно все: тогда значения в пикселях читаются как
           доли, зажимаются в 1.0 и линия превращается в полоску в 1 пиксель.
           Поэтому значения считаем в ТЕХ единицах, которые параметр реально
           отдаёт (см. _crop_real_units).

        После установки проверяем ФАКТИЧЕСКИЙ размер выхода: не совпал — пробуем
        другой режим единиц и пишем в отчёт, что сработало.
        """
        t = self.tun
        W, H = int(t['w']), int(t['h'])
        # Защита от путаницы форматов. Раньше сюда однажды приехало (x, y, w, h)
        # вместо коробки: правый край оказался равен ширине, область сжалась до
        # одного пикселя, и браузер растягивал его на весь прямоугольник —
        # «кривые квадраты» вместо мазка. Такая коробка вывернута наизнанку
        # (правый край левее левого), и это надо видеть в отчёте, а не гадать.
        if rect is not None and (rect[2] < rect[0] or rect[3] < rect[1]):
            l0, t0, r0, b0 = (float(rect[0]), float(rect[1]),
                              float(rect[2]), float(rect[3]))
            if r0 > 0 and b0 > 0:
                self.err('crop-rect',
                         'область передана как (x, y, w, h), а нужна коробка '
                         '(l, t, r, b): получил %s — читаю как x,y,w,h'
                         % ((l0, t0, r0, b0),))
                rect = (l0, t0, l0 + r0, t0 + b0)
        l, tp, w, h = _rect_int(rect, W, H)
        # Поправки ориентации (их ставит check_crop_orientation, если сборка TD
        # берёт область зеркально): зеркалим САМ прямоугольник. Простая
        # перестановка краёв местами делает область пустой — TD отдаёт 1 пиксель.
        if getattr(self, 'crop_flip_v', False):
            tp = H - (tp + h)
        if getattr(self, 'crop_flip_h', False):
            l = W - (l + w)
        want = (w, h)
        bad = []
        for units, tokens in (('pixels', ('pixels', 'pixel', 'native')),
                              ('fraction', ('fraction', 'relative', 'frac', '1'))):
            for parname in ('cropleftunit', 'croprightunit', 'croptopunit',
                            'cropbottomunit'):
                try:
                    set_menu(crop.par[parname], *tokens)
                except Exception:
                    pass
            # считаем по фактическим единицам каждой оси, а не по тем, что просили
            ux, uy = self._crop_real_units(crop)
            if ux == 'pixels':
                vals = [float(l), float(l + w)]
            else:
                vals = [l / float(W), (l + w) / float(W)]
            # По вертикали TD работает в своей оси (v = 0 внизу): croptop — это
            # ВЕРХНИЙ край, cropbottom — НИЖНИЙ, и оба отсчитываются снизу.
            # Поэтому верхний край = H - tp, а нижний = H - (tp + h). Если задать
            # их «как на экране» (сверху вниз), область выходит пустой — TD
            # отдаёт картинку высотой 1 пиксель.
            if uy == 'pixels':
                vals += [float(H - tp), float(H - (tp + h))]
            else:
                vals += [(H - tp) / float(H), (H - (tp + h)) / float(H)]
            try:
                crop.par.cropleft = vals[0]
                crop.par.cropright = vals[1]
                crop.par.croptop = vals[2]
                crop.par.cropbottom = vals[3]
            except Exception:
                self.err('crop-pars', traceback.format_exc())
                continue
            try:
                crop.cook(force=True)
            except Exception:
                pass
            got = self._crop_out_size(crop)
            if got is None:
                self.diag['crop_ok'] = units + '?'
                return want                     # размер не читается — доверяем
            name = '%s (оси: %s/%s)' % (units, ux, uy)
            if abs(got[0] - want[0]) <= 1 and abs(got[1] - want[1]) <= 1:
                self.diag['crop_ok'] = name
                self.diag['crop_last'] = 'rect=%s -> %dx%d' % (want, got[0], got[1])
                self._check_crop_content(crop, l, tp, want)
                if units != 'pixels' or ux != 'pixels' or uy != 'pixels':
                    if self.diag.get('crop_warned') != name:
                        self.diag['crop_warned'] = name
                        self.log('кроп вырезает верно в режиме «%s»' % name)
                return want
            # не совпало: запоминаем всё, что нужно для разбора
            self.diag['crop_last'] = 'rect=%s -> %dx%d (%s, values=%s)' % (
                want, got[0], got[1], name, vals)
            bad.append('%s: получилось %dx%d вместо %dx%d'
                       % (name, got[0], got[1], want[0], want[1]))
        self.diag['crop_ok'] = 'НЕ ВЫШЛО'
        self.diag['crop_bad'] = ' | '.join(bad)
        self.err('crop-size', 'кроп не вырезает нужную область: ' + ' | '.join(bad))
        return want

    def _push(self, stack, rect, path, layer='paint'):
        stack.append({'rect': rect, 'path': path, 'layer': layer})
        keep = int(self.tun.get('undodepth') or 24)
        while len(stack) > max(1, keep):
            old = stack.pop(0)
            self._forget(old)

    def _forget(self, entry):
        try:
            p = entry.get('path')
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    def _cmd_clear(self):
        rect = (0, 0, self.tun['w'], self.tun['h'])
        try:
            path = self._capture('cropundo', rect, 'clear')
            self._push(self.undo, rect, path)
            self.redo = []
        except Exception:
            self.err('clear-snap', traceback.format_exc())
        self.clear_frames = 1
        # Накопленные, но ещё не нарисованные штампы сбрасываем: иначе после
        # очистки они доедут до слоя и «вернут» стёртый мазок. Маску не трогаем —
        # очистка относится к слою краски.
        self.dab_pending['paint'] = []
        self.dab_pending_rect['paint'] = None
        if self.dab_ready and self.dab_ready[0] == 'paint':
            self.dab_ready = None
            self.dab_ready_rect = None
        self.paint_dirty = False
        self.send_rect = _union(self.send_rect, rect)
        self.force_patch = True
        self._broadcast({'t': 'history', **self.history()})
        # Именно notice: это НЕ ошибка, а сообщение о выполненном действии.
        # Раньше уходило {'t': 'error'}, и клиент показывал «Ошибка сервера:
        # Слой краски очищен» — пугало на ровном месте.
        self._broadcast({'t': 'notice', 'msg': 'Слой краски очищен'})
        self.log('очистка слоя краски')

    def _cmd_history(self, direction):
        if direction < 0:
            src, dst, tag = self.undo, self.redo, 'undo'
        else:
            src, dst, tag = self.redo, self.undo, 'redo'
        if not src:
            self._broadcast({'t': 'history', **self.history()})
            return
        entry = src.pop()
        layer = entry.get('layer', 'paint')
        try:
            cur = self._capture('cropundo', entry['rect'],
                                'redo' if tag == 'undo' else 'undo')
            dst.append({'rect': entry['rect'], 'path': cur, 'layer': layer})
        except Exception:
            self.err('history-snap', traceback.format_exc())
        self._load_patch(entry['path'], layer)
        self.restore_req = {'rect': entry['rect'], 'path': entry['path'],
                            'frame': self.frame, 'layer': layer}
        self._broadcast({'t': 'history', **self.history()})
        self.log('%s: область %s (%s)' % (tag, entry['rect'],
                                          'маска' if layer == 'mask' else 'краска'))

    def _finish_strokes(self):
        """Завершение/отмена мазков: снимок региона в стек undo."""
        # Штампы доезжают до слоя со задержкой в кадр-два. Если закрыть мазок
        # раньше, снимок области и последний патч в браузер уедут БЕЗ хвоста
        # мазка — а после undo/redo хвост пропадёт насовсем.
        if (self.dab_pending['paint'] or self.dab_pending['mask']
                or self.dab_ready):
            return []
        done = []
        now = time.time()
        for sid, st in list(self.strokes.items()):
            if not st.get('end'):
                # страховка: клиент замолчал, значит мазок давно закончился
                if now - st.get('last_t', now) > 10.0:
                    st['end'] = True
                    self.log('мазок %s закрыт по тишине (клиент не прислал конец)' % sid)
                else:
                    continue
            if st['queue']:
                continue
            bbox = st.get('bbox')
            layer = 'mask' if st.get('target') == 'mask' else 'paint'
            crop_rel = self._crop_for(layer)
            done.append(sid)
            if bbox is None:
                continue
            if st.get('cancel'):
                try:
                    path = self._capture(crop_rel, bbox, 'cancel')
                    self._load_patch(path, layer)
                    self.restore_req = {'rect': bbox, 'path': path,
                                        'frame': self.frame, 'layer': layer}
                    self.log('мазок отменён, область %s' % (bbox,))
                except Exception:
                    self.err('cancel', traceback.format_exc())
            elif st.get('snap'):
                try:
                    path = self._capture(crop_rel, bbox, 'stroke')
                    self._push(self.undo, bbox, path, layer)
                    self.redo = []
                except Exception:
                    self.err('stroke-snap', traceback.format_exc())
                self.paint_dirty = True
                if layer == 'mask':
                    self.send_rect_m = _union(self.send_rect_m, bbox)
                else:
                    self.send_rect = _union(self.send_rect, bbox)
                self._broadcast({'t': 'history', **self.history()})
            else:
                self.log('мазок без своего снимка (рисовали в два потока) — undo пропущен')
        for sid in done:
            self.strokes.pop(sid, None)
        if not self.strokes:
            self.snap_depth = 0
            self._unlock_snap()
        return done

    def _unlock_snap(self):
        s = self.try_o('snap')
        if s is not None:
            try:
                s.lock = False
            except Exception:
                pass

    # -- кадр ---------------------------------------------------------------
    def _ensure_state(self):
        """Дотянуть поля состояния, если рантайм живёт с прошлой версией кода.

        Колбэки, кадровый цикл и HTTP держат ОДИН объект рантайма (так и задумано,
        см. pw_boot). Но автопересборка меняет только текст DAT: код становится
        новым, а поля уже созданного объекта остаются прежними. Так `dab_pending`
        из списка не превращался в словарь, и каждый кадр падал с
        «list indices must be integers, not str» — компонент выглядел мёртвым,
        хотя перезапуск TD всё чинил.

        Поэтому форму полей проверяем на каждом кадре (это чтение нескольких
        атрибутов) и при несовпадении приводим к нужной.
        """
        if not isinstance(getattr(self, 'dab_pending', None), dict):
            self.dab_pending = {'paint': [], 'mask': []}
            self.dab_pending_rect = {'paint': None, 'mask': None}
            self.dab_pending_mode = {'paint': 0.0, 'mask': 1.0}
            self.dab_ready = None
            self.dab_ready_rect = None
        if not isinstance(getattr(self, 'paint_rect', None), dict):
            self.paint_rect = {'paint': None, 'mask': None}
        for name, want in (('frame_dabs_m', list()), ('frame_rect_m', None),
                           ('send_rect_m', None), ('mask_filled', False),
                           ('switch_layer', 'paint')):
            cur = getattr(self, name, None)
            if name == 'mask_filled':
                if not isinstance(cur, bool):
                    setattr(self, name, False)
            elif name == 'switch_layer':
                if cur not in ('paint', 'mask'):
                    setattr(self, name, 'paint')
            elif isinstance(want, list):
                if not isinstance(cur, list):
                    setattr(self, name, [])
            elif not hasattr(self, name):
                setattr(self, name, want)
        if not isinstance(getattr(self, 'tun', None), dict):
            self.tun = dict(DEFAULT_TUN)
        for name, want in (('autostart_done', False), ('_fit_key', None)):
            if not hasattr(self, name):
                setattr(self, name, want)

    def _mask_fill_check(self):
        """Залить маску белым после (пере)сборки.

        Маска по умолчанию означает «источник виден целиком», то есть она должна
        быть залита. Пересборка пересоздаёт буферы, а признак «уже залито» живёт
        в объекте рантайма и переживает её — поэтому после пересборки маска
        оставалась пустой, а композит чёрным (в отчёте это выглядело как
        «композит ничего не отдаёт» и пустой `sync_mask_last.png`).

        Признак привязываем к отпечатку сборки из DAT `server/version`: он меняется
        ровно тогда, когда компонент пересобрали.
        """
        ver = self.try_o('server/version')
        sig = ''
        try:
            if ver is not None:
                for line in str(ver.text or '').splitlines():
                    line = line.strip()
                    if line and not line.startswith('#'):
                        sig = line
                        break
        except Exception:
            sig = ''
        if sig and sig != getattr(self, '_mask_sig', None):
            self._mask_sig = sig
            self.mask_filled = False
            self.diag['mask_fill'] = 'после сборки %s' % sig

    def on_frame_start(self, frame):
        self.frame = frame
        t0 = time.time()
        # Сначала — САМОВОССТАНОВЛЕНИЕ: проверка отпечатка файлов и, если надо,
        # пересборка. Раньше этот блок стоял после чтения параметров, и падение в
        # чтении (например NameError из файла, сохранённого на середине правки)
        # обрывало кадр до проверки отпечатка: компонент оставался сломанным
        # навсегда, хотя на диске уже лежал исправленный файл. Теперь автопочинка
        # работает независимо от остального кадра.
        try:
            self._ensure_state()
            self._mask_fill_check()
            if not getattr(self, '_web_done', False):
                # После сборки в компоненте лежат DAT-ы страницы: если файлов в
                # папке web нет (компонент вставили из .tox в другой проект),
                # раскладываем их из компонента.
                self._web_done = True
                self.materialize_web()
            if not self.autostart_done:
                self.autostart(reason='открытие проекта')
            self.watch_sources()
            self.open_button_check()
        except Exception:
            self.err('autostart', traceback.format_exc())
        try:
            self.read_pars()
            self._ensure_sizes()
            self._ensure_wiring()
            # разовая проверка кропа: от неё зависит, уедут ли в браузер патчи
            try:
                self.check_crop_orientation()
                self.check_crop()
            except Exception:
                self.err('crop-test', traceback.format_exc())
            self._apply_source()
            self._drain()
            restoring = self.switch_until >= frame
            if not restoring:
                self._build_dabs()
            self._apply_restore(frame)
            self._apply_uniforms()
            self._apply_compose()
        except Exception:
            self.err('frameStart', traceback.format_exc())
        self.diag['ms_start'] = (time.time() - t0) * 1000.0

    def on_frame_end(self, frame):
        t0 = time.time()
        try:
            ended = self._finish_strokes()
            # Изменённое в этом кадре забираем в область патча: рисование должно
            # доезжать до браузера кадр за кадром, а не одним куском в конце мазка.
            for key, attr in (('paint', 'send_rect'), ('mask', 'send_rect_m')):
                box = self.paint_rect[key]
                if box:
                    setattr(self, attr, _union(getattr(self, attr), box))
                    self.paint_rect[key] = None
            # Режим отправки: патчи областями (по умолчанию) или целый кадр.
            if self.tun.get('patchmode') == 'fullframe':
                self._flush_fullframe()
                self._flush_mask_patches()      # маска едет патчами всегда
            else:
                self._flush_patches(force_final=bool(ended))
            self._flush_proxy()
            self._flush_sync()
            self._drop_stale_clients()
            self._flush_log()
            # один раз через пару секунд после старта: к этому моменту шейдеры
            # уже скомпилированы, и отчёт на диске сразу показывает проблемы
            if not self.report_done and (time.time() - self.t0) > 2.0:
                self.report_done = True
                self._rep_t = time.time()
                self.write_report('boot')
            # Дальше обновляем отчёт раз в 5 секунд. Иначе на диске остаётся
            # снимок момента запуска, и по нему не видно, что происходило при
            # рисовании (сколько штампов, сколько патчей, какие ошибки).
            elif (time.time() - self._rep_t) > 5.0:
                self._rep_t = time.time()
                self.write_report('live', quiet=True)
        except Exception:
            self.err('frameEnd', traceback.format_exc())
        self.frame_dabs = []
        self.frame_rect = None
        self.frame_dabs_m = []
        self.frame_rect_m = None
        self.clear_frames = 0
        if self.switch_until >= frame:
            self.switch_until = -1
            sw = self.try_o('swm' if getattr(self, 'switch_layer', 'paint') == 'mask'
                            else 'sw')
            if sw is not None:
                try:
                    sw.par.index = 0
                except Exception:
                    pass
        self.diag['ms_end'] = (time.time() - t0) * 1000.0
        self.diag['ms'] = self.diag['ms_start'] + self.diag['ms_end']

    # -- размеры и источник --------------------------------------------------
    def _ensure_wiring(self):
        """Проверить и, если надо, восстановить петлю обратной связи.

        Зачем в рантайме, а не только в сборке: в живом TD связи, поставленные
        через `inputConnectors[i].connect()`, молча не вставали — у шейдера
        кисти не было ни предыдущего кадра, ни текстуры штампов, поэтому слой
        оставался пустым: в браузере мазок рисовался и исчезал при отпускании
        кнопки (патч с сервера приходил пустым), а из base не выходило ничего.

        Здесь мы не полагаемся на то, что сборка когда-то всё поставила: каждый
        кадр (дёшево — это чтение пары параметров) проверяем, что
        feedbackTOP берёт `paint/buf` и входом, и параметром Target TOP.
        Оба варианта означают одно и то же (предыдущий кадр слоя), но разные
        сборки TD используют то один, то другой механизм.
        """
        for fb_rel, holder_rel in (('fb', 'buf'), ('fbm', 'bufm')):
            self._ensure_loop(fb_rel, holder_rel)

    def _ensure_loop(self, fb_rel, holder_rel):
        """Проверить и, если надо, восстановить одну петлю обратной связи.

        Их две: слой краски (fb -> buf) и слой маски (fbm -> bufm). Правило
        одинаковое для обеих, поэтому проверка вынесена в один метод.
        """
        fb = self.try_o(fb_rel)
        buf = self.try_o(holder_rel)
        if fb is None or buf is None:
            return
        # 1) вход
        try:
            got = [x for x in fb.inputs if x is not None]
        except Exception:
            got = None
        if got is not None and not got:
            try:
                fb.setInputs([buf])
                self.diag['wire_input_' + fb_rel] = 'restored'
                self.log('восстановил вход %s <- %s' % (fb_rel, holder_rel))
            except Exception:
                self.err('wire-input', traceback.format_exc())
        # 2) параметр Target TOP
        par = None
        try:
            par = fb.par['top']
        except Exception:
            par = None
        if par is not None:
            try:
                cur = str(par.eval() or '')
            except Exception:
                cur = ''
            if cur.strip() != buf.path:
                try:
                    par.val = buf.path
                    self.diag['wire_top_' + fb_rel] = 'restored'
                    self.log('восстановил %s.top = %s' % (fb_rel, holder_rel))
                except Exception:
                    self.err('wire-top', traceback.format_exc())

    def _ensure_sizes(self):
        t = self.tun
        size = (int(t['w']), int(t['h']))
        if size == self.sizes:
            return
        self.sizes = size
        W, H = size
        for rel in ('brush', 'restore', 'buf', 'fit',
                    'brushm', 'restorem', 'bufm', 'maskapply',
                    'over'):
            o = self.try_o(rel)
            if o is None:
                continue
            try:
                set_menu(o.par.outputresolution, 'custom', 'customresolution')
                o.par.resolutionw = W
                o.par.resolutionh = H
            except Exception:
                self.err('size:' + rel, traceback.format_exc())
        px = self.try_o('proxy')
        if px is not None:
            try:
                set_menu(px.par.outputresolution, 'custom', 'customresolution')
                px.par.resolutionw = max(64, W // 2)
                px.par.resolutionh = max(36, H // 2)
            except Exception:
                self.err('size:proxy', traceback.format_exc())
        self.log('полотно %dx%d' % (W, H))

    def fit_name(self):
        """Имя текущего режима вставки источника (для клиента и отчёта)."""
        idx = int(self.tun.get('fitmode') or 0)
        idx = max(0, min(len(FIT_MODES) - 1, idx))
        return FIT_MODES[idx][0]

    def set_fit(self, name):
        """Сменить режим вставки источника по имени (команда со страницы)."""
        for i, (key, _label, _tokens) in enumerate(FIT_MODES):
            if key == str(name):
                try:
                    _OP(self.base).par.Fitmode = i
                except Exception:
                    self.err('fit-par', traceback.format_exc())
                    return False
                self.tun['fitmode'] = i
                self._apply_fit(force=True)
                # Картинка источника изменилась — значит клиентам нужен новый
                # постер, иначе на странице останется прежняя вставка до
                # перезагрузки (ровно так же было со сменой источника).
                with self.mu:
                    for st in self.clients.values():
                        st['need_poster'] = True
                self.next_proxy_t = 0.0
                self.log('вставка источника: %s' % FIT_MODES[i][1])
                return True
        return False

    def _apply_fit(self, force=False):
        """Выставить fitTOP режим вписывания источника в полотно.

        Режим приходит числом из своего параметра (меню-параметры в этой сборке
        TD себя не подтвердили), а здесь превращается в пункт меню fitTOP. Если
        сборка назовёт пункты иначе, значение ищется по подписи, а что реально
        встало — видно в отчёте (`fit_mode`).
        """
        node = self.try_o('fit')
        if node is None:
            return
        idx = int(self.tun.get('fitmode') or 0)
        idx = max(0, min(len(FIT_MODES) - 1, idx))
        key, _label, tokens = FIT_MODES[idx]
        if not force and getattr(self, '_fit_key', None) == key:
            return
        picked = self._set_menu_par(node, 'fit', tokens)
        self._fit_key = key
        self.diag['fit_mode'] = '%s (%s)' % (picked or '?', key)
        if picked is None:
            self.err('fit-mode', 'fitTOP не принял режим «%s» (пробовал %s)'
                     % (key, list(tokens)))
        else:
            # Положение внутри кадра — по центру: иначе «как есть» и «по ширине»
            # прилипали бы к левому верхнему углу, и это выглядело бы как сдвиг.
            for par, tokens2 in (('justifyh', ('center', 'centered', 'middle')),
                                 ('justifyv', ('center', 'centered', 'middle'))):
                self._set_menu_par(node, par, tokens2)

    def _apply_source(self):
        t = self.tun
        self._apply_fit()
        src = t['srcfile']
        # Внешний источник — это selectTOP `ext` с путём в параметре Top. Именно
        # параметр, а не провод: TD не соединяет операторы из разных компонентов,
        # а путь работает из любой сети.
        extpath = str(t.get('externalsrc') or '').strip()
        ext = self.try_o('ext')
        if ext is not None and extpath != getattr(self, '_ext_path', None):
            self._ext_path = extpath
            try:
                ext.par.top = extpath
                self.log('внешний источник: %s' % (extpath or 'нет'))
            except Exception:
                self.err('ext-top', traceback.format_exc())
        # выбрать внешний источник или файл
        sel = self.try_o('src')
        if sel is not None:
            # switchTOP: 0 — файл/фильм, 1 — внешний TOP (Externalsrc)
            want = 1 if (t['useext'] and extpath) else 0
            try:
                if int(float(sel.par.index.eval())) != want:
                    sel.par.index = want
            except Exception:
                self.err('src-select', traceback.format_exc())
        if src != self.src_path:
            ext = os.path.splitext(src)[1].lower()
            self.src_kind = 'video' if ext in VID_EXT else 'image'
            if src and os.path.exists(src):
                mv = self.try_o('movie')
                if mv is not None:
                    try:
                        mv.par.file = src
                        mv.par.reloadpulse.pulse()
                        mv.par.play = 1 if self.src_kind == 'video' else 0
                    except Exception:
                        self.err('src-load', traceback.format_exc())
            else:
                self.src_kind = 'image'
            self.src_path = src
            with self.mu:
                for st in self.clients.values():
                    st['need_poster'] = True
            self.next_proxy_t = 0.0
            self._broadcast({'t': 'layer', 'op': 'prop', 'id': 0, 'layer': self.layers()[0]})
            self.log('источник: %s (%s)' % (os.path.basename(src), self.src_kind))

    # -- uniform-ы -----------------------------------------------------------
    def _vec(self, top, idx, name, values):
        try:
            p = top.par
            p['vec%dname' % idx] = name
            for comp, v in zip('xyzw', values):
                p['vec%dvalue%s' % (idx, comp)] = float(v)
            return True
        except Exception:
            self.err('vec%d:%s' % (idx, top.name), traceback.format_exc())
            return False

    def _apply_uniforms(self):
        t = self.tun
        W, H = float(t['w']), float(t['h'])
        br = self.try_o('brush')
        if br is None:
            return
        r, g, b = self._rgb()
        hsign = -1.0 if t.get('flipy') else 1.0
        clear = 1.0 if self.clear_frames else 0.0

        # --- доставка штампов: запись в текстуру и рисование в РАЗНЫХ кадрах ---
        if self.frame_dabs:
            self.dab_pending['paint'].extend(self.frame_dabs)
            self.dab_pending_rect['paint'] = _union(self.dab_pending_rect['paint'],
                                                   self.frame_rect)
        if self.frame_dabs_m:
            self.dab_pending['mask'].extend(self.frame_dabs_m)
            self.dab_pending_rect['mask'] = _union(self.dab_pending_rect['mask'],
                                                  self.frame_rect_m)

        target, n, mode, rect_box = None, 0, 0.0, None
        if self.switch_until >= self.frame:
            # Кадр показывает восстановление области (undo/отмена): выход кисти в
            # слой не попадает. Готовую пачку в таком кадре не расходуем, иначе
            # штампы сгорят впустую — нарисуем их следующим кадром.
            pass
        elif self.dab_ready:
            # прошлый кадр записал пачку — текстура уже загружена, можно рисовать
            target, n, mode = self.dab_ready
            rect_box = self.dab_ready_rect
            self.dab_ready = None
            self.dab_ready_rect = None
        elif self.dab_pending['paint'] or self.dab_pending['mask']:
            # пачки нет — отправляем накопленное в текстуру; рисуем следующим кадром
            target = 'paint' if self.dab_pending['paint'] else 'mask'
            batch = self.dab_pending[target]
            box = self.dab_pending_rect[target]
            mode = self.dab_pending_mode[target]
            self.dab_pending[target] = []
            self.dab_pending_rect[target] = None
            self.push_dabs(batch)
            if self.diag.get('dab_png'):
                self.dab_ready = (target, len(batch), mode)
                self.dab_ready_rect = box
            target, n, mode = None, 0, 0.0
            rect_box = None

        self.diag['dab_count'] = n
        self.diag['dab_target'] = target or '-'
        # Какой режим реально ушёл в кисть в этом кадре (0 краска, 1 маска,
        # 2 ластик). Раньше этого поля не было, и «ластик рисует как кисть»
        # приходилось искать вслепую.
        self.diag['dab_mode'] = mode if target else 0.0
        self.diag['dab_pending'] = (len(self.dab_pending['paint'])
                                    + len(self.dab_pending['mask']))
        self.diag['dab_clear'] = clear
        # Что в этом кадре реально изменилось в слое — это и есть область для патча.
        # Без этого патчи уходили только по концу мазка: браузер показывал линию
        # лишь после отпускания, а ластик выглядел «не работающим до отпускания».
        if n > 0 and rect_box:
            layer_of = 'mask' if target == 'mask' else 'paint'
            self.paint_rect[layer_of] = _union(self.paint_rect[layer_of], rect_box)
            self.paint_dirty = True

        # Кисть рисует только одну пачку за кадр: вторая получает uCount = 0 и
        # просто копирует свой предыдущий кадр (шейдер так и устроен).
        n_paint = n if target == 'paint' else 0
        n_mask = n if target == 'mask' else 0
        self._vec(br, 0, 'uRes', (W, H * hsign, 1.0 / W, 1.0 / H))
        self._vec(br, 1, 'uCount', (n_paint,
                                    2.0 if (target == 'paint' and mode >= 1.5) else 0.0,
                                    clear, 1.0))
        self._vec(br, 2, 'uColor', (r, g, b, 1.0))
        self._vec(br, 3, 'uRect', self._rect_arg(rect_box))

        # Слой маски. Он всегда залит белым, пока маску не стирали: источник по
        # умолчанию виден целиком, а ластик по слою «Источник» его прячет.
        brm = self.try_o('brushm')
        if brm is not None:
            fill = 2.0 if not self.mask_filled else 0.0
            self._vec(brm, 0, 'uRes', (W, H * hsign, 1.0 / W, 1.0 / H))
            self._vec(brm, 1, 'uCount', (n_mask,
                                         2.0 if (target == 'mask' and mode >= 1.5) else 1.0,
                                         fill, 1.0))
            self._vec(brm, 2, 'uColor', (1.0, 1.0, 1.0, 1.0))
            self._vec(brm, 3, 'uRect',
                      self._rect_arg(rect_box if target == 'mask' else None))
            if fill > 0.5:
                self.mask_filled = True

    def _rect_arg(self, rect_box):
        """Коробка (l, t, r, b) -> (x, y, w, h) для uRect шейдера."""
        if not rect_box:
            return (-4.0, -4.0, 0.0, 0.0)
        l, tp = float(rect_box[0]), float(rect_box[1])
        rr, bb = float(rect_box[2]), float(rect_box[3])
        return (l, tp, max(0.0, rr - l), max(0.0, bb - tp))

    def _apply_compose(self):
        """Композит: прозрачность слоёв.

        Сам композит собран в сети нод: источник → maskapply (умножение на альфу
        маски) → over + слой краски. Здесь только множители прозрачности, которые
        приходят из браузера. Раньше тут ещё переключался режим «краска/маска» —
        теперь режим определяется выбранным слоем, а не общим тумблером.
        """
        t = self.tun
        src_mul = _clip(t['srcopacity'] * t['srcvisible'] * t.get('srcint', 1.0), 0.0, 1.0)
        paint_mul = _clip(t['paintopacity'] * t['paintvisible'] * t.get('paintint', 1.0),
                          0.0, 1.0)
        mask_mul = _clip(t.get('maskint', 1.0), 0.0, 1.0)
        color_k = _clip(t.get('colorint', 0.0), 0.0, 1.0)
        cr, cg, cb = kelvin_rgb(t.get('colortemp', 6500.0))
        key = (round(src_mul, 4), round(paint_mul, 4), round(mask_mul, 4),
               round(color_k, 4), round(cr, 3), round(cg, 3), round(cb, 3))
        if key == getattr(self, '_compose_key', None):
            return                            # параметры не дёргаем без изменений
        self._compose_key = key
        for rel, par, val in (('src_level', 'opacity', src_mul),
                              ('paint_level', 'opacity', paint_mul),
                              ('mask_level', 'opacity', mask_mul)):
            o = self.try_o(rel)
            if o is None:
                continue
            try:
                o.par[par] = val
            except Exception:
                self.err('compose:' + rel, traceback.format_exc())
        # Слой монотонного цвета: RGB — цвет по температуре, альфа — его
        # интенсивность (0 полностью выключает слой).
        o = self.try_o('color_src')
        if o is not None:
            for par, val in (('colorr', cr), ('colorg', cg), ('colorb', cb),
                             ('alpha', color_k)):
                try:
                    o.par[par] = val
                except Exception:
                    self.err('compose:color_src', traceback.format_exc())
                    break

    def _apply_restore(self, frame):
        """Включить восстановление области ровно на один кадр.

        Работает и для краски, и для маски: у каждой свой буфер, свой патч-вход и
        свой переключатель, поэтому запись в undo хранит, к какому слою относится.
        """
        req = self.restore_req
        layer = req.get('layer', 'paint') if req else 'paint'
        sw = self.try_o('swm' if layer == 'mask' else 'sw')
        if req is None or sw is None:
            return
        if frame <= req['frame']:
            return                                  # даём кадр на загрузку файла патча
        mv = self.try_o('patchinm' if layer == 'mask' else 'patchin')
        l, tp, w, h = _rect_int(req['rect'], int(self.tun['w']), int(self.tun['h']))
        if mv is not None:
            try:
                size_ok = int(mv.width) == w and int(mv.height) == h
            except Exception:
                size_ok = True
            if not size_ok:
                if frame > req['frame'] + 8:
                    # файл так и не загрузился: лучше пропустить undo, чем залить
                    # в слой картинку не того размера
                    self.err('restore', 'патч %s не загрузился (%sx%s вместо %sx%s)'
                             % (req['path'], getattr(mv, 'width', '?'),
                                getattr(mv, 'height', '?'), w, h))
                    self.restore_req = None
                    self._broadcast({'t': 'error',
                                     'msg': 'Не удалось восстановить область (файл патча)'})
                return
        r = self.try_o('restorem' if layer == 'mask' else 'restore')
        if r is not None:
            W = float(self.tun['w'])
            H = float(self.tun['h'])
            hsign = -1.0 if self.tun.get('flipy') else 1.0
            self._vec(r, 0, 'uRes', (W, H * hsign, 1.0 / W, 1.0 / H))
            self._vec(r, 1, 'uRect', (l, tp, w, h))
        try:
            sw.par.index = 1
        except Exception:
            self.err('sw', traceback.format_exc())
        self.switch_until = frame
        self.switch_layer = layer
        if layer == 'mask':
            self.send_rect_m = _union(self.send_rect_m, (l, tp, l + w, tp + h))
        else:
            self.send_rect = _union(self.send_rect, (l, tp, l + w, tp + h))
        self.force_patch = True
        self.restore_req = None

    def _load_patch(self, path, layer='paint'):
        mv = self.try_o('patchinm' if layer == 'mask' else 'patchin')
        if mv is None:
            return
        try:
            mv.par.file = path
            mv.par.reloadpulse.pulse()
        except Exception:
            self.err('patch-load', traceback.format_exc())

    # -- отправка клиентам ---------------------------------------------------
    def _encode_layer(self, rect=None, fmt='.png', dump=None, layer='paint'):
        """Снять область слоя (или весь слой) и вернуть готовые байты картинки.

        dump — путь, куда положить эти же байты для разбора. Нужен, потому что
        иначе содержимое патча нигде не увидеть: в браузере он может не
        появиться, а понять, что именно уехало (формат, альфа, где краска),
        можно только по файлу.
        """
        W, H = int(self.tun['w']), int(self.tun['h'])
        if rect is None:
            l, tp, w, h = 0, 0, W, H
        else:
            l, tp, w, h = _rect_int(rect, W, H)
        crop = self.o('cropm' if layer == 'mask' else 'crop')
        # В _set_crop прямоугольник — это КОРОБКА (l, t, r, b), а _rect_int выше
        # вернул (l, t, w, h). Раньше сюда уходило (l, tp, w, h) — и _set_crop
        # считал правый край равным ШИРИНЕ: область выходила размером (w - l,
        # h - tp), то есть в 1 пиксель при обычном патче. Браузер растягивал этот
        # пиксель на весь прямоугольник — те самые «кривые квадраты». Полный кадр
        # при этом работал, потому что там l = tp = 0 и ошибка не проявлялась.
        self._set_crop(crop, (l, tp, l + w, tp + h))
        crop.cook(force=True)
        # В браузер уходит STRAIGHT alpha: слой в TD премультиплицирован, а
        # drawImage домножил бы RGB на альфу второй раз (мягкие края мазка
        # темнели бы). Снимаем премульт отдельным шейдером — так картинка на
        # планшете совпадает с тем, что видно в TD. Для маски это не нужно: она
        # и так белая, а клиент берёт из неё только альфу.
        src = crop if layer == 'mask' else (self.try_o('unpremult') or crop)
        try:
            src.cook(force=True)
        except Exception:
            pass
        blob = bytes(src.saveByteArray(fmt))
        if dump and blob:
            try:
                folder = os.path.dirname(dump)
                if folder and not os.path.isdir(folder):
                    os.makedirs(folder)
                with open(dump, 'wb') as f:
                    f.write(blob)
            except Exception:
                # Молчаливое «не сохранилось» тут дорого стоит: по этому файлу
                # разбирают, что именно уехало в браузер.
                self.err('patch-dump', traceback.format_exc())
        return blob, (l, tp, w, h)

    def _flush_fullframe(self):
        """Режим «слать весь кадр»: вместо патчей — целый слой, но не чаще Fullfps.

        Зачем: у патчей есть слабое место — надо точно знать прямоугольник и
        попасть им в то же место канваса. Целый кадр этого не требует: клиент
        просто заменяет слой. Плата — трафик: полный кадр 1920x1080 PNG весит
        сотни килобайт, поэтому частота ограничена, а формат можно переключить на
        JPEG (меньше и быстрее, но с потерями на краях мазка).
        """
        if not self.paint_dirty or not self.send_rect:
            return
        clients = self._ready_clients()
        if not clients:
            return
        now = time.time()
        fps = float(self.tun.get('fullfps') or 0.0)
        if fps <= 0:
            return
        if (now - self.last_patch_t) < (1.0 / max(0.5, fps)):
            return
        fmt = '.jpg' if self.tun.get('fulljpeg') else '.png'
        try:
            blob, _rect = self._encode_layer(None, fmt)
        except Exception:
            self.err('fullframe-encode', traceback.format_exc())
            return
        W, H = int(self.tun['w']), int(self.tun['h'])
        self.seq += 1
        head = {'t': 'sync', 'layer': 1, 'seq': self.seq, 'w': W, 'h': H,
                'full': 1, 'fmt': 'jpg' if fmt == '.jpg' else 'png'}
        for c in clients:
            self._send_bin(c, head, blob)
            st = self.clients.get(c)
            if st is not None:
                st['patches'] = st.get('patches', 0) + 1
        self.diag['fullframes'] = self.diag.get('fullframes', 0) + 1
        self.last_patch_t = now
        self.paint_dirty = False
        self.send_rect = None

    def _flush_patches(self, force_final=False):
        self._flush_one_patch('paint', self.send_rect, force_final)
        self._flush_mask_patches(force_final)

    def _flush_mask_patches(self, force_final=False):
        """Патчи слоя маски. Маска едет патчами даже в режиме «целый кадр»:
        она меняется редко и небольшими областями, а целый кадр маски в браузер
        — это лишние сотни килобайт на каждое движение кисти."""
        self._flush_one_patch('mask', self.send_rect_m, force_final)

    def _flush_one_patch(self, layer, rect, force_final=False):
        if rect is None:
            return
        clients = self._ready_clients()
        if not clients:
            if layer == 'paint':
                self.send_rect = None
                self.force_patch = False
            else:
                self.send_rect_m = None
            return
        now = time.time()
        due = (now - self.last_patch_t) >= (1.0 / max(1.0, self.tun['patchhz']))
        if not due and not self.force_patch and not force_final:
            return
        try:
            dirs = self.dirs()
            dump = None
            if force_final or self.force_patch:
                dump = os.path.join(dirs.get('tmp') or '',
                                    'patch_mask_last.png' if layer == 'mask'
                                    else 'patch_last.png')
            png, (l, tp, w, h) = self._encode_layer(rect, '.png', dump, layer)
        except Exception:
            self.err('patch-encode', traceback.format_exc())
            if layer == 'paint':
                self.send_rect = None
                self.force_patch = False
            else:
                self.send_rect_m = None
            return
        self.seq += 1
        head = {'t': 'patch', 'layer': 1 if layer == 'paint' else 2,
                'x': l, 'y': tp, 'w': w, 'h': h, 'seq': self.seq,
                'final': 1 if (self.force_patch or force_final) else 0}
        for c in clients:
            self._send_bin(c, head, png)
            st = self.clients.get(c)
            if st is not None:
                st['patches'] = st.get('patches', 0) + 1
        self.diag['patches'] += 1
        self.last_patch_t = now
        if layer == 'paint':
            self.send_rect = None
            self.force_patch = False
        else:
            self.send_rect_m = None

    def _flush_sync(self):
        with self.mu:
            pending = [c for c, st in self.clients.items()
                       if st.get('need_sync') or st.get('need_sync_mask')]
        if not pending:
            return
        c = pending[0]                              # по одному клиенту за кадр
        st = self.clients.get(c) or {}
        # Синхронизация идёт двумя шагами: сначала слой краски, следующим кадром —
        # маска. Иначе браузер показал бы источник без маски, а потом «моргнул».
        layer = 'mask' if (st.get('need_sync_mask') and not st.get('need_sync')) else 'paint'
        W, H = int(self.tun['w']), int(self.tun['h'])
        try:
            dirs = self.dirs()
            dump = os.path.join(dirs.get('tmp') or '',
                                'sync_mask_last.png' if layer == 'mask'
                                else 'sync_last.png')
            png, _rect = self._encode_layer(None, '.png', dump, layer)
        except Exception:
            self.err('sync-encode', traceback.format_exc())
            with self.mu:
                if c in self.clients:
                    self.clients[c]['need_sync'] = False
                    self.clients[c]['need_sync_mask'] = False
            return
        self.seq += 1
        self._send_bin(c, {'t': 'sync', 'layer': 1 if layer == 'paint' else 2,
                           'seq': self.seq, 'w': W, 'h': H}, png)
        with self.mu:
            if c in self.clients:
                if layer == 'paint' and self.clients[c].get('need_sync'):
                    self.clients[c]['need_sync'] = False
                    self.clients[c]['need_sync_mask'] = True
                else:
                    self.clients[c]['need_sync'] = False
                    self.clients[c]['need_sync_mask'] = False
            if c in self.clients:
                self.clients[c]['need_sync'] = False
        self.log('полная выгрузка слоя для %s (%dx%d)' % (c, W, H))

    def _flush_proxy(self):
        """Отправить клиентам картинку источника (постер) и/или живой прокси.

        Постер — единственный путь, которым клиент получает САМ источник: если он
        не уехал, на планшете пусто вместо картинки (и «чинится» это только
        перезагрузкой страницы). Поэтому здесь всё, что может помешать, попадает
        в диагностику: сколько клиентов ждут постер, что вернул кодировщик.
        """
        t = self.tun
        with self.mu:
            poster = [c for c, st in self.clients.items() if st.get('need_poster')]
            n_clients = len(self.clients)
        now = time.time()
        live = t['proxyfps'] > 0 and self.src_kind == 'video'
        do_live = live and now >= self.next_proxy_t
        if not poster and not do_live:
            return
        # Тормозим только ПОВТОРНЫЕ попытки после неудачи: успешная отправка не
        # должна задерживаться, иначе новый клиент останется без картинки.
        if (not do_live and (now - self._proxy_fail_t) < 1.0):
            return
        self.diag['proxy_try'] = self.diag.get('proxy_try', 0) + 1
        px = self.try_o('proxy')
        if px is None:
            self.diag['proxy_skip'] = 'нет оператора proxy'
            return
        blob, fmt = None, 'jpg'
        for f, q in (('.jpg', 0.75), ('.png', None)):
            try:
                px.cook(force=True)
                got = px.saveByteArray(f, q) if q is not None else px.saveByteArray(f)
                got = bytes(got or b'')
            except Exception:
                got = b''
            if got:
                blob, fmt = got, ('jpg' if f == '.jpg' else 'png')
                break
        if not blob:
            self._proxy_fail_t = now
            self.diag['proxy_skip'] = ('кодировщик вернул пусто (клиентов %d, '
                                       'ждут постер %d)' % (n_clients, len(poster)))
            self.err('proxy-encode', 'proxy.saveByteArray вернул пустые данные')
            return
        try:
            w, h = int(px.width), int(px.height)
        except Exception:
            w, h = 0, 0
        self.seq += 1
        if poster:
            head = {'t': 'proxy', 'kind': 'poster', 'seq': self.seq, 'w': w, 'h': h, 'fmt': fmt}
            sent = 0
            for c in poster:
                if self._send_bin(c, head, blob):
                    sent += 1
                with self.mu:
                    if c in self.clients:
                        self.clients[c]['need_poster'] = False
            self.diag['proxy_skip'] = 'постер отправлен %d клиентам (%dx%d, %s, %d байт)' % (
                sent, w, h, fmt, len(blob))
            self.log('постер источника отправлен (%dx%d, %s, %d байт)'
                     % (w, h, fmt, len(blob)))
        elif do_live:
            head = {'t': 'proxy', 'kind': 'live', 'seq': self.seq, 'w': w, 'h': h, 'fmt': fmt}
            for c in self._ready_clients():
                self._send_bin(c, head, blob)
        self.diag['proxy'] += 1
        if live:
            self.next_proxy_t = now + 1.0 / max(0.2, t['proxyfps'])

    def _flush_log(self):
        now = time.time()
        if now - self._log_flush < 1.0:
            return
        self._log_flush = now
        d = self.try_o('server/log')
        if d is not None:
            try:
                d.text = '\n'.join(self.log_lines[-200:])
            except Exception:
                pass

    # -- отчёт на диск --------------------------------------------------------
    REPORT_OPS = (
        ('server/ws', ('port', 'active', 'callbacks')),
        # сами DAT-ы рантайма: ошибка в них — самая частая причина «ничего не
        # рисуется», и она обязана попадать в отчёт, а не только в TD
        ('server/callbacks', ()),
        ('server/runtime', ()),
        ('tick', ('active', 'framestart', 'frameend', 'start')),
        ('server/version', ()),
        ('server/address', ()),
        ('dabpng', ('file', 'play')),
        ('ext', ('top',)),
        ('fb', ('top', 'format')),
        ('brush', ('format', 'outputresolution', 'resolutionw', 'resolutionh',
                         'pixeldat')),
        ('restore', ('format', 'outputresolution', 'pixeldat')),
        ('sw', ('index', 'format')),
        ('buf', ('format',)),
        # Кроп — источник патчей для браузера и снимков undo. Если он вырезает не
        # то, что просили, в браузер уходят пустые патчи. Поэтому в отчёт кладём
        # ВСЕ его параметры и единицы: по ним видно, что именно понял TD.
        ('crop', ('cropleft', 'cropright', 'croptop', 'cropbottom',
                  'cropleftunit', 'croprightunit', 'croptopunit', 'cropbottomunit',
                  'outputresolution', 'resolutionw', 'resolutionh', 'format',
                  'extend', 'fillmode')),
        ('cropundo', ('cropleft', 'cropright', 'croptop', 'cropbottom')),
        ('cropsnap', ('cropleft', 'cropright', 'croptop', 'cropbottom')),
        ('unpremult', ('format', 'outputresolution', 'resolutionw', 'resolutionh',
                       'pixeldat')),
        ('snap', ('format',)),
        # Слой маски — зеркало слоя краски; в отчёте его тоже видно целиком,
        # иначе «маска не работает в TD» опять пришлось бы искать вслепую.
        ('fbm', ('top', 'format')),
        ('brushm', ('format', 'outputresolution', 'resolutionw', 'resolutionh',
                    'pixeldat')),
        ('restorem', ('format', 'outputresolution', 'pixeldat')),
        ('swm', ('index', 'format')),
        ('bufm', ('format',)),
        ('snapm', ('format',)),
        ('cropm', ('cropleft', 'cropright', 'croptop', 'cropbottom',
                   'cropleftunit', 'outputresolution', 'format')),
        ('cropsnapm', ('cropleft', 'cropright', 'croptop', 'cropbottom')),
        ('patchinm', ('file', 'play')),
        ('src_level', ('opacity',)),
        ('paint_level', ('opacity',)),
        ('maskapply', ('format', 'outputresolution', 'resolutionw', 'resolutionh',
                       'pixeldat')),
        ('src', ('index',)),
        ('fit', ('fit', 'justifyh', 'justifyv', 'outputresolution', 'resolutionw',
                 'resolutionh')),
        ('proxy', ('outputresolution', 'resolutionw', 'resolutionh')),
        ('out1', ('format',)),
        ('out', ()),
    )

    def write_report(self, tag='boot', quiet=False):
        """Положить машиночитаемый отчёт на диск.

        Смысл: диагностика не должна зависеть от копирования Textport. Файл
        читается с диска как есть — вместе с тем, какие токены меню реально
        применились и компилируются ли шейдеры (это видно по errors()).
        """
        d = self.dirs()
        if not d.get('tmp'):
            return None
        if not os.path.isdir(d['tmp']):
            try:
                os.makedirs(d['tmp'])
            except Exception:
                return None
        info = {
            'tag': tag,
            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'td': getattr(_APP, 'version', '?') if _APP is not None else '?',
            'td_build': getattr(_APP, 'build', '?') if _APP is not None else '?',
            'base': self.base,
            'canvas': {'w': self.tun.get('w'), 'h': self.tun.get('h')},
            'pars': {'patchhz': self.tun.get('patchhz'),
                     'proxyfps': self.tun.get('proxyfps'),
                     'undodepth': self.tun.get('undodepth'),
                     'fitmode': self.fit_name(),
                     'flipy': self.tun.get('flipy'),
                     'useext': self.tun.get('useext'),
                     'externalsrc': self.tun.get('externalsrc'),
                     'patchmode': self.tun.get('patchmode'),
                     'fullfps': self.tun.get('fullfps'),
                     'srcfile': self.tun.get('srcfile'),
                     'datadir': self.tun.get('datadir')},
            'layers': self.layers(),
            'ops': {},
            'shader_errors': {},
            'errors_total': 0,
            'runtime': self.status(),
        }
        for rel, pars in self.REPORT_OPS:
            o = self.try_o(rel)
            if o is None:
                info['ops'][rel] = 'НЕТ ОПЕРАТОРА'
                info['errors_total'] += 1
                continue
            entry = {'pars': {}, 'errors': [], 'warnings': []}
            for name in pars:
                try:
                    entry['pars'][name] = str(o.par[name].eval())
                except Exception as e:
                    entry['pars'][name] = 'НЕТ ПАРАМЕТРА (%s)' % e
                    info['errors_total'] += 1
            for nm in ('width', 'height'):
                try:
                    entry[nm] = int(getattr(o, nm))
                except Exception:
                    pass
            # входы ноды: без них не видно, например, что кроп смотрит не туда
            try:
                entry['inputs'] = [x.path if x is not None else None
                                   for x in o.inputs]
            except Exception:
                pass
            try:
                # TD отдаёт сообщения то строкой, то списком: строка, итерированная
                # по символам, однажды превратила текст ошибки компиляции в «массив
                # букв» и спрятала настоящую причину
                entry['errors'] = _as_list(o.errors())
                entry['warnings'] = _as_list(o.warnings())
            except Exception:
                pass
            if entry['errors'] or any('compile' in w.lower() or 'error' in w.lower()
                                      for w in entry['warnings']):
                info['errors_total'] += 1
                info['shader_errors'][rel] = (entry['errors'] + entry['warnings'])
            info['ops'][rel] = entry
        path = os.path.join(d['tmp'], 'paint_web_report.json')
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(info, f, ensure_ascii=False, indent=1)
            if not quiet:
                self.log('отчёт записан: %s' % path)
            return path
        except Exception:
            self.err('report', traceback.format_exc())
            return None

    # -- запуск мазка --------------------------------------------------------
    def snapshot_start(self, layer='paint'):
        """Снять состояние слоя до мазка (lock на paint/snap или paint/snapm).

        Возвращает True, если снимок принадлежит этому мазку.
        """
        if self.snap_depth > 0:
            return False                      # уже снимаем для другого мазка
        s = self.try_o('snapm' if layer == 'mask' else 'snap')
        if s is None:
            return False
        try:
            s.lock = False
            s.cook(force=True)
            s.lock = True
        except Exception:
            self.err('snapshot', traceback.format_exc())
            return False
        self.snap_depth += 1
        self.paint_dirty = True
        return True

    # -- проверка тракта штампов ---------------------------------------------
    def _b255(self, v):
        try:
            return int(round(max(0.0, min(1.0, float(v))) * 255.0))
        except Exception:
            return -1

    DAB_CHECK_VALUES = (1000.0, 500.0, 24.0, 128, 255)   # x, y, raduis, hardness, flow

    def dab_check_bytes(self):
        """Байты эталонного штампа: то, что мы кладём в PNG."""
        x, y, rad, hard, flow = self.DAB_CHECK_VALUES
        out = bytearray(16)
        hi, lo = _pack16(x)
        out[0], out[1] = hi, lo
        hi, lo = _pack16(y)
        out[2], out[3] = hi, lo
        hi, lo = _pack16(rad)
        out[4], out[5] = hi, lo
        out[6] = int(hard)
        out[7] = int(flow)
        out[8], out[9], out[10] = 255, 0, 0
        return bytes(out)

    def check_dab_pipeline(self, force=False):
        """Проверить, что данные штампов доезжают до текстуры БЕЗ искажений.

        Зачем: moviefileinTOP — это оператор для КАРТИНОК, и он вправе
        обращаться с файлом как с цветом: перевести из sRGB в рабочее
        пространство (гамма-кривая!) и премультиплицировать RGB на альфу. Для
        картинки это правильно, а для наших ДАННЫХ — смерть: мы храним координаты
        и радиус как 16-битные числа в байтах, и любая кривая превращает их в
        мусор. Снаружи это выглядит так: на планшете линия рисуется, а в TD
        появляются точки не там, где рисуешь, и патч приходит пустой.

        Проверяем фактом: пишем эталонный PNG, заставляем moviefileinTOP его
        прочитать и читаем текстуры через sample(). Если байты не совпали —
        пробуем варианты параметров и говорим, что именно помогло.

        Возвращает (ok, текст).
        """
        if not force and self.diag.get('dab_check_ok') is not None:
            return bool(self.diag['dab_check_ok']), str(self.diag.get('dab_check', ''))
        d = self.try_o('dabpng')
        if d is None:
            return False, 'нет оператора dabpng'
        dirs = self.dirs()
        if not dirs.get('tmp'):
            return False, 'нет папки tmp'
        if not os.path.isdir(dirs['tmp']):
            try:
                os.makedirs(dirs['tmp'])
            except Exception:
                return False, 'нет папки tmp'
        path = os.path.join(dirs['tmp'], 'dab_check.png')
        try:
            _write_png_rgba8(path, 4, 1, self.dab_check_bytes())
        except Exception:
            return False, 'не смог записать эталонный PNG: %s' % traceback.format_exc()
        # варианты настроек: от «правильного для данных» к «как получится»
        variants = (
            ('linear+no-premult', {'inputcolorspace': ('linear', 'lin'),
                                   'premultrgbbyalpha': ('none', 'off', 'no', 'nopremult'),
                                   'format': ('rgba8fixed', 'rgba8')}),
            ('linear', {'inputcolorspace': ('linear', 'lin')}),
            ('no-premult', {'premultrgbbyalpha': ('none', 'off', 'no', 'nopremult')}),
            ('как есть', {}),
        )
        tried = []
        try:
            for name, pars in variants:
                if pars:
                    for parname, tokens in pars.items():
                        self._set_menu_par(d, parname, tokens)
                bad = self._dab_readback(d, path)
                tried.append('%s: %s' % (name, bad or 'ок'))
                if not bad:
                    self.diag['dab_check_ok'] = True
                    self.diag['dab_check'] = 'ок (%s)' % name
                    if name != variants[0][0]:
                        self.log('тракт штампов работает в режиме «%s»' % name)
                    return True, 'ок (%s)' % name
            self.diag['dab_check_ok'] = False
            self.diag['dab_check'] = ' | '.join(tried)
            self.err('dab-check',
                     'данные штампов искажаются по дороге в текстуру: ' + ' | '.join(tried))
            return False, self.diag['dab_check']
        finally:
            # вернуть рабочую текстуру: проверочный файл не должен попасть в кисть
            try:
                d.par.file = os.path.join(dirs['tmp'], 'dabs_a.png')
                d.par.reloadpulse.pulse()
            except Exception:
                pass

    def _set_menu_par(self, o, name, tokens):
        """Выставить меню-параметр по токену или куску подписи (как в сборке)."""
        try:
            par = o.par[name]
        except Exception:
            return None
        if par is None:
            return None
        try:
            names = [str(x) for x in par.menuNames]
            labels = [str(x) for x in par.menuLabels]
        except Exception:
            names, labels = [], []
        for t in tokens:
            if t in names:
                try:
                    par.val = t
                    return t
                except Exception:
                    pass
        for t in tokens:
            for i, lab in enumerate(labels):
                if t in lab.lower() and i < len(names):
                    try:
                        par.val = names[i]
                        return names[i]
                    except Exception:
                        pass
        # Третий путь: список пунктов прочитать не удалось (другая сборка TD), но
        # сам параметр есть — пробуем присвоить токен напрямую.
        for t in tokens:
            try:
                par.val = t
                return t
            except Exception:
                pass
        return None

    def _dab_readback(self, d, path):
        """Прочитать эталон обратно через TOP. Возвращает описание расхождения.

        Строгость разная по байтам: старший байт пары задаёт координату с шагом
        32 пикселя, поэтому он обязан совпасть точно, а младшему достаточно
        точности в один шаг (1/8 пикселя). Так проверка ловит именно порчу
        данных, а не округление float-а при чтении.
        """
        try:
            d.par.file = path
            d.par.reloadpulse.pulse()
        except Exception:
            return 'не смог подсунуть файл: %s' % traceback.format_exc().splitlines()[-1]
        try:
            d.cook(force=True)
        except Exception:
            pass
        got = []
        for i in range(4):
            try:
                s = d.sample(x=i, y=0)
            except Exception:
                return 'sample() недоступен: %s' % traceback.format_exc().splitlines()[-1]
            got += [self._b255(c) for c in s]
        want = list(self.dab_check_bytes())
        hi_idx = (0, 2, 4)                 # старшие байты x, y и радиуса
        diff = []
        for i in range(16):
            tol = 0 if i in hi_idx else 1
            if abs(got[i] - want[i]) > tol:
                diff.append(i)
        if not diff:
            return None
        x, y, rad, hard, flow = self.DAB_CHECK_VALUES
        gx = (got[0] * 256 + got[1]) / 8.0
        gy = (got[2] * 256 + got[3]) / 8.0
        grad = (got[4] * 256 + got[5]) / 8.0
        return ('байты %s разошлись: хотели %s, получили %s '
                '(координата %.0f,%.0f -> %.0f,%.0f; радиус %.0f -> %.0f)'
                % (diff, [want[i] for i in diff], [got[i] for i in diff],
                   x, y, gx, gy, rad, grad))

    # -- текстура штампов ----------------------------------------------------
    def dab_width(self):
        """Ширина текстуры штампов в текселах (её же ждёт brush.glsl).

        Раньше здесь стояло имя DAB_W, которого в файле нет: функция не вызывалась
        ниоткуда, поэтому ошибка не всплывала — а проверка имён констант её нашла.
        """
        return DAB_TEX_W

    def push_dabs(self, dabs=None):
        """Упаковать пачку штампов в крошечный PNG и подсунуть его шейдеру.

        Раньше это делал scriptTOP через numpy, но вызов его колбэков в сборке
        пользователя не подтвердился: текстура оставалась пустой, и на полотне
        не появлялось ничего. Здесь только те операторы, которые в этой же
        сборке уже работают: Python пишет файл, moviefileinTOP его читает.

        Пачка передаётся аргументом: рисует её кисть СЛЕДУЮЩИМ кадром (см.
        _apply_uniforms), потому что файл читается не мгновенно.

        На один штамп 4 тексела RGBA8 (см. brush.glsl):
          +0: x_hi, x_lo, y_hi, y_lo   (1/8 пикселя)
          +1: rad_hi, rad_lo, hardness, flow
          +2: r, g, b, 0
          +3: резерв
        """
        if dabs is None:
            dabs = self.frame_dabs
        n = min(len(dabs), MAXDABS)
        self.diag['dab_tex'] = n
        # Счётчик «сколько уехало в текстуру» обнуляем сразу: по нему рантайм
        # решает, можно ли рисовать пачку следующим кадром. Если оставить прежнее
        # значение, сорвавшаяся отправка выглядела бы как удачная.
        self.diag['dab_png'] = 0
        # Первый раз перед реальной отрисовкой убеждаемся, что данные штампов
        # вообще доезжают до текстуры без искажений (см. check_dab_pipeline).
        if self.diag.get('dab_check_ok') is None:
            try:
                self.check_dab_pipeline()
            except Exception:
                self.err('dab-check', traceback.format_exc())
        if n <= 0:
            self.diag['dab_png'] = 0
            return                      # пустая пачка: текстуру не трогаем, uCount = 0
        d = self.try_o('dabpng')
        if d is None:
            self.err('dabs', 'нет оператора paint/dabpng — штампы некуда положить')
            return
        data = bytearray(DAB_TEX_W * 4)
        for i in range(n):
            x, y, rad, hard, flow, r, g, b = dabs[i]
            o = i * 16
            hi, lo = _pack16(x)
            data[o], data[o + 1] = hi, lo
            hi, lo = _pack16(y)
            data[o + 2], data[o + 3] = hi, lo
            hi, lo = _pack16(rad)
            data[o + 4], data[o + 5] = hi, lo
            data[o + 6] = _pack8(hard)
            data[o + 7] = _pack8(flow)
            data[o + 8] = _pack8(r)
            data[o + 9] = _pack8(g)
            data[o + 10] = _pack8(b)
        dirs = self.dirs()
        if not dirs.get('tmp'):
            return
        if not os.path.isdir(dirs['tmp']):
            try:
                os.makedirs(dirs['tmp'])
            except Exception:
                return
        # Два чередующихся файла: смена пути гарантирует, что moviefileinTOP
        # перечитает картинку, а не решит, что файл тот же самый.
        self._dab_flip = not getattr(self, '_dab_flip', False)
        path = os.path.join(dirs['tmp'], 'dabs_b.png' if self._dab_flip else 'dabs_a.png')
        t0 = time.time()
        try:
            _write_png_rgba8(path, DAB_TEX_W, 1, data)
            d.par.file = path
            d.par.reloadpulse.pulse()
        except Exception:
            self.err('dabs-png', traceback.format_exc())
            return
        self.diag['dab_png'] = n
        self.diag['dab_pushes'] = self.diag.get('dab_pushes', 0) + 1
        self.diag['dab_png_file'] = os.path.basename(path)
        self.diag['dab_png_ms'] = round((time.time() - t0) * 1000.0, 2)

    # -- HTTP ---------------------------------------------------------------
    def http(self, request, response):
        method = str(request.get('method') or 'GET').upper()
        uri = str(request.get('uri') or '/')
        pars = request.get('pars') or {}
        path = uri.split('?')[0]

        def finish(code, reason, data, ctype):
            response['statusCode'] = code
            response['statusReason'] = reason
            response['content-type'] = ctype
            response['data'] = data
            return response

        try:
            self.read_pars()
            # Каждый запрос к API виден в журнале: если кнопка на странице «не
            # работает», по журналу сразу понятно, дошёл ли запрос и что отдали.
            if path.startswith('/api/'):
                self.diag['http_last'] = '%s %s' % (method, path)
                self.diag['http_count'] = self.diag.get('http_count', 0) + 1
            if method == 'POST' and path == '/api/upload':
                return self._upload(request, pars, finish)
            if path in ('/api/state',):
                body = json.dumps({
                    't': 'welcome', 'ver': PROTO_VER,
                    'canvas': {'w': self.tun['w'], 'h': self.tun['h']},
                    'layers': self.layers(), 'tool': self.tool,
                    'patchHz': self.tun['patchhz'], 'proxyFps': self.tun['proxyfps'],
                    'history': self.history(), 'clients': len(self.clients),
                }, ensure_ascii=False)
                return finish(200, 'OK', body, MIME['.json'])
            if path == '/api/sources':
                return finish(200, 'OK', json.dumps({'list': self.source_list()},
                                                    ensure_ascii=False), MIME['.json'])
            if path == '/api/status':
                return finish(200, 'OK', json.dumps(self.status(), ensure_ascii=False),
                              MIME['.json'])
            # Кнопки в интерфейсе: страница сама просит TD пересобраться или
            # проверить себя — Textport для этого не нужен.
            if path == '/api/rebuild':
                how = self.request_rebuild('кнопка на странице', throttle=False)
                return finish(200, 'OK', json.dumps(
                    {'ok': bool(how), 'how': how,
                     'sig': self.diag.get('sig'), 'stored': self.diag.get('sig_stored')},
                    ensure_ascii=False), MIME['.json'])
            if path == '/api/selftest':
                how = self.request_selftest('кнопка на странице')
                return finish(200, 'OK', json.dumps(
                    {'ok': bool(how), 'how': how}, ensure_ascii=False), MIME['.json'])
            if path == '/api/open':
                return finish(200, 'OK', json.dumps(
                    {'ok': self.open_page(), 'urls': self.addresses()[0]},
                    ensure_ascii=False), MIME['.json'])
            if path == '/api/git':
                # Кнопка «Обновить из git»: подтянуть исходники и дать
                # автопересборке заметить изменения. Отдельно жать «Пересобрать»
                # после этого не нужно.
                return finish(200, 'OK', json.dumps(self.git_update(),
                                                    ensure_ascii=False), MIME['.json'])
            if path == '/api/address':
                return finish(200, 'OK', json.dumps(
                    {'local': self.addresses()[0], 'lan': self.addresses()[1],
                     'text': self.publish_address()}, ensure_ascii=False), MIME['.json'])
            if path == '/api/report':
                # тот же отчёт, что пишется на диск: удобно смотреть прямо
                # в браузере с планшета или с ПК
                p = self.write_report('http')
                if p and os.path.isfile(p):
                    with open(p, 'r', encoding='utf-8') as f:
                        body = f.read()
                    return finish(200, 'OK', body, MIME['.json'])
                return finish(500, 'Error', '{"error":"не удалось записать отчёт"}',
                              MIME['.json'])
            return self._static(path, finish)
        except Exception:
            tb = traceback.format_exc()
            self.err('http', tb)
            return finish(500, 'Internal Error',
                          '<pre>%s</pre>' % tb.replace('<', '&lt;'), MIME['.html'])

    def _upload(self, request, pars, finish):
        name = os.path.basename(str(pars.get('name') or 'upload.bin'))
        data = request.get('data')
        if data is None:
            return finish(400, 'Bad Request', 'нет тела запроса', MIME['.txt'])
        if isinstance(data, str):
            data = data.encode('utf-8', 'ignore')
        d = self.dirs()
        folder = d['uploads']
        if not os.path.isdir(folder):
            os.makedirs(folder)
        safe = ''.join(ch for ch in name if ch.isalnum() or ch in '._- ')
        if not safe:
            safe = 'upload.bin'
        full = os.path.join(folder, safe)
        with open(full, 'wb') as f:
            f.write(bytes(data))
        rel = os.path.relpath(full, d['root']).replace('\\', '/')
        self.log('загружен файл %s (%d КБ)' % (safe, len(data) // 1024))
        self._set_src(rel)
        return finish(200, 'OK', json.dumps({'ok': True, 'name': safe, 'path': rel}),
                      MIME['.json'])

    # Страница, стили и клиентский скрипт лежат не только файлами в папке web,
    # но и текстовыми DAT-ами внутри компонента. Это делает .tox самодостаточным:
    # вставил компонент в другой проект — сервер отдаёт свою страницу, даже если
    # рядом нет папки paint/web.
    WEB_DATS = {'index.html': 'web/index', 'app.js': 'web/app',
                'style.css': 'web/style'}

    def _web_dat(self, name):
        """Прочитать файл клиента из DAT компонента (если он там есть)."""
        rel = self.WEB_DATS.get(str(name).lower())
        if rel is None:
            rel = 'web/' + os.path.splitext(str(name))[0]
        d = self.try_o(rel)
        if d is None:
            return None
        try:
            text = d.text
        except Exception:
            return None
        return text if text else None

    def _web_hash(self, text):
        import hashlib
        return hashlib.sha1(text.encode('utf-8', 'replace')).hexdigest()

    def _web_marks(self, d):
        """Отпечатки страниц, которые в эту папку положил сам компонент."""
        import json
        try:
            with open(os.path.join(d['tmp'], 'web_materialized.json'),
                      'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass
        return {}

    def _web_marks_save(self, d, marks):
        import json
        try:
            with open(os.path.join(d['tmp'], 'web_materialized.json'),
                      'w', encoding='utf-8') as f:
                json.dump(marks, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def materialize_web(self):
        """Разложить страницу из DAT-ов в папку web.

        Страница лежит в компоненте тремя DAT-ами, поэтому .tox работает в чужом
        проекте, где папки web нет. Правила записи:
          * файла нет — кладём из компонента;
          * файл есть и его же положил компонент (помним отпечатки) — обновляем;
          * файл есть, а содержимое другое — это рабочая копия или чужая правка,
            её НЕ трогаем, только пишем в журнал.
        Иначе страница из компонента затирала бы файлы, которых не писала: так
        однажды был затёрт рабочий web/app.js заглушкой из тестовой сборки.
        """
        d = self.dirs()
        folder = d['web']
        marks = self._web_marks(d)
        made = []
        for name, rel in self.WEB_DATS.items():
            text = self._web_dat(name)
            if not text:
                continue
            if not os.path.isdir(folder):
                try:
                    os.makedirs(folder)
                except Exception:
                    continue
            full = os.path.join(folder, name)
            try:
                cur = None
                if os.path.isfile(full):
                    with open(full, 'r', encoding='utf-8') as f:
                        cur = f.read()
                if cur == text:
                    marks[name] = self._web_hash(text)
                    continue
                if cur and marks.get(name) != self._web_hash(cur):
                    self.log('%s не тронут: файл не от компонента (правка на диске)'
                             % name)
                    continue
                with open(full, 'w', encoding='utf-8') as f:
                    f.write(text)
                marks[name] = self._web_hash(text)
                made.append(name)
            except Exception:
                self.err('web-write', traceback.format_exc())
        self._web_marks_save(d, marks)
        if made:
            self.log('страница разложена из компонента: %s' % ', '.join(made))
        return made

    def git_update(self):
        """Подтянуть исходники из git (кнопка «Обновить из git» на странице).

        Работает в папке исходников: там лежит `.git` (отдельный репозиторий
        веб-интерфейса, см. README). После удачного `pull` файлы на диске
        меняются, поэтому здесь же сбрасываем отпечаток — автопересборка увидит
        изменения и соберёт компонент сама.
        """
        root = self.paint_dir() or ''
        have_git = bool(root) and os.path.isdir(os.path.join(root, '.git'))
        if not have_git:
            why = ('папка исходников не найдена' if not root
                   else 'в папке %s нет git-репозитория' % root)
            self.diag['git'] = why
            self.log('git: %s' % why)
            return {'ok': False, 'why': why, 'dir': root}
        try:
            res = subprocess.run(['git', '-C', root, 'pull', '--ff-only'],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=90)
            out = (res.stdout or b'')
            if isinstance(out, bytes):
                out = out.decode('utf-8', 'replace')
            text = str(out).strip()
            ok = int(res.returncode) == 0
        except Exception as e:
            text = traceback.format_exc().splitlines()[-1]
            ok = False
        self.diag['git'] = ('ок: %s' % text.replace('\n', ' | ')[:200] if ok
                            else 'ошибка: %s' % text.replace('\n', ' | ')[:200])
        self.log('git pull: %s' % text.replace('\n', ' | ')[:300])
        if ok:
            # Пусть автопересборка увидит новые файлы сразу, а не через проверку.
            self._sig = ''
            self._sig_since = 0.0
            self._sig_checked = 0.0
        return {'ok': ok, 'out': text[-2000:], 'dir': root}

    def _static(self, path, finish):
        d = self.dirs()
        if path in ('', '/'):
            path = '/index.html'
        rel = path.lstrip('/').replace('\\', '/')
        if '..' in rel:
            return finish(403, 'Forbidden', 'нет', MIME['.txt'])
        full = os.path.normpath(os.path.join(d['web'], rel))
        if not full.startswith(os.path.normpath(d['web'])):
            return finish(403, 'Forbidden', 'нет', MIME['.txt'])
        ext = os.path.splitext(full)[1].lower()
        if os.path.isfile(full):
            with open(full, 'rb') as f:
                blob = f.read()
        else:
            # Файла на диске нет — это нормально, если компонент вставили в другой
            # проект из .tox: страница лежит в DAT-ах компонента.
            text = self._web_dat(rel)
            if text is None:
                return finish(404, 'Not Found', 'не найдено: %s' % rel, MIME['.txt'])
            blob = text.encode('utf-8')
        response = finish(200, 'OK', blob, MIME.get(ext, 'application/octet-stream'))
        if ext in ('.html', '.css', '.js'):
            response['cache-control'] = 'no-store'
        return response


def _union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _rect_int(rect, W, H):
    l = int(math.floor(_clip(rect[0], 0, W)))
    tp = int(math.floor(_clip(rect[1], 0, H)))
    r = int(math.ceil(_clip(rect[2], 0, W)))
    b = int(math.ceil(_clip(rect[3], 0, H)))
    return (l, tp, max(1, r - l), max(1, b - tp))
