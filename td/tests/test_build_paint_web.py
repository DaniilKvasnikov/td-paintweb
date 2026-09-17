"""Прогон САМОГО скрипта сборки (build_paint_web.py) на фейковом TouchDesigner.

    python paint/td/tests/test_build_paint_web.py

Зачем: скрипт сборки пользователь запускает первым действием, и до этого теста он
ни разу не исполнялся. Здесь он прогоняется целиком: создаёт дерево нод, связи,
данные, свои параметры, файлы и отчёт — и всё это проверяется.

Имена параметров операторов фейк берёт из стабов установленной сборки TD, поэтому
опечатка в имени параметра (в сборке или в рантайме) валится тестом, а не у
пользователя. А вот токены меню (например 'pixels' у cropTOP) в стабах отсутствуют —
их правильность подтвердит только сам TD, в тесте используются предположения
из fake_td.MENU_TOKENS.
"""

import os
import shutil
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


def run_builder(paint_dir, project_dir):
    """Исполнить build_paint_web.py так же, как это делает Textport."""
    path = os.path.join(PAINT, 'td', 'build_paint_web.py')
    with open(path, 'r', encoding='utf-8') as f:
        src = f.read()
    ns = {
        'op': fake_td.make_op_func(),
        'project': fake_td.FakeProject(project_dir),
        '__name__': '__main__',
        # __file__ намеренно НЕ задаём: ровно как при exec(open(...).read())
        # в Textport — значит проверяется и запасной путь поиска папки paint/
    }
    exec(compile(src, path, 'exec'), ns)
    return ns


def runtime_signature_for(paint_dir):
    """Отпечаток так, как его считает рантайм: исполняем paint_runtime.py.

    Именно этот способ использует и сама сборка (runtime_signature), поэтому
    проверка ловит расхождение двух способов подсчёта.
    """
    path = os.path.join(PAINT, 'td', 'runtime', 'paint_runtime.py')
    with open(path, 'r', encoding='utf-8') as f:
        src = f.read()
    ns = {'__name__': 'sig_check'}
    exec(compile(src, path, 'exec'), ns)
    return ns['sources_signature'](paint_dir)


test_build_paint_web_sig = runtime_signature_for


def main():
    print('\n=== сборка paint_web на фейковом TD ==========================')
    if not fake_td.stubs_available():
        print('  стабы TD не найдены — тест пропущен (нужен установленный TD)')
        return 0

    tmp = os.path.join(HERE, '_tmp_build')
    shutil.rmtree(tmp, ignore_errors=True)
    paint_dir = os.path.join(tmp, 'paint')
    os.makedirs(os.path.join(paint_dir, 'td'))
    os.makedirs(os.path.join(paint_dir, 'web'))
    os.makedirs(os.path.join(paint_dir, 'uploads'))
    # сборка требует на месте код рантайма и непустую веб-папку
    shutil.copytree(os.path.join(PAINT, 'td', 'runtime'),
                    os.path.join(paint_dir, 'td', 'runtime'))
    # Скрипты сборки и проверок — тоже: рантайм считает дерево исходников готовым
    # только когда видит их рядом с рантаймом (иначе пишет «нет файла сборки»).
    for name in sorted(os.listdir(os.path.join(PAINT, 'td'))):
        src_py = os.path.join(PAINT, 'td', name)
        if name.endswith('.py') and os.path.isfile(src_py):
            shutil.copy2(src_py, os.path.join(paint_dir, 'td', name))
    with open(os.path.join(paint_dir, 'web', 'index.html'), 'w', encoding='utf-8') as f:
        f.write('<html>test</html>')
    with open(os.path.join(paint_dir, 'web', 'app.js'), 'w', encoding='utf-8') as f:
        f.write('// test client')
    with open(os.path.join(paint_dir, 'web', 'style.css'), 'w', encoding='utf-8') as f:
        f.write('/* test styles */')
    # тестовую картинку кладём заранее, чтобы не гонять питон-генератор
    sample = os.path.join(paint_dir, 'uploads', 'sample.png')
    with open(sample, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n')          # содержимое тут не важно

    fake_td.FakeOp.REG.clear()
    fake_td.FakeOp('/', 'baseCOMP')            # корень проекта, как в TD
    fake_td.FakeOp('/project1', 'baseCOMP')    # стандартный корневой компонент
    ns = run_builder(paint_dir, tmp)

    rep = ns.get('REPORT', [])
    fails = ns.get('FAIL', [])
    print('  отчёт сборки: %d строк, проблем: %d' % (len(rep), len(fails)))
    for line in fails:
        print('      ПРОБЛЕМА: %s' % line)
    check('сборка прошла без проблем', not fails, fails)
    check('сборка подтвердила завершение (нет ложной зелени)',
          ns.get('COMPLETED') is True, ns.get('COMPLETED'))

    base = fake_td.FakeOp.REG.get('/project1/paint_web')
    check('создан /project1/paint_web', base is not None)
    if base is None:
        return 1

    # ---------------------------------------------------------- дерево
    print('\n[дерево]')
    expect = ['server/ws', 'server/callbacks', 'server/runtime', 'server/log',
              'ext', 'movie', 'src', 'fit', 'proxy',
              'dabpng', 'fb', 'brush',
              'restore', 'patchin', 'sw', 'buf', 'snap',
              'crop', 'cropundo', 'cropsnap',
              'fbm', 'brushm', 'restorem', 'patchinm', 'swm', 'bufm', 'snapm',
              'cropm', 'cropsnapm',
              'shaders/paint', 'shaders/restore', 'shaders/maskapply',
              'src_level', 'paint_level', 'maskapply',
              'over', 'out1', 'out1', 'tick']
    missing = [p for p in expect if base.op(p) is None]
    check('все ноды на месте (%d)' % len(expect), not missing, missing)
    types_ok = (base.op('server/ws').type == 'webserverDAT'
                and base.op('brush').type == 'glslmultiTOP'
                and base.op('tick').type == 'executeDAT'
                and base.op('dabpng').type == 'moviefileinTOP'
                and base.op('src').type == 'switchTOP')
    check('типы ключевых нод верные', types_ok)

    # ---------------------------------------------------------- связи
    print('\n[связи]')
    def src_of(rel, idx):
        o = base.op(rel)
        got = o.inputs.get(idx)
        return got.path if got is not None else None

    check('fit <- src', src_of('fit', 0) == '/project1/paint_web/src')
    check('src[0] <- movie (источник: файл)',
          src_of('src', 0) == '/project1/paint_web/src'.replace('src', 'movie'))
    check('src[1] <- ext (внешний TOP по пути)',
          src_of('src', 1) == '/project1/paint_web/ext')
    check('src — switchTOP, а не selectTOP (у select в этой сборке TD нет входов)',
          base.op('src').type == 'switchTOP', base.op('src').type)
    check('src.index = 0 (по умолчанию источник — файл)',
          int(base.op('src').par.index.eval()) == 0,
          base.op('src').par.index.eval())
    check('brush[0] <- fb', src_of('brush', 0) == '/project1/paint_web/fb')
    check('brush[1] <- dabpng (текстура штампов)',
          src_of('brush', 1) == '/project1/paint_web/dabpng')
    check('sw[0] <- brush', src_of('sw', 0) == '/project1/paint_web/brush')
    check('sw[1] <- restore', src_of('sw', 1) == '/project1/paint_web/restore')
    check('buf <- sw', src_of('buf', 0) == '/project1/paint_web/sw')
    check('fb <- buf (обратная связь есть)',
          src_of('fb', 0) == '/project1/paint_web/buf')
    check('snap <- buf', src_of('snap', 0) == '/project1/paint_web/buf')
    check('src_level <- fit (источник)', src_of('src_level', 0)
          == '/project1/paint_web/fit')
    check('paint_level <- buf (краска)', src_of('paint_level', 0)
          == '/project1/paint_web/buf')
    check('over[0] <- paint_level, over[1] <- base_layer (цвет поверх источника)',
          src_of('over', 0) == '/project1/paint_web/paint_level'
          and src_of('over', 1) == '/project1/paint_web/base_layer')
    # Цепочка маски — зеркало цепочки краски: своя петля, своя кисть, своё
    # восстановление области для undo. Без этого мазок по слою «Источник» было
    # некуда положить, и маска в TD не влияла ни на что.
    check('brushm[0] <- fbm, brushm[1] <- dabpng',
          src_of('brushm', 0) == '/project1/paint_web/fbm'
          and src_of('brushm', 1) == '/project1/paint_web/dabpng')
    check('restorem <- fbm и patchinm',
          src_of('restorem', 0) == '/project1/paint_web/fbm'
          and src_of('restorem', 1) == '/project1/paint_web/patchinm')
    check('swm <- brushm и restorem',
          src_of('swm', 0) == '/project1/paint_web/brushm'
          and src_of('swm', 1) == '/project1/paint_web/restorem')
    check('bufm <- swm, fbm <- bufm (петля маски замкнута)',
          src_of('bufm', 0) == '/project1/paint_web/swm'
          and src_of('fbm', 0) == '/project1/paint_web/bufm')
    check('snapm <- bufm, cropm <- bufm, cropsnapm <- snapm',
          src_of('snapm', 0) == '/project1/paint_web/bufm'
          and src_of('cropm', 0) == '/project1/paint_web/bufm'
          and src_of('cropsnapm', 0) == '/project1/paint_web/snapm')
    check('maskapply[0] <- src_level, maskapply[1] <- mask_level',
          src_of('maskapply', 0) == '/project1/paint_web/src_level'
          and src_of('maskapply', 1) == '/project1/paint_web/mask_level')
    # out1 — смотреть готовый композит внутри сети; out (outTOP) — тот же
    # композит на ВЫХОДНОМ разъёме компонента: только так картинка уходит в
    # другую сеть (провод между компонентами TD не проводит).
    check('out1 <- over (смотреть здесь)', src_of('out1', 0) == '/project1/paint_web/over')
    check('out (outTOP) <- over: выход компонента наружу',
          base.op('out').type == 'outTOP'
          and src_of('out', 0) == '/project1/paint_web/over',
          (base.op('out').type, src_of('out', 0)))

    # ---------------------------------------------------------- параметры нод
    print('\n[параметры нод]')
    ws = base.op('server/ws')
    check('сервер включён', ws.par.port.eval() == 9980 and ws.par.active.eval() == 1,
          (ws.par.port.eval(), ws.par.active.eval()))
    check('callbacks сервера указывают на DAT',
          str(ws.par.callbacks.eval()).endswith('callbacks'))
    tick = base.op('tick')
    check('executeDAT: активен', tick.par.active.eval() == 1)
    check('executeDAT: включён Frame Start', tick.par.framestart.eval() == 1)
    check('executeDAT: включён Frame End', tick.par.frameend.eval() == 1)
    check('executeDAT: скрипт записан', 'onFrameStart' in tick.text
          and 'paintweb' in tick.text.lower(), len(tick.text))
    check('исполняемый скрипт записан в executeDAT', 'onFrameStart' in base.op('tick').text)
    check('шейдер кисти записан', 'MAXDABS' in base.op('shaders/paint').text)
    check('шейдер маски записан и кисть маски на него ссылается',
          'sTD2DInputs[1]' in base.op('shaders/maskapply').text
          and str(base.op('brushm').par.pixeldat.eval()).endswith('shaders/paint')
          and str(base.op('maskapply').par.pixeldat.eval()).endswith('shaders/maskapply'))
    check('композит: источник с маской и краска поверх',
          base.op('over').type == 'overTOP'
          and base.op('maskapply').type == 'glslmultiTOP'
          and base.op('mode') is None, base.op('mode'))
    check('шейдер восстановления записан', 'uRect' in base.op('shaders/restore').text)
    check('кисть ссылается на DAT шейдера',
          str(base.op('brush').par.pixeldat.eval()).endswith('shaders/paint'))
    check('таблицы штампов: имя uCount',
          base.op('brush').par.vec1name.eval() == 'uCount')
    check('uRect на месте', base.op('brush').par.vec3name.eval() == 'uRect')
    check('у кисти маски те же таблицы uniform-ов',
          base.op('brushm').par.vec1name.eval() == 'uCount'
          and base.op('brushm').par.vec3name.eval() == 'uRect')
    # Источник обратной связи задаётся И входом, И параметром Target TOP: это одно
    # и то же (предыдущий кадр слоя), но разные сборки TD используют то один, то
    # другой механизм. Предупреждение TD про цикл кука при этом ожидаемо.
    check('feedback берёт buf входом и параметром Target TOP',
          src_of('fb', 0) == '/project1/paint_web/buf'
          and str(base.op('fb').par.top.eval()).endswith('/buf'),
          'top=%r inputs=%r' % (base.op('fb').par.top.eval(),
                                [x.path for x in base.op('fb').inputs
                                 if x is not None]))
    check('switch на нулевом входе', base.op('sw').par.index.eval() == 0)
    check('patchin не играет', base.op('patchin').par.play.eval() == 0)
    # dabpng несёт данные, а не картинку: сборка обязана попросить TD не трогать
    # значения (линейное пространство, без премульта, линейный формат).
    dab = base.op('dabpng')
    check('dabpng: цветовое пространство «линейное» (данные!)',
          str(dab.par.inputcolorspace.eval()).lower() == 'linear',
          dab.par.inputcolorspace.eval())
    check('dabpng: RGB не премультиплицируется на альфу',
          str(dab.par.premultrgbbyalpha.eval()).lower()
          in ('none', 'off', 'no', 'nopremultiply'),
          dab.par.premultrgbbyalpha.eval())
    check('dabpng: формат без sRGB (rgba8fixed)',
          str(dab.par.format.eval()).lower().startswith('rgba8fixed'),
          dab.par.format.eval())
    # ---------------------------------------------------- запуск без Textport
    # Отпечаток исходников: по нему рантайм сам решает, нужна ли пересборка при
    # открытии проекта. Одна и та же функция обязана считать его и здесь, и в
    # рантайме, иначе компонент будет пересобираться по кругу.
    ver = base.op('server/version')
    check('есть DAT с отпечатком сборки', ver is not None)
    if ver is not None:
        want = test_build_paint_web_sig(paint_dir)
        got = [l.strip() for l in str(ver.text or '').splitlines()
               if l.strip() and not l.startswith('#')]
        check('отпечаток совпадает с расчётом рантайма', got and got[0] == want,
              (got[:1], want))
    addr = base.op('server/address')
    check('есть DAT с адресом страницы',
          addr is not None and 'PaintWeb' in str(addr.text or ''),
          (addr.text or '')[:60] if addr is not None else 'нет DAT')
    check('executeDAT включён на старте проекта (start=1)',
          int(base.op('tick').par.start.eval()) == 1,
          base.op('tick').par.start.eval())
    check('кроп в пикселях',
          str(base.op('crop').par.cropleftunit.eval()) == 'pixels',
          base.op('crop').par.cropleftunit.eval())
    check('разрешение кисти = полотну',
          base.op('brush').par.resolutionw.eval() == 1920
          and base.op('brush').par.resolutionh.eval() == 1080)

    # ---------------------------------------------------------- свои параметры
    print('\n[свои параметры компонента]')
    need = ['Canvasw', 'Canvash', 'Port', 'Patchhz', 'Proxyfps', 'Undodepth',
            'Srcfile', 'Srcvisible', 'Srcopacity', 'Paintvisible', 'Paintopacity',
            'Fitmode', 'Useext', 'Flipy', 'Datadir']
    miss = [n for n in need if n not in base.par]
    check('все параметры есть (%d)' % len(need), not miss, miss)
    check('Canvasw = 1920', base.par.Canvasw.eval() == 1920, base.par.Canvasw.eval())
    check('Port = 9980', base.par.Port.eval() == 9980)
    # Datadir — папка данных ЭТОГО проекта. В другом проекте такого пути нет, и
    # рантайм сам переходит на <проект>/paint (проверяется отдельным тестом),
    # поэтому компонент из .tox работает и на чужом проекте.
    check('Datadir указывает на папку данных проекта',
          os.path.normcase(str(base.par.Datadir.eval())) == os.path.normcase(paint_dir),
          base.par.Datadir.eval())
    # Ссылка на интерфейс прямо в базе: адрес в параметре Page, кнопка Openpage.
    missing_link = [n for n in ('Page', 'Openpage') if n not in base.par]
    check('в базе есть ссылка на интерфейс (параметры Page и Openpage)',
          not missing_link, missing_link)
    pulse = base.op('onpulse')
    check('кнопка «Открыть интерфейс» подключена к parameterExecuteDAT onpulse',
          pulse is not None and pulse.type == 'parameterexecuteDAT'
          and 'Openpage' in str(getattr(pulse, 'text', '') or ''),
          None if pulse is None else (pulse.type, len(str(pulse.text or ''))))
    check('страница, стили и клиент лежат в компоненте (tox самодостаточен)',
          all(base.op('web/' + n) is not None and len(str(base.op('web/' + n).text or '')) > 5
              for n in ('index', 'app', 'style')),
          [(n, base.op('web/' + n) is not None) for n in ('index', 'app', 'style')])
    check('Srcfile указывает на sample.png',
          str(base.par.Srcfile.eval()).endswith('sample.png'))
    check('Flipy выключен по умолчанию', base.par.Flipy.eval() == 0)

    # ---------------------------------------------------------- файлы и отчёт
    print('\n[файлы]')
    for sub in ('web', 'uploads', 'media', 'tmp'):
        check('папка paint/%s создана' % sub, os.path.isdir(os.path.join(paint_dir, sub)))
    rp = os.path.join(paint_dir, 'td', 'last_build_report.txt')
    check('отчёт записан на диск', os.path.isfile(rp))
    if os.path.isfile(rp):
        with open(rp, 'r', encoding='utf-8') as f:
            body = f.read()
        check('в отчёте нет проблем', '=== проблемы ===\nнет' in body.replace('\r', ''),
              body[-200:])
        check('в отчёте есть фактические токены параметров',
              'crop: cropleftunit=pixels' in body and 'brush: format=' in body,
              [l for l in body.splitlines() if 'cropleftunit' in l][:1])
        check('в отчёте есть адрес и команда файрвола с настоящим портом',
              'New-NetFirewallRule' in body and '-LocalPort 9980' in body
              and '127.0.0.1:9980' in body,
              [l for l in body.splitlines() if 'Firewall' in l][:1])
    tox_saved = [p for p in (base.saved or [])]
    check('tox сохранён рядом с проектом', any(p.endswith('paint_web.tox') for p in tox_saved),
          tox_saved)

    # ---------------------------------------------------------- генератор картинки
    print('\n[тестовая картинка]')
    png = os.path.join(tmp, 'gen.png')
    try:
        ns['make_png'](png, 96, 54)
        import test_selftest_png
        w, h, nch, rows = test_selftest_png.__dict__ and _decode(png)
        check('make_png делает валидный PNG %dx%d' % (w, h), (w, h) == (96, 54), (w, h))
        check('make_png: три канала RGB', nch == 3, nch)
        prof = _profile(rows, nch, w)
        check('make_png: картинка не пустая', max(prof) > 1000, max(prof))
    except Exception as e:
        check('make_png делает валидный PNG', False, e)

    # ---------------------------------------------------------- повторный запуск
    print('\n[повторный запуск — идемпотентность]')
    before = sorted(fake_td.FakeOp.REG.keys())
    ns2 = run_builder(paint_dir, tmp)
    after = sorted(fake_td.FakeOp.REG.keys())
    fails2 = ns2.get('FAIL', [])
    check('повторная сборка тоже без проблем', not fails2, fails2)
    check('состав нод не изменился', before == after,
          [p for p in after if p not in before][:5])
    base2 = fake_td.FakeOp.REG.get('/project1/paint_web')
    check('параметры не задвоились', base2 is not None
          and len([p for p in base2.customPages if p.name == 'Paint']) == 1,
          [p.name for p in (base2.customPages if base2 else [])])
    check('код рантайма перезалит', 'class Runtime' in base2.op('server/runtime').text)

    # ---------------------------------------------------------- сквозная проверка
    print('\n[сквозная проверка: рантайм на собранном дереве]')
    try:
        _integration(base2, paint_dir)
    except Exception:
        import traceback
        check('рантайм работает на собранном дереве', False,
              traceback.format_exc().splitlines()[-1])

    print('\n[запасной путь: нет /project1]')
    fake_td.FakeOp.REG.clear()
    fake_td.FakeOp('/', 'baseCOMP')
    fake_td.FakeOp('/some_root', 'baseCOMP')   # единственный компонент в проекте
    ns3 = run_builder(paint_dir, tmp)
    check('сборка нашла корень сам', not ns3.get('FAIL', []), ns3.get('FAIL'))
    check('компонент создан в первом попавшемся COMP',
          fake_td.FakeOp.REG.get('/some_root/paint_web') is not None,
          sorted(fake_td.FakeOp.REG.keys())[:4])

    # ---------------------------------------------------------- уборка и защита
    # Это проверка на ту самую поломку, которую увидел пользователь: из base
    # ничего не выходило, потому что TD НЕ соединяет операторы из разных
    # компонентов. Теперь весь граф TOP-ов лежит в одной сети, и связи обязаны
    # встать — а если не встанут, сборка обязана сказать об этом, а не молчать.
    print('\n[связи задаются setInputs и это проверяется]')
    base3 = fake_td.FakeOp.REG.get('/some_root/paint_web')
    fb3 = base3.op('fb')
    check('feedback берёт buf входом', fb3.inputs.get(0) is base3.op('buf'))
    check('feedback.top указывает на buf',
          str(fb3.par.top.eval()).endswith('/buf'), fb3.par.top.eval())
    check('входы композита реально встали',
          base3.op('src_level').inputs.get(0) is base3.op('fit')
          and base3.op('paint_level').inputs.get(0) is base3.op('buf')
          and base3.op('over').inputs.get(1) is base3.op('base_layer'))
    check('out1 и out получают композит',
          base3.op('out1').inputs.get(0) is base3.op('over')
          and base3.op('out').inputs.get(0) is base3.op('over'))
    # Все TOP-ы обязаны лежать в ОДНОЙ сети: иначе TD молча не соединит их.
    sub_comps = [c.name for c in base3.children
                 if c.isCOMP and c.name not in ('server', 'shaders', 'web')]
    check('в компоненте нет под-компонентов с TOP-ами (иначе связи не встанут)',
          not sub_comps, sub_comps)
    check('источник — selectTOP с параметром Top (провод между сетями не идёт)',
          base3.op('ext').type == 'selectTOP'
          and base3.op('ext').par['top'] is not None,
          base3.op('ext').type)

    # ---------------------------------------------------------------- вёрстка
    # Пользователь жаловался, что ноды лежат нечитаемо. Позиции теперь задаются
    # явно, и это проверяется: уникальные координаты + порядок слева направо
    # вдоль потока данных.
    print('\n[вёрстка: ноды расставлены читаемо]')
    tops = [o for o in base3.children if not o.isCOMP and o.type.endswith('TOP')]
    pos = [(int(o.nodeX), int(o.nodeY)) for o in tops]
    check('позиции нод заданы и уникальны',
          len(set(pos)) == len(pos) and all(p != (0, 0) for p in pos),
          sorted(set(p for p in pos if pos.count(p) > 1)))

    def nx(rel):
        return int(base3.op(rel).nodeX)
    flow = ['movie', 'src', 'fit', 'brush', 'sw', 'buf', 'paint_level',
            'maskapply', 'over', 'out1']
    xs = [nx(r) for r in flow]
    check('поток данных идёт слева направо (%s)' % ' → '.join(flow),
          xs == sorted(xs), list(zip(flow, xs)))
    check('цепочка маски стоит в тех же колонках, что и краска',
          nx('fbm') == nx('fb') and nx('brushm') == nx('brush')
          and nx('bufm') == nx('buf') and nx('cropm') == nx('crop'),
          [(r, nx(r)) for r in ('fb', 'fbm', 'brush', 'brushm', 'buf', 'bufm')])
    check('подписи групп в сети есть',
          all(base3.op('note_%d' % i) is not None and
              len(str(base3.op('note_%d' % i).text or '')) > 40 for i in range(4)),
          [bool(base3.op('note_%d' % i)) for i in range(4)])

    # устаревшие ноды прошлых вариантов сборки: FakeOp сам регистрируется у родителя
    bp = base3.path
    fake_td.FakeOp(bp + '/comp', 'baseCOMP')
    fake_td.FakeOp(bp + '/comp/comp', 'glslmultiTOP')
    fake_td.FakeOp(bp + '/paint', 'baseCOMP')
    fake_td.FakeOp(bp + '/paint/dabs', 'scriptTOP')
    fake_td.FakeOp(bp + '/in', 'baseCOMP')
    fake_td.FakeOp(bp + '/in/ext', 'inTOP')
    check('подложили устаревшие ноды', base3.op('comp/comp') is not None
          and base3.op('paint/dabs') is not None)
    ns4 = run_builder(paint_dir, tmp)
    check('сборка прошла после уборки', not ns4.get('FAIL', []), ns4.get('FAIL'))
    check('устаревшие под-компоненты удалены',
          [n for n in ('in', 'paint', 'comp') if base3.op(n) is not None] == [],
          [n for n in ('in', 'paint', 'comp') if base3.op(n) is not None])
    check('рабочие ноды на месте', base3.op('out1') is not None
          and base3.op('maskapply') is not None and base3.op('out') is not None
          and base3.op('bufm') is not None)

    # И главное: если TD всё-таки не поставит вход, сборка обязана это заметить, а
    # не написать «всё чисто». Подменяем setInputs на «делаю вид, что поставил» —
    # и предварительно СНИМАЕМ входы, иначе проверять было бы нечего: с прошлой
    # сборки они уже стоят и «ничего не делать» выглядело бы успехом.
    print('\n[если связи не встали — сборка обязана сказать об этом]')
    real_set = fake_td.FakeOp.setInputs
    for o in [base3] + base3.findChildren():
        try:
            for i in range(len(o.inputs)):
                o.inputs[i] = None
        except Exception:
            pass
    check('входы сняты перед проверкой',
          base3.op('over').inputs.get(0) is None)

    def deaf(self, ops):
        return None                              # молча ничего не подключил
    ver3 = base3.op('server/version')
    if ver3 is not None:
        ver3.text = 'SENTINEL-СТАРЫЙ-ОТПЕЧАТОК'
    fake_td.FakeOp.setInputs = deaf
    try:
        ns5 = run_builder(paint_dir, tmp)
        fails5 = ns5.get('FAIL', []) or []
        check('отсутствие связей попало в провалы сборки',
              any('не совпали' in str(x) or 'вход' in str(x) for x in fails5),
              fails5[:3])
        check('сборка не отчиталась «чисто» при пустых входах',
              ns5.get('COMPLETED') is not True or bool(fails5),
              (ns5.get('COMPLETED'), len(fails5)))
        # Ключевое для автозапуска: упавшая сборка НЕ должна обновлять отпечаток,
        # иначе рантайм решит, что всё собрано, и не повторит попытку.
        check('упавшая сборка не обновила отпечаток',
              ver3 is not None and 'SENTINEL' in str(ver3.text or ''),
              (ver3.text if ver3 is not None else 'нет DAT'))
    finally:
        fake_td.FakeOp.setInputs = real_set
    ns6 = run_builder(paint_dir, tmp)
    check('после возврата setInputs сборка снова чистая', not ns6.get('FAIL', []),
          (ns6.get('FAIL') or [])[:3])
    check('входы вернулись', base3.op('over').inputs.get(1)
          is base3.op('base_layer'))

    # ---------------------------------------------------------- честность заглушки
    print('\n[заглушка ведёт себя как живой TD]')
    page = base.appendCustomPage('Проверка')
    rejected = False
    try:
        page.appendInt('X', 'Подпись')      # позиционная подпись — ошибка в TD
    except Exception as e:
        rejected = 'Single name argument expected' in str(e)
    check('позиционная подпись отвергается, как в TD', rejected)
    made = page.appendInt('Y', label='Подпись')   # а именованная работает
    check('именованная подпись принимается', made is not None
          and base.par['Y'].eval() == 0)
    base.removeCustomPage(page)

    print('\n===============================================================')
    bad = [r for r in RESULTS if not r[0]]
    print('проверок: %d, провалов: %d' % (len(RESULTS), len(bad)))
    for _, name, extra in bad:
        print('  ПРОВАЛ: %s   %s' % (name, extra))
    shutil.rmtree(tmp, ignore_errors=True)
    return 1 if bad else 0


# --- вспомогательное: декодер/профиль берём из теста самопроверки ---

def _decode(path):
    import test_selftest_png
    m = test_selftest_png.load_selftest_module()
    return m.png_read(path)


def _profile(rows, nch, w):
    import test_selftest_png
    m = test_selftest_png.load_selftest_module()
    return m.row_profile(rows, nch, w)


def _integration(base, paint_dir):
    """Прогнать рантайм на дереве, которое создала сборка: мазок -> патч -> undo."""
    import json
    import struct
    # Сборка оставляет Datadir пустым (это правильно для .tox: путь к папке данных
    # у каждого проекта свой, и рантайм ищет <проект>/paint). В тесте проекта нет,
    # поэтому папку данных задаём явно — иначе рантайм писал бы по относительным
    # путям от текущего каталога процесса.
    try:
        base.par.Datadir.val = paint_dir
    except Exception:
        pass
    boot_path = os.path.join(PAINT, 'td', 'runtime', 'pw_boot.py')
    with open(boot_path, 'r', encoding='utf-8') as f:
        boot_src = f.read()
    boot = types.ModuleType('paintweb_boot')
    boot.__dict__['op'] = fake_td.make_op_func()
    # Рантайм ищет папку исходников рядом с проектом (<проект>/paint или корень
    # репозитория). В тесте проекта нет — подставляем фальшивый с папкой
    # исходников: иначе автозапуск честно пишет «не нашёл папку paint» и проверка
    # «рантайм не логировал ошибок» падает на пустом месте.
    boot.__dict__['project'] = fake_td.FakeProject(paint_dir)
    base.op('server/runtime').text = open(
        os.path.join(PAINT, 'td', 'runtime', 'paint_runtime.py'),
        encoding='utf-8').read()
    exec(compile(boot_src, boot_path, 'exec'), boot.__dict__)
    # Изоляция: рантайм живёт в sys.modules по пути компонента, а путь у всех
    # тестов один (/project1/paint_web). Сначала создаём модуль, потом сбрасываем
    # накопленный инстанс, и берём свежий.
    boot.attach(base, fake_td.make_op_func())
    sys.modules[boot.SHARED_MODULE]._INSTANCES.clear()
    rt = boot.attach(base, fake_td.make_op_func())
    check('рантайм привязался к собранному дереву', rt is not None)
    rt.clients.clear()
    rt.strokes.clear()
    del rt.undo[:]
    del rt.redo[:]
    rt.snap_depth = 0
    rt.switch_until = -1
    rt.restore_req = None
    rt.frame = 0

    sent = []
    rt._send = lambda c, o: (sent.append(('text', o)) or True)
    rt._send_bin = lambda c, h, b: (sent.append((h, len(b))) or True)
    rt.clients['t'] = {'ready': True, 'need_poster': False, 'sync_queue': []}

    # Кадры дёргаем через НАСТОЯЩИЙ DAT executeDAT (`tick`), как это делает TD:
    # исполняется тот же код с тем же загрузчиком, а не методы рантайма напрямую.
    tick_ns = {'op': fake_td.make_op_func(), 'me': base.op('tick'), 'print': print}
    exec(compile(base.op('tick').text, 'tick', 'exec'), tick_ns)
    check('в executeDAT есть onFrameStart/onFrameEnd',
          callable(tick_ns.get('onFrameStart')) and callable(tick_ns.get('onFrameEnd')))

    def frames(n, start):
        peak = 0
        for i in range(n):
            tick_ns['onFrameStart'](start + i)
            base.op('out1').cook(force=True)
            tick_ns['onFrameEnd'](start + i)
            peak = max(peak, rt.diag.get('dabs', 0))
        return peak

    rt.ws_text('t', json.dumps({'t': 'tool', 'tool': {'tool': 'brush', 'size': 40,
                                                      'color': '#00ff00',
                                                      'spacing': 0.15}}))
    pts = [(300.0 + i * 40.0, 400.0, 255) for i in range(6)]
    body = struct.pack('<BIBBH', 1, 1, 1, 1, len(pts))
    for (x, y, pr) in pts:
        body += struct.pack('<HHBBH', int(x / 1920.0 * 65535), int(y / 1080.0 * 65535),
                            pr, 0, 0)
    rt.ws_binary('t', body)
    peak1 = frames(3, 10)
    rt.ws_binary('t', struct.pack('<BIBBH', 1, 1, 2, 1, 0))
    frames(3, 20)
    # в конце кадра diag.dabs обнуляется (штампы считаются на кадр), поэтому
    # смотрим пик за время прогона
    check('штампы посчитаны', peak1 > 0, peak1)
    check('undo-запись появилась на собранном дереве', len(rt.undo) == 1, len(rt.undo))
    binary = [s for s in sent if isinstance(s, tuple) and len(s) == 2
              and isinstance(s[0], dict)]
    check('патч закодирован и отправлен', len(binary) > 0, len(binary))
    if binary:
        head, blen = binary[-1]
        check('патч про слой 1 и финальный', head.get('layer') == 1 and head.get('final') == 1,
              head)
        check('прямоугольник патча накрывает мазок',
              270 <= head.get('x', 0) <= 285 and 370 <= head.get('y', 0) <= 385
              and head.get('w', 0) > 200 and head.get('h', 0) > 30, head)
    # Патч кодируется НЕ с кропа, а с узла снятия премультипликации: слой в TD
    # премультиплицирован, а PNG в браузере читается как straight alpha.
    check('патч кодировал узел снятия премульта (unpremult)',
          len(base.op('unpremult').encoded) > 0,
          base.op('unpremult').encoded[:2])
    check('unpremult получает вырезанную область',
          base.op('unpremult').inputs.get(0) is base.op('crop'))
    check('патч кодировался как .png (суффикс с точкой)',
          all(f == '.png' for f, _q in base.op('unpremult').encoded),
          base.op('unpremult').encoded[:3])
    # Дамп последнего патча: по нему видно, что именно уехало в браузер
    # (формат, размер, где краска) — иначе проверить это нечем.
    last_patch = os.path.join(paint_dir, 'tmp', 'patch_last.png')
    check('последний патч сохранён для разбора', os.path.isfile(last_patch), last_patch)
    check('units кропа выставлены в пиксели',
          base.op('crop').par.cropleftunit.eval() == 'pixels')
    check('кроп вырезал ровно запрошенную область (иначе патч пустой)',
          str(rt.diag.get('crop_ok') or '').startswith('pixels'),
          rt.diag.get('crop_ok'))
    check('разовая проверка кропа при старте прошла',
          str(rt.diag.get('crop_test') or '').startswith('ок'),
          rt.diag.get('crop_test'))

    # ------------------------------------------- ориентация кропа (зеркало)
    # Размер области может совпасть, а содержимое прийти зеркальным — тогда патч
    # «вставляется не туда», хотя по логу всё правильно. Проверяем фактом:
    # эталонная картинка из четырёх квадрантов и попытка вылечить зеркало.
    print('\n[кроп: не зеркалит ли он область]')
    check('кроп не зеркалит', rt.diag.get('crop_orient') == 'ок',
          rt.diag.get('crop_orient'))
    check('эталонные квадранты пришли по местам',
          rt.diag.get('crop_probe') == 'red,green,blue,white',
          rt.diag.get('crop_probe'))

    # Поправки ориентации должны реально менять параметры кропа: иначе «лечение»
    # зеркала было бы декоративным.
    crop = base.op('crop')
    rt.crop_flip_v = rt.crop_flip_h = False
    rt._set_crop(crop, (100, 50, 300, 150))
    plain = (crop.par.cropleft.eval(), crop.par.cropright.eval(),
             crop.par.croptop.eval(), crop.par.cropbottom.eval())
    rt.crop_flip_v = True
    rt._set_crop(crop, (100, 50, 300, 150))
    flipped_v = (crop.par.cropleft.eval(), crop.par.cropright.eval(),
                 crop.par.croptop.eval(), crop.par.cropbottom.eval())
    rt.crop_flip_v = False
    rt.crop_flip_h = True
    rt._set_crop(crop, (100, 50, 300, 150))
    flipped_h = (crop.par.cropleft.eval(), crop.par.cropright.eval(),
                 crop.par.croptop.eval(), crop.par.cropbottom.eval())
    rt.crop_flip_v = rt.crop_flip_h = False
    check('поправка «вертикаль наоборот» зеркалит прямоугольник по вертикали',
          flipped_v[0] == plain[0] and flipped_v[1] == plain[1]
          and flipped_v[2] == 1080.0 - plain[3] and flipped_v[3] == 1080.0 - plain[2],
          (plain, flipped_v))
    check('поправка «горизонталь наоборот» зеркалит прямоугольник по горизонтали',
          flipped_h[2] == plain[2] and flipped_h[3] == plain[3]
          and flipped_h[0] == 1920.0 - plain[1] and flipped_h[1] == 1920.0 - plain[0],
          (plain, flipped_h))
    rt._set_crop(crop, (100, 50, 300, 150))

    # ------------------------------------------- режим «отправлять целый кадр»
    # Альтернатива патчам: клиент просто заменяет слой целиком. Так не нужно
    # попадать прямоугольником — но трафик больше, поэтому частота ограничена.
    print('\n[режим отправки: патчи или целый кадр]')
    base.par.Fullframe.val = 1
    rt.read_pars()
    check('режим переключился на целый кадр',
          rt.tun.get('patchmode') == 'fullframe', rt.tun.get('patchmode'))
    sent[:] = []
    rt.paint_dirty = True
    rt.send_rect['paint'] = (0, 0, 1920, 1080)
    rt.last_patch_t = 0.0
    frames(1, 200)
    heads = [s[0] for s in sent if isinstance(s, tuple) and isinstance(s[0], dict)]
    fulls = [h for h in heads if h.get('t') == 'sync' and h.get('full')]
    check('в этом режиме уходит sync с полным кадром', bool(fulls), heads[:2])
    check('полный кадр размечен как 1920x1080',
          fulls and fulls[0].get('w') == 1920 and fulls[0].get('h') == 1080,
          fulls[:1])
    check('счётчик целых кадров растёт', rt.diag.get('fullframes', 0) > 0,
          rt.diag.get('fullframes'))
    check('патчи в этом режиме не отправляются',
          not [h for h in heads if h.get('t') == 'patch'], heads[:2])
    base.par.Fullframe.val = 0
    rt.read_pars()

    # ------------------------------------------- сломанный файл не должен уехать
    # Автопересборка запускается сама: если файл сохранён «на середине правки»,
    # неработающий код уедет в компонент и убьёт рантайм вместе с веб-сервером.
    # Проверяем, что сборка это замечает ДО записи в ноды.
    print('\n[сломанный исходник: сборка не должна применяться]')
    rt_before = base.op('server/runtime').text
    broken = os.path.join(paint_dir, 'td', 'runtime', 'paint_runtime.py')
    with open(broken, 'r', encoding='utf-8') as f:
        good_src = f.read()
    bad_src = good_src.replace('def sources_compile(paint_dir):',
                               'def sources_compile(paint_dir):\n    ', 1)
    bad_src = bad_src.replace('class Runtime(object):', 'class Runtime(object)\n', 1)
    with open(broken, 'w', encoding='utf-8') as f:
        f.write(bad_src)
    try:
        ns_bad = run_builder(paint_dir, os.path.dirname(paint_dir))
        fails = ns_bad.get('FAIL', []) or []
        check('сборка заметила синтаксическую ошибку',
              any('не компилируется' in str(x) for x in fails), fails[:2])
        check('текст рантайма в нодах не тронут',
              base.op('server/runtime').text == rt_before, 'текст изменился')
        check('сборка не отчиталась «чисто»',
              ns_bad.get('COMPLETED') is not True or bool(fails),
              ns_bad.get('COMPLETED'))
    finally:
        with open(broken, 'w', encoding='utf-8') as f:
            f.write(good_src)
    ns_ok = run_builder(paint_dir, os.path.dirname(paint_dir))
    check('после починки файла сборка снова чистая', not ns_ok.get('FAIL', []),
          (ns_ok.get('FAIL') or [])[:2])
    # Путь подачи штампов: Python пишет PNG, moviefileinTOP его подхватывает.
    # Смотрим накопительный счётчик: в конце прогона кадр уже пустой.
    check('текстура штампов подана в moviefileinTOP',
          rt.diag.get('dab_pushes', 0) > 0
          and os.path.basename(str(base.op('dabpng').par.file.eval())
                               ).startswith('dabs_'),
          (rt.diag.get('dab_pushes'), base.op('dabpng').par.file.eval()))
    check('reloadpulse у текстуры штампов пульнул',
          base.op('dabpng').par.reloadpulse.pulses > 0,
          base.op('dabpng').par.reloadpulse.pulses)

    # ------------------------------------------- самовосстановление связей
    # Ровно та поломка, которую увидел пользователь: у feedbackTOP пропал вход
    # (связи через коннекторы в живом TD молча не вставали), слой оставался
    # пустым. Рантайм обязан это заметить и починить сам, а не ждать пересборки.
    print('\n[рантайм сам восстанавливает петлю обратной связи]')
    fb = base.op('fb')
    fb.inputs[0] = None
    fb.par.top.val = ''
    rt.diag.pop('wire_input', None)
    rt.diag.pop('wire_top', None)
    frames(1, 50)
    check('вход feedback восстановлен рантаймом',
          fb.inputs.get(0) is base.op('buf'), rt.diag.get('wire_input'))
    check('параметр Target TOP восстановлен рантаймом',
          str(fb.par.top.eval()) == base.op('buf').path, rt.diag.get('wire_top'))
    check('об этом сказано в журнале',
          any('восстановил' in l for l in rt.log_lines),
          [l for l in rt.log_lines if 'восстановил' in l][-2:])

    # ---------------------------------------------------------- отчёт на диск
    print('\n[отчёт на диск: машиночитаемая диагностика]')
    import json as _json
    rp = rt.write_report('test')
    check('отчёт записан', bool(rp) and os.path.isfile(rp), rp)
    with open(rp, 'r', encoding='utf-8') as f:
        rep = _json.load(f)
    check('в отчёте есть версия TD', 'td' in rep)
    check('в отчёте есть полотно', rep.get('canvas', {}).get('w') == 1920, rep.get('canvas'))
    check('в отчёте есть токены меню и форматы',
          rep['ops']['brush']['pars'].get('format') not in (None, ''),
          rep['ops']['brush']['pars'])
    check('в отчёте есть единицы кропа',
          rep['ops']['crop']['pars'].get('cropleftunit') == 'pixels',
          rep['ops']['crop']['pars'])
    check('в отчёте есть счётчик ошибок', 'errors_total' in rep)
    check('в отчёте перечислены слои (источник, цвет, краска, две маски)',
          [l['kind'] for l in rep.get('layers', [])] == ['source', 'color', 'paint',
                                                         'mask', 'mask'],
          rep.get('layers'))
    # HTTP-эндпоинт отдаёт тот же отчёт
    resp = {}
    out = rt.http({'method': 'GET', 'uri': '/api/report', 'pars': {}}, resp)
    check('/api/report отдаёт JSON отчёта',
          out['statusCode'] == 200 and _json.loads(out['data']).get('tag') == 'http',
          out['statusCode'])
    # самая полезная проверка: рантайм за все кадры не записал ни одной ошибки,
    # значит ни один параметр/оператор, к которым он обращался, не «не найден»
    check('рантайм не логировал ошибок на собранном дереве', not rt.errors,
          list(rt.errors.keys())[:3])

    # ---------------------------------------------- сборка и рантайм не разошлись
    # Рантайм обращается к операторам и параметрам по именам. Если имя разошлось
    # со сборкой, это НЕ падает — просто «ничего не работает» (так и было с
    # межкомпонентными связями). Поэтому сверяем списки целиком.
    print('\n[имена в сборке и в рантайме совпадают]')
    miss_ops = [rel for rel, _p in rt.REPORT_OPS if base.op(rel) is None]
    check('все операторы из отчёта рантайма есть в дереве', not miss_ops, miss_ops)
    bad_pars = []
    for rel, pars in rt.REPORT_OPS:
        o = base.op(rel)
        if o is None:
            continue
        for name in pars:
            try:
                o.par[name].eval()
            except Exception:
                bad_pars.append('%s.%s' % (rel, name))
    check('у них есть параметры, которые рантайм читает', not bad_pars, bad_pars)
    # и наоборот: каждый TOP дерева, к которому рантайм лезет по имени, — на месте
    used = ('movie', 'ext', 'src', 'fit', 'proxy', 'dabpng', 'fb', 'brush',
            'restore', 'patchin', 'sw', 'buf', 'snap', 'crop', 'cropundo',
            'cropsnap', 'src_level', 'paint_level', 'maskapply', 'over', 'out1', 'out',
            'fbm', 'brushm', 'restorem', 'patchinm', 'swm', 'bufm', 'snapm',
            'cropm', 'cropsnapm')
    check('все операторы, к которым рантайм обращается, есть в дереве',
          [r for r in used if base.op(r) is None] == [],
          [r for r in used if base.op(r) is None])

    # ------------------------------------------- тракт данных штампов в текстуру
    # Данные штампов (координаты, радиус, цвет) упакованы в байты PNG, а читает их
    # moviefileinTOP — оператор для КАРТИНОК. Он вправе перевести файл в рабочее
    # цветовое пространство (гамма!) и премультиплицировать RGB на альфу: для
    # картинки это правильно, для наших данных — мусор. Снаружи это выглядит как
    # «на планшете линия, в TD точки не там» и «патч в браузер пустой».
    print('\n[тракт данных штампов проверяется фактом]')
    ok, msg = rt.check_dab_pipeline(force=True)
    check('данные штампов доезжают до текстуры без искажений', ok, msg)
    check('результат проверки попал в диагностику',
          rt.diag.get('dab_check_ok') is True, rt.diag.get('dab_check'))

    real_sample = fake_td.FakeOp.sample

    def gamma_sample(self, x=0, y=0):
        # как если бы TD применил гамма-кривую к файлу
        return tuple((v ** 2.2) if v > 0 else 0.0
                     for v in real_sample(self, x, y))
    fake_td.FakeOp.sample = gamma_sample
    try:
        ok2, msg2 = rt.check_dab_pipeline(force=True)
        check('искажение данных штампов ловится', not ok2 and 'разошлись' in msg2, msg2)
    finally:
        fake_td.FakeOp.sample = real_sample
    ok3, msg3 = rt.check_dab_pipeline(force=True)
    check('после снятия искажения проверка снова зелёная', ok3, msg3)

    # Отчёт должен обновляться и ПОСЛЕ старта: иначе на диске остаётся снимок
    # момента запуска, и по нему не видно, что было при рисовании.
    rt._rep_t = time.time() - 10.0
    rt.report_done = True
    frames(1, 60)
    with open(rp, 'r', encoding='utf-8') as f:
        rep2 = _json.load(f)
    check('отчёт обновляется в живом режиме', rep2.get('tag') == 'live',
          rep2.get('tag'))
    check('в живом отчёте видно счётчики работы',
          rep2.get('runtime', {}).get('dab_pushes', 0) > 0,
          {k: v for k, v in rep2.get('runtime', {}).items()
           if k.startswith('dab')})

    # ---------------------------------------------------------- связка шейдеров
    # Имена uniform-ов в шейдере обязаны совпадать с тем, что рантайм реально
    # выставил: опечатка тут не падает, а просто даёт нули в кадре.
    import re
    for op_rel, sh_rel in (('brush', 'shaders/paint'),
                           ('restore', 'shaders/restore')):
        opo = base.op(op_rel)
        glsl = base.op(sh_rel).text
        declared = set(re.findall(r'uniform\s+vec4\s+(\w+)', glsl))
        assigned = set()
        for i in range(4):
            nm = opo.par['vec%dname' % i].eval()
            if nm:
                assigned.add(str(nm))
        missing = declared - assigned
        extra = assigned - declared
        check('%s: все uniform-ы шейдера выставлены рантаймом' % op_rel,
              not missing, sorted(missing))
        check('%s: нет лишних uniform-ов' % op_rel, not extra, sorted(extra))
        idx = sorted(set(int(m) for m in re.findall(r'sTD2DInputs\[(\d+)\]', glsl)))
        unconnected = [i for i in idx if opo.inputs.get(i) is None]
        check('%s: входы sTD2DInputs подключены (%s)' % (op_rel, idx or 'нет'),
              not unconnected, unconnected)


if __name__ == '__main__':
    sys.exit(main())
