"""PaintWeb — колбэки webserverDAT.

Задача этого DAT-а — быть максимально тонким: принять запрос/сообщение и
положить его в очередь рантайма. Вся работа идёт в onFrameStart/onFrameEnd
на главном потоке (см. paint_runtime.py).
"""


def onHTTPRequest(dat, request, response):
    try:
        return _pw(dat).http(request, response)
    except Exception:
        _plog('onHTTPRequest')
        response['statusCode'] = 500
        response['statusReason'] = 'Internal Error'
        response['content-type'] = 'text/plain; charset=utf-8'
        response['data'] = traceback.format_exc()
        return response


def onWebSocketOpen(dat, client, uri=None):
    try:
        _pw(dat).ws_open(client, uri)
    except Exception:
        _plog('onWebSocketOpen')


def onWebSocketClose(dat, client):
    try:
        _pw(dat).ws_close(client)
    except Exception:
        _plog('onWebSocketClose')


def onWebSocketReceiveText(dat, client, data):
    try:
        _pw(dat).ws_text(client, data)
    except Exception:
        _plog('onWebSocketReceiveText')


def onWebSocketReceiveBinary(dat, client, data):
    try:
        _pw(dat).ws_binary(client, data)
    except Exception:
        _plog('onWebSocketReceiveBinary')


def onWebSocketReceivePing(dat, client, data):
    try:
        dat.webSocketSendPong(client, data=data)
    except Exception:
        _plog('onWebSocketReceivePing')


def onWebSocketReceivePong(dat, client, data):
    return


def onServerStart(dat):
    try:
        _pw(dat).log('HTTP/WS сервер запущен, порт %s' % dat.par.port.eval())
    except Exception:
        _plog('onServerStart')


def onServerStop(dat):
    try:
        _pw(dat).log('сервер остановлен')
    except Exception:
        pass
