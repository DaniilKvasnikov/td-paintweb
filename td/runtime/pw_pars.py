"""PaintWeb — колбэки parameterExecuteDAT: кнопки на самом компоненте.

Кнопка «Открыть интерфейс» — Pulse-параметр `Openpage` на компоненте paint_web.
Она открывает страницу в браузере на этом ПК: то же самое, что кнопка «Открыть на
ПК» на странице, но прямо из TouchDesigner, без Textport и без ручного набора
адреса. Адрес для планшета при этом лежит рядом, в параметре `Page`, и в DAT
`server/address`.

Запускается это колбэком parameterExecuteDAT (нода `onpulse` внутри компонента),
а на случай если в сборке TD колбэк не придёт — рантайм каждый кадр сверяет счётчик
нажатий параметра (см. Runtime.open_button_check).
"""


def onPulse(par):
    try:
        _pw(me).open_page(reason='кнопка на компоненте')
    except Exception:
        _plog('onPulse')


def onValueChange(par, prev):
    return


def onValuesChange(pars, prev):
    return
