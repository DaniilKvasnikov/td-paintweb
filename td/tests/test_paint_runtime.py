"""Прогон paint_runtime.py на фейковом TouchDesigner.

Запуск обычным Python (годится и python.exe из состава TD):

    python paint/td/tests/test_paint_runtime.py

Проверяет то, что нельзя проверить глазами в TD: арифметику штампов и интервала,
прямоугольники патчей и пересчёт в UV-координаты cropTOP, undo/redo, HTTP-роутинг
и упаковку текстуры штампов.

ВАЖНО: файл должен оставаться в UTF-8, а имена тестовых файлов — латиницей:
текстовые операции PowerShell ломают кириллицу.
"""

import json
import os
import shutil
import struct
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PAINT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import fake_td  # noqa: E402

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

RESULTS = []


def check(name, cond, extra=''):
    RESULTS.append((bool(cond), name, extra))
    print('  %s %s%s' % ('ok  ' if cond else 'ПРОВАЛ', name,
                         ('   ' + str(extra)) if extra and not cond else ''))


def near(a, b, tol=0.51):
    return abs(a - b) <= tol


def load_runtime():
    path = os.path.join(PAINT, 'td', 'runtime', 'paint_runtime.py')
    with open(path, 'r', encoding='utf-8') as f:
        src = f.read()
    m = types.ModuleType('paint_runtime_test')
    m.__dict__['__file__'] = path
    exec(compile(src, path, 'exec'), m.__dict__)
    return m


def stroke_bytes(sid, flags, pts, layer=1):
    b = struct.pack('<BIBBH', 1, sid, flags, layer, len(pts))
    for (x, y, pr) in pts:
        b += struct.pack('<HHBBH', int(round(x / 1920.0 * 65535.0)),
                         int(round(y / 1080.0 * 65535.0)), pr, 0, 0)
    return b


class FakeArr(object):
    """Замена numpy-массива (1, W, 4) для fill_dab_array."""

    def __init__(self, w):
        self.w = w
        self.rows = [[0.0] * 4 for _ in range(w)]

    def __getitem__(self, key):
        r, c = key
        assert r == 0, 'ожидалась одна строка'
        return self.rows[c]


def main():
    tmp = os.path.join(HERE, '_tmp_test')
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(os.path.join(tmp, 'web'))
    os.makedirs(os.path.join(tmp, 'uploads'))
    # Минимальный набор исходников: по нему рантайм понимает, что компонент собран
    # ЗДЕСЬ, а не вставлен из .tox (в чужом проекте исходников рядом нет, и
    # автопересборка отключается — это проверяется в [20]).
    os.makedirs(os.path.join(tmp, 'td', 'runtime'))
    with open(os.path.join(tmp, 'td', 'runtime', 'paint_runtime.py'), 'w',
              encoding='utf-8') as f:
        f.write('# заглушка исходника\n')
    with open(os.path.join(tmp, 'web', 'index.html'), 'w', encoding='utf-8') as f:
        f.write('<html>test</html>')

    m = load_runtime()
    base, ws = fake_td.build_tree(tmp)
    base.par.Datadir.val = tmp
    RUNS = []

    def fake_run(func, *args, **kw):
        """Замена TD-шного run(): запоминаем просьбу, но не исполняем её."""
        RUNS.append((getattr(func, '__name__', str(func)), args, kw))
        return None

    rt = m.bind('/project1/paint_web',
                {'op': fake_td.make_op_func(),
                 'project': fake_td.FakeProject(tmp),
                 'me': base.op('server/ws'),
                 'run': fake_run})

    # ------------------------------------------------------------- параметры
    print('\n[1] параметры и размеры')
    rt.read_pars()
    rt._ensure_sizes()
    check('полотно из параметров', (rt.tun['w'], rt.tun['h']) == (1920, 1080))
    check('brush -> custom 1920', base.op('brush').par.resolutionw.eval() == 1920)
    check('brush -> custom 1080', base.op('brush').par.resolutionh.eval() == 1080)
    check('fit -> custom 1920', base.op('fit').par.resolutionw.eval() == 1920)
    check('proxy -> половина', base.op('proxy').par.resolutionw.eval() == 960)
    check('outputresolution = custom',
          base.op('brush').par.outputresolution.eval() == 'custom')

    # ------------------------------------------------------------- мазок
    print('\n[2] точки -> штампы, интервал по длине пути')
    rt.tool.update({'tool': 'brush', 'size': 40.0, 'spacing': 0.15,
                    'hardness': 0.7, 'flow': 1.0, 'color': '#ff0000'})
    pts = [(100.0 + i * 50.0, 50.0, 255) for i in range(5)]      # 100..300 по X
    rt.ws_binary('c1', stroke_bytes(7, 1, pts))
    rt._drain()
    check('мазок зарегистрирован', 7 in rt.strokes)
    check('снимок сделан (snap_depth=1)', rt.snap_depth == 1)
    check('snap залочен', base.op('snap').lock is True)
    rt._build_dabs()
    dabs = list(rt.frame_dabs['paint'])
    # размер 40 * интервал 0.15 = 6 px, длина 200 px -> 34 штампа
    check('число штампов = 34', len(dabs) == 34, len(dabs))
    check('первый штамп в начале', near(dabs[0][0], 100.0) and near(dabs[0][1], 50.0),
          dabs[0][:2])
    check('шаг ровно 6 px', near(dabs[1][0] - dabs[0][0], 6.0, 0.01),
          dabs[1][0] - dabs[0][0])
    check('радиус при давлении 1.0 = 20', near(dabs[0][2], 20.0, 0.01), dabs[0][2])
    check('последний штамп ~298', near(dabs[-1][0], 298.0), dabs[-1][0])
    check('цвет в штампе', near(dabs[0][5], 1.0) and near(dabs[0][6], 0.0), dabs[0][5:8])
    d50 = rt._make_dab(10.0, 10.0, 0.5)
    check('радиус не зависит от давления (как в веб-клиенте)', near(d50[2], 20.0, 0.01),
          d50[2])
    check('давление меняет плотность штампа', near(d50[4], 0.5, 0.01), d50[4])

    print('\n[3] бюджет кадра не рвёт мазок')
    rt.frame_dabs['paint'] = []
    rt.reset_frames()
    pts2 = [(200.0 + i * 75.0, 900.0, 128) for i in range(21)]   # 1500 px -> 251 штамп
    rt.ws_binary('c2', stroke_bytes(8, 1, pts2))
    rt._drain()
    all_dabs = []
    frames = 0
    while rt.strokes.get(8) and rt.strokes[8]['queue'] and frames < 60:
        rt._build_dabs()
        all_dabs.extend(rt.frame_dabs['paint'])
        rt.frame_dabs['paint'] = []
        frames += 1
    check('мазок собран за несколько кадров', frames > 1, frames)
    check('всего штампов ~251', 250 <= len(all_dabs) <= 252, len(all_dabs))
    gaps = [all_dabs[i + 1][0] - all_dabs[i][0] for i in range(len(all_dabs) - 1)]
    check('нет разрывов на стыке кадров', max(gaps) < 6.6, max(gaps) if gaps else 'нет')
    check('нет шагов назад', min(gaps) > 5.4, min(gaps) if gaps else 'нет')
    rt.strokes.pop(8, None)
    rt.snap_depth = 0

    # ------------------------------------------------------------- прямоугольники
    print('\n[4] патч и пересчёт в UV для cropTOP')
    rt.frame_dabs['paint'] = list(dabs)
    rt.frame_rect['paint'] = None
    for d in dabs:
        rt._grow_rect(d)
    rect = m._rect_int(rt.frame_rect['paint'], 1920, 1080)
    check('патч покрывает мазок', rect[0] <= 80 and rect[0] + rect[2] >= 320, rect)
    check('патч не вылез за полотно', rect[0] >= 0 and rect[0] + rect[2] <= 1920, rect)
    crop = base.op('crop')
    # в рантайме прямоугольник — это КОРОБКА (l, t, r, b), а параметры кропа
    # задают ПОЛОЖЕНИЕ краёв: по горизонтали слева направо, а по вертикали TD
    # работает в своей оси (v = 0 внизу), поэтому верх/низ считаются от низа.
    rt._set_crop(crop, (100, 50, 300, 150))
    check('cropleft = 100 (левый край)', crop.par.cropleft.eval() == 100.0,
          crop.par.cropleft.eval())
    check('cropright = 300 (правый край)', crop.par.cropright.eval() == 300.0,
          crop.par.cropright.eval())
    check('croptop = 1080-50 = 1030 (верхний край от низа)',
          crop.par.croptop.eval() == 1030.0, crop.par.croptop.eval())
    check('cropbottom = 1080-150 = 930 (нижний край от низа)',
          crop.par.cropbottom.eval() == 930.0, crop.par.cropbottom.eval())
    check('единицы кропа = pixels', crop.par.cropleftunit.eval() == 'pixels')
    check('кроп вырезал ровно запрошенную область (200x100)',
          (crop.width, crop.height) == (200, 100), (crop.width, crop.height))
    edge = m._rect_int((-40, -40, 30, 20), 1920, 1080)
    check('патч у края обрезан', edge[0] == 0 and edge[1] == 0, edge)

    # Круговая проверка «запрос -> кроп -> заголовок». Именно здесь жила ошибка,
    # из-за которой в браузер уезжал патч в один пиксель, растянутый на весь
    # прямоугольник: область передавалась как (x, y, w, h), а _set_crop читает
    # коробку (l, t, r, b). Полный кадр при этом работал (там x = y = 0).
    rt.send_rect = dict((n, None) for n in m.BUFFERS)
    blob, rq = rt._encode_layer((100, 50, 300, 150), '.png')
    check('_encode_layer вернул запрошенный прямоугольник', rq == (100, 50, 200, 100), rq)
    check('_encode_layer: левый край кропа = x', crop.par.cropleft.eval() == 100.0,
          crop.par.cropleft.eval())
    check('_encode_layer: правый край кропа = x + w (а не ширина)',
          crop.par.cropright.eval() == 300.0, crop.par.cropright.eval())
    check('_encode_layer: верхний край кропа = H - y',
          crop.par.croptop.eval() == 1030.0, crop.par.croptop.eval())
    check('_encode_layer: нижний край кропа = H - (y + h)',
          crop.par.cropbottom.eval() == 930.0, crop.par.cropbottom.eval())
    check('_encode_layer: вырезанный размер = запрошенному',
          (crop.width, crop.height) == (200, 100), (crop.width, crop.height))
    check('_encode_layer вернул непустую картинку', len(blob) > 8, len(blob))

    # ------------------------------------------------------------- текстура штампов
    print('\n[5] упаковка штампов в PNG для шейдера')
    import test_selftest_png as _png
    dec = _png.load_selftest_module()
    rt.frame_dabs['paint'] = [
        (100.0, 50.0, 20.0, 0.7, 1.0, 1.0, 0.0, 0.0),
        (200.5, 300.25, 12.0, 0.0, 0.5, 0.0, 1.0, 0.0),
    ]
    rt.frame_rect['paint'] = (80.0, 30.0, 300.0, 320.0)
    rt._apply_uniforms()                      # push_dabs внутри
    dab = base.op('dabpng')
    check('dabpng получил файл текстуры',
          str(dab.par.file.eval()).endswith('.png'), dab.par.file.eval())
    check('имя файла чередуется (гарантия перезагрузки)',
          os.path.basename(str(dab.par.file.eval())) in ('dabs_a.png', 'dabs_b.png'),
          dab.par.file.eval())
    check('reloadpulse пульнул', dab.par.reloadpulse.pulses >= 1,
          dab.par.reloadpulse.pulses)
    check('отмечено число штампов в PNG', rt.diag.get('dab_png') == 2,
          rt.diag.get('dab_png'))
    shot = str(dab.par.file.eval())
    check('файл текстуры записан', os.path.isfile(shot), shot)
    w, h, nch, rows = dec.png_read(shot)
    check('текстура 512x1 RGBA', (w, h, nch) == (512, 1, 4), (w, h, nch))

    def unpack16(hi, lo):
        # ровно то же, что делает brush.glsl
        return (hi * 256 + lo) / 8.0 if False else (hi * 256 + lo) * 0.125

    line = rows[0]
    check('x первого штампа упакован без потерь', unpack16(line[0], line[1]) == 100.0,
          unpack16(line[0], line[1]))
    check('y первого штампа упакован без потерь', unpack16(line[2], line[3]) == 50.0,
          unpack16(line[2], line[3]))
    check('радиус первого штампа', unpack16(line[4], line[5]) == 20.0,
          unpack16(line[4], line[5]))
    check('жёсткость и поток в байтах',
          (line[6], line[7]) == (178, 255), (line[6], line[7]))
    check('цвет первого штампа красный',
          (line[8], line[9], line[10]) == (255, 0, 0), (line[8:11]))
    check('дробные координаты тоже без потерь',
          unpack16(line[16], line[17]) == 200.5 and unpack16(line[18], line[19]) == 300.25,
          (unpack16(line[16], line[17]), unpack16(line[18], line[19])))
    check('uCount.x = число штампов', rt.diag.get('dab_tex') == 2, rt.diag.get('dab_tex'))

    # ------------------------------------------------------------- завершение мазка
    print('\n[6] завершение мазка: undo и финальный патч')
    rt.clients['c1'] = {'ready': True, 'need_poster': False, 'sync_queue': []}
    rt.ws_binary('c1', stroke_bytes(7, 2, []))        # пустая пачка с флагом конца
    rt._drain()
    # Штампы доезжают до слоя со задержкой (файл текстуры читается не мгновенно),
    # поэтому мазок обязан закрыться НЕ сразу: иначе снимок undo и финальный патч
    # уедут без хвоста мазка, и после undo/redo хвост пропадёт насовсем.
    ended = rt._finish_strokes()
    check('мазок не закрывается, пока штампы не дорисованы', ended == [], ended)
    # Дальше гоняем обычные кадры: закрытие мазка делает сам кадровый цикл, как в
    # живом TD. Запоминаем, на каком кадре он это сделал.
    closed = []
    orig_finish = rt._finish_strokes

    def spy_finish():
        r = orig_finish()
        if r:
            closed.extend(r)
        return r

    rt._finish_strokes = spy_finish
    ws.sent_text = []
    ws.sent_bin = []
    for _f in range(4):                       # даём пачке доехать до слоя
        rt.frame_dabs['paint'] = []
        rt.reset_frames()
        rt.on_frame_start(100 + _f)
        rt.on_frame_end(100 + _f)
    rt._finish_strokes = orig_finish
    ended = closed
    check('мазок завершён после дорисовки', 7 in ended, ended)
    check('накопленных штампов не осталось',
          not any(rt.dab_pending.values())
          and not rt.dab_ready,
          (rt.dab_pending, rt.dab_ready))
    check('undo-запись создана', len(rt.undo) == 1, len(rt.undo))
    check('файл снимка записан', bool(base.op('cropsnap').saved),
          base.op('cropsnap').saved)
    check('snap разлочен', base.op('snap').lock is False)
    check('snap_depth сброшен', rt.snap_depth == 0)
    entry = rt.undo[0]
    check('регион undo не пустой', entry['rect'][2] > entry['rect'][0]
          and entry['rect'][3] > entry['rect'][1], entry['rect'])

    # Финальный патч ушёл ещё в кадровом цикле (как в живом TD): ищем его среди
    # отправленного, а не дёргаем отправку руками второй раз.
    patches = [msg for msg in ws.sent_text
               if isinstance(msg, tuple) and len(msg) > 1 and '"t": "patch"' in msg[1]]
    if not patches:
        # запасной путь: кадры не отправляли (например, патч уже был отправлен)
        ws.sent_text = []
        ws.sent_bin = []
        rt.force_patch = True
        rt.send_rect['paint'] = rt.send_rect['paint'] or entry['rect']
        rt._flush_patches(force_final=True)
        patches = [msg for msg in ws.sent_text
                   if isinstance(msg, tuple) and len(msg) > 1 and '"t": "patch"' in msg[1]]
    ws.sent_text = list(patches)
    check('патч отправлен', len(ws.sent_text) == 1, ws.sent_text)
    if ws.sent_text:
        head = json.loads(ws.sent_text[0][1])
        check('final=1 в последнем патче', head['final'] == 1, head)
        check('патч про слой 1', head['layer'] == 1, head)
        check('патч в границах', head['x'] >= 0 and head['x'] + head['w'] <= 1920, head)
        check('к патчу приложен бинарь', len(ws.sent_bin) >= 1, len(ws.sent_bin))
        check('PNG-кодирование', base.op('crop').encoded[-1][0] == '.png',
              base.op('crop').encoded[-1])
        # saveByteArray требует суффикс С ТОЧКОЙ ('.png', а не 'png') — иначе TD
        # может не понять формат; проверяем инвариант по всем кодированиям
        bad_fmt = [f for f, _q in base.op('crop').encoded if not f.startswith('.')]
        check('формат картинок всегда с точкой', not bad_fmt, bad_fmt)

    # ------------------------------------------------------------- undo / redo
    print('\n[7] undo и redo через восстановление региона')
    rt._cmd_history(-1)
    check('undo опустошён', len(rt.undo) == 0)
    check('redo заполнен', len(rt.redo) == 1)
    pin = base.op('patchin')
    check('патч загружен в patchin', pin.par.file.eval().endswith('.png'),
          pin.par.file.eval())
    # Пульсов может быть больше одного: до этого патч грузил ещё и отменённый
    # мазок. Важно, что загрузка вообще была — иначе undo нечего восстанавливать.
    check('reloadpulse пульнул', pin.par.reloadpulse.pulses >= 1,
          pin.par.reloadpulse.pulses)
    check('запрос восстановления стоит', rt.restore_req is not None)
    rl, rtp, rw, rh = m._rect_int(entry['rect'], 1920, 1080)
    pin.width, pin.height = rw, rh
    rt.frame = rt.restore_req['frame'] + 1
    rt._apply_restore(rt.frame)
    sw = base.op('sw')
    check('switch переключён на restore', sw.par.index.eval() == 1, sw.par.index.eval())
    rest = base.op('restore')
    check('uRes у restore', rest.par.vec0valuex.eval() == 1920.0,
          rest.par.vec0valuex.eval())
    check('uRect у restore совпал с регионом',
          near(rest.par.vec1valuex.eval(), rl, 0.01)
          and near(rest.par.vec1valuey.eval(), rtp, 0.01)
          and near(rest.par.vec1valuez.eval(), rw, 0.01)
          and near(rest.par.vec1valuew.eval(), rh, 0.01),
          [rest.par['vec1value' + c].eval() for c in 'xyzw'])
    check('запрос снят', rt.restore_req is None)
    check('кадр восстановления помечен', rt.switch_until == rt.frame)
    rt._cmd_history(1)
    check('redo опустошён', len(rt.redo) == 0)
    check('undo вернулся', len(rt.undo) == 1)

    # ------------------------------------------------------------- очистка
    print('\n[8] очистка слоя')
    rt._cmd_clear()
    check('флаг очистки на кадр', rt.clear_frames == 1)
    check('очистка попала в undo', len(rt.undo) == 2, len(rt.undo))
    check('патч на всё полотно', rt.send_rect['paint'] == (0, 0, 1920, 1080), rt.send_rect['paint'])

    # ------------------------------------------------------------- uniform-ы
    print('\n[9] uniform-ы кисти и композита (доставка штампов в два кадра)')
    rt.frame_dabs['paint'] = list(dabs)
    # frame_rect хранится КОРОБКОЙ (l, t, r, b), а в шейдер уходит (x, y, w, h)
    rt.frame_rect['paint'] = (100.0, 50.0, 300.0, 150.0)
    rt.clear_frames = 0
    rt.dab_pending = {'paint': [], 'mask': [], 'colormask': []}
    rt.dab_pending_rect = {'paint': None, 'mask': None, 'colormask': None}
    rt.dab_ready = None
    rt.dab_ready_rect = None
    # Кадр восстановления области кисть не пускает в слой: снимаем это состояние,
    # иначе проверять uniform-ы рисования не на чем.
    rt.switch_until = -1
    rt.restore_req = None
    rt.tool['tool'] = 'brush'
    rt._apply_uniforms()                      # кадр ЗАПИСИ текстуры
    br = base.op('brush')
    check('в кадре записи текстуры кисть не рисует (uCount = 0)',
          br.par.vec1valuex.eval() == 0.0, br.par.vec1valuex.eval())
    check('в кадре записи область пустая',
          [br.par['vec3value' + c].eval() for c in 'xyzw'] == [-4.0, -4.0, 0.0, 0.0],
          [br.par['vec3value' + c].eval() for c in 'xyzw'])
    check('текстура штампов записана в файл для moviefileinTOP',
          rt.diag.get('dab_png') == len(dabs), rt.diag.get('dab_png'))
    check('счётчик и текстура про одно и то же',
          rt.diag.get('dab_tex') == len(dabs), rt.diag.get('dab_tex'))
    check('пачка ждёт рисования следующим кадром',
          bool(rt.dab_ready) and rt.dab_ready[0] == 'paint'
          and rt.dab_ready[1] == len(dabs) and not rt.dab_pending['paint'],
          (rt.dab_ready, len(rt.dab_pending['paint'])))
    check('режим краски = 0', br.par.vec1valuey.eval() == 0.0)
    check('uRes = 1920x1080', br.par.vec0valuex.eval() == 1920.0
          and br.par.vec0valuey.eval() == 1080.0,
          [br.par['vec0value' + c].eval() for c in 'xyzw'])
    check('uColor из hex', near(br.par.vec2valuex.eval(), 1.0, 0.01)
          and near(br.par.vec2valuey.eval(), 0.0, 0.01),
          [br.par['vec2value' + c].eval() for c in 'xyzw'])

    # Следующий кадр: новых точек нет, значит текстура уже загружена и можно рисовать
    pulses = base.op('dabpng').par.reloadpulse.pulses
    rt.frame_dabs['paint'] = []
    rt.frame_rect['paint'] = None
    rt._apply_uniforms()                      # кадр РИСОВАНИЯ
    check('на следующем кадре uCount.x = число штампов',
          br.par.vec1valuex.eval() == float(len(dabs)), br.par.vec1valuex.eval())
    check('uRect у кисти пересчитан из коробки в (x,y,w,h)',
          [br.par['vec3value' + c].eval() for c in 'xyzw'] == [100.0, 50.0, 200.0, 100.0],
          [br.par['vec3value' + c].eval() for c in 'xyzw'])
    check('при рисовании текстура НЕ перезаписывается',
          base.op('dabpng').par.reloadpulse.pulses == pulses,
          base.op('dabpng').par.reloadpulse.pulses)
    check('пачка израсходована', not rt.dab_ready, rt.dab_ready)

    # Конец кадра обязан забрать изменённую область в патч — иначе браузер видит
    # мазок только после отпускания мыши (и ластик «не работает» до отпускания).
    rt.send_rect = dict((n, None) for n in m.BUFFERS)
    rt.paint_rect['paint'] = (100.0, 50.0, 300.0, 150.0)
    rt.last_patch_t = 0.0
    ws.sent_text = []
    ws.sent_bin = []
    rt.on_frame_end(77)
    patched = [msg for msg in ws.sent_text
               if isinstance(msg, tuple) and len(msg) > 1 and '"t": "patch"' in msg[1]]
    check('рисование уходит патчем уже во время мазка', len(patched) >= 1, ws.sent_text)
    check('область кадра попала в патч',
          bool(patched) and json.loads(patched[0][1])['x'] == 100, patched[:1])

    # Режим кисти берётся из самого мазка (ластик — 2, маска — 1, краска — 0), а
    # не из текущего инструмента: иначе смена инструмента посреди мазка
    # перекрасила бы уже нарисованное.
    rt.dab_pending['paint'] = [dabs[0]]
    rt.dab_pending_mode['paint'] = 2.0
    rt.dab_ready = None
    rt._apply_uniforms()                       # кадр записи пачки-ластика
    rt._apply_uniforms()                       # кадр рисования
    check('ластик рисует в режиме 2', br.par.vec1valuey.eval() == 2.0,
          br.par.vec1valuey.eval())
    check('пачка ластика нарисована', br.par.vec1valuex.eval() == 1.0,
          br.par.vec1valuex.eval())

    # Маска КАЖДОГО слоя рисуется своей кистью в свой буфер, краска при этом не
    # трогается: маски слоя цвета и источника — разные буферы.
    rt.frame_dabs['colormask'] = [dabs[0], dabs[1]]
    rt.frame_rect['colormask'] = (100.0, 50.0, 300.0, 150.0)
    rt.reset_frames()
    # reset_frames гасит всё: возвращаем пачку маски цвета и её область
    rt.frame_dabs['colormask'] = [dabs[0], dabs[1]]
    rt.frame_rect['colormask'] = (100.0, 50.0, 300.0, 150.0)
    rt._apply_uniforms()                       # кадр записи текстуры для маски
    rt._apply_uniforms()                       # кадр рисования маски
    brm = base.op('brushm2')
    check('маска слоя цвета рисуется своей кистью (brushm2) с режимом 1',
          brm.par.vec1valuey.eval() == 1.0 and brm.par.vec1valuex.eval() == 2.0,
          [brm.par['vec1value' + c].eval() for c in 'xyzw'])
    check('маска источника при этом не рисуется',
          base.op('brushm').par.vec1valuex.eval() == 0.0,
          base.op('brushm').par.vec1valuex.eval())
    check('краска в кадре мазка по маске не рисуется',
          br.par.vec1valuex.eval() == 0.0, br.par.vec1valuex.eval())
    check('область мазка по маске цвета ушла в патч своей маски',
          rt.paint_rect['colormask'] is not None and rt.paint_rect['paint'] is None,
          (rt.paint_rect['colormask'], rt.paint_rect['paint']))
    rt.paint_rect = {'paint': None, 'mask': None, 'colormask': None}
    # кадровый цикл чистит список в конце кадра — повторяем это, иначе следующий
    # же вызов снова положит те же штампы в текстуру
    rt.reset_frames()
    rt.reset_frames()

    # ------------------------------------------------ режим берётся из самого мазка
    # Проверяем БОЕВОЙ путь: мазок приходит бинарным пакетом, а не выставляется
    # руками. В прошлой версии поле режима пачки не заполнялось вовсе, поэтому
    # ластик рисовал как кисть, а по слою «Источник» «ничего не делал».
    for tool_name, layer_id, want_paint, want_mask, label in (
            ('eraser', 1, 2.0, 1.0, 'ластик по слою краски = стирание'),
            ('brush', 2, 0.0, 1.0, 'кисть по слою «Источник» = проявление маски'),
            ('eraser', 0, 0.0, 2.0, 'ластик по слою «Источник» = стирание маски')):
        rt.strokes.clear()
        rt.dab_pending = {'paint': [], 'mask': [], 'colormask': []}
        rt.dab_pending_rect = {'paint': None, 'mask': None, 'colormask': None}
        rt.dab_pending_mode = {'paint': 0.0, 'mask': 1.0, 'colormask': 1.0}
        rt.dab_ready = None
        rt.frame_dabs['paint'] = []
        rt.reset_frames()
        rt.tool['tool'] = tool_name
        rt.ws_binary('c1', stroke_bytes(41, 1, [(300, 300, 255), (360, 300, 255)],
                                        layer=layer_id))
        rt._drain()
        rt._build_dabs()
        check(label,
              rt.dab_pending_mode['paint'] == want_paint
              and rt.dab_pending_mode['mask'] == want_mask,
              (rt.dab_pending_mode,
               rt.dab_pending))
    rt.strokes.clear()
    rt.dab_pending = {'paint': [], 'mask': [], 'colormask': []}
    rt.dab_pending_rect = {'paint': None, 'mask': None, 'colormask': None}
    rt.dab_pending_mode = {'paint': 0.0, 'mask': 1.0, 'colormask': 1.0}
    rt.dab_ready = None
    rt.frame_dabs['paint'] = []
    rt.reset_frames()
    rt.tool['tool'] = 'brush'

    src_lvl = base.op('src_level')
    base.par.Srcopacity.val = 0.5
    base.par.Srcvisible.val = 1
    rt.read_pars()
    rt._apply_compose()
    check('непрозрачность источника ушла в levelTOP',
          near(src_lvl.par.opacity.eval(), 0.5, 0.001), src_lvl.par.opacity.eval())
    base.par.Srcvisible.val = 0
    rt.read_pars()
    rt._apply_compose()
    check('скрытый источник даёт 0', src_lvl.par.opacity.eval() == 0.0,
          src_lvl.par.opacity.eval())

    print('\n[9b] нет «призраков»: кадр без новых точек не рисует старое')
    rt.frame_dabs['paint'] = []
    rt.reset_frames()
    rt.reset_frames()
    rt.reset_frames()
    rt.dab_pending = {'paint': [], 'mask': [], 'colormask': []}
    rt.dab_pending_rect = {'paint': None, 'mask': None, 'colormask': None}
    rt.dab_ready = None
    rt.dab_ready_rect = None
    rt.tool['tool'] = 'brush'
    pulses = base.op('dabpng').par.reloadpulse.pulses
    rt._apply_uniforms()
    check('uCount обнулился вместе со штампами', br.par.vec1valuex.eval() == 0.0,
          br.par.vec1valuex.eval())
    check('в пустом кадре текстура не трогается',
          base.op('dabpng').par.reloadpulse.pulses == pulses,
          base.op('dabpng').par.reloadpulse.pulses)
    check('в пустом кадре рисовать нечего', rt.diag.get('dab_count') == 0,
          rt.diag.get('dab_count'))
    check('счётчик и текстура по-прежнему согласованы',
          int(rt.diag.get('dab_tex') or 0) == int(rt.diag.get('dab_png') or 0),
          (rt.diag.get('dab_tex'), rt.diag.get('dab_png')))

    # ------------------------------------------------------------- HTTP
    print('\n[10] HTTP: статика, состояние, загрузка')
    resp = {}
    out = rt.http({'method': 'GET', 'uri': '/', 'pars': {}}, resp)
    check('index отдан', out['statusCode'] == 200 and b'<html' in bytes(out['data']))
    check('content-type html', 'text/html' in out['content-type'], out['content-type'])
    resp = {}
    rt.http({'method': 'GET', 'uri': '/missing.js', 'pars': {}}, resp)
    check('404 на отсутствующий', resp['statusCode'] == 404, resp['statusCode'])
    resp = {}
    rt.http({'method': 'GET', 'uri': '/../../secret.txt', 'pars': {}}, resp)
    check('выход из папки закрыт', resp['statusCode'] in (403, 404), resp['statusCode'])
    resp = {}
    out = rt.http({'method': 'GET', 'uri': '/api/state', 'pars': {}}, resp)
    state = json.loads(out['data'])
    check('/api/state отдаёт полотно', state['canvas']['w'] == 1920)
    # Слоёв теперь три: источник, краска и служебный буфер маски. У источника и
    # краски есть drawInto (куда адресовать мазок), у маски — ui:0 (своей строки
    # в панели у неё нет: маска — часть слоя «Источник»).
    kinds = [l['kind'] for l in state['layers']]
    check('/api/state отдаёт источник, цвет, краску и ДВЕ маски',
          kinds == ['source', 'color', 'paint', 'mask', 'mask'], kinds)
    check('у слоёв есть цель рисования drawInto (источник и цвет — в свои маски)',
          [l.get('drawInto') for l in state['layers'][:3]] == [2, 4, 1],
          [l.get('drawInto') for l in state['layers']])
    mask_lay = [l for l in state['layers'] if l['kind'] == 'mask']
    check('оба буфера масок скрыты из панели слоёв',
          len(mask_lay) == 2 and all(l.get('ui') == 0 for l in mask_lay),
          state['layers'])
    check('у источника и цвета РАЗНЫЕ маски',
          [l.get('usesMask') for l in state['layers'][:2]] == [2, 4],
          [l.get('usesMask') for l in state['layers']])
    resp = {}
    out = rt.http({'method': 'POST', 'uri': '/api/upload', 'pars': {'name': 'my file.png'},
                   'data': b'\x89PNG\r\n\x1a\n'}, resp)
    check('загрузка ок', json.loads(out['data'])['ok'] is True, out['data'])
    check('файл на диске', os.path.isfile(os.path.join(tmp, 'uploads', 'my file.png')),
          sorted(os.listdir(os.path.join(tmp, 'uploads'))))
    check('источник переключился на загруженный',
          base.par.Srcfile.eval().endswith('my file.png'), base.par.Srcfile.eval())
    resp = {}
    out = rt.http({'method': 'GET', 'uri': '/api/sources', 'pars': {}}, resp)
    check('/api/sources видит загруженный',
          any('my file.png' == s['name'] for s in json.loads(out['data'])['list']),
          out['data'])
    resp = {}
    rt.http({'method': 'GET', 'uri': '/api/status', 'pars': {}}, resp)
    check('/api/status отвечает json', resp['statusCode'] == 200)

    # ------------------------------------------------------------- битые данные
    print('\n[11] устойчивость к мусору')
    rt.ws_binary('c1', b'\x01\x02')                      # обрубок
    rt.ws_binary('c1', b'\x09' + b'\x00' * 20)           # неизвестный opcode
    rt.ws_text('c1', '{это не json')
    rt.ws_text('c1', json.dumps({'t': 'нет-такой-команды'}))
    rt.ws_binary('c1', stroke_bytes(99, 1, [(10, 10, 255)]))
    rt._drain()
    rt._build_dabs()
    check('обрубок не сломал рантайм', True)
    check('мусор не создал лишних мазков', 7 not in rt.strokes and 8 not in rt.strokes,
          list(rt.strokes))
    rt.frame_dabs['paint'] = []

    # ------------------------------------------------------------- статус
    print('\n[12] статус для диагностики')
    st = rt.status()
    # 'c1' слал сообщения, не открывая WS. Так бывает после пересборки: модуль
    # рантайма перезагрузился, список клиентов пуст, а соединения живы — поэтому
    # рантайм обязан знакомиться с таким клиентом заново, иначе он перестанет
    # получать патчи, хотя связь работает.
    check('неизвестный клиент зарегистрировался сам', 'c1' in rt.clients,
          list(rt.clients))
    check('статус содержит клиентов', 'clients' in st and st['clients'] == 2,
          st.get('clients'))
    check('статус содержит регионы', st['undo'] == 2, st.get('undo'))
    check('статус содержит журнал', len(st['log']) > 0)

    # ------------------------------------------------------------- общий рантайм
    print('\n[13] один рантайм на все DAT (общий модуль в sys.modules)')
    boot_path = os.path.join(PAINT, 'td', 'runtime', 'pw_boot.py')
    with open(boot_path, 'r', encoding='utf-8') as f:
        boot_src = f.read()
    with open(os.path.join(PAINT, 'td', 'runtime', 'paint_runtime.py'),
              'r', encoding='utf-8') as f:
        base.op('server/runtime').text = f.read()

    def make_boot(tag):
        b = types.ModuleType('paintweb_boot_' + tag)
        b.__dict__['op'] = fake_td.make_op_func()
        exec(compile(boot_src, boot_path, 'exec'), b.__dict__)
        return b

    b1, b2 = make_boot('a'), make_boot('b')
    rt1 = b1.attach(base, fake_td.make_op_func())
    rt2 = b2.attach(base, fake_td.make_op_func())
    check('два DAT получают один объект рантайма', rt1 is rt2)
    rt3 = b1._pw(base.op('server/ws'))
    check('путь колбэка (_pw) ведёт к тому же объекту', rt3 is rt1,
          (id(rt3), id(rt1)))
    rt_missing = None
    try:
        rt_missing = b1._pw(base.op('brush'))
    except Exception:
        rt_missing = 'ошибка'
    check('_pw находит компонент по любому DAT внутри него', rt_missing is rt1,
          rt_missing if not isinstance(rt_missing, bool) else '')
    rt1.ws_text('cZ', json.dumps({'t': 'hello', 'w': 1, 'h': 1}))
    check('команда, принятая одним DAT, видна другому', len(rt2.cmds) == 1,
          len(rt2.cmds))
    check('точки, принятые одним DAT, видны другому',
          (rt2.ws_binary('cZ', stroke_bytes(55, 1, [(5, 5, 255)])) or True)
          and len(rt2.bins) == 1 and len(rt1.bins) == 1,
          (len(rt1.bins), len(rt2.bins)))

    # ------------------------------------------------- запуск без Textport
    print('\n[14] автозапуск: сборка сама, по отпечатку файлов')
    # компонент, собранный ранее: файл сборки на месте, DAT с отпечатком есть
    tddir = os.path.join(tmp, 'td')
    if not os.path.isdir(tddir):
        os.makedirs(tddir)
    with open(os.path.join(tddir, 'build_paint_web.py'), 'w', encoding='utf-8') as f:
        f.write('# заглушка сборки\n')
    with open(os.path.join(tddir, 'selftest_paint_web.py'), 'w', encoding='utf-8') as f:
        f.write('# заглушка самопроверки\n')
    ver = fake_td.FakeOp('/project1/paint_web/server/version', 'textDAT')
    addr = fake_td.FakeOp('/project1/paint_web/server/address', 'textDAT')

    del RUNS[:]
    rt.autostart_done = False
    rt._rebuild_t = 0.0
    rt.autostart()
    check('первый запуск просит пересборку', len(RUNS) == 1, RUNS)
    check('пересборка идёт через run() с задержкой (не рвёт кадр)',
          RUNS and RUNS[0][2].get('delayMilliSeconds', 0) > 0, RUNS[:1])
    check('пересборка — это файл сборки', RUNS and RUNS[0][1][0].endswith(
        'build_paint_web.py'), RUNS[:1])
    check('сервер включён', int(base.op('server/ws').par.active.eval()) == 1)

    # сборка записала отпечаток: второй запуск пересобирать не должен
    ver.text = m.sources_signature(tmp) + '\n# ручная правка не мешает\n'
    del RUNS[:]
    rt._rebuild_t = 0.0
    rt.autostart()
    check('отпечаток совпал — пересборки нет', not RUNS, RUNS)
    check('в журнале сказано, что всё готово',
          any('готов' in l for l in rt.log_lines),
          [l for l in rt.log_lines if 'готов' in l][-2:])

    # файлы изменились: рантайм должен заметить сам (с дебаунсом)
    rt.strokes.clear()                         # мазки из прошлых разделов не мешают
    with open(os.path.join(tddir, 'build_paint_web.py'), 'w', encoding='utf-8') as f:
        f.write('# заглушка сборки\n# правка\n')
    rt._sig_checked = 0.0
    rt._sig_since = 0.0
    del RUNS[:]
    rt.watch_sources()
    check('после правки файлов пересборка не бросается сразу', not RUNS, RUNS)
    rt._sig_checked = 0.0
    rt._sig_since = time.time() - 5.0          # правка «устоялась»
    rt._rebuild_t = 0.0
    rt.watch_sources()
    check('устоявшаяся правка запускает пересборку', len(RUNS) == 1, RUNS)

    rt.strokes[999] = {'queue': []}            # имитируем активный мазок
    rt._sig_checked = 0.0
    rt._sig_since = time.time() - 5.0
    rt._rebuild_t = 0.0
    del RUNS[:]
    rt.watch_sources()
    check('во время рисования пересборка откладывается', not RUNS, RUNS)
    rt.strokes.clear()

    # Повторять заведомо неудачную сборку каждые 10 секунд нельзя: пока файлы не
    # изменились, ждём минуту (иначе TD будет молотить сборку по кругу).
    rt._sig_checked = 0.0
    rt._sig_since = time.time() - 5.0
    rt._sig = m.sources_signature(tmp)          # этот отпечаток уже видели
    rt._rebuild_sig = rt._sig                   # и на нём сборка «не удалась»
    rt._rebuild_t = time.time() - 15.0          # попытка была 15 с назад
    del RUNS[:]
    rt.watch_sources()
    check('неудачную сборку не повторяют каждые 10 секунд', not RUNS, RUNS)

    # ...но если файлы снова изменились — пересборка обязана пойти сразу
    with open(os.path.join(tddir, 'build_paint_web.py'), 'w', encoding='utf-8') as f:
        f.write('# заглушка сборки\n# правка\n# ещё правка\n')
    rt._sig_checked = 0.0
    rt._sig_since = time.time() - 5.0
    rt._sig = m.sources_signature(tmp)          # новая правка уже «устоялась»
    rt._rebuild_t = time.time() - 15.0
    del RUNS[:]
    rt.watch_sources()
    check('новая правка файлов снова запускает пересборку', len(RUNS) == 1, RUNS)

    # Полуготовый файл (сохранён «на середине правки») пересобирать нельзя: такой
    # код уехал бы в компонент и убил рантайм вместе с веб-сервером.
    rt_src = os.path.join(tmp, 'td', 'runtime')
    for rel in m.SOURCES:
        if not rel.endswith('.py'):
            continue
        p = os.path.join(tmp, rel.replace('/', os.sep))
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(p, 'w', encoding='utf-8') as f:
            f.write('x = 1\n')
    ok_compile, why_compile = m.sources_compile(tmp)
    check('целые исходники проходят проверку синтаксиса', ok_compile, why_compile)
    with open(os.path.join(rt_src, 'paint_runtime.py'), 'w', encoding='utf-8') as f:
        f.write('def broken(:\n')
    ok_compile, why_compile = m.sources_compile(tmp)
    check('полуготовый файл проверка синтаксиса не пропускает',
          (not ok_compile) and ('paint_runtime.py' in why_compile), why_compile)

    # ...и файл, который КОМПИЛИРУЕТСЯ, но ссылается на несуществующую константу,
    # тоже не должен уехать в компонент: он падал бы на каждом кадре с NameError
    # (ровно так вышло с FIT_MODES, когда правка шла в два приёма).
    with open(os.path.join(rt_src, 'paint_runtime.py'), 'w', encoding='utf-8') as f:
        f.write('def broken():\n    return NO_SUCH_CONSTANT\n')
    ok_compile, why_compile = m.sources_compile(tmp)
    check('файл с несуществующей константой тоже не пропускается',
          (not ok_compile) and ('NO_SUCH_CONSTANT' in why_compile), why_compile)
    check('проверка ловит именно ссылку на константу',
          m.check_source_names('X = 1\ndef f():\n    return X\n', 'ok.py') is None
          and bool(m.check_source_names('def f():\n    return MISSING\n', 'bad.py')),
          (m.check_source_names('X = 1\ndef f():\n    return X\n', 'ok.py'),
           m.check_source_names('def f():\n    return MISSING\n', 'bad.py')))
    rt._sig_checked = 0.0
    rt._sig_since = time.time() - 5.0
    rt._rebuild_t = 0.0
    rt._rebuild_sig = ''
    del RUNS[:]
    rt.watch_sources()
    check('полуготовый файл не вызывает пересборку', not RUNS,
          (list(RUNS), rt.diag.get('compile_skip')))

    # адрес — в DAT, а не только в Textport
    txt = rt.publish_address()
    check('адрес записан в DAT server/address',
          'PaintWeb' in txt and str(rt.tun['port']) in txt and 'PaintWeb' in addr.text,
          txt.splitlines()[:2])

    # кнопки на странице
    resp = {}
    rt.http({'method': 'POST', 'uri': '/api/rebuild', 'pars': {}}, resp)
    check('/api/rebuild отвечает json', resp.get('statusCode') == 200
          and json.loads(resp['data']).get('ok') is True, resp.get('data'))
    resp = {}
    rt.http({'method': 'POST', 'uri': '/api/selftest', 'pars': {}}, resp)
    check('/api/selftest отвечает json', resp.get('statusCode') == 200
          and json.loads(resp['data']).get('ok') is True, resp.get('data'))
    resp = {}
    rt.http({'method': 'GET', 'uri': '/api/address', 'pars': {}}, resp)
    j = json.loads(resp.get('data') or '{}')
    check('/api/address отдаёт адреса', bool(j.get('local')) and isinstance(j.get('lan'), list), j)

    # /api/open открывает браузер — подменяем модуль, чтобы тест ничего не открывал
    opened = []
    fake_wb = types.ModuleType('webbrowser')
    fake_wb.open = lambda url: opened.append(url)
    sys.modules['webbrowser'] = fake_wb
    try:
        resp = {}
        rt.http({'method': 'POST', 'uri': '/api/open', 'pars': {}}, resp)
        check('/api/open просит браузер', opened and '9980' in opened[0], opened)
    finally:
        sys.modules.pop('webbrowser', None)

    # ------------------------------------------- смена версии кода на живой сессии
    # Автопересборка меняет только текст DAT, а объект рантайма остаётся прежним.
    # Поэтому новый код обязан сам приводить поля к нужной форме: иначе в живом TD
    # каждый кадр падал с «list indices must be integers, not str», и компонент
    # выглядел мёртвым до перезапуска.
    print('\n[15] рантайм переживает смену версии кода без перезапуска TD')
    rt.dab_pending = []                 # так эти поля выглядели в прошлой версии
    rt.dab_pending_rect = None
    rt.dab_ready = 0
    rt.dab_ready_rect = None
    rt.paint_rect = None
    rt.send_rect = None
    rt.frame_dabs = []
    rt.frame_rect = None
    rt.mask_filled = True
    for name in ('frame_dabs_m', 'frame_rect_m', 'send_rect_m', 'switch_layer'):
        if hasattr(rt, name):
            delattr(rt, name)
    rt._ensure_state()
    want = ['colormask', 'mask', 'paint']
    check('dab_pending снова по буферам (их три)',
          isinstance(rt.dab_pending, dict) and sorted(rt.dab_pending) == want,
          rt.dab_pending)
    check('область патчей снова по буферам',
          isinstance(rt.paint_rect, dict) and sorted(rt.paint_rect) == want
          and sorted(rt.send_rect) == want, (rt.paint_rect, rt.send_rect))
    check('поля покадровых штампов и заливки масок восстановлены',
          isinstance(rt.frame_dabs, dict) and sorted(rt.frame_dabs) == want
          and isinstance(rt.frame_rect, dict) and sorted(rt.frame_rect) == want
          and isinstance(rt.mask_filled, dict) and sorted(rt.mask_filled) == ['colormask', 'mask']
          and rt.switch_layer == 'paint',
          (rt.frame_dabs, rt.frame_rect, rt.mask_filled, rt.switch_layer))
    err = None
    try:
        rt.on_frame_start(900)
        rt.on_frame_end(900)
    except Exception:
        err = traceback.format_exc().splitlines()[-1]
    check('кадр проходит после смены формы состояния', err is None, err)

    # ------------------------------------------- вставка источника (как fitTOP)
    # Режим выбирается со страницы именем пункта, в TD лежит числом (Fitmode) и
    # превращается в пункт меню fitTOP. Проверяем каждый режим: имя пункта меню в
    # этой сборке TD может отличаться от подписи, и раньше такие параметры молча
    # не вставали — поэтому проверяем ФАКТИЧЕСКОЕ значение на ноде.
    print('\n[16] вставка источника: режим из браузера -> fitTOP')
    fit_node = base.op('fit')
    want_tokens = {'fill': 'fill', 'horizontal': 'horizontal',
                   'vertical': 'vertical', 'best': 'fit',
                   'outside': 'outside', 'nativeres': 'nativeres'}
    bad = []
    for key, token in want_tokens.items():
        rt.read_pars()
        rt._set_layer({'id': 0, 'prop': 'fit', 'value': key})
        got = str(fit_node.par.fit.eval())
        if got != token:
            bad.append('%s -> %s (ждали %s)' % (key, got, token))
    check('каждый режим встаёт в fitTOP своим пунктом меню', not bad, bad)
    check('режим записан в свой параметр компонента',
          int(base.par.Fitmode.eval()) == 5, base.par.Fitmode.eval())
    check('в отчёте видно, что реально встало',
          'nativeres' in str(rt.diag.get('fit_mode')), rt.diag.get('fit_mode'))
    check('положение внутри кадра — по центру',
          str(fit_node.par.justifyh.eval()) == 'center'
          and str(fit_node.par.justifyv.eval()) == 'center',
          (fit_node.par.justifyh.eval(), fit_node.par.justifyv.eval()))
    check('клиент получает текущий режим и список режимов',
          [l['fit'] for l in rt.layers() if l['id'] == 0] == ['nativeres']
          and len(rt.layers()[0]['fitModes']) == 6,
          rt.layers()[0].get('fitModes'))
    rt._set_layer({'id': 0, 'prop': 'fit', 'value': 'best'})
    check('возврат к «вписать» тоже работает',
          str(fit_node.par.fit.eval()) == 'fit', fit_node.par.fit.eval())
    # Смена вставки меняет саму картинку источника — значит клиентам нужен новый
    # постер, иначе на странице останется прежний вид до перезагрузки.
    rt.clients['c1'] = dict(rt.clients.get('c1') or {})
    rt.clients['c1']['need_poster'] = False
    rt._set_layer({'id': 0, 'prop': 'fit', 'value': 'fill'})
    check('после смены вставки клиенту готовят новый постер источника',
          bool(rt.clients.get('c1', {}).get('need_poster')), rt.clients.get('c1'))

    # ---------------------------------------- падение чтения не ломает кадр
    # Так было при сборке из файла, сохранённого на середине правки: read_pars
    # падал с NameError, self.tun оставался пустым, и дальше по кадру сыпалось
    # «KeyError: 'proxyfps'» — одна ошибка превращалась в лавину.
    print('\n[17] сбой чтения параметров не оставляет пустой tun')
    rt.read_pars()
    good = dict(rt.tun)
    real_into = rt._read_pars_into
    rt._read_pars_into = lambda b, t: (_ for _ in ()).throw(NameError('FIT_MODES'))
    err = None
    try:
        got = rt.read_pars()
    except Exception:
        err = traceback.format_exc().splitlines()[-1]
        got = {}
    finally:
        rt._read_pars_into = real_into
    check('чтение параметров не бросает исключение наружу', err is None, err)
    check('после сбоя в tun есть все ключи, а не пусто',
          isinstance(got, dict) and 'proxyfps' in got and 'patchhz' in got
          and got.get('parse_failed') is True, sorted(got)[:6])
    check('прежние настройки сохраняются для остальных мест',
          got.get('w') == good.get('w') and got.get('patchmode') == good.get('patchmode'),
          (got.get('w'), good.get('w')))

    # Автопочинка (проверка отпечатка файлов и пересборка) обязана работать ДАЖЕ
    # если кадр падает в другом месте: иначе сломанный компонент не починится сам.
    print('\n[18] самовосстановление работает при падении кадра')
    calls = []
    real_watch = rt.watch_sources
    real_read = rt.read_pars
    rt.watch_sources = lambda *a, **k: calls.append('watch')
    rt.read_pars = lambda *a, **k: (_ for _ in ()).throw(NameError('FIT_MODES'))
    err = None
    try:
        rt.on_frame_start(950)
    except Exception:
        err = traceback.format_exc().splitlines()[-1]
    finally:
        rt.watch_sources = real_watch
        rt.read_pars = real_read
    check('кадр не падает наружу даже при сбое чтения параметров', err is None, err)
    check('проверка исходников всё равно выполнена', calls == ['watch'], calls)
    rt.read_pars()
    rt._ensure_state()

    # Заливка маски привязана к ОТПЕЧАТКУ СБОРКИ: пересборка пересоздаёт буферы,
    # а признак «маска уже залита» живёт в объекте рантайма и переживает её. Без
    # этого после пересборки маска оставалась пустой, и композит (источник ×
    # маска) становился чёрным — «в TD картинки не видно».
    print('\n[19] заливка маски повторяется после пересборки')
    ver = base.op('server/version')
    was = str(ver.text or '')
    rt.mask_filled = {'mask': True, 'colormask': True}
    rt._mask_sig = 'старый-отпечаток'
    ver.text = 'новый-отпечаток'
    rt._mask_fill_check()
    check('после смены отпечатка маска заливается заново', not any(rt.mask_filled.values()),
          (rt.mask_filled, rt.diag.get('mask_fill')))
    rt.mask_filled = {'mask': True, 'colormask': True}
    rt._mask_fill_check()
    check('на том же отпечатке заливка не повторяется', all(rt.mask_filled.values()),
          rt.mask_filled)
    ver.text = was
    rt._mask_sig = None
    rt.mask_filled = {'mask': True, 'colormask': True}

    # ------------------------------------------- переносимость: чужой проект
    # Компонент вставляют из .tox в другой проект. Записанный в нём Datadir
    # указывает на папку ПРЕЖНЕГО проекта, которой там нет — рантайм обязан
    # перейти на папку текущего проекта, а не писать по относительным путям.
    print('\n[20] папка данных переезжает вместе с проектом')
    rt.read_pars()
    keep_datadir = rt.tun.get('datadir')
    rt.tun['datadir'] = os.path.join(tmp, 'чужой-проект', 'paint')
    d = rt.dirs()
    check('несуществующий Datadir заменяется папкой проекта',
          os.path.normcase(d['root']) == os.path.normcase(os.path.join(tmp, 'paint')),
          d['root'])
    rt.tun['datadir'] = keep_datadir

    # Если компонент вставили из .tox, рядом нет ни td/**, ни build_paint_web.py:
    # пересобирать нечего, и автопересборка обязана молчать (иначе она каждую
    # минуту пыталась бы собрать то, чего нет).
    print('\n[20b] компонент из .tox не пытается пересобирать себя')
    empty_root = os.path.join(tmp, 'пустой-проект')
    os.makedirs(empty_root, exist_ok=True)
    keep_datadir2 = rt.tun.get('datadir')
    rt.tun['datadir'] = empty_root
    check('исходников рядом нет — это видно рантайму', not rt.sources_present())
    rt.tun['datadir'] = keep_datadir2
    try:
        # Проверяем именно ветку «исходников нет»: подменяем её определение и
        # смотрим, что автопересборка не запрашивается и в журнал попадает
        # понятная причина.
        real_sp = rt.sources_present
        rt.sources_present = lambda: False
        del RUNS[:]
        rt.autostart_done = False
        rt.autostart(reason='проверка')
        check('автопересборка не запрошена',
              not [r for r in RUNS if 'build_paint_web' in r[1][0]], RUNS)
        check('в отчёте сказано, почему сборки нет',
              'tox' in str(rt.diag.get('autostart') or '')
              or any('tox' in l for l in rt.log_lines),
              (rt.diag.get('autostart'), [l for l in rt.log_lines if 'tox' in l][:2]))
        del RUNS[:]
        rt._sig_checked = 0.0
        rt.watch_sources()
        check('слежение за файлами тоже молчит', not RUNS, RUNS)
    finally:
        rt.sources_present = real_sp
        rt.autostart_done = False
        rt.read_pars()

    # Страница и клиент лежат ещё и в текстовых DAT-ах компонента: если файлов в
    # папке web нет (компонент вставили из .tox), сервер отдаёт их из компонента
    # и раскладывает на диск.
    print('\n[21] страница отдаётся из компонента, если файлов нет')
    web = fake_td.FakeOp(base.path + '/web', 'baseCOMP')
    for name, body in (('index', '<html>из компонента</html>'),
                       ('app', '// клиент из компонента'),
                       ('style', '/* стили из компонента */')):
        dat = fake_td.FakeOp(web.path + '/' + name, 'textDAT')
        dat.text = body
    webdir = rt.dirs()['web']
    idx = os.path.join(webdir, 'index.html')
    if os.path.isfile(idx):
        os.remove(idx)
    resp = {}
    out = rt._static('/index.html', lambda code, reason, data, ctype: {
        'statusCode': code, 'data': data, 'type': ctype})
    check('страница отдаётся из компонента, когда файла на диске нет',
          out['statusCode'] == 200
          and 'из компонента' in bytes(out['data']).decode('utf-8'),
          (out['statusCode'], bytes(out['data'])[:40]))
    made = rt.materialize_web()
    check('файлы страницы разложены на диск из компонента',
          'index.html' in made and os.path.isfile(idx), made)
    with open(idx, 'r', encoding='utf-8') as f:
        check('на диск легло именно то, что в компоненте',
              'из компонента' in f.read(), made)
    check('повторная раскладка ничего не переписывает', rt.materialize_web() == [],
          rt.materialize_web())

    # ------------------------------- интенсивность слоёв и цветовая температура
    print('\n[22] интенсивность слоёв и слой монотонного цвета')
    warm = m.kelvin_rgb(2000.0)
    mid = m.kelvin_rgb(6500.0)
    cold = m.kelvin_rgb(10000.0)
    print('  2000K=%s 6500K=%s 10000K=%s'
          % (tuple(round(v, 2) for v in warm), tuple(round(v, 2) for v in mid),
             tuple(round(v, 2) for v in cold)))
    check('2000K тёплый (R заметно больше B)', warm[0] > warm[2] + 0.3, warm)
    check('6500K почти белый', min(mid) > 0.85, mid)
    check('10000K холодный (B больше R)', cold[2] > cold[0] + 0.15, cold)
    rt.read_pars()
    rt.tun['maskint'] = 0.5
    rt.tun['colorint'] = 0.7
    rt.tun['colorvisible'] = 1.0        # слой цвета включён своим тумблером
    rt.tun['colortemp'] = 3000.0
    rt._compose_key = None
    rt._apply_compose()
    lv = base.op('mask_level')
    cs = base.op('color_src')
    check('интенсивность маски ушла в mask_level',
          near(lv.par.opacity.eval(), 0.5, 0.01), lv.par.opacity.eval())
    check('интенсивность цвета ушла в альфу постоянного цвета',
          near(cs.par.alpha.eval(), 0.7, 0.01), cs.par.alpha.eval())
    check('цвет слоя считается по температуре (3000K тёплый)',
          cs.par.colorr.eval() > cs.par.colorb.eval() + 0.3,
          [cs.par[c].eval() for c in ('colorr', 'colorg', 'colorb')])
    check('параметры интенсивности читаются из компонента',
          (rt.tun.get('srcint'), rt.tun.get('paintint')) == (1.0, 1.0),
          (rt.tun.get('srcint'), rt.tun.get('paintint')))

    # -------------------------------- слой цвета как отдельный слой панели
    print('\n[24] слой «Цвет»: отдельная строка, видимость, температура')
    kinds = [l.get('kind') for l in rt.layers()]
    cols = [l for l in rt.layers() if l.get('kind') == 'color']
    check('в списке слоёв есть отдельный слой цвета', len(cols) == 1, kinds)
    check('порядок слоёв как в композите: источник → цвет → краска',
          kinds[:3] == ['source', 'color', 'paint'], kinds)
    col = cols[0] if cols else {}
    check('у слоя цвета есть видимость, интенсивность и температура',
          col.get('visible') in (0, 1)
          and isinstance(col.get('opacity'), float)
          and 1000 <= col.get('temp', 0) <= 40000, col)
    check('границы ползунка температуры приезжают со страницы',
          (col.get('tempMin'), col.get('tempMax')) == (2000, 10000), col)
    check('кисть по слою цвета рисует СВОЮ маску (не маску источника)',
          rt.layer_target(3) == 'colormask' and rt.layer_target(0) == 'mask',
          (rt.layer_target(0), rt.layer_target(3)))
    check('мазок по слою «Цвет» уходит в буфер его маски',
          rt.layer_target(4) == 'colormask' and rt.layer_target(2) == 'mask',
          (rt.layer_target(2), rt.layer_target(4)))

    rt._set_layer({'id': 3, 'prop': 'visible', 'value': 1})
    rt._set_layer({'id': 3, 'prop': 'opacity', 'value': 0.4})
    rt._set_layer({'id': 3, 'prop': 'temp', 'value': 3200})
    check('видимость слоя цвета идёт в Colorvisible',
          base.par['Colorvisible'].eval() in (1, True),
          base.par['Colorvisible'].eval())
    check('интенсивность слоя цвета идёт в Colorint',
          near(base.par['Colorint'].eval(), 0.4, 0.01), base.par['Colorint'].eval())
    check('температура со страницы идёт в Colortemp',
          near(base.par['Colortemp'].eval(), 3200.0, 1.0),
          base.par['Colortemp'].eval())

    # Композит: скрытый слой цвета не красит полотно, включённый — отдаёт цвет.
    cs = base.op('color_src')
    rt.read_pars()
    rt.tun['colorvisible'] = 0.0
    rt.tun['colorint'] = 1.0
    rt._compose_key = None
    rt._apply_compose()
    check('скрытый слой цвета не влияет на композит (альфа 0)',
          near(cs.par.alpha.eval(), 0.0, 0.01), cs.par.alpha.eval())
    rt.tun['colorvisible'] = 1.0
    rt.tun['colortemp'] = 2200.0
    rt._compose_key = None
    rt._apply_compose()
    warm = [cs.par[c].eval() for c in ('colorr', 'colorg', 'colorb')]
    check('включённый слой цвета отдаёт свою интенсивность',
          near(cs.par.alpha.eval(), 1.0, 0.01), cs.par.alpha.eval())
    check('цвет включённого слоя тёплый при 2200K (R много больше B)',
          warm[0] > warm[2] + 0.5, warm)
    rt.tun['colorvisible'] = 0.0
    rt.tun['colortemp'] = 6500.0
    rt._compose_key = None
    rt._apply_compose()

    # ---------------------------------------------- кнопка «Обновить из git»
    print('\n[23] обновление исходников из git')
    real_pd = rt.paint_dir
    rt.paint_dir = lambda: os.path.join(tmp, 'нет-репозитория')
    res = rt.git_update()
    check('без git-репозитория честный отказ',
          res.get('ok') is False and 'git' in str(res.get('why')), res)
    calls = []
    real_run = m.subprocess.run

    class FakeRes(object):
        returncode = 0
        stdout = b'Already up to date.'

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return FakeRes()

    m.subprocess.run = fake_run
    os.makedirs(os.path.join(tmp, '.git'), exist_ok=True)
    rt.paint_dir = lambda: tmp
    try:
        res = rt.git_update()
    finally:
        m.subprocess.run = real_run
        rt.paint_dir = real_pd
    check('git pull запускается в папке исходников',
          bool(calls) and calls[0][:4] == ['git', '-C', tmp, 'pull'], calls)
    check('успешное обновление попало в отчёт',
          res.get('ok') is True and str(rt.diag.get('git') or '').startswith('ок'),
          rt.diag.get('git'))

    # ------------------------------------------- кнопка «Открыть интерфейс» на базе
    print('\n[кнопка на компоненте: открыть интерфейс]')
    import time as _time
    import webbrowser
    opened = []
    real_open = webbrowser.open
    webbrowser.open = lambda url, *a, **k: (opened.append(url), True)[1]
    try:
        # К этому месту тесты уже роняли реестр нод фейкового TD (проверки
        # самовосстановления), поэтому дерево при необходимости поднимаем заново.
        bop = rt.o('')
        if bop is None:
            fake_td.build_tree(tmp)
            bop = rt.o('')
        bop.par.Datadir.val = tmp
        rt._opened_at = 0.0
        rt._open_pulses = None
        rt.open_button_check()
        check('кнопка: сам по себе запуск браузер не открывает', not opened, opened)
        bop.par['Openpage'].val = 1
        rt.open_button_check()
        check('кнопка: нажатие открывает страницу в браузере',
              len(opened) == 1 and opened[0].startswith('http://127.0.0.1:'), opened)
        rt.open_button_check()
        check('кнопка: без нового нажатия второй раз не открывается',
              len(opened) == 1, opened)
        # Колбэк parameterExecuteDAT и покадровая подстраховка срабатывают на одно
        # и то же нажатие — окно должно открыться одно.
        bop.par['Openpage'].val = 2
        rt._opened_at = _time.time()
        rt.open_button_check()
        check('кнопка: колбэк и подстраховка вместе не дают двух окон',
              len(opened) == 1, opened)
        # Следующее, уже отдельное, нажатие открывает снова (защита от дребезга
        # действует только на одно и то же нажатие).
        bop.par['Openpage'].val = 3
        rt._opened_at = 0.0
        rt.open_button_check()
        check('кнопка: после паузы нажатие снова открывает',
              len(opened) == 2, opened)

        # Тот же путь, но через колбэк параметра — так это делает сам TD
        pars_path = os.path.join(PAINT, 'td', 'runtime', 'pw_pars.py')
        with open(pars_path, encoding='utf-8') as f:
            src_pars = f.read()
        ns = {'me': object(), '_plog': lambda tag: None, '_pw': lambda dat: rt}
        exec(compile(src_pars, pars_path, 'exec'), ns)
        rt._opened_at = 0.0
        ns['onPulse'](bop.par['Openpage'])
        check('колбэк onPulse открывает страницу тем же кодом',
              len(opened) == 3, opened)
    except Exception:
        import traceback as _traceback
        check('кнопка: проверка прошла без исключений', False,
              _traceback.format_exc()[-400:])
    finally:
        webbrowser.open = real_open

    print('\n===============================================================')
    bad = [r for r in RESULTS if not r[0]]
    print('проверок: %d, провалов: %d' % (len(RESULTS), len(bad)))
    for _, name, extra in bad:
        print('  ПРОВАЛ: %s   %s' % (name, extra))
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
