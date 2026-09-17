# -*- coding: utf-8 -*-
"""PaintWeb: точечная диагностика — почему синтетический мазок не попадает в слой.

Зачем отдельный скрипт. Самопроверка говорит «мазка нет», но не говорит, на
каком шаге он теряется: штампы могли не построиться, могли не доехать до
текстуры, могли приехать с опозданием на кадр (moviefileinTOP читает файл не
мгновенно), а могли быть отброшены переключателем слоя. Здесь на каждом кадре
печатается всё сразу:

  * что решил рантайм (сколько штампов построено, сколько записано в текстуру);
  * что реально стоит в uniform-ах кисти (uCount, uRect, uRes) — читаем обратно
    ИЗ НОДЫ, а не из своих переменных;
  * что лежит в текстуре штампов (читаем её через sample и сравниваем с тем,
    что мы туда положили);
  * что лежит в слое (buf) и на выходе (out1) в точке мазка.

В конце — решающие опыты A/B на одной и той же пачке штампов:

  A. записать текстуру и нарисовать в ТОМ ЖЕ кадре (как делает рантайм сейчас);
  B. нарисовать на СЛЕДУЮЩЕМ кадре, не трогая текстуру;
  C. то же, но с областью на весь холст (отделяет «не та область» от прочего).

Если краска появляется в B, но не в A — текстура доезжает до шейдера с
опозданием на кадр, и лечится это доставкой штампов, а не шейдером.

Запуск (Textport, одна строка):

  exec(open(r"C:\\Users\\DaniilNotebook\\Documents\\PixelFlow\\paint\\td\\diag_paint.py", encoding="utf-8").read())

ВНИМАНИЕ: диагностика стирает слой краски (ей нужен чистый холст).

Отчёт: paint/tmp/diag_paint.txt
"""

import json
import os
import struct
import time
import traceback
import types

BASE = '/project1/paint_web'
OUT = []


def say(line=''):
    OUT.append(str(line))
    print(line)


def pack16(v):
    """То же, что paint_runtime._pack16 (см. brush.glsl)."""
    n = int(round(max(0.0, min(8000.0, float(v))) * 8.0))
    return (n >> 8) & 0xFF, n & 0xFF


def pack8(v):
    return int(round(max(0.0, min(1.0, float(v))) * 255.0))


def load_rt(base):
    """Живой экземпляр рантайма — тот же, что у колбэков и кадрового цикла."""
    # Папку ищем в проекте: параметр Datadir пуст намеренно (компонент вставляют
    # из .tox в другие проекты), и путь из него вышел бы относительным.
    cands = []
    try:
        cands.append(os.path.join(project.folder, 'paint'))
    except Exception:
        pass
    try:
        cands.append(str(base.par.Datadir.eval() or ''))
    except Exception:
        pass
    boot_path = None
    for cand in cands:
        if not cand:
            continue
        p = os.path.join(cand, 'td', 'runtime', 'pw_boot.py')
        if os.path.isfile(p):
            boot_path = p
            break
    if boot_path is None:
        raise RuntimeError('не нашёл td/runtime/pw_boot.py — укажи параметр Datadir')
    with open(boot_path, 'r', encoding='utf-8') as f:
        src = f.read()
    boot = types.ModuleType('paintweb_boot')
    boot.__dict__['op'] = op
    exec(compile(src, boot_path, 'exec'), boot.__dict__)
    return boot.attach(base, op)


def stroke_frame(sid, flags, pts, W, H):
    b = struct.pack('<BIBBH', 1, sid, flags, 1, len(pts))
    for (x, y, pr) in pts:
        b += struct.pack('<HHBBH', int(round(x / float(W) * 65535.0)),
                         int(round(y / float(H) * 65535.0)), pr, 0, 0)
    return b


def par_of(o, name):
    try:
        p = o.par[name]
        return str(p.eval()) if p is not None else None
    except Exception:
        return '?'


def vec_of(o, idx):
    """Прочитать обратно uniform-ы кисти: что реально стоит в ноде."""
    out = []
    for comp in 'xyzw':
        try:
            out.append(round(float(o.par['vec%dvalue%s' % (idx, comp)].eval()), 3))
        except Exception:
            out.append(None)
    return out


def errors_of(o):
    try:
        e = o.errors() or []
        if isinstance(e, str):
            e = [e]
        return [str(s) for s in e]
    except Exception:
        return ['<нет доступа>']


def warnings_of(o):
    try:
        w = o.warnings() or []
        if isinstance(w, str):
            w = [w]
        return [str(s) for s in w]
    except Exception:
        return []


def main():
    base = op(BASE)
    if base is None:
        say('НЕТ компонента %s' % BASE)
        return
    say('=== PaintWeb: диагностика доставки штампов ===')
    say('время: %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    rt = load_rt(base)
    saved_frame = rt.frame                # не сбивать кадровый счётчик проекта
    W = int(rt.tun['w'])
    H = int(rt.tun['h'])
    say('полотно %dx%d, flipy=%s, вставка источника=%s, режим отправки=%s'
        % (W, H, rt.tun.get('flipy'), rt.fit_name(), rt.tun.get('patchmode')))

    out = base.op('out1')
    buf = base.op('buf')
    brush = base.op('brush')
    dab = base.op('dabpng')
    sw = base.op('sw')
    fb = base.op('fb')

    # --- состояние нод, которое важно для доставки штампов ---
    say()
    say('--- ноды ---')
    for rel in ('dabpng', 'brush', 'fb', 'sw', 'buf', 'out1', 'crop', 'unpremult'):
        o = base.op(rel)
        if o is None:
            say('  %-9s НЕТ НОДЫ' % rel)
            continue
        ins = []
        try:
            ins = [x.path for x in o.inputs]
        except Exception:
            pass
        wh = ''
        try:
            wh = ' %dx%d' % (int(o.width), int(o.height))
        except Exception:
            pass
        say('  %-9s%s входы=%s' % (rel, wh, ins))
    if dab is not None:
        say('  dabpng: файл=%s inputcolorspace=%s premultrgbbyalpha=%s format=%s play=%s'
            % (os.path.basename(str(dab.par.file.eval())),
               par_of(dab, 'inputcolorspace'), par_of(dab, 'premultrgbbyalpha'),
               par_of(dab, 'format'), par_of(dab, 'play')))
        say('  dabpng: ошибки=%s' % errors_of(dab))
    if fb is not None:
        say('  fb.target=%s' % par_of(fb, 'top'))
    if brush is not None:
        say('  brush: ошибки=%s' % errors_of(brush))
        say('  brush: предупреждения=%s' % warnings_of(brush)[:1])

    # --- перехват отправки клиенту, чтобы диагностика ничего не слала в браузер ---
    real_send, real_send_bin = rt._send, rt._send_bin
    sent = []

    def fake_send(client, obj):
        sent.append(obj)
        return True

    def fake_send_bin(client, head, blob):
        sent.append(head)
        return True

    rt._send = fake_send
    rt._send_bin = fake_send_bin
    with rt.mu:
        rt.clients['diag'] = {'w': 800, 'h': 600, 'dpr': 1.0, 'ready': True,
                              'need_poster': False, 'sync_queue': [],
                              'last_patch': 0.0, 'patches': 0, 'since': time.time()}

    # --- следим, что именно строит _build_dabs ---
    seen = {'cur_dabs': None, 'cur_rect': None,
            'last_dabs': None, 'last_rect': None}
    orig_build = rt._build_dabs

    def spy_build():
        orig_build()
        if rt.frame_dabs['paint']:
            seen['cur_dabs'] = list(rt.frame_dabs['paint'])
            seen['cur_rect'] = rt.frame_rect['paint']
            seen['last_dabs'] = list(rt.frame_dabs['paint'])
            seen['last_rect'] = rt.frame_rect['paint']

    rt._build_dabs = spy_build

    cx, cy = int(W * 0.5), int(H * 0.5)
    cy_s = H - 1 - cy                      # sample() считает y снизу

    def sample_at(o):
        try:
            return tuple(round(v, 3) for v in o.sample(x=cx, y=cy_s))
        except Exception as ex:
            return 'sample: %s' % ex

    def set_uniforms(cnt, rect, color=(1.0, 0.0, 0.0, 1.0), mode=None, clear=0.0):
        hsign = -1.0 if rt.tun.get('flipy') else 1.0
        m = float(rt.diag.get('dab_mode') or 0.0) if mode is None else mode
        rt._vec(brush, 0, 'uRes', (float(W), float(H) * hsign, 1.0 / W, 1.0 / H))
        rt._vec(brush, 1, 'uCount', (float(cnt), m, float(clear), 1.0))
        rt._vec(brush, 2, 'uColor', color)
        rt._vec(brush, 3, 'uRect', rect)

    def clear_layer():
        """Стереть слой ровно одним кадром очистки."""
        set_uniforms(0, (-4.0, -4.0, 0.0, 0.0), clear=1.0)
        out.cook(force=True)

    def probe(tag, frame):
        vals = {}
        if brush is not None:
            vals['uRes'] = vec_of(brush, 0)
            vals['uCount'] = vec_of(brush, 1)
            vals['uColor'] = vec_of(brush, 2)
            vals['uRect'] = vec_of(brush, 3)
        d = rt.diag
        say('  кадр %-6s %s' % (frame, tag))
        say('      рантайм: dabs=%s dab_tex=%s dab_png=%s pushes=%s clear=%s mode=%s rect=%s'
            % (d.get('dabs'), d.get('dab_tex'), d.get('dab_png'), d.get('dab_pushes'),
               d.get('dab_clear'), d.get('dab_mode'), seen.get('cur_rect')))
        say('      состояние: strokes=%d sw.index=%s switch_until=%s restore=%s ошибки=%s'
            % (len(rt.strokes), par_of(sw, 'index') if sw is not None else '?',
               getattr(rt, 'switch_until', '?'),
               bool(getattr(rt, 'restore_req', None)),
               len(rt.diag.get('error_list') or [])))
        say('      uniform: uCount=%s uRect=%s uRes.y=%s uColor=%s'
            % (vals.get('uCount'), vals.get('uRect'),
               (vals.get('uRes') or [None, None])[1], vals.get('uColor')))
        say('      buf в точке мазка %s | out1 %s'
            % (sample_at(buf) if buf is not None else '?',
               sample_at(out) if out is not None else '?'))
        batch = seen.get('cur_dabs') or seen.get('last_dabs')
        if dab is not None and batch:
            x, y, rad, hard, flow, r, g, b = batch[0]
            want = [pack16(x), pack16(y), pack16(rad),
                    (pack8(hard), pack8(flow)), (pack8(r), pack8(g), pack8(b), 0)]
            got = []
            for i in range(4):
                try:
                    s = dab.sample(x=i, y=0)
                    got.append(tuple(int(round(max(0.0, min(1.0, float(c))) * 255.0))
                                     for c in s))
                except Exception as ex:
                    got.append(str(ex))
            say('      текстура (файл %s): штамп 0 = %s'
                % (os.path.basename(str(dab.par.file.eval())), want))
            say('      текстура прочитана:            %s' % (got,))
        elif dab is not None:
            say('      текстура: штампов в этом кадре не было, файл %s'
                % os.path.basename(str(dab.par.file.eval())))

    try:
        # ---------- чистый холст ----------
        say()
        say('--- чистка слоя и мазок одной точкой ---')
        rt._cmd_clear()
        f = 5000
        for i in range(2):
            rt.on_frame_start(f + i)
            out.cook(force=True)
            rt.on_frame_end(f + i)
        probe('после очистки', f)

        rt.ws_text('diag', json.dumps({
            't': 'tool', 'tool': {'tool': 'brush', 'color': '#ff0000', 'size': 200,
                                  'hardness': 1.0, 'flow': 1.0, 'spacing': 0.15}}))
        rt.ws_binary('diag', stroke_frame(9, 1, [(cx, cy, 255)], W, H))
        say('  отправлено: кисть 200px красная, одна точка в центре (%d,%d)' % (cx, cy))

        for i in range(8):
            fr = f + 100 + i
            seen['cur_dabs'] = None
            seen['cur_rect'] = None
            rt.on_frame_start(fr)
            out.cook(force=True)
            rt.on_frame_end(fr)
            probe('', fr)

        # ---------- решающие опыты A/B на одной и той же пачке штампов ----------
        say()
        say('--- опыты A/B: одна и та же пачка штампов ---')
        batch = seen.get('last_dabs') or []
        rect_rt = seen.get('last_rect')
        if not batch:
            say('  пропуск: за все кадры рантайм не построил ни одного штампа')
        else:
            n = len(batch)
            if rect_rt:
                urt = (float(rect_rt[0]), float(rect_rt[1]),
                       float(rect_rt[2]) - float(rect_rt[0]),
                       float(rect_rt[3]) - float(rect_rt[1]))
            else:
                urt = (0.0, 0.0, float(W), float(H))
            say('  штампов в пачке: %d, первый: %s' % (n, batch[0]))
            say('  uRect этой пачки: %s' % (urt,))

            def clean_and_report(name, cook):
                clear_layer()
                before = sample_at(buf)
                got = cook()
                after = sample_at(buf)
                ok = (not isinstance(after, str)) and max(after) > 0.01
                say('  %-34s до=%s -> после=%s  %s'
                    % (name, before, after, 'КРАСКА ЕСТЬ' if ok else 'пусто'))
                return ok

            # A. как в рантайме: записать текстуру и нарисовать в том же кадре
            def cook_a():
                rt.frame_dabs['paint'] = list(batch)
                rt.frame_rect['paint'] = rect_rt
                rt.push_dabs()
                set_uniforms(n, urt)
                out.cook(force=True)
                rt.frame_dabs['paint'] = []
                rt.frame_rect['paint'] = None
                return sample_at(buf)

            res_a = clean_and_report('A: запись текстуры + кадр', cook_a)

            def cook_b():
                set_uniforms(n, urt)
                out.cook(force=True)
                return sample_at(buf)

            def cook_big():
                set_uniforms(n, (0.0, 0.0, float(W), float(H)))
                out.cook(force=True)
                return sample_at(buf)

            res_b = clean_and_report('B: без перезаписи текстуры', cook_b)
            res_c = clean_and_report('C: без перезаписи + весь холст', cook_big)
            res_d = clean_and_report('D: ноль штампов + весь холст',
                                     lambda: (set_uniforms(0, (0.0, 0.0, float(W), float(H))),
                                              out.cook(force=True), sample_at(buf))[-1])

            say()
            if not res_a and res_b:
                say('  ВЫВОД: виновата доставка — текстура штампов становится видна шейдеру')
                say('         только на следующем кадре. Рантайм обязан рисовать пачку')
                say('         кадром позже, а не в том же кадре, когда записал файл.')
            elif res_c and not res_b:
                say('  ВЫВОД: виновата область uRect: с областью рантайма краски нет,')
                say('         а с областью на весь холст — есть.')
            elif not res_a and not res_b and not res_c:
                say('  ВЫВОД: шейдер не рисует даже с готовой текстурой — дело в кисти/связях')
                say('         (uCount, входы brush, sTD2DInputs[1], формат текстуры).')
            else:
                say('  ВЫВОД: краска появляется — свериться с опытом D (там должно быть пусто)')
    except Exception:
        say('СБОЙ ДИАГНОСТИКИ:')
        say(traceback.format_exc())
    finally:
        rt._send, rt._send_bin = real_send, real_send_bin
        rt._build_dabs = orig_build
        with rt.mu:
            rt.clients.pop('diag', None)
        # Уборка: диагностика гоняла кадры со своими номерами и рисовала в слой.
        # Если это не откатить, живой кадровый цикл получит чужой номер кадра и
        # застрявшее состояние (например, переключатель на восстановлении).
        try:
            rt.strokes.clear()
            rt.snap_depth = 0
            rt.restore_req = None
            rt.switch_until = -1
            rt.clear_frames = 0
            # Форма полей — как в рантайме: здесь стоял список вместо словаря по
            # слоям, диагностика портила состояние живого рантайма, и кадры потом
            # падали с «list indices must be integers, not str».
            rt.dab_pending = dict((n, []) for n in rt.BUFFERS)
            rt.dab_pending_rect = dict((n, None) for n in rt.BUFFERS)
            rt.dab_pending_mode = dict((n, 0.0 if n == 'paint' else 1.0)
                                       for n in rt.BUFFERS)
            rt.dab_ready = None
            rt.dab_ready_rect = None
            rt.paint_rect = dict((n, None) for n in rt.BUFFERS)
            rt.frame_dabs = dict((n, []) for n in rt.BUFFERS)
            rt.frame_rect = dict((n, None) for n in rt.BUFFERS)
            rt.send_rect = dict((n, None) for n in rt.BUFFERS)
            rt.force_patch = False
            rt.frame = saved_frame
            if sw is not None:
                sw.par.index = 0
            snap = base.op('snap')
            if snap is not None:
                snap.lock = False
            rt._cmd_clear()
        except Exception:
            say('  уборка не удалась: %s' % traceback.format_exc().splitlines()[-1])

    say()
    say('--- итог ---')
    try:
        tmp = rt.dirs()['tmp']
        shot = os.path.join(tmp, 'diag_layer.png')
        buf.save(shot)
        say('  слой сохранён: %s (%d байт)' % (shot, os.path.getsize(shot)))
        o1 = os.path.join(tmp, 'diag_out.png')
        out.save(o1)
        say('  выход сохранён: %s (%d байт)' % (o1, os.path.getsize(o1)))
    except Exception:
        say('  снимки не сохранились: %s' % traceback.format_exc().splitlines()[-1])
    say('  ошибки рантайма: %s' % (rt.diag.get('error_list') or 'нет'))
    say('  счётчики: dab_pushes=%s dab_png=%s dab_tex=%s'
        % (rt.diag.get('dab_pushes'), rt.diag.get('dab_png'), rt.diag.get('dab_tex')))

    try:
        tmp = rt.dirs()['tmp']
    except Exception:
        tmp = os.path.normpath(os.path.join(rt.tun.get('datadir') or '', 'tmp'))
    if not os.path.isdir(tmp):
        os.makedirs(tmp)
    path = os.path.join(tmp, 'diag_paint.txt')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(OUT))
    print('отчёт диагностики: %s' % path)


main()
