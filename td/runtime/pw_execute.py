"""PaintWeb — колбэки executeDAT: по два вызова на кадр.

onFrameStart — разобрать входящее, посчитать штампы, выставить uniform-ы.
onFrameEnd   — закодировать и разослать патчи, прокси-кадры, снимки undo.
"""


def onStart():
    """Запуск проекта: здесь и происходит «запуск без команд».

    Проверяем, совпадает ли собранный компонент с файлами на диске. Если нет —
    сборка запускается сама (см. Runtime.autostart), сервер поднимается, а адрес
    для планшета кладётся в DAT server/address. Textport не нужен.
    """
    try:
        _pw(me).log('PaintWeb: запуск, полотно %sx%s' % (me.parent().par.Canvasw.eval(),
                                                         me.parent().par.Canvash.eval()))
    except Exception:
        _plog('onStart')
    try:
        _pw(me).autostart(reason='открытие проекта')
    except Exception:
        _plog('onStart:autostart')


def onCreate():
    return


def onExit():
    try:
        _pw(me).log('PaintWeb: остановка')
    except Exception:
        pass


def onFrameStart(frame):
    try:
        _pw(me).on_frame_start(frame)
    except Exception:
        _plog('onFrameStart')


def onFrameEnd(frame):
    try:
        _pw(me).on_frame_end(frame)
    except Exception:
        _plog('onFrameEnd')


def onPlayStateChange(state):
    """Подстраховка: если проект открылся на паузе, onStart мог не сработать.

    Как только кадры пошли — проверяем отпечаток и, если нужно, пересобираемся.
    """
    try:
        _pw(me).autostart(reason='запуск воспроизведения')
    except Exception:
        _plog('onPlayStateChange')


def onProjectPreSave():
    return


def onProjectPostSave():
    return
