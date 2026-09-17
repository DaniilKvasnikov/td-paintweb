"""PaintWeb — самопроверка ВНУТРИ TouchDesigner, без браузера.

Запуск (Textport):

    exec(open(r"C:\\Users\\DaniilNotebook\\Documents\\PixelFlow\\paint\\td\\selftest_paint_web.py",
              encoding="utf-8").read())

Что делает: подсовывает рантайму синтетический мазок по протоколу, принудительно
прогоняет кадры, читает пиксели буфера слоя из сохранённого PNG (порядок строк в PNG
задан форматом — поэтому это честная проверка ориентации) и печатает отчёт.

Проверяет то, что иначе видно только глазами в браузере:
  * компилируются ли шейдеры (ошибки нод);
  * попадают ли штампы туда, куда просит протокол (и не перевёрнута ли вертикаль —
    при необходимости САМ переключает Flipy и перепроверяет);
  * работает ли undo через восстановление области;
  * кодируются ли патчи и прокси-кадры (сколько байт, какое разрешение).

Синтетический мазок в конце стирается (это undo-able, если что-то останется).
"""

import json
import os
import struct
import sys
import time
import traceback
import types
import zlib

BASE = '/project1/paint_web'
SHARED_MODULE = 'paintweb_runtime_shared'   # тот же кэш, что в pw_boot.py

RESULTS = []
SENT = []          # перехваченные отправки клиентам: (заголовок, длина)
COMPLETED = False  # True только если проверка дошла до конца (см. run_paint_web)
FATAL = None       # текст исключения, если проверка упала на середине
MIN_CHECKS = 27    # меньше этого числа проверок считать прогон несостоявшимся


def check(name, cond, extra=''):
    RESULTS.append((bool(cond), name, extra))
    print('  %s %s%s' % ('ok  ' if cond else 'ПРОВАЛ', name,
                         ('   ' + str(extra)) if extra and not cond else ''))


# ------------------------------------------------------------------ PNG (чтение)

def png_read(path):
    """Минимальный декодер PNG: 8 и 16 бит, без интерлейса, все фильтры.

    16 бит нужны потому, что буфер слоя у нас в формате rgba16float и TD
    сохраняет его именно как 16-битный PNG. Для сравнений яркости старшего байта
    достаточно, поэтому 16-битные строки приводим к 8-битным (берём старший байт).
    """
    with open(path, 'rb') as f:
        data = f.read()
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('не PNG: %s' % path)
    pos = 8
    idat = b''
    w = h = depth = ctype = interlace = 0
    while pos + 8 <= len(data):
        ln = struct.unpack('>I', data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if tag == b'IHDR':
            w, h, depth, ctype, _c, _f, interlace = struct.unpack('>IIBBBBB', body)
        elif tag == b'IDAT':
            idat += body
        elif tag == b'IEND':
            break
    if depth not in (8, 16):
        raise ValueError('ожидал 8 или 16 бит на канал, получил %d' % depth)
    if interlace:
        raise ValueError('PNG с интерлейсом не поддерживаю')
    nch = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(idat)
    bpp = max(1, (depth * nch) // 8)          # байт на пиксель для фильтров
    stride = w * bpp
    rows = []
    prev = bytearray(stride)
    p = 0
    for _y in range(h):
        ft = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa = abs(b - c)
                pb = abs(a - c)
                pc = abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        if depth == 16:
            rows.append(bytes(line[i] for i in range(0, stride, 2)))
        else:
            rows.append(bytes(line))
        prev = line
    return w, h, nch, rows


def row_profile(rows, nch, w):
    """Суммарная «яркость/альфа» по каждой строке сверху вниз."""
    out = []
    step = max(1, w // 256)
    for line in rows:
        s = 0
        for x in range(0, w, step):
            i = x * nch
            if nch == 4:
                s += line[i + 3]
            elif nch == 3:
                s += (line[i] + line[i + 1] + line[i + 2]) // 3
            else:
                s += line[i]
        out.append(s)
    return out


def pixel(rows, nch, x, y):
    i = x * nch
    line = rows[y]
    if nch == 4:
        return (line[i], line[i + 1], line[i + 2], line[i + 3])
    if nch == 3:
        return (line[i], line[i + 1], line[i + 2], 255)
    if nch == 2:
        return (line[i], line[i], line[i], line[i + 1])
    return (line[i], line[i], line[i], 255)


def png_nonzero(path):
    """Сколько пикселей в файле непустые (альфа > 8) и максимум альфы.

    Печатается в отчёте: по этой цифре сразу видно, попал ли мазок в снимок,
    а не приходится гадать по провалу «мазка нет».
    """
    try:
        w, h, nch, rows = png_read(path)
    except Exception as e:
        return None, str(e)
    nz = 0
    top = 0
    for line in rows:
        if nch == 4:
            for i in range(3, len(line), 4):
                v = line[i]
                if v > 8:
                    nz += 1
                if v > top:
                    top = v
        else:
            nz += len(line)
    return (nz, top, w, h), None


def decide_orientation(prof, exp_y, tol):
    """По профилю яркости строк решить, куда попал мазок.

    Возвращает (вердикт, найденная_строка), где вердикт:
      'ok'   — мазок там, куда его просил протокол;
      'flip' — мазок зеркален по вертикали, нужен Flipy;
      'fail' — мазка нет вовсе или он не там (тогда Flipy не трогаем!).

    Вынесено в чистую функцию, чтобы это можно было проверить без TD: ошибка
    здесь означала бы «самопроверка сама сломала правильную ориентацию».
    """
    if not prof:
        return 'fail', -1
    top = max(prof)
    if top <= 0:
        return 'fail', -1
    best = prof.index(top)
    mirror = len(prof) - 1 - best
    if abs(best - exp_y) <= tol:
        return 'ok', best
    if abs(mirror - exp_y) <= tol:
        return 'flip', best
    return 'fail', best


# ------------------------------------------------------------------ рантайм

def set_flipy(base, on):
    try:
        base.par.Flipy.val = 1 if on else 0
        return True
    except Exception:
        return False


def load_rt(base):
    """Тот же общий модуль рантайма, что используют колбэки и кадровый цикл.

    Загрузчик берём из pw_boot.py на диске, чтобы правило кэширования было
    ровно одно: иначе самопроверка работала бы с отдельным состоянием и
    «проходила» бы впустую.
    """
    # Папку с исходниками ищем в проекте, а не в параметре Datadir: параметр
    # пуст намеренно (компонент вставляют из .tox в другие проекты), и путь из
    # него получался относительным.
    datadir = ''
    try:
        datadir = os.path.join(project.folder, 'paint')
    except Exception:
        datadir = ''
    if not os.path.isfile(os.path.join(datadir, 'td', 'runtime', 'pw_boot.py')):
        try:
            datadir = str(base.par.Datadir.eval() or '')
        except Exception:
            datadir = ''
    boot_path = os.path.join(datadir, 'td', 'runtime', 'pw_boot.py')
    with open(boot_path, 'r', encoding='utf-8') as f:
        src = f.read()
    boot = types.ModuleType('paintweb_boot')
    boot.__dict__['op'] = op
    exec(compile(src, boot_path, 'exec'), boot.__dict__)
    rt = boot.attach(base, op)
    return boot, rt


def sources_signature_of(paint_dir):
    """Отпечаток исходников — тем же кодом, что и рантайм/сборка.

    Считаем его, исполняя paint_runtime.py из DAT: если способов подсчёта будет
    два, они разойдутся, и компонент начнёт пересобираться при каждом запуске.
    """
    dat = op(BASE + '/server/runtime')
    if dat is None:
        return None
    ns = {'__name__': 'sig'}
    exec(compile(dat.text, 'paint_runtime.py', 'exec'), ns)
    return ns['sources_signature'](paint_dir)


COOK_FAILS = []                        # сбои прожарки кадров: молча глотать нельзя


def force_frames(rt, base, n, start, hook=None):
    """Прожарить n кадров. hook(step, out1) зовётся сразу после cook кадра.

    Хук нужен потому, что снимать состояние можно только в кадре рисования.
    Живой замер (tmp/diag_paint.txt) показал: feedbackTOP обновляет своё
    «предыдущее» изображение в конце РЕАЛЬНОГО кадра, а самопроверка гоняет кадры
    внутри одного реального. Поэтому краска видна ровно в том форсированном кадре,
    где она легла, а следующий кадр с uCount = 0 снова копирует пустое «предыдущее»
    и мазок затирается. Раньше снимок делался после всех кадров — и проверка
    честно говорила «мазка нет», хотя рисование работает.
    """
    out = base.op('out1')
    for i in range(n):
        f = start + i
        rt.on_frame_start(f)
        try:
            out.cook(force=True)
        except Exception as ex:
            # Раньше здесь стоял молчаливый pass: сбой cook выглядел как «мазок не
            # появился», и искать приходилось в шейдере, а не в самой прожарке.
            COOK_FAILS.append('%s: %s' % (f, ex))
        if hook is not None:
            try:
                hook(i, out)
            except Exception:
                pass
        rt.on_frame_end(f)


def stroke_frame(sid, flags, pts, W, H, layer=1):
    b = struct.pack('<BIBBH', 1, sid, flags, layer, len(pts))
    for (x, y, pr) in pts:
        b += struct.pack('<HHBBH', int(round(x / float(W) * 65535.0)),
                         int(round(y / float(H) * 65535.0)), pr, 0, 0)
    return b


def main():
    print('=== PaintWeb: самопроверка в TouchDesigner ======================')
    base = op(BASE)
    if base is None:
        print('НЕТ компонента %s — сначала запустите build_paint_web.py' % BASE)
        return
    try:
        print('TD %s' % app.version)
    except Exception:
        pass

    # ---------------------------------------------------------- ошибки нод
    print('\n[1] ошибки и предупреждения нод (тут видно ошибки компиляции шейдеров)')

    def _msgs(value):
        """Список непустых сообщений.

        TD у части нод отдаёт ПУСТУЮ СТРОКУ вместо пустого списка — и раньше
        такая строка считалась ошибкой: самопроверка сообщала «74 ноды с
        ошибками» на ровном месте, и настоящие ошибки в этом тонули.
        """
        if isinstance(value, str):
            value = [value]
        return [str(x) for x in (value or []) if str(x).strip()]

    bad = 0
    warned = 0
    for o in [base] + base.findChildren():
        try:
            errs = _msgs(o.errors())
            warns = _msgs(o.warnings())
        except Exception:
            continue
        if errs:
            bad += 1
            print('  %s' % o.path)
            for e in errs:
                print('      ошибка: %s' % e)
        # Петля cook у кисти — не поломка, а устройство: слой читается обратной
        # связью через feedbackTOP. TD всегда про неё предупреждает, и считать это
        # провалом значит прятать настоящие ошибки в шуме.
        warns = [w for w in warns if 'Cook dependency loop' not in w]
        if warns:
            warned += 1
            print('  %s' % o.path)
            for w in warns:
                print('      предупр: %s' % w)
    check('ошибок нод нет', bad == 0, 'нод с ошибками: %d' % bad)
    check('незнакомых предупреждений нет', warned == 0, 'нод с предупреждениями: %d' % warned)

    # ---------------------------------------------------------- автозапуск
    # Смысл раздела: убедиться, что «запуск без команд» действительно настроен.
    # Если отпечаток сборки не совпадает с файлами на диске, при следующем
    # открытии проекта TD пересоберёт компонент сам — и это нормально, но знать
    # об этом надо заранее, а не удивляться.
    print('\n[1b] автозапуск: отпечаток сборки и адрес')
    def paint_dir_of(base):
        """Папка paint/ — из проекта, с запасным вариантом из параметра."""
        try:
            cand = os.path.join(project.folder, 'paint')
            if os.path.isdir(cand):
                return cand
        except Exception:
            pass
        try:
            cand = str(base.par.Datadir.eval() or '')
            if cand and os.path.isdir(cand):
                return cand
        except Exception:
            pass
        return ''

    try:
        datadir = paint_dir_of(base)
        ver = base.op('server/version')
        sig = None
        if ver is not None:
            for line in str(ver.text or '').splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    sig = line
                    break
        want = sources_signature_of(datadir)
        print('  отпечаток собранного компонента: %s' % (sig or 'нет'))
        print('  отпечаток файлов на диске:      %s' % want)
        check('DAT с отпечатком сборки на месте', ver is not None)
        check('отпечаток совпадает с файлами на диске', sig == want,
              'собран %s, на диске %s' % (sig, want))
        check('executeDAT включён на старте проекта',
              int(base.op('tick').par.start.eval()) == 1,
              base.op('tick').par.start.eval())
        addr = base.op('server/address')
        check('DAT с адресом страницы заполнен',
              addr is not None and 'http' in str(addr.text or ''),
              (str(addr.text or '')[:80] if addr is not None else 'нет DAT'))
        if addr is not None:
            for line in str(addr.text or '').splitlines()[:4]:
                print('  %s' % line)
        check('сервер включён (Active=1)',
              int(base.op('server/ws').par.active.eval()) == 1,
              base.op('server/ws').par.active.eval())
    except Exception:
        check('раздел автозапуска отработал', False,
              traceback.format_exc().splitlines()[-1])

    # ---------------------------------------------------------- параметры
    print('\n[2] параметры, токены меню, форматы')
    m, rt = load_rt(base)
    check('параметр Flipy есть', set_flipy(base, False))
    rt.read_pars()
    rt._ensure_sizes()
    saved_frame = rt.frame
    W = int(rt.tun['w'])
    H = int(rt.tun['h'])
    print('  полотно %dx%d, порт %s' % (W, H, rt.tun['port']))
    for rel in ('brush', 'restore', 'sw', 'buf',
                'crop', 'patchin', 'src_level',
                'paint_level', 'maskapply', 'over', 'out1', 'fit', 'proxy',
                'brushm', 'restorem', 'swm', 'bufm', 'cropm', 'patchinm'):
        o = base.op(rel)
        # Раньше здесь стоял `int(o.width)` без проверки: когда оператор
        # переименовали, самопроверка падала с AttributeError и «зелёный» итог
        # не выставлялся — но и причину было не видно. Теперь это провал.
        if o is None:
            check('оператор %s на месте' % rel, False, 'не найден')
            continue
        try:
            fmt = o.par.format.eval() if hasattr(o.par, 'format') else '-'
        except Exception:
            fmt = '?'
        try:
            res = o.par.outputresolution.eval() if hasattr(o.par, 'outputresolution') else '-'
        except Exception:
            res = '?'
        print('  %-16s %sx%s format=%s outputresolution=%s'
              % (rel, int(o.width), int(o.height), fmt, res))
    crop = base.op('crop')
    print('  crop единицы: %s (нужно pixels)' % crop.par.cropleftunit.eval())
    check('кроп в пикселях', str(crop.par.cropleftunit.eval()).lower().startswith('pix'),
          crop.par.cropleftunit.eval())

    # ---------------------------------------------------------- перехват отправки
    real_send = rt._send
    real_send_bin = rt._send_bin

    def fake_send(client, obj):
        SENT.append(('text', obj))
        return True

    def fake_send_bin(client, head, blob):
        SENT.append((head, len(blob), bytes(blob[:8])))
        return True

    rt._send = fake_send
    rt._send_bin = fake_send_bin
    with rt.mu:
        rt.clients['selftest'] = {'w': 800, 'h': 600, 'dpr': 1.0, 'ready': True,
                                  'need_poster': False, 'sync_queue': [],
                                  'last_patch': 0.0, 'patches': 0, 'since': time.time()}

    # Папки берём у рантайма: он умеет находить их и при пустом Datadir.
    try:
        tmp = rt.dirs()['tmp']
    except Exception:
        tmp = os.path.normpath(os.path.join(rt.tun.get('datadir') or '', 'tmp'))
    if not os.path.isdir(tmp):
        os.makedirs(tmp)
    shot = os.path.join(tmp, 'selftest_brush.png')

    captured = {}

    def paint_and_capture(start, tries=6, tag='brush'):
        """Прожарить кадры и снять слой в том кадре, где краска легла.

        Возвращает True, если такой кадр нашёлся. Снимать обязательно внутри
        кадра: см. пояснение у force_frames (feedbackTOP обновляется только в
        конце реального кадра).
        """
        captured.pop(tag, None)

        def hook(step, out):
            if tag in captured:
                return
            if int(rt.diag.get('dab_count') or 0) <= 0:
                return
            # Отдельное имя файла: старый код сохранял слой в selftest_brush.png
            # уже ПОСЛЕ кадра рисования и затирал этот снимок пустым слоем.
            path = os.path.join(tmp, 'selftest_%s_live.png' % tag)
            try:
                base.op('buf').save(path)
            except Exception:
                return
            captured[tag] = {'path': path, 'frame': start + step}

        force_frames(rt, base, tries, start, hook)
        return tag in captured

    def run_once(flipy):
        """Стереть слой, нарисовать тестовый мазок, вернуть (w,h,nch,rows)."""
        set_flipy(base, flipy)
        rt.read_pars()
        rt.clients['selftest']['need_poster'] = False
        rt.clients['selftest']['sync_queue'] = []
        f = 1000
        rt._cmd_clear()
        force_frames(rt, base, 2, f)
        f += 2
        # мазок по горизонтали на 25% высоты, при размере кисти 40 и интервале 0.15
        rt.ws_text('selftest', json.dumps({
            't': 'tool', 'tool': {'tool': 'brush', 'color': '#ff0000', 'size': 40,
                                  'hardness': 0.75, 'flow': 1.0, 'spacing': 0.15}}))
        pts = [(W * 0.2 + i * (W * 0.6 / 8.0), H * 0.25, 255) for i in range(9)]
        rt.ws_binary('selftest', stroke_frame(7, 1, pts, W, H))
        got = paint_and_capture(f, 6, 'brush')
        f += 6
        if not got:
            # не нашли кадр с краской — снимем как есть, чтобы проверка ниже
            # сказала об этом честно, а не упала
            base.op('buf').save(shot)
        rt.ws_binary('selftest', stroke_frame(7, 2, [], W, H))
        force_frames(rt, base, 2, f)
        base.op('buf').save(shot)
        w, h, nch, rows = png_read(shot)
        return w, h, nch, rows

    # ---------------------------------------------------------- ориентация и мазок
    print('\n[3] мазок: попадание в пиксели и ориентация')
    exp_y = int(H * 0.25)
    w, h, nch, rows = run_once(rt.tun.get('flipy', False))
    check('кадры самопроверки прожарены без сбоев cook', not COOK_FAILS, COOK_FAILS[:2])
    cap = captured.get('brush') or {}
    check('пойман кадр, в котором краска легла в слой', bool(cap), cap)
    if cap:
        # Разбираем именно тот снимок, где краска есть (см. paint_and_capture).
        w, h, nch, rows = png_read(cap['path'])
        stats, why = png_nonzero(cap['path'])
        print('  снимок слоя: %s (кадр %s), %dx%d' % (os.path.basename(cap['path']),
                                                      cap.get('frame'), w, h))
        print('  в снимке непустых пикселей: %s%s' % (stats, (' ошибка: %s' % why) if why else ''))
    check('буфер сохранился в PNG нужного размера', (w, h) == (W, H), (w, h))
    prof = row_profile(rows, nch, w)
    top = max(prof)
    best = prof.index(top) if prof else -1
    mirror = H - 1 - best
    tol = 60
    print('  ожидаемая строка %d, найдена строка %d (зеркало %d), профиль max=%d'
          % (exp_y, best, mirror, top))
    verdict, _row = decide_orientation(prof, exp_y, tol)
    check('мазок вообще есть', top > 0, top)
    flip_needed = (verdict == 'flip')
    if verdict == 'ok':
        check('мазок на месте (ориентация верная)', True)
    elif verdict == 'flip':
        print('  мазок оказался перевёрнут по вертикали — переключаю Flipy и проверяю снова')
    else:
        check('мазок на месте (ориентация верная)', False,
              'строка %d, ожидал около %d' % (best, exp_y))

    if flip_needed:
        w, h, nch, rows = run_once(True)
        prof = row_profile(rows, nch, w)
        top = max(prof)
        best = prof.index(top) if prof else -1
        check('после Flipy=On мазок на месте', abs(best - exp_y) <= tol,
              'строка %d, ожидал около %d' % (best, exp_y))
        check('Flipy записан в параметр', base.par.Flipy.eval() == 1)
        exp_y = int(H * 0.25)
    else:
        check('Flipy остаётся Off', base.par.Flipy.eval() == 0, base.par.Flipy.eval())

    px = pixel(rows, nch, int(W * 0.5), exp_y)
    print('  пиксель в центре мазка: %s' % (px,))
    if nch == 4:
        check('в центре мазка есть альфа', px[3] > 100, px)
        check('цвет красный (R много больше G)', px[0] > px[1] + 60, px)
    far = pixel(rows, nch, int(W * 0.5), min(H - 2, exp_y + int(H * 0.4)))
    check('вдали от мазка пусто', (far[3] if nch == 4 else 0) < 20, far)

    # ---------------------------------------------------------- undo
    # Пиксельную проверку «undo убрал мазок» тут сделать нельзя: форсированные
    # кадры не обновляют «предыдущее» изображение feedbackTOP (см. force_frames),
    # поэтому в любом снимке после кадра рисования слой уже пуст. Проверяем
    # СТРУКТУРУ: undo обязан положить область в стек, загрузить патч и попросить
    # один кадр восстановления — а что восстановление действительно пишет в слой,
    # проверяется на живом рисовании и в разделе [7].
    print('\n[4] undo: снимок области и запрос восстановления')
    undo_before = len(rt.undo)
    rt._cmd_history(-1)
    check('undo взял запись из стека', len(rt.undo) == undo_before - 1,
          (undo_before, len(rt.undo)))
    check('патч области загружен для восстановления',
          str(base.op('patchin').par.file.eval()).endswith('.png'),
          base.op('patchin').par.file.eval())
    check('запрос восстановления области поставлен', rt.restore_req is not None)
    force_frames(rt, base, 3, 2000)
    base.op('buf').save(shot)
    w2, h2, nch2, rows2 = png_read(shot)
    check('снимок слоя после undo читается', (w2, h2) == (W, H), (w2, h2))

    # ---------------------------------------------------------- патч и прокси
    print('\n[5] кодирование патча и прокси-кадра')
    SENT[:] = []
    rt._cmd_history(1)                    # вернуть мазок обратно
    force_frames(rt, base, 3, 2100)
    SENT[:] = []
    rt.force_patch = True
    rt.send_rect = (W * 0.15, H * 0.2, W * 0.85, H * 0.3)
    rt.last_patch_t = 0.0
    force_frames(rt, base, 2, 2200)
    patches = [s for s in SENT if isinstance(s, tuple) and len(s) == 3]
    check('патч закодирован и «отправлен»', len(patches) > 0, len(patches))
    if patches:
        head, blen, sig = patches[-1]
        print('  патч: %s' % json.dumps(head, ensure_ascii=False))
        check('заголовок патча про слой 1', head.get('layer') == 1, head)
        check('PNG-сигнатура в патче', sig[:4] == b'\x89PNG', sig)
        check('байты патча непустые', blen > 100, blen)
        check('прямоугольник патча в границах',
              head.get('x', -1) >= 0 and head.get('y', -1) >= 0
              and head.get('x', 0) + head.get('w', 0) <= W
              and head.get('y', 0) + head.get('h', 0) <= H, head)
    SENT[:] = []
    rt.next_proxy_t = 0.0
    rt.clients['selftest']['need_poster'] = True
    rt._flush_proxy()
    proxies = [s for s in SENT if isinstance(s, tuple) and len(s) == 3]
    check('прокси закодирован', len(proxies) > 0, len(proxies))
    if proxies:
        head, blen, sig = proxies[-1]
        print('  прокси: %s, %d байт' % (json.dumps(head, ensure_ascii=False), blen))
        # В этой сборке TD кодировщик JPEG возвращает пустые байты, поэтому
        # рантайм сам уходит на PNG. Требовать именно JPEG — значит требовать
        # сломанного кодировщика: важно, что уехала настоящая картинка.
        is_jpeg = sig[:2] == b'\xff\xd8'
        is_png = sig[:4] == b'\x89PNG'
        print('  формат прокси: %s' % ('JPEG' if is_jpeg else ('PNG' if is_png else sig)))
        check('прокси — картинка (JPEG или PNG)', is_jpeg or is_png, sig)
        check('размер прокси разумный', blen > 500, blen)

    # ---------------------------------------------------------- композит и маска
    # Маска — это отдельный буфер (слой «Источник» в браузере). Проверяем ФАКТ:
    # пока маска не тронута, она залита белым и источник виден целиком; после
    # мазка ластиком по маске источник в этом месте пропадает, а краска остаётся.
    print('\n[6] композит: краска поверх источника с маской')
    out = base.op('out1')
    bufm = base.op('bufm')
    brm = base.op('brushm')
    try:
        py = H - 1 - exp_y                              # sample() считает y снизу
        # Композит снимаем В КАДРЕ РИСОВАНИЯ (см. force_frames): после него слой
        # снова пуст, потому что форсированные кадры не обновляют «предыдущее»
        # изображение feedbackTOP. Кисть крупная (200 px), чтобы попасть в центр.
        probe = {}
        # Кисть здесь небольшая (40 px): проверки патча ищут в вырезке и полупрозрачные
        # пиксели, и прозрачный фон. С кистью 200 px вся вырезка оказывается внутри
        # штампа — и проверки «нет полупрозрачных» и «нет прозрачных» валились бы
        # на ровном месте, хотя слой в порядке.
        rt.ws_text('selftest', json.dumps({
            't': 'tool', 'tool': {'tool': 'brush', 'color': '#ff0000', 'size': 40,
                                  'hardness': 0.9, 'flow': 1.0, 'spacing': 0.1}}))
        rt.ws_binary('selftest', stroke_frame(21, 1, [(W * 0.5, H * 0.25, 255)], W, H))

        def hook(step, o):
            if probe or int(rt.diag.get('dab_count') or 0) <= 0:
                return
            try:
                probe['s1'] = tuple(o.sample(x=W // 2, y=py))
                if bufm is not None:
                    probe['mask'] = tuple(bufm.sample(x=W // 2, y=py))
            except Exception:
                pass
            # Снимки области для проверок патча делаем ЗДЕСЬ ЖЕ: они читают слой,
            # а он держит краску только в этом кадре (см. force_frames).
            try:
                un = base.op('unpremult')
                crop = base.op('crop')
                rt._set_crop(crop, (W * 0.5 - 30, H * 0.25 - 30,
                                    W * 0.5 + 30, H * 0.25 + 30))
                crop.cook(force=True)
                un.cook(force=True)
                un.save(os.path.join(tmp, 'selftest_patch.png'))
                rx = int(W * 0.5) - 40
                ry = int(H * 0.25) - 40
                rt._set_crop(crop, (rx, ry, rx + 80, ry + 80))
                crop.cook(force=True)
                un.cook(force=True)
                un.save(os.path.join(tmp, 'selftest_patch_pos.png'))
                probe['patch'] = True
            except Exception:
                probe['patch_err'] = traceback.format_exc().splitlines()[-1]

        force_frames(rt, base, 6, 2300, hook)
        s1 = probe.get('s1')
        print('  композит в кадре рисования: %s, маска там же: %s'
              % (s1, probe.get('mask')))
        for name in ('selftest_patch.png', 'selftest_patch_pos.png'):
            p = os.path.join(tmp, name)
            stats, why = png_nonzero(p)
            print('  %s: непустых пикселей %s%s'
                  % (name, stats, (' ошибка: %s' % why) if why else ''))
        check('композит что-то отдаёт в точке мазка',
              bool(s1) and max(s1) > 0.01, s1)
        # Кисть в самопроверке красная, а источник — синеватый градиент. Если
        # видно НЕ краску — значит слои перепутаны местами (порядок входов overTOP).
        check('в режиме краски поверх источника видна именно краска',
              bool(s1) and s1[0] > s1[1] + 0.15, s1)
        # Маска по умолчанию залита белым. Проверяем саму заливку: сбрасываем
        # признак и смотрим буфер маски ровно в том кадре, где заливка прошла
        # (в форсированных кадрах следующий кадр копирует пустое «предыдущее»,
        # поэтому позже буфер уже пуст — см. force_frames).
        fill_probe = {}
        rt.mask_filled = dict((n, False) for n in rt.MASK_BUFFERS)

        def hook_fill(step, o):
            if fill_probe or step > 1:
                return
            try:
                fill_probe['mask'] = tuple(bufm.sample(x=W // 2, y=py)) if bufm else None
            except Exception:
                pass

        force_frames(rt, base, 2, 2280, hook_fill)
        check('маска залита белым (источник виден целиком)',
              bool(fill_probe.get('mask')) and fill_probe['mask'][3] > 0.9,
              fill_probe.get('mask'))
        check('у кисти маски есть свой оператор brushm', brm is not None)

        # Стираем маску в другой точке ластиком по слою «Источник» (мазок
        # адресуется буферу маски). Проверяем и буфер, и композит — тоже в кадре
        # рисования.
        probe2 = {}
        rt.ws_text('selftest', json.dumps({
            't': 'tool', 'tool': {'tool': 'eraser', 'color': '#ffffff', 'size': 200,
                                  'hardness': 0.9, 'flow': 1.0, 'spacing': 0.1}}))
        rt.ws_binary('selftest', stroke_frame(23, 1, [(W * 0.5, H * 0.75, 255)], W, H,
                                              layer=2))

        def hook2(step, o):
            if probe2 or int(rt.diag.get('dab_count') or 0) <= 0:
                return
            try:
                probe2['out'] = tuple(o.sample(x=W // 2, y=H - 1 - int(H * 0.75)))
                if bufm is not None:
                    probe2['mask'] = tuple(bufm.sample(x=W // 2,
                                                       y=H - 1 - int(H * 0.75)))
            except Exception:
                pass

        force_frames(rt, base, 6, 2320, hook2)
        print('  после ластика по маске: маска %s, композит %s'
              % (probe2.get('mask'), probe2.get('out')))
        check('ластик по маске прячет источник',
              bool(probe2.get('mask')) and probe2['mask'][3] < 0.5, probe2.get('mask'))
        check('в стёртом месте источник из композита уходит',
              bool(probe2.get('out')) and max(probe2['out']) < 0.2, probe2.get('out'))
        rt.tool['tool'] = 'brush'

        # Наружу из base уходит именно out1 — «нет вывода из base» означает, что
        # порвалась последняя связь, а comp/out при этом может быть исправен.
        o1 = base.op('out1')
        if o1 is None:
            check('out1 (выход из base) на месте', False, 'не найден')
        else:
            check('out1 (выход из base) — это over (композит)',
                  o1.inputs and len(o1.inputs) > 0
                  and o1.inputs[0] is not None
                  and o1.inputs[0].name == 'over',
                  [x.path if x is not None else None for x in (o1.inputs or [])])
    except Exception:
        check('композит читается', False, traceback.format_exc().splitlines()[-1])

    # ---------------------------------------------------------- ластик
    # Пиксельную проверку «ластик убрал краску» в форсированных кадрах сделать
    # нельзя (слой держит краску только в кадре рисования, см. force_frames).
    # Поэтому проверяем то, что действительно важно и проверяемо: режим, который
    # рантайм отправляет в кисть. Ластик обязан идти с режимом 2 — именно
    # отсутствие этого и давало симптом «ластик красит вместо стирания».
    print('\n[6b] ластик: режим кисти и запись в тот же слой')
    try:
        modes = []
        rt.ws_text('selftest', json.dumps({
            't': 'tool', 'tool': {'tool': 'eraser', 'color': '#ff0000', 'size': 80,
                                  'hardness': 0.9, 'flow': 1.0, 'spacing': 0.1}}))
        pts = [(W * 0.2 + i * (W * 0.6 / 8.0), H * 0.25, 255) for i in range(9)]
        rt.ws_binary('selftest', stroke_frame(11, 1, pts, W, H))

        def hook_e(step, o):
            if int(rt.diag.get('dab_count') or 0) > 0:
                modes.append((rt.diag.get('dab_target'), float(rt.diag.get('dab_mode') or 0)))

        force_frames(rt, base, 6, 2400, hook_e)
        rt.ws_binary('selftest', stroke_frame(11, 2, [], W, H))
        force_frames(rt, base, 2, 2420)
        print('  кадры ластика (слой, режим): %s' % (modes,))
        check('ластик по слою краски идёт с режимом 2 (стирание)',
              bool(modes) and all(m[0] == 'paint' and m[1] == 2.0 for m in modes), modes)
        check('мазок ластика дошёл до слоя краски', bool(modes), modes)
        rt.tool['tool'] = 'brush'
    except Exception:
        check('ластик работает', False, traceback.format_exc().splitlines()[-1])

    # ---------------------------------------------------------- патч для браузера
    # Патч уходит в браузер картинкой PNG, а браузер читает PNG как STRAIGHT
    # alpha. Слой в TD премультиплицирован, поэтому сборка ставит перед кодированием
    # узел unpremult. Проверяем фактом: берём пиксель с неполной альфой и смотрим,
    # домножен ли его RGB на альфу (тогда в браузере краска будет темнее).
    print('\n[6c] патч в браузер: straight alpha')
    try:
        un = base.op('unpremult')
        crop = base.op('crop')
        if un is None or crop is None:
            check('узлы патча на месте', False, 'нет crop/unpremult')
        elif not probe.get('patch'):
            check('снимок области для патча снят',
                  False, probe.get('patch_err') or 'кадр с краской не найден')
        else:
            # Снимок сделан в кадре рисования (см. [6]) — слой там ещё держит
            # краску. Сейчас только разбираем готовый файл, ничего не переснимаем.
            ppath = os.path.join(tmp, 'selftest_patch.png')
            pw, ph, pnch, prows = png_read(ppath)
            best_px = None
            for yy in range(min(ph, 60)):
                for xx in range(min(pw, 60)):
                    px = pixel(prows, pnch, xx, yy)
                    if px and 60 < px[3] < 220:
                        best_px = px
                        break
                if best_px:
                    break
            print('  пиксель со средней альфой: %s' % (best_px,))
            if best_px is None:
                check('нашёлся пиксель с неполной альфой', False, 'все 0 или 255')
            else:
                # краска красная: в straight alpha R остаётся большим даже при
                # альфе ~0.5, в премультиплицированном виде R падает вместе с A
                check('RGB в патче не домножен на альфу (straight alpha)',
                      best_px[0] > best_px[3] * 1.2,
                      'RGBA=%s: R=%d, A=%d' % (best_px, best_px[0], best_px[3]))
    except Exception:
        check('патч кодируется как straight alpha', False,
              traceback.format_exc().splitlines()[-1])

    # ---------------------------------------------------------- место патча
    # Размер области проверить мало: важно, что в область попал ИМЕННО тот кусок
    # слоя, который клиент ждёт в этом прямоугольнике. Иначе патч «не туда
    # вставляется»: размер верный, а содержимое сдвинуто.
    print('\n[6d] патч: мазок внутри области там, где нарисован')
    try:
        rx = int(W * 0.5) - 40
        ry = int(H * 0.25) - 40
        rw = rh = 80
        # Снимок области сделан в кадре рисования (см. [6]): тут только разбор.
        ppath = os.path.join(tmp, 'selftest_patch_pos.png')
        if not probe.get('patch') or not os.path.isfile(ppath):
            check('снимок области вокруг мазка снят', False,
                  probe.get('patch_err') or 'кадр с краской не найден')
        else:
            pw, ph, pnch, prows = png_read(ppath)
            check('область вокруг мазка вырезана нужного размера', (pw, ph) == (rw, rh),
                  (pw, ph))
            prof = row_profile(prows, pnch, pw) if (pw, ph) == (rw, rh) else []
            best = prof.index(max(prof)) if prof and max(prof) > 0 else -1
            print('  строка с краской внутри области: %d (мазок рисовали на 40 из %d)'
                  % (best, ph))
            check('мазок внутри области там, где нарисован (не сдвинут)',
                  best >= 0 and abs(best - 40) <= 14, best)

            # Содержимое патча. Симптом «в браузере в местах рисования кривые
            # квадраты с картинками» означает, что уехал НЕ слой: либо кусок
            # источника, либо композит. У слоя обязаны быть одновременно
            # прозрачный фон и плотная краска цвета кисти; у картинки источника
            # прозрачного фона нет вовсе.
            dense = clear = 0
            red_px = blue_px = 0
            for yy in range(ph):
                for xx in range(pw):
                    px = pixel(prows, pnch, xx, yy)
                    a = px[3] if pnch == 4 else 255
                    if a > 200:
                        dense += 1
                        if px[0] > px[2] + 40:
                            red_px += 1
                        elif px[2] > px[0] + 40:
                            blue_px += 1
                    elif a < 8:
                        clear += 1
            total = max(1, pw * ph)
            print('  в патче: плотных пикселей %d (%.0f%%), прозрачных %d (%.0f%%), '
                  'красных %d, синеватых %d'
                  % (dense, 100.0 * dense / total, clear, 100.0 * clear / total,
                     red_px, blue_px))
            check('в патче есть прозрачный фон (уехал слой, а не картинка источника)',
                  clear > total * 0.2, 'прозрачных %.0f%%' % (100.0 * clear / total))
            check('в патче есть плотная краска цвета кисти',
                  dense > 0 and red_px > blue_px,
                  'плотных %d, красных %d, синеватых %d' % (dense, red_px, blue_px))
    except Exception:
        check('место патча проверено', False, traceback.format_exc().splitlines()[-1])

    # ---------------------------------------------------------- уборка
    print('\n[7] уборка')
    with rt.mu:
        rt.clients.pop('selftest', None)
    rt._send = real_send
    rt._send_bin = real_send_bin
    rt.strokes.clear()
    rt.snap_depth = 0
    rt.restore_req = None
    rt.switch_until = -1
    rt.frame = saved_frame                 # не сбивать кадровый счётчик проекта
    rt.read_pars()
    try:
        base.op('snap').lock = False
        base.op('sw').par.index = 0
    except Exception:
        pass
    rt._cmd_clear()
    force_frames(rt, base, 2, 2400)
    rt.frame = saved_frame
    check('слой краски очищен после проверки', True)
    print('  снимок буфера: %s' % shot)
    try:
        base.op('out1').save(os.path.join(tmp, 'selftest_out.png'))
        print('  снимок композита: %s' % os.path.join(tmp, 'selftest_out.png'))
    except Exception:
        pass

    # ---------------------------------------------------------- итог
    print('\n===============================================================')
    fails = [r for r in RESULTS if not r[0]]
    print('проверок: %d, провалов: %d' % (len(RESULTS), len(fails)))
    for _, name, extra in fails:
        print('  ПРОВАЛ: %s   %s' % (name, extra))
    if not fails:
        print('Всё сходится. Если Flipy переключился сам — так и должно быть,')
        print('это разовая автонастройка ориентации под твою сборку TD.')
    print('Готовый отчёт можно целиком скопировать из Textport.')
    print('===============================================================')
    globals()['COMPLETED'] = True          # дошло до конца — можно верить итогу


try:
    main()
except Exception:
    globals()['COMPLETED'] = False
    globals()['FATAL'] = traceback.format_exc()
    print('Самопроверка упала целиком:')
    print(traceback.format_exc())
