"""Мини-заглушка TouchDesigner, чтобы прогонять код PaintWeb обычным Python.

Нужна только для тестов: позволяет проверить арифметику штампов, прямоугольников
патчей, undo, HTTP и — главное — сам скрипт сборки, не запуская TouchDesigner.

Имена параметров операторов берутся из стабов установленной сборки TD
(bin/Lib/tdi/ops/<семейство>/<тип>.py), поэтому опечатка в имени параметра в
build_paint_web.py или в рантайме всплывает в тестах, а не у пользователя.

Чего заглушка НЕ проверяет: реальные токены меню-параметров (их в стабах нет,
список MENU_TOKENS — моё предположение) и семантику кукинга TD.
"""

import os
import posixpath
import re
import struct
import zlib

STUB_ROOTS = [r'C:\Program Files\Derivative\TouchDesigner\bin\Lib\tdi\ops']
STUB_FAMILIES = ('tops', 'dats', 'comps', 'chops', 'sops', 'mats', 'pops')

# Токены меню — предположение (в стабах их нет). Нужны, чтобы set_menu() в
# рантайме и в сборке что-то выбрал; настоящие токены проверяются уже в TD.
MENU_TOKENS = {
    'outputresolution': ['useinput', 'custom', 'pixel', 'natural', 'customresolution'],
    'format': ['rgba8', 'rgba16', 'rgba16float', 'rgba32float', 'rgba8fixed'],
    'cropleftunit': ['fraction', 'pixels', 'nativeres'],
    'croprightunit': ['fraction', 'pixels', 'nativeres'],
    'croptopunit': ['fraction', 'pixels', 'nativeres'],
    'cropbottomunit': ['fraction', 'pixels', 'nativeres'],
    'fit': ['fit', 'best', 'native', 'inside', 'outside', 'horizontal', 'vertical'],
    'justifyh': ['left', 'center', 'right'],
    'justifyv': ['bottom', 'center', 'top'],
    'language': ['python', 'tscript'],
    'extension': ['dat', 'language', 'custom'],
    'playmode': ['lock', 'sequential', 'skip'],
    'chanmask': ['rgba', 'rgb', 'alpha'],
    'fillmode': ['input', 'nativeres', 'fill', 'fit'],
    'inputfiltertype': ['nearest', 'linear', 'mipmap'],
    'filtertype': ['nearest', 'linear', 'mipmap'],
    'type': ['texture2d', 'texture3d', 'texturearray'],
    # цветовые параметры moviefileinTOP: от них зависит, доедут ли ДАННЫЕ штампов
    # до текстуры без гамма-преобразований (см. Runtime.check_dab_pipeline)
    'inputcolorspace': ['automatic', 'srgb', 'linear', 'rec709', 'passthrough'],
    'premultrgbbyalpha': ['automatic', 'premultiply', 'nopremultiply', 'none', 'off'],
}

_par_cache = {}


def stub_pars(optype):
    """Имена параметров оператора по стабам установленной сборки TD."""
    if optype in _par_cache:
        return _par_cache[optype]
    names = set()
    for root in STUB_ROOTS:
        for fam in STUB_FAMILIES:
            path = os.path.join(root, fam, optype + '.py')
            if not os.path.isfile(path):
                continue
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    # В стабах на одной строке бывает несколько объявлений:
                    #   resolutionw : ParInt; resolutionh : ParInt
                    #   vec0valuex : ParXYZW; vec0valuey : ParXYZW; ...
                    # поэтому берём ВСЕ пары, а не только первую.
                    for m in re.finditer(r'([A-Za-z0-9_]+) : (Par[A-Za-z]+)', line):
                        if m.group(2) != 'ParGroup':
                            names.add(m.group(1))
    # в стабах объявлена только первая запись последовательности (vec0name),
    # а в TD их несколько — разворачиваем, чтобы не ловить ложные «нет параметра»
    for n in list(names):
        m = re.match(r'^([a-z]+)0(name|value[xyzw]?)$', n)
        if m:
            for i in range(4):
                names.add('%s%d%s' % (m.group(1), i, m.group(2)))
    _par_cache[optype] = names
    return names


def stubs_available():
    for root in STUB_ROOTS:
        if os.path.isdir(root):
            return True
    return False


class CookLevelStub(object):
    AUTOMATIC = 'AUTOMATIC'
    ON_CHANGE = 'ON_CHANGE'
    WHEN_USED = 'WHEN_USED'
    ALWAYS = 'ALWAYS'


_numpy = None


def numpy_module():
    """numpy нужен только колбэкам scriptTOP; если его нет — вернём заглушку."""
    global _numpy
    if _numpy is None:
        try:
            import numpy as _np
            _numpy = _np
        except Exception:
            _numpy = _NumpyStub()
    return _numpy


class _NumpyStub(object):
    """На случай окружения без numpy: колбэк всё равно должен исполниться."""

    @staticmethod
    def zeros(shape, dtype=None):
        h, w, c = shape
        return [[[0.0] * c for _ in range(w)] for _ in range(h)]


class Par(object):
    def __init__(self, name, value=None, menu=None):
        self.name = name
        self._v = value
        self.menuNames = list(menu or [])
        self.menuLabels = list(menu or [])
        self.pulses = 0

    def eval(self):
        # в TD для OP-параметров eval() отдаёт путь строкой, а .val — сам оператор
        v = self._v
        path = getattr(v, 'path', None)
        if isinstance(path, str):
            return path
        return v

    @property
    def val(self):
        return self._v

    @val.setter
    def val(self, v):
        if self.menuNames and v not in self.menuNames:
            raise ValueError('меню %s: %r нет в %s' % (self.name, v, self.menuNames))
        self._v = v

    def pulse(self):
        self.pulses += 1


class Pars(object):
    def __init__(self, **kw):
        object.__setattr__(self, '_d', dict(kw))

    def __setattr__(self, k, v):
        d = self.__dict__.get('_d')
        if d is not None and k in d and isinstance(d[k], Par):
            d[k].val = v          # как в TD: запись в параметр идёт через Par
            return
        object.__setattr__(self, k, v)

    def __getattr__(self, k):
        try:
            return self._d[k]
        except KeyError:
            raise AttributeError('нет параметра %s' % k)

    def __getitem__(self, k):
        if k in self._d:
            return self._d[k]
        raise KeyError(k)

    def __setitem__(self, k, v):
        if k in self._d and isinstance(self._d[k], Par):
            self._d[k].val = v
            return
        raise KeyError(k)

    def __contains__(self, k):
        return k in self._d


class Connector(object):
    def __init__(self, op, idx):
        self.op = op
        self.idx = idx

    def connect(self, other):
        self.op.inputs[self.idx] = other

    def disconnect(self):
        self.op.inputs[self.idx] = None


class FakePage(object):
    """Страница своих параметров (appendCustomPage).

    Поведение append* повторяет TD: подпись (label) допустима ТОЛЬКО именованным
    аргументом. Позиционная подпись падает с тем же текстом, что и в живом TD —
    на этом уже один раз обожглись (все пользовательские параметры молча
    не создались, а сборка отчиталась, что всё на месте).
    """

    def __init__(self, op, name):
        self.op = op
        self.name = name
        self.pars = []

    def _add(self, name, defaults, args, kw):
        if args:
            raise Exception(
                'Single name argument expected.  Labels, etc are specified with '
                'keyword arguments. Value:%r Type:%s.' % ((name,) + tuple(args), tuple))
        label = kw.get('label')
        p = Par(name, defaults)
        self.op.par._d[name] = p
        self.pars.append(p)
        p.label = label
        return p

    def appendInt(self, name, *args, **kw):
        return self._add(name, 0, args, kw)

    def appendFloat(self, name, *args, **kw):
        return self._add(name, 0.0, args, kw)

    def appendToggle(self, name, *args, **kw):
        return self._add(name, 0, args, kw)

    def appendStr(self, name, *args, **kw):
        return self._add(name, '', args, kw)

    def appendFile(self, name, *args, **kw):
        return self._add(name, '', args, kw)

    def appendPulse(self, name, *args, **kw):
        return self._add(name, None, args, kw)

    def appendMenu(self, name, *args, **kw):
        items = kw.pop('items', None) or args or ()
        p = self._add(name, items[0] if items else '', (), kw)
        p.menuNames = list(items)
        p.menuLabels = list(items)
        return p


def png_bytes(w, h, rgb=(0, 0, 0), a=0):
    """Настоящий RGBA8-PNG (однотонная картинка) в виде байтов.

    Заглушка не умеет считать пиксели (это делает GPU), но файл обязан быть
    настоящим PNG: самопроверка читает его своим декодером, и без файла она
    обрывалась на чтении — то есть вторая половина самопроверки (undo, патчи,
    прокси, композит) не исполнялась нигде и ни разу.
    """
    row = bytearray()
    for _ in range(max(1, int(w))):
        row += bytes((rgb[0], rgb[1], rgb[2], a))
    raw = b''.join(b'\x00' + bytes(row) for _ in range(max(1, int(h))))

    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', int(w), int(h), 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6))
            + chunk(b'IEND', b''))


def write_png(path, w, h, rgb=(0, 0, 0), a=0):
    data = png_bytes(w, h, rgb, a)
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    with open(path, 'wb') as f:
        f.write(data)
    return len(data)


def png_size(path):
    """Размер PNG из заголовка файла — так его знает и moviefileinTOP."""
    try:
        with open(path, 'rb') as f:
            head = f.read(33)
    except Exception:
        return None
    if len(head) < 33 or head[:8] != b'\x89PNG\r\n\x1a\n' or head[12:16] != b'IHDR':
        return None
    w, h = struct.unpack('>II', head[16:24])
    return int(w), int(h)


def png_pixels(path):
    """Пиксели 8-битного PNG: список строк по (r, g, b, a).

    Нужно, чтобы заглушка честно отвечала на TOP.sample(): именно так рантайм
    проверяет, что данные штампов доехали до текстуры без искажений.
    """
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except Exception:
        return None
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        return None
    pos = 8
    w = h = nch = None
    idat = b''
    while pos + 8 <= len(data):
        ln = struct.unpack('>I', data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if tag == b'IHDR':
            w, h, depth, ctype = struct.unpack('>IIBB', body[:10])
            if depth != 8:
                return None
            nch = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype)
            if nch is None:
                return None
        elif tag == b'IDAT':
            idat += body
        elif tag == b'IEND':
            break
    if not w or not h or not nch:
        return None
    raw = zlib.decompress(idat)
    stride = w * nch
    rows = []
    prev = bytearray(stride)
    i = 0
    for _ in range(h):
        f = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - nch] if x >= nch else 0
            b = prev[x]
            c = prev[x - nch] if x >= nch else 0
            if f == 1:
                line[x] = (line[x] + a) & 0xFF
            elif f == 2:
                line[x] = (line[x] + b) & 0xFF
            elif f == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 0xFF
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 0xFF
        px = []
        for x in range(w):
            o = x * nch
            if nch == 4:
                px.append((line[o], line[o + 1], line[o + 2], line[o + 3]))
            elif nch == 3:
                px.append((line[o], line[o + 1], line[o + 2], 255))
            elif nch == 2:
                px.append((line[o], line[o], line[o], line[o + 1]))
            else:
                px.append((line[o], line[o], line[o], 255))
        rows.append(px)
        prev = line
    return rows


class InputList(list):
    """`OP.inputs` в TD — список входов. Старые тесты читают его как словарь
    (`inputs.get(i)`), поэтому список умеет и то, и другое."""

    def get(self, i, default=None):
        try:
            v = self[i]
        except IndexError:
            return default
        return default if v is None else v


class FakeOp(object):
    REG = {}

    def __init__(self, path, otype='nullTOP'):
        self.path = path
        self.name = posixpath.basename(path)
        self.type = otype
        self.inputs = InputList([None] * 4)
        self.kids = {}
        self.par = Pars(**dict((n, Par(n, 0, MENU_TOKENS.get(n)))
                               for n in stub_pars(otype)))
        self.customPages = []
        self.lock = False
        self.text = ''
        self._w = 64
        self._h = 64
        self._forced = False
        self.cooks = 0
        self.copied = None
        self.cook_error = None
        self.saved = []
        self.saved_bytes = 0
        self.sampled = []
        self.encoded = []
        self.sent_text = []
        self.sent_bin = []
        self.errors_list = []
        self.nodeX = 0
        self.nodeY = 0
        self.viewer = False
        self.display = False
        FakeOp.REG[path] = self
        parent = posixpath.dirname(path)
        if parent and parent != path and parent in FakeOp.REG:
            FakeOp.REG[parent].kids[self.name] = self

    # -- дерево
    @property
    def children(self):
        return list(self.kids.values())

    # Размер TOP, как в TD: явно заданный размер (так тесты имитируют реально
    # загруженную в TOP картинку) важнее всего, дальше — своё разрешение, размер
    # файла у moviefileinTOP, размер входа. Без этого заглушка врала бы про
    # размеры, а на них держатся и сохранение буфера, и загрузка патча области.
    def _size_from_inputs(self):
        # moviefileinTOP показывает размер файла из параметра File — именно так
        # рантайм проверяет, что патч области действительно загрузился.
        if 'moviefilein' in str(self.type).lower():
            try:
                got = png_size(str(self.par['file'].eval() or ''))
            except Exception:
                got = None
            if got:
                return got
        for o in self.inputs:
            if o is not None:
                try:
                    return int(o.width), int(o.height)
                except Exception:
                    continue
        return 64, 64

    def _crop_size(self, w, h):
        """cropTOP сохраняет ВЫРЕЗАННУЮ область, а не весь вход.

        Как в живом TD: параметры задают ПОЛОЖЕНИЕ краёв. По горизонтали это
        позиции слева направо (ширина = cropright - cropleft), а по вертикали TD
        работает в своей оси (v = 0 внизу), поэтому высота = croptop - cropbottom.
        Если края задать наоборот, область пустая и TD отдаёт 1 пиксель — ровно
        так и получались патчи 645x1.

        Единицы измерения задаются отдельными параметрами, и бывает, что «pixels»
        применяется не ко всем: значение в пикселях читается как доля и зажимается
        в 1.0. Заглушка это повторяет.
        """
        if 'crop' not in str(self.type).lower():
            return w, h
        try:
            unit = str(self.par['cropleftunit'].eval()).lower()
            l = float(self.par['cropleft'].eval())
            r = float(self.par['cropright'].eval())
            t = float(self.par['croptop'].eval())
            b = float(self.par['cropbottom'].eval())
        except Exception:
            return w, h
        if unit.startswith(('pix', 'nat')):
            return max(1, int(round(r - l))), max(1, int(round(t - b)))
        # доли: значения больше единицы TD зажимает, положение края умножается на
        # размер входа
        l, r = min(1.0, max(0.0, l)), min(1.0, max(0.0, r))
        t, b = min(1.0, max(0.0, t)), min(1.0, max(0.0, b))
        return (max(1, int(round(w * (r - l)))),
                max(1, int(round(h * (t - b)))))

    @property
    def width(self):
        if getattr(self, '_forced', False):
            return self._w
        try:
            if str(self.par['outputresolution'].eval()) == 'custom':
                return int(self.par['resolutionw'].eval())
        except Exception:
            pass
        w, h = self._size_from_inputs()
        return self._crop_size(w, h)[0]

    @width.setter
    def width(self, v):
        self._w = int(v)
        self._forced = True

    @property
    def height(self):
        if getattr(self, '_forced', False):
            return self._h
        try:
            if str(self.par['outputresolution'].eval()) == 'custom':
                return int(self.par['resolutionh'].eval())
        except Exception:
            pass
        w, h = self._size_from_inputs()
        return self._crop_size(w, h)[1]

    @height.setter
    def height(self, v):
        self._h = int(v)
        self._forced = True

    @property
    def isCOMP(self):
        return str(self.type).upper().endswith('COMP')
    def op(self, rel):
        p = posixpath.normpath(posixpath.join(self.path, rel))
        return FakeOp.REG.get(p)

    def parent(self):
        p = posixpath.dirname(self.path)
        if not p or p == '/':
            return None
        return FakeOp.REG.get(p)

    def create(self, otype, name):
        return FakeOp(posixpath.join(self.path, name), otype)

    def destroy(self):
        FakeOp.REG.pop(self.path, None)
        parent = self.parent()
        if parent is not None:
            parent.kids.pop(self.name, None)

    def findChildren(self):
        out = []
        for k in self.kids.values():
            out.append(k)
            out.extend(k.findChildren())
        return out

    # -- свои параметры
    def appendCustomPage(self, name):
        page = FakePage(self, name)
        self.customPages.append(page)
        return page

    def removeCustomPage(self, page):
        try:
            self.customPages.remove(page)
        except ValueError:
            pass
        for p in getattr(page, 'pars', []):
            self.par._d.pop(p.name, None)

    def layoutChildren(self):
        return

    @property
    def inputConnectors(self):
        return [Connector(self, i) for i in range(4)]

    def setInputs(self, ops):
        """Как в TD: задать весь набор входов сразу.

        В этой сборке TD у части операторов входы динамические, и через
        `inputConnectors[i].connect()` связи молча не вставали. Проверяем ровно
        то же, что проверяет настоящий TD: список без дыр, иначе ошибка.
        """
        if ops is None:
            ops = []
        if not isinstance(ops, (list, tuple)):
            raise TypeError('setInputs ожидает список операторов')
        ops = list(ops)
        while len(self.inputs) < len(ops):
            self.inputs.append(None)
        for i, o in enumerate(ops):
            self.inputs[i] = o

    # -- кукинг и вывод
    def cook(self, force=False):
        self.cooks += 1
        # Как TD: у scriptTOP кук вызывает onCook из DAT-а колбэков. Без этого
        # путь «кук → onCook → copyNumpyArray» в тестах не исполнялся бы вообще.
        cb = None
        try:
            if 'callbacks' in self.par:
                cb = self.par['callbacks'].eval()
        except Exception:
            cb = None
        if isinstance(cb, str):                 # eval() отдаёт путь, как в TD
            cb = make_op_func()(cb)
        if isinstance(cb, FakeOp) and cb.text:
            ns = {
                'op': make_op_func(),
                'me': self,
                'scriptOp': self,
                'numpy': numpy_module(),
                'CookLevel': CookLevelStub,
                'print': print,
            }
            try:
                exec(compile(cb.text, cb.path, 'exec'), ns)
                fn = ns.get('onCook')
                if callable(fn):
                    fn(self)
                self.cook_error = None
            except Exception as e:
                self.cook_error = '%s: %s' % (type(e).__name__, e)

    def copyNumpyArray(self, arr):
        self.copied = arr

    def save(self, path):
        self.saved.append(path)
        # Пишем настоящий (однотонный) PNG: иначе самопроверка обрывалась на
        # чтении файла и не доходила до второй половины проверок.
        w, h = self._crop_size(self.width, self.height)
        try:
            self.saved_bytes = write_png(path, w, h)
        except Exception as e:
            self.saved_bytes = 0
            self.save_error = '%s: %s' % (type(e).__name__, e)

    def saveByteArray(self, fmt, quality=1.0, metadata=[]):
        """Отдать байты картинки. Кодировщика в заглушке нет, поэтому PNG
        собирается по-настоящему (его проверяет сигнатурой и размером сама
        самопроверка), а JPEG — только сигнатурой и запасом байтов: настоящих
        JPEG-пикселей GPU всё равно не даёт, их проверяет уже живой TD."""
        self.encoded.append((fmt, quality))
        f = str(fmt).lower()
        w, h = self._crop_size(self.width, self.height)
        if f.endswith('.png'):
            return bytearray(png_bytes(w, h))
        # FF D8 — сигнатура JPEG, дальше сегмент-комментарий с запасом байтов.
        body = b'fake-jpeg' * 70
        return bytearray(b'\xff\xd8\xff\xfe' + struct.pack('>H', len(body) + 2) + body
                         + b'\xff\xd9')

    def sample(self, x=0, y=0):
        """TOP.sample() — цвет пикселя.

        Для moviefileinTOP отдаём РЕАЛЬНЫЙ пиксель из файла, а для cropTOP —
        пиксель входа по смещению (в осях TD: слева-направо и снизу вверх). На
        этом держатся проверки рантайма «кроп вырезал то, что просили» и «кроп не
        зеркалит»: без честного sample() они бы ничего не проверяли.
        """
        self.sampled.append((x, y))
        t = str(self.type).lower()
        if 'crop' in t:
            src = self.inputs.get(0)
            if src is None:
                return (0.0, 0.0, 0.0, 0.0)
            try:
                l = int(round(float(self.par['cropleft'].eval())))
                b = int(round(float(self.par['cropbottom'].eval())))
            except Exception:
                return (0.0, 0.0, 0.0, 0.0)
            return src.sample(l + int(x), b + int(y))
        if 'moviefilein' in str(self.type).lower():
            try:
                path = str(self.par['file'].eval() or '')
            except Exception:
                path = ''
            rows = png_pixels(path) if path else None
            if rows:
                # в TD v отсчитывается снизу — для высоты в 1 пиксель это та же строка
                row = rows[max(0, min(len(rows) - 1, len(rows) - 1 - int(y)))]
                col = max(0, min(len(row) - 1, int(x)))
                r, g, b, a = row[col]
                return (r / 255.0, g / 255.0, b / 255.0, a / 255.0)
        return (0.0, 0.0, 0.0, 0.0)

    def errors(self):
        return list(self.errors_list)

    def warnings(self):
        return []

    # -- веб-сервер
    def webSocketSendText(self, client, data):
        self.sent_text.append((client, data))

    def webSocketSendBinary(self, client, data):
        self.sent_bin.append((client, bytes(data)))


class FakeProject(object):
    def __init__(self, folder):
        self.folder = folder


def new_par(**kw):
    return Pars(**kw)


def intpar(name, v):
    return Par(name, v)


def menu_par(name, value, menu):
    return Par(name, value, menu)


def build_tree(paint_dir):
    """Создать ровно те ноды, с которыми работает рантайм.

    ВАЖНО: TOP-ы лежат ПЛОСКО, прямо в paint_web. TouchDesigner не соединяет
    операторы из разных компонентов (данные между сетями ходят только через
    In/Out-операторы), поэтому вся картинка живёт в одной сети, а в
    под-компонентах остались только DAT-ы.
    """
    P = '/project1/paint_web'
    base = FakeOp(P, 'baseCOMP')
    for n, t in (('server', 'baseCOMP'), ('shaders', 'baseCOMP')):
        FakeOp(P + '/' + n, t)

    ws = FakeOp(P + '/server/ws', 'webserverDAT')
    for n in ('runtime', 'log', 'callbacks', 'version', 'address'):
        FakeOp(P + '/server/' + n, 'textDAT')

    FakeOp(P + '/ext', 'selectTOP')
    FakeOp(P + '/movie', 'moviefileinTOP')
    sel = FakeOp(P + '/src', 'switchTOP')
    fit = FakeOp(P + '/fit', 'fitTOP')
    prox = FakeOp(P + '/proxy', 'nullTOP')

    FakeOp(P + '/dabpng', 'moviefileinTOP')
    fb = FakeOp(P + '/fb', 'feedbackTOP')
    brush = FakeOp(P + '/brush', 'glslmultiTOP')
    rest = FakeOp(P + '/restore', 'glslmultiTOP')
    patchin = FakeOp(P + '/patchin', 'moviefileinTOP')
    sw = FakeOp(P + '/sw', 'switchTOP')
    buf = FakeOp(P + '/buf', 'nullTOP')
    snap = FakeOp(P + '/snap', 'nullTOP')
    for n in ('crop', 'cropundo', 'cropsnap'):
        FakeOp(P + '/' + n, 'cropTOP')

    # Второй слой — маска источника: своя петля, своя кисть, своё восстановление.
    fbm = FakeOp(P + '/fbm', 'feedbackTOP')
    brushm = FakeOp(P + '/brushm', 'glslmultiTOP')
    restm = FakeOp(P + '/restorem', 'glslmultiTOP')
    patchinm = FakeOp(P + '/patchinm', 'moviefileinTOP')
    swm = FakeOp(P + '/swm', 'switchTOP')
    bufm = FakeOp(P + '/bufm', 'nullTOP')
    snapm = FakeOp(P + '/snapm', 'nullTOP')
    for n in ('cropm', 'cropsnapm'):
        FakeOp(P + '/' + n, 'cropTOP')
    maskapply = FakeOp(P + '/maskapply', 'glslmultiTOP')

    gc = FakeOp(P + '/mode', 'switchTOP')
    FakeOp(P + '/src_level', 'levelTOP')
    FakeOp(P + '/paint_level', 'levelTOP')
    mask_level = FakeOp(P + '/mask_level', 'levelTOP')
    color_src = FakeOp(P + '/color_src', 'constantTOP')
    colapply = FakeOp(P + '/colapply', 'glslmultiTOP')
    base_layer = FakeOp(P + '/base_layer', 'overTOP')
    FakeOp(P + '/over', 'overTOP')
    FakeOp(P + '/out1', 'nullTOP')
    FakeOp(P + '/out', 'outTOP')
    outpaint = FakeOp(P + '/outpaint', 'outTOP')
    outmask = FakeOp(P + '/outmask', 'outTOP')

    # параметры базового компонента
    base.par = Pars(
        Canvasw=Par('Canvasw', 1920), Canvash=Par('Canvash', 1080),
        Port=Par('Port', 9980), Patchhz=Par('Patchhz', 30.0),
        Proxyfps=Par('Proxyfps', 2.0), Undodepth=Par('Undodepth', 24),
        Srcfile=Par('Srcfile', ''), Srcvisible=Par('Srcvisible', 1),
        Srcopacity=Par('Srcopacity', 1.0), Paintvisible=Par('Paintvisible', 1),
        Paintopacity=Par('Paintopacity', 1.0), Fitmode=Par('Fitmode', 3),
        Useext=Par('Useext', 0), Externalsrc=Par('Externalsrc', ''),
        Flipy=Par('Flipy', 0), Datadir=Par('Datadir', paint_dir),
        # Слой монотонного цвета: интенсивность, температура и видимость.
        Colorint=Par('Colorint', 1.0), Colortemp=Par('Colortemp', 6500.0),
        Colorvisible=Par('Colorvisible', 0),
    # Ссылка на интерфейс в самой базе: адрес и кнопка (Pulse — счётчик нажатий).
    Page=Par('Page', ''), Openpage=Par('Openpage', 0))

    # меню-параметры как в реальном TD
    UNITS = ['fraction', 'pixels', 'nativeres']
    RES = ['useinput', 'custom', 'pixel', 'natural']
    for o in (brush, rest, gc, fit, brushm, restm, maskapply):
        o.par = Pars(outputresolution=Par('outputresolution', 'useinput', RES),
                     resolutionw=Par('resolutionw', 1280),
                     resolutionh=Par('resolutionh', 720))
        for i in range(4):
            o.par._d['vec%dname' % i] = Par('vec%dname' % i, '')
            for c in 'xyzw':
                o.par._d['vec%dvalue%s' % (i, c)] = Par('vec%dvalue%s' % (i, c), 0.0)
    prox.par = Pars(outputresolution=Par('outputresolution', 'useinput', RES),
                    resolutionw=Par('resolutionw', 1280),
                    resolutionh=Par('resolutionh', 720))
    # fitTOP: меню вписывания источника — как в живом TD
    # (подписи и имена пунктов там разные, поэтому заглушка отдаёт оба списка).
    FIT_TOKENS = ['fit', 'fill', 'horizontal', 'vertical', 'outside', 'nativeres']
    FIT_LABELS = ['Fit Best', 'Fill', 'Fit Horizontal', 'Fit Vertical',
                  'Fit Outside', 'Native Resolution']
    JUSTIFY = ['left', 'center', 'right']
    fit.par._d['fit'] = Par('fit', 'fit', FIT_TOKENS)
    fit.par._d['fit'].menuLabels = FIT_LABELS      # подписи отличаются от имён
    fit.par._d['justifyh'] = Par('justifyh', 'center', JUSTIFY)
    fit.par._d['justifyv'] = Par('justifyv', 'center', JUSTIFY)
    for n in ('crop', 'cropundo', 'cropsnap', 'cropm', 'cropsnapm'):
        o = FakeOp.REG[P + '/' + n]
        o.par = Pars(
            cropleft=Par('cropleft', 0.0), cropleftunit=Par('cropleftunit', 'fraction', UNITS),
            cropright=Par('cropright', 0.0), croprightunit=Par('croprightunit', 'fraction', UNITS),
            croptop=Par('croptop', 0.0), croptopunit=Par('croptopunit', 'fraction', UNITS),
            cropbottom=Par('cropbottom', 0.0), cropbottomunit=Par('cropbottomunit', 'fraction', UNITS),
            format=Par('format', 'rgba8', ['rgba8', 'rgba16float']))
    sw.par = Pars(index=Par('index', 0), format=Par('format', 'rgba8', ['rgba8', 'rgba16float']))
    swm.par = Pars(index=Par('index', 0),
                   format=Par('format', 'rgba8', ['rgba8', 'rgba16float']))
    # switchTOP в живом TD умеет своё разрешение — рантайм это использует
    gc.par = Pars(index=Par('index', 0),
                  outputresolution=Par('outputresolution', 'useinput', RES),
                  resolutionw=Par('resolutionw', 1280),
                  resolutionh=Par('resolutionh', 720))
    for rel in ('src_level', 'paint_level', 'mask_level'):
        FakeOp.REG[P + '/' + rel].par = Pars(opacity=Par('opacity', 1.0))
    color_src.par = Pars(colorr=Par('colorr', 1.0), colorg=Par('colorg', 1.0),
                        colorb=Par('colorb', 1.0), alpha=Par('alpha', 1.0))
    snap.par = Pars()
    snapm.par = Pars()
    sel.par = Pars(index=Par('index', 0))
    # selectTOP внешнего источника: путь к TOP задаётся параметром Top
    FakeOp.REG[P + '/ext'].par = Pars(top=Par('top', ''))
    fb.par = Pars(top=Par('top', ''))
    fbm.par = Pars(top=Par('top', ''))
    patchin.par = Pars(file=Par('file', ''), reloadpulse=Par('reloadpulse', None),
                       play=Par('play', 0))
    patchinm.par = Pars(file=Par('file', ''), reloadpulse=Par('reloadpulse', None),
                        play=Par('play', 0))
    ws.par = Pars(port=Par('port', 9980), active=Par('active', 1))

    # Связи: без них размеры в заглушке не распространяются по цепочке, и
    # проверка «кроп вырезал ровно то, что просили» работать не может.
    def wire(o, srcs):
        o.setInputs(srcs)
    wire(sel, [FakeOp.REG[P + '/movie'], FakeOp.REG[P + '/ext']])
    wire(fit, [sel])
    wire(prox, [fit])
    wire(brush, [fb, FakeOp.REG[P + '/dabpng']])
    wire(rest, [fb, patchin])
    wire(fb, [buf])
    wire(sw, [brush, rest])
    wire(buf, [sw])
    wire(snap, [buf])
    for n in ('crop', 'cropundo'):
        wire(FakeOp.REG[P + '/' + n], [buf])
    wire(FakeOp.REG[P + '/cropsnap'], [snap])
    # цепочка маски — зеркало цепочки краски
    wire(brushm, [fbm, FakeOp.REG[P + '/dabpng']])
    wire(restm, [fbm, patchinm])
    wire(fbm, [bufm])
    wire(swm, [brushm, restm])
    wire(bufm, [swm])
    wire(snapm, [bufm])
    wire(FakeOp.REG[P + '/cropm'], [bufm])
    wire(FakeOp.REG[P + '/cropsnapm'], [snapm])
    wire(FakeOp.REG[P + '/src_level'], [fit])
    wire(FakeOp.REG[P + '/paint_level'], [buf])
    wire(maskapply, [FakeOp.REG[P + '/src_level'], mask_level])
    wire(mask_level, [bufm])
    wire(colapply, [color_src, mask_level])
    wire(base_layer, [colapply, maskapply])
    wire(outpaint, [FakeOp.REG[P + '/paint_level']])
    wire(outmask, [mask_level])
    wire(FakeOp.REG[P + '/over'],
         [FakeOp.REG[P + '/paint_level'], base_layer])
    wire(FakeOp.REG[P + '/out1'], [FakeOp.REG[P + '/over']])
    wire(FakeOp.REG[P + '/out'], [FakeOp.REG[P + '/over']])
    return base, ws


def make_op_func():
    """Как в TD: для несуществующего пути op() возвращает None, а не падает."""
    def _op(path):
        return FakeOp.REG.get(path if path == '/' else posixpath.normpath(path))
    return _op
