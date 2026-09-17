'use strict';
/* =============================================================================
   PixelFlow PaintWeb — браузерный клиент рисования для TouchDesigner.
   Контракт: paint/PAINT_PROTOCOL.md, версия 1 (файл менять нельзя).

   Vanilla JS. Без фреймворков, сборки, CDN, внешних шрифтов и картинок.
   Разделы:
     1  константы
     2  состояние
     3  DOM
     4  утилиты
     5  лог и тосты
     6  оффскрин-канвасы (полотно)
     7  композит
     8  вид (pan/zoom)
     9  штампы и оптимистичный мазок
     10 бинарные пакеты точек (§4 протокола)
     11 WebSocket
     12 приём сообщений
     13 инструмент, цвет, слайдеры
     14 панель слоёв
     15 источники (список, загрузка)
     16 ввод: Pointer Events
     17 клавиатура
     18 запуск
   ============================================================================= */

/* -----------------------------------------------------------------------------
   1. КОНСТАНТЫ
   -------------------------------------------------------------------------- */

const DEFAULT_CANVAS = { w: 1920, h: 1080 };   // совпадает с дефолтом сервера (§1)

// Запасной список режимов вставки источника (сервер присылает свой вместе со
// слоем). Порядок совпадает с FIT_MODES в paint_runtime.py.
const DEFAULT_FIT_MODES = [
  { key: 'fill', label: 'Заполнить' },
  { key: 'horizontal', label: 'По ширине' },
  { key: 'vertical', label: 'По высоте' },
  { key: 'best', label: 'Вписать' },
  { key: 'outside', label: 'Заполнить с обрезкой' },
  { key: 'nativeres', label: 'Как есть (native)' },
];
const MIN_SCALE = 0.05;
const MAX_SCALE = 16;
const PING_MS = 2000;                          // период ping
const UI_SEND_MS = 80;                         // склейка исходящих tool/layer (≤12.5 сообщ/с)
const MAX_BATCH_POINTS = 127;                  // count в §4 — u8-подобное поле 1..127
const FLUSH_MIN_MS = 16;                       // не чаще 60 пачек в секунду (§4)
const BUFFER_HIGH_WATER = 262144;              // порог ws.bufferedAmount для троттлинга
const MIN_POINT_DIST = 0.6;                    // px полотна: антиспам для медленного пальца
const LOG_VIEW_LINES = 50;                     // сколько строк видно в панели «Лог»
const LOG_KEEP_LINES = 300;                    // сколько храним (копируется целиком)
const TOAST_MS = 4200;
const STAMP_CACHE_MAX = 24;
const STAMP_STOPS = 12;                        // сэмплов smoothstep в градиенте штампа
const TAU = Math.PI * 2;
const HEX_RE = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i;
const SWATCH_MAX = 8;
const LS_SWATCHES = 'pf_swatches_v1';

const EYE_ON_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z"/>' +
  '<circle cx="12" cy="12" r="2.6"/></svg>';
const EYE_OFF_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
  'stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-6.5 10-6.5c2 0 3.7.6 5.1 1.5"/>' +
  '<path d="M22 12s-3.6 6.5-10 6.5c-2 0-3.7-.6-5.1-1.5"/><path d="M4 4l16 16"/></svg>';

/* -----------------------------------------------------------------------------
   2. СОСТОЯНИЕ
   -------------------------------------------------------------------------- */

const state = {
  canvas: { w: DEFAULT_CANVAS.w, h: DEFAULT_CANVAS.h },
  layers: [],
  tool: {
    tool: 'brush',        // brush | eraser | pan
    color: '#ff3366',
    size: 40,             // px полотна
    hardness: 0.7,
    flow: 0.9,
    spacing: 0.15,
    target: null          // id слоя-цели (tool.target)
  },
  patchHz: 30,
  proxyFps: 2,
  history: { undo: 0, redo: 0 },

  // связь
  wsState: 'off',         // off | connecting | open
  welcomed: false,
  reconnecting: false,
  retry: 0,
  rtt: null,              // мс
  patchRate: 0,           // измеренная частота патчей, шт/с

  // прокси источника
  proxyKind: null,        // null | poster | live
  proxySeq: 0,
  proxyRate: 0,

  // список источников с /api/sources
  sources: null,
  sourcesAt: 0,

  swatches: []
};

// Оффскрин-канвасы полного размера полотна.
const surf = {
  source: null,           // прокси источника, растянутый на всё полотно
  overlay: null,          // оптимистичное превью текущего мазка
  temp: null,             // временный буфер композита (маска)
  mask: null,             // объединённая маска (paint ∪ overlay)
  paint: new Map(),       // layerId → { c, x }  (RGBA-слои от сервера)
  overlayLayerId: null,   // на каком слое нарисовано текущее превью
  overlayHasContent: false,
  maskReady: false        // пиксели маски уже пришли от TD
};

// Вид: экранные координаты в CSS-пикселях. screen = canvas * scale + t
const view = { tx: 0, ty: 0, scale: 1, inited: false };
let viewDpr = 1;

const cursor = { inside: false, x: 0, y: 0, visible: false };
const pointers = new Map();      // pointerId → { type, screen, canvas, pressure }
let panState = null;             // активный пан
let gesture = null;              // активный пинч двумя пальцами
let spaceDown = false;

// Мазок.
const stroke = {
  active: false,
  id: 0,
  nextId: 1,
  pointerId: null,
  pointerType: null,
  layerId: 0,
  lastPt: null,        // { x, y, p } в px полотна
  acc: 0,              // пройденное расстояние с последнего штампа
  pending: [],         // точки к отправке (уже в wire-формате)
  firstSent: false,
  lastFlush: 0,
  lastWire: null
};

// WebSocket.
let ws = null;
let reconnectTimer = null;
let pendingHeader = null;        // заголовок patch/sync/proxy, ждущий бинарный кадр
let lastSourceFrame = null;      // последний кадр источника (его держим: welcome
                                 // пересоздаёт полотно и стирает картинку)
let sourceWaitTimer = null;      // ждём картинку источника и просим заново
let decodeChain = Promise.resolve();  // строгий порядок применения кадров
let rafId = 0;

// Служебные таймеры/админ-состояние UI.
// ВАЖНО: отметки «последней локальной правки» стартуют из −∞, иначе сразу после
// загрузки страницы (performance.now() ещё мал) синхронизация UI от сервера
// ошибочно считалась бы «пользователь только что тянул» и игнорировалась.
const NEVER = -1e9;
const localEditAt = { size: NEVER, hardness: NEVER, flow: NEVER, spacing: NEVER, color: NEVER };
const layerSendLast = new Map();
const layerSendTimers = new Map();
let lastSliderDragAt = NEVER;
let toolPending = null;
let lastToolSent = 0;
let toolTimer = null;
let logDirty = false;
let logRenderTimer = null;
let pickerLayerId = null;
let pickerAnchor = null;
let sourceBadgeEls = [];
let stampCache = new Map();
let checkerPattern = null;
let checkerDpr = 0;
let pingSentAt = 0;
let patchWindow = 0;
let proxyWindow = 0;

/* -----------------------------------------------------------------------------
   3. DOM
   -------------------------------------------------------------------------- */

const el = {};

function cacheDom() {
  el.app = document.getElementById('app');
  el.view = document.getElementById('view');
  el.canvasWrap = document.getElementById('canvasWrap');
  el.toolbar = document.getElementById('toolbar');

  el.toolButtons = Array.from(document.querySelectorAll('#toolButtons .seg-btn'));

  el.colorPicker = document.getElementById('colorPicker');
  el.colorHex = document.getElementById('colorHex');
  el.swatches = document.getElementById('swatches');

  el.size = document.getElementById('size');
  el.hardness = document.getElementById('hardness');
  el.flow = document.getElementById('flow');
  el.spacing = document.getElementById('spacing');
  el.sizeOut = document.getElementById('sizeOut');
  el.hardnessOut = document.getElementById('hardnessOut');
  el.flowOut = document.getElementById('flowOut');
  el.spacingOut = document.getElementById('spacingOut');

  el.undoBtn = document.getElementById('undoBtn');
  el.redoBtn = document.getElementById('redoBtn');
  el.clearBtn = document.getElementById('clearBtn');
  el.fitBtn = document.getElementById('fitBtn');
  el.layersBtn = document.getElementById('layersBtn');
  el.logBtn = document.getElementById('logBtn');
  el.rebuildBtn = document.getElementById('rebuildBtn');
  el.selftestBtn = document.getElementById('selftestBtn');
  el.openBtn = document.getElementById('openBtn');
  el.gitBtn = document.getElementById('gitBtn');

  el.statusPill = document.getElementById('statusPill');
  el.proxyBadge = document.getElementById('proxyBadge');
  el.toasts = document.getElementById('toasts');
  el.drawerTab = document.getElementById('drawerTab');
  el.targetHint = document.getElementById('targetHint');
  el.drawerBackdrop = document.getElementById('drawerBackdrop');

  el.layersPanel = document.getElementById('layersPanel');
  el.layersClose = document.getElementById('layersClose');
  el.layersList = document.getElementById('layersList');

  el.logPanel = document.getElementById('logPanel');
  el.logToggle = document.getElementById('logToggle');
  el.logCopy = document.getElementById('logCopy');
  el.logClear = document.getElementById('logClear');
  el.logView = document.getElementById('logView');
  el.logCounter = document.getElementById('logCounter');

  el.picker = document.getElementById('sourcePicker');

  el.viewCtx = el.view.getContext('2d');
}

/* -----------------------------------------------------------------------------
   4. УТИЛИТЫ
   -------------------------------------------------------------------------- */

function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

function sameId(a, b) { return String(a) === String(b); }

function lerp(a, b, t) { return a + (b - a) * t; }

function smoothstep(e0, e1, x) {
  if (e1 <= e0) return x < e0 ? 0 : 1;
  const t = clamp((x - e0) / (e1 - e0), 0, 1);
  return t * t * (3 - 2 * t);
}

/* «#abc» / «abcdef» → «#aabbcc»; null если не HEX. */
function normalizeHex(v) {
  if (typeof v !== 'string') return null;
  const m = HEX_RE.exec(v.trim());
  if (!m) return null;
  let h = m[1].toLowerCase();
  if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  return '#' + h;
}

function hexToRgb(hex) {
  const h = (normalizeHex(hex) || '#ffffff').slice(1);
  return {
    r: parseInt(h.slice(0, 2), 16),
    g: parseInt(h.slice(2, 4), 16),
    b: parseInt(h.slice(4, 6), 16)
  };
}

function cssRgba(hex, a) {
  const c = hexToRgb(hex);
  return 'rgba(' + c.r + ',' + c.g + ',' + c.b + ',' + a + ')';
}

function two(n) { return (n < 10 ? '0' : '') + n; }

function nowStamp() {
  const d = new Date();
  return two(d.getHours()) + ':' + two(d.getMinutes()) + ':' + two(d.getSeconds()) +
         '.' + String(d.getMilliseconds()).padStart(3, '0');
}

function makeSurface(w, h) {
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(w));
  c.height = Math.max(1, Math.round(h));
  const x = c.getContext('2d');
  return { c: c, x: x };
}

function paintSurface(id, create) {
  let s = surf.paint.get(String(id));
  if (!s && create) {
    s = makeSurface(state.canvas.w, state.canvas.h);
    surf.paint.set(String(id), s);
  }
  return s || null;
}

/* -----------------------------------------------------------------------------
   5. ЛОГ И ТОСТЫ
   -------------------------------------------------------------------------- */

const logBuf = [];   // { ts: метка времени, msg, kind }

function log(msg, kind) {
  logBuf.push({ ts: nowStamp(), msg: String(msg), kind: kind || '' });
  if (logBuf.length > LOG_KEEP_LINES) logBuf.splice(0, logBuf.length - LOG_KEEP_LINES);
  logDirty = true;
  if (!logRenderTimer) logRenderTimer = setTimeout(flushLogRender, 200);
}

function arrowFor(kind) {
  if (kind === 'tx') return '→';
  if (kind === 'rx') return '←';
  if (kind === 'ok') return '✔';
  if (kind === 'warn') return '▲';
  if (kind === 'err') return '✖';
  return '·';
}

/* Перерисовка панели лога — не чаще одного раза в 200 мс. */
function flushLogRender() {
  logRenderTimer = null;
  if (!logDirty) return;
  logDirty = false;
  if (!el.logView) return;

  const nearBottom = el.logView.scrollTop + el.logView.clientHeight >= el.logView.scrollHeight - 24;
  const from = Math.max(0, logBuf.length - LOG_VIEW_LINES);
  const frag = document.createDocumentFragment();

  for (let i = from; i < logBuf.length; i++) {
    const rec = logBuf[i];
    const line = document.createElement('div');
    line.className = 'log-line' + (rec.kind ? ' k-' + rec.kind : '');

    const ts = document.createElement('span');
    ts.className = 'ts';
    ts.textContent = rec.ts;

    const ar = document.createElement('span');
    ar.className = 'ar';
    ar.textContent = arrowFor(rec.kind);

    const tx = document.createElement('span');
    tx.className = 'tx';
    tx.textContent = rec.msg;

    line.append(ts, ar, tx);
    frag.appendChild(line);
  }

  el.logView.textContent = '';
  el.logView.appendChild(frag);
  if (nearBottom) el.logView.scrollTop = el.logView.scrollHeight;
  if (el.logCounter) el.logCounter.textContent = 'записей: ' + logBuf.length;
}

function buildLogText() {
  const head = [
    '=== PixelFlow PaintWeb — лог протокола ===',
    'время: ' + new Date().toISOString(),
    'url: ' + location.href,
    'ua: ' + navigator.userAgent,
    'полотно: ' + state.canvas.w + 'x' + state.canvas.h +
      ', dpr=' + (window.devicePixelRatio || 1) +
      ', экран ' + window.innerWidth + 'x' + window.innerHeight,
    'связь: ' + state.wsState + (state.welcomed ? ' (welcome получен)' : '') +
      ', RTT=' + (state.rtt === null ? '—' : state.rtt + 'мс') +
      ', patchHz=' + state.patchHz + ', proxyFps=' + state.proxyFps,
    'слои: ' + JSON.stringify(state.layers),
    '=== события ==='
  ];
  const body = logBuf.map(function (r) { return r.ts + ' ' + arrowFor(r.kind) + ' ' + r.msg; });
  return head.concat(body).join('\n');
}

function copyLog() {
  const text = buildLogText();
  const done = function () { toast('Лог скопирован в буфер обмена', 'ok'); };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(function (err) {
      if (fallbackCopy(text)) done();
      else log('Не удалось скопировать лог: ' + err.message, 'err');
    });
  } else if (fallbackCopy(text)) {
    done();
  } else {
    log('Копирование недоступно в этом браузере', 'err');
  }
}

/* Резервный путь (http без secure-context): скрытая textarea + execCommand. */
function fallbackCopy(text) {
  try {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', 'readonly');
    ta.style.position = 'fixed';
    ta.style.top = '-2000px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return ok;
  } catch (e) {
    return false;
  }
}

/* Всплывающее сообщение (без alert — по требованию ТЗ). */
function toast(msg, kind) {
  if (!el.toasts) return;
  const div = document.createElement('div');
  div.className = 'toast' + (kind ? ' toast--' + kind : '');
  div.textContent = msg;
  el.toasts.appendChild(div);
  // Держим не больше четырёх подсказок. Цикл ограничен по числу шагов: если
  // удаление почему-то не сработает, страница не должна зависнуть навсегда
  // (на этом уже один раз повесился прогон клиента в Node).
  let guard = 0;
  while (el.toasts.children.length > 4 && guard++ < 32) {
    el.toasts.removeChild(el.toasts.children[0]);
  }
  setTimeout(function () {
    if (div.parentNode) div.parentNode.removeChild(div);
  }, TOAST_MS);
}

/* -----------------------------------------------------------------------------
   6. ОФФСКРИН-КАНВАСЫ (ПОЛОТНО)
   -------------------------------------------------------------------------- */

function setCanvasSize(w, h) {
  const nw = Math.max(1, Math.round(w || DEFAULT_CANVAS.w));
  const nh = Math.max(1, Math.round(h || DEFAULT_CANVAS.h));
  if (state.canvas.w === nw && state.canvas.h === nh && surf.source) return;

  state.canvas.w = nw;
  state.canvas.h = nh;

  surf.source = makeSurface(nw, nh);
  surf.overlay = makeSurface(nw, nh);
  surf.temp = makeSurface(nw, nh);
  surf.mask = makeSurface(nw, nh);
  surf.paint.clear();           // содержимое придёт заново через sync
  surf.overlayHasContent = false;
  surf.overlayLayerId = null;
  stampCache.clear();

  log('Полотно: ' + nw + '×' + nh + ' px (по welcome)', 'ok');

  // Welcome пересоздаёт полотно — то есть стирает уже нарисованную картинку
  // источника. Держим последний кадр и возвращаем его на место: иначе источник
  // пропадал до перезагрузки страницы (сервер повторно его не присылает).
  if (lastSourceFrame) {
    log('Полотно пересоздано — возвращаю картинку источника', 'warn');
    drawSourceFrame(lastSourceFrame.h, lastSourceFrame.bmp, true);
  }
  requestRender();
}

function clearOverlay() {
  if (!surf.overlay) return;
  surf.overlay.x.setTransform(1, 0, 0, 1, 0, 0);
  surf.overlay.x.globalAlpha = 1;
  surf.overlay.x.globalCompositeOperation = 'source-over';
  surf.overlay.x.clearRect(0, 0, state.canvas.w, state.canvas.h);
  surf.overlayHasContent = false;
  surf.overlayLayerId = null;
  requestRender();
}

function clearOverlayRect(x, y, w, h) {
  if (!surf.overlay || !surf.overlayHasContent) return;
  surf.overlay.x.clearRect(x, y, w, h);
}

/* -----------------------------------------------------------------------------
   7. КОМПОЗИТ
   Порядок строго по §3.4 протокола. Вызывается не чаще одного раза за кадр
   и только когда что-то изменилось (патч, прокси, мазок, панорама).
   -------------------------------------------------------------------------- */

function requestRender() {
  if (!rafId) rafId = requestAnimationFrame(frame);
}

function frame() {
  rafId = 0;

  // Отправка накопленных точек мазка — пачками, не чаще 60 раз в секунду.
  if (stroke.active && stroke.pending.length &&
      performance.now() - stroke.lastFlush >= FLUSH_MIN_MS) {
    flushStroke(false);
  }

  composite();
  flushLogRender();

  // Остались неотправленные точки (сработал ограничитель частоты) — ещё кадр.
  if (stroke.active && stroke.pending.length) requestRender();
}

/* Шахматка прозрачности — в экранных пикселях (не масштабируется зумом). */
function getCheckerPattern(ctx) {
  const dpr = viewDpr || 1;
  if (checkerPattern && checkerDpr === dpr) return checkerPattern;
  const step = Math.max(4, Math.round(8 * dpr));
  const c = document.createElement('canvas');
  c.width = step * 2;
  c.height = step * 2;
  const x = c.getContext('2d');
  x.fillStyle = '#2a2e35';
  x.fillRect(0, 0, step * 2, step * 2);
  x.fillStyle = '#23272d';
  x.fillRect(0, 0, step, step);
  x.fillRect(step, step, step, step);
  checkerPattern = ctx.createPattern(c, 'repeat');
  checkerDpr = dpr;
  return checkerPattern;
}

function composite() {
  const vx = el.viewCtx;
  if (!vx) return;
  const vw = el.view.width;
  const vh = el.view.height;
  const dpr = viewDpr || 1;

  vx.setTransform(1, 0, 0, 1, 0, 0);
  vx.globalAlpha = 1;
  vx.globalCompositeOperation = 'source-over';

  // 1) фон и шахматка внутри прямоугольника полотна
  vx.fillStyle = '#15171b';
  vx.fillRect(0, 0, vw, vh);

  const sx = view.tx * dpr;
  const sy = view.ty * dpr;
  const sw = state.canvas.w * view.scale * dpr;
  const sh = state.canvas.h * view.scale * dpr;

  vx.save();
  vx.fillStyle = getCheckerPattern(vx);
  vx.fillRect(sx, sy, sw, sh);
  vx.restore();

  // 2..5) слои снизу вверх через видовое преобразование
  if (surf.source) {
    vx.save();
    vx.setTransform(view.scale * dpr, 0, 0, view.scale * dpr, sx, sy);
    drawLayers(vx);
    vx.restore();
  }

  // 6) кольцо курсора — уже в экранных координатах
  drawCursorRing(vx);
  drawHints(vx);
}

function findMaskLayer() {
  for (let i = 0; i < state.layers.length; i++) {
    const L = state.layers[i];
    if (L && L.kind === 'mask') return L;
  }
  return null;
}

function drawLayers(vx) {
  const W = state.canvas.w;
  const H = state.canvas.h;
  const layers = state.layers;
  const mask = findMaskLayer();
  const maskSurf = mask ? paintSurface(mask.id, false) : null;

  for (let i = 0; i < layers.length; i++) {
    const L = layers[i];
    if (!L || L.visible === 0 || L.kind === 'mask') continue;
    const opacity = clamp(typeof L.opacity === 'number' ? L.opacity : 1, 0, 1);
    if (opacity <= 0) continue;

    if (L.kind === 'source') {
      // Источник рисуется один раз — с маской, если маска уже пришла. Пока её
      // нет, показываем источник целиком: в TD маска при старте залита белым
      // (источник виден весь), и пустая маска на странице дала бы чёрный холст.
      vx.globalAlpha = opacity;
      if (maskSurf && surf.maskReady) {
        drawMaskedSource(vx, maskSurf, opacity);
      } else {
        vx.drawImage(surf.source.c, 0, 0, W, H);
      }
      vx.globalAlpha = 1;
      continue;
    }

    if (L.kind !== 'paint') continue;

    const ps = paintSurface(L.id, false);
    const withOverlay = surf.overlayHasContent && surf.overlayLayerId !== null &&
                        sameId(surf.overlayLayerId, L.id);

    vx.globalAlpha = opacity;
    if (ps) vx.drawImage(ps.c, 0, 0, W, H);
    if (withOverlay) vx.drawImage(surf.overlay.c, 0, 0, W, H);
    vx.globalAlpha = 1;
  }

  vx.globalAlpha = 1;
}

/* Источник ∩ маска. Маска — белая с альфой, поэтому применяем её альфу через
   destination-in на копии источника. */
function drawMaskedSource(vx, maskSurf, opacity) {
  const W = state.canvas.w;
  const H = state.canvas.h;
  const tmp = surf.temp.x;

  tmp.setTransform(1, 0, 0, 1, 0, 0);
  tmp.globalAlpha = 1;
  tmp.globalCompositeOperation = 'source-over';
  tmp.clearRect(0, 0, W, H);
  tmp.drawImage(surf.source.c, 0, 0, W, H);
  tmp.globalCompositeOperation = 'destination-in';
  tmp.drawImage(maskSurf.c, 0, 0, W, H);
  tmp.globalCompositeOperation = 'source-over';

  vx.globalAlpha = opacity;
  vx.drawImage(surf.temp.c, 0, 0, W, H);
  vx.globalAlpha = 1;
}

/* Кольцо реального размера кисти. Радиус — size/2 в пикселях полотна,
   переведённый в экранные пиксели. Рисуется после restore() трансформа. */
function drawCursorRing(vx) {
  if (!cursor.visible || !cursor.inside) return;
  if (state.tool.tool === 'pan') return;
  const dpr = viewDpr || 1;
  const r = Math.max(2, (state.tool.size / 2) * view.scale * dpr);
  const cx = cursor.x * dpr;
  const cy = cursor.y * dpr;

  vx.setTransform(1, 0, 0, 1, 0, 0);
  vx.globalAlpha = 1;
  vx.globalCompositeOperation = 'source-over';

  vx.lineWidth = Math.max(1, dpr) * 2;
  vx.strokeStyle = 'rgba(0,0,0,0.65)';
  vx.beginPath();
  vx.arc(cx, cy, r, 0, TAU);
  vx.stroke();

  vx.lineWidth = Math.max(1, dpr);
  vx.strokeStyle = 'rgba(255,255,255,0.92)';
  vx.beginPath();
  vx.arc(cx, cy, r, 0, TAU);
  vx.stroke();

  // точка центра — чтобы видеть положение при большом размере
  vx.fillStyle = 'rgba(255,255,255,0.8)';
  vx.fillRect(cx - dpr, cy - dpr, dpr * 2, dpr * 2);
}

/* Подсказки поверх полотна: «Подключение…», «нет связи». */
function drawHints(vx) {
  if (state.welcomed) return;
  const dpr = viewDpr || 1;
  let main = 'Подключение…';
  let sub = 'Ожидаю сообщение welcome от TouchDesigner.';
  if (state.wsState === 'off') {
    main = 'Нет связи';
    sub = 'Сервер не отвечает. Проверьте, что TouchDesigner запущен и порт 9980 доступен.';
  } else if (state.reconnecting) {
    main = 'Переподключение…';
    sub = 'Соединение потеряно, пробую снова.';
  }

  const vw = el.view.width;
  const vh = el.view.height;
  vx.setTransform(1, 0, 0, 1, 0, 0);
  vx.globalAlpha = 1;

  const fMain = Math.round(20 * dpr);
  const fSub = Math.round(13 * dpr);
  vx.font = '600 ' + fMain + 'px system-ui, -apple-system, Segoe UI, Roboto, sans-serif';
  const wMain = vx.measureText(main).width;
  vx.font = fSub + 'px system-ui, -apple-system, Segoe UI, Roboto, sans-serif';
  const wSub = vx.measureText(sub).width;
  const boxW = Math.max(wMain, wSub) + 40 * dpr;
  const boxH = fMain + fSub + 40 * dpr;

  vx.fillStyle = 'rgba(14,16,19,0.82)';
  vx.strokeStyle = 'rgba(255,255,255,0.12)';
  vx.lineWidth = Math.max(1, dpr);
  roundRect(vx, (vw - boxW) / 2, (vh - boxH) / 2, boxW, boxH, 12 * dpr);
  vx.fill();
  vx.stroke();

  vx.textAlign = 'center';
  vx.textBaseline = 'middle';
  vx.fillStyle = '#e8edf4';
  vx.font = '600 ' + fMain + 'px system-ui, -apple-system, Segoe UI, Roboto, sans-serif';
  vx.fillText(main, vw / 2, vh / 2 - fSub * 0.7);
  vx.fillStyle = '#8b95a5';
  vx.font = fSub + 'px system-ui, -apple-system, Segoe UI, Roboto, sans-serif';
  vx.fillText(sub, vw / 2, vh / 2 + fMain * 0.7);
  vx.textAlign = 'left';
  vx.textBaseline = 'alphabetic';
}

function roundRect(ctx, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

/* -----------------------------------------------------------------------------
   8. ВИД: PAN / ZOOM
   screenX = canvasX * scale + tx   ⇒   canvasX = (screenX - tx) / scale
   -------------------------------------------------------------------------- */

function viewBox() {
  const r = el.view.getBoundingClientRect();
  return { w: r.width, h: r.height };
}

function resizeView() {
  const r = el.view.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(r.width * dpr));
  const h = Math.max(1, Math.round(r.height * dpr));
  if (el.view.width !== w || el.view.height !== h) {
    el.view.width = w;
    el.view.height = h;
  }
  viewDpr = dpr;
  if (!view.inited && r.width > 2 && r.height > 2) {
    view.inited = true;
    fitView();
  }
  requestRender();
}

function fitView() {
  const box = viewBox();
  if (box.w < 2 || box.h < 2) return;
  const s = Math.min(box.w / state.canvas.w, box.h / state.canvas.h) * 0.96;
  view.scale = clamp(s, MIN_SCALE, MAX_SCALE);
  view.tx = (box.w - state.canvas.w * view.scale) / 2;
  view.ty = (box.h - state.canvas.h * view.scale) / 2;
  requestRender();
}

function zoomAt(screenPt, factor) {
  const ns = clamp(view.scale * factor, MIN_SCALE, MAX_SCALE);
  const k = ns / view.scale;
  view.tx = screenPt.x - (screenPt.x - view.tx) * k;
  view.ty = screenPt.y - (screenPt.y - view.ty) * k;
  view.scale = ns;
  requestRender();
}

/* Экранные координаты указателя относительно бокса канваса (CSS-пиксели). */
function screenPointOf(e) {
  const r = el.view.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}

/* §ТЗ: canvasX = (screenX - tx) / scale */
function screenToCanvas(pt) {
  return { x: (pt.x - view.tx) / view.scale, y: (pt.y - view.ty) / view.scale };
}

/* -----------------------------------------------------------------------------
   9. ШТАМПЫ И ОПТИМИСТИЧНЫЙ МАЗОК
   Алгоритм §5: интервал max(1, size*spacing) по ДЛИНЕ пути, линейная
   интерполяция координат и давления, радиальный градиент со smoothstep.
   -------------------------------------------------------------------------- */

/* Спрайт штампа кэшируется по (радиус, жёсткость, цвет/ластик). */
function stampSprite(radius, hardness, color, erase) {
  const key = (Math.round(radius * 100) / 100) + '|' + Math.round(hardness * 1000) + '|' +
              (erase ? 'erase' : color);
  const hit = stampCache.get(key);
  if (hit) return hit;

  const size = Math.max(2, Math.ceil(radius * 2) + 2);
  const surfStamp = makeSurface(size, size);
  const cx = size / 2;
  const inner = radius * (0.98 * clamp(hardness, 0, 1));
  const rgb = erase ? { r: 0, g: 0, b: 0 } : hexToRgb(color);

  const g = surfStamp.x.createRadialGradient(cx, cx, inner, cx, cx, radius);
  for (let i = 0; i <= STAMP_STOPS; i++) {
    const t = i / STAMP_STOPS;
    const dist = inner + (radius - inner) * t;
    const a = 1 - smoothstep(inner, radius, dist);   // §5
    g.addColorStop(clamp(dist / radius, 0, 1),
      'rgba(' + rgb.r + ',' + rgb.g + ',' + rgb.b + ',' + a.toFixed(4) + ')');
  }
  surfStamp.x.fillStyle = g;
  surfStamp.x.fillRect(0, 0, size, size);

  if (stampCache.size > STAMP_CACHE_MAX) stampCache.clear();
  const sprite = { c: surfStamp.c, size: size, r: cx, radius: radius };
  stampCache.set(key, sprite);
  return sprite;
}

/* Один отпечаток кисти. По слою краски он идёт в оверлей превью, по слою
   «Источник» — прямо в локальную копию маски: маска меняет саму картинку, и
   увидеть это можно только через неё. Серверный патч потом выровняет область. */
function stampAt(cx, cy, pressure) {
  const t = state.tool;
  const erase = (t.tool === 'eraser');
  const radius = Math.max(0.5, t.size / 2);
  const sprite = stampSprite(radius, t.hardness, t.color, erase);
  const pf = pressure > 0 ? pressure : 0.5;           // мышь/палец без давления → 0.5
  const alpha = clamp(t.flow * pf, 0, 1);

  const onMask = Number(stroke.drawInto) === 2;
  if (onMask) {
    const ms = paintSurface(stroke.layerId, true);
    if (ms) {
      const mx = ms.x;
      mx.setTransform(1, 0, 0, 1, 0, 0);
      mx.globalAlpha = alpha;
      // Кисть проявляет источник, ластик прячет (destination-out по альфе).
      // Цвет отпечатка тут не важен: маска читается только по альфе.
      mx.globalCompositeOperation = erase ? 'destination-out' : 'source-over';
      mx.drawImage(sprite.c, cx - sprite.r, cy - sprite.r, sprite.size, sprite.size);
      mx.globalAlpha = 1;
      mx.globalCompositeOperation = 'source-over';
    }
    requestRender();
    return;
  }

  if (!surf.overlay) return;
  const ctx = surf.overlay.x;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.globalAlpha = alpha;
  ctx.globalCompositeOperation = erase ? 'destination-out' : 'source-over';
  ctx.drawImage(sprite.c, cx - sprite.r, cy - sprite.r, sprite.size, sprite.size);
  ctx.globalAlpha = 1;
  ctx.globalCompositeOperation = 'source-over';

  // Ластик: оверлей лежит ПОВЕРХ слоя краски, поэтому destination-out внутри
  // оверлея не стирает саму краску — до прихода патча с сервера не менялось
  // ничего (симптом «стираю, а вижу только после отпускания»). Гасим ту же
  // область прямо в локальной копии слоя: серверный патч затем заменит её
  // состоянием TD, так что расхождение живёт миллисекунды.
  if (erase) {
    const ps = paintSurface(stroke.layerId, false);
    if (ps) {
      const pctx = ps.x;
      pctx.setTransform(1, 0, 0, 1, 0, 0);
      pctx.globalAlpha = alpha;
      pctx.globalCompositeOperation = 'destination-out';
      pctx.drawImage(sprite.c, cx - sprite.r, cy - sprite.r, sprite.size, sprite.size);
      pctx.globalAlpha = 1;
      pctx.globalCompositeOperation = 'source-over';
      requestRender();
    }
  }

  surf.overlayHasContent = true;
  surf.overlayLayerId = stroke.layerId;
}

/* Проход по отрезку A→B с шагом max(1, size*spacing) по длине пути. */
function stampSegment(a, b) {
  const spacing = Math.max(1, state.tool.size * state.tool.spacing);
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const d = Math.sqrt(dx * dx + dy * dy);
  if (d < 1e-4) return;

  let travelled = 0;
  while (stroke.acc + (d - travelled) >= spacing) {
    const need = spacing - stroke.acc;
    travelled += need;
    const k = travelled / d;
    stampAt(a.x + dx * k, a.y + dy * k, lerp(a.p, b.p, k));
    stroke.acc = 0;
  }
  stroke.acc += d - travelled;
}

/* Точка в wire-формате (§4): u16 x, u16 y, u8 pressure, u8 flags. */
function wirePoint(canvasX, canvasY, pressure, brk) {
  const nx = clamp(canvasX / state.canvas.w, 0, 0.99999);
  const ny = clamp(canvasY / state.canvas.h, 0, 0.99999);
  const p = pressure > 0 ? pressure : 0.5;
  return {
    x: Math.round(nx * 65535),
    y: Math.round(ny * 65535),
    p: Math.round(clamp(p, 0, 1) * 255),
    brk: brk ? 1 : 0
  };
}

function findLayer(id) {
  for (let i = 0; i < state.layers.length; i++) {
    if (sameId(state.layers[i].id, id)) return state.layers[i];
  }
  return null;
}

/* Слой, в который пойдёт мазок: выбранный в панели слоёв.
   Раньше здесь искался только слой вида paint, и выбор «Источник» тут же
   сбрасывался обратно на «Краску» — именно на это и жаловались. Теперь годится
   любой выбранный слой: сервер сам знает, в какой буфер его адресовать
   (поле drawInto: у «Источника» это маска, у «Краски» — краска). */
function drawTargetLayer() {
  const sel = findLayer(state.tool.target);
  if (sel && sel.visible !== 0) return sel;
  const p = anyPaintLayer();
  if (p) return p;
  return state.layers.length ? state.layers[0] : null;
}

function anyPaintLayer() {
  for (let i = 0; i < state.layers.length; i++) {
    if (state.layers[i].kind === 'paint') return state.layers[i];
  }
  return null;
}

function beginStroke(pointerId, pointerType, canvasPt, pressure) {
  if (stroke.active) flushStroke(true);        // прошлый мазок (напр. с другого указателя) закрываем
  // Инструмент уходит на сервер без задержки: мазок не несёт с собой «какой
  // сейчас инструмент», и если сообщение tool ещё висело в дебаунсе, сервер
  // считал мазок нарисованным кистью — ластик «красил вместо стирания».
  flushToolNow();
  const L = drawTargetLayer();
  if (!L) {
    toast('Нет слоя для рисования — рисуем после подключения к серверу', 'warn');
    return false;
  }
  if (!sameId(L.id, state.tool.target)) {
    state.tool.target = L.id;
    sendJson({ t: 'tool', tool: { target: L.id } });
    renderLayers();
  }

  stroke.active = true;
  stroke.id = stroke.nextId++;
  if (stroke.nextId > 0xFFFFFFFF) stroke.nextId = 1;
  stroke.pointerId = pointerId;
  stroke.pointerType = pointerType;
  // Маски и краска живут в разных буферах TD. Куда адресовать мазок, говорит
  // сервер полем drawInto: у слоя «Источник» это буфер маски, у «Краски» — краски.
  stroke.layerId = Number(L.drawInto !== undefined && L.drawInto !== null
                          ? L.drawInto : L.id);
  stroke.drawInto = stroke.layerId;
  stroke.lastPt = null;
  stroke.acc = 0;
  stroke.pending.length = 0;
  stroke.firstSent = false;
  stroke.lastFlush = 0;
  stroke.lastWire = null;

  clearOverlay();

  addStrokePoint(pointerId, canvasPt, pressure, true);
  log('Мазок #' + stroke.id + ' начат (слой ' + L.id + ', ' + pointerType + ')', 'tx');
  return true;
}

function addStrokePoint(pointerId, canvasPt, pressure, isFirst) {
  const pt = { x: canvasPt.x, y: canvasPt.y, p: pressure > 0 ? pressure : 0.5 };

  if (stroke.lastPt && !isFirst) {
    const dx = pt.x - stroke.lastPt.x;
    const dy = pt.y - stroke.lastPt.y;
    if (Math.sqrt(dx * dx + dy * dy) < MIN_POINT_DIST) return;   // антиспам
  }

  // wire-точка для сервера
  const wp = wirePoint(pt.x, pt.y, pt.p, isFirst);
  stroke.pending.push(wp);
  stroke.lastWire = wp;

  // локальный штамп
  if (!stroke.lastPt || isFirst) {
    stampAt(pt.x, pt.y, pt.p);
    stroke.acc = 0;
  } else {
    stampSegment(stroke.lastPt, pt);
    surf.overlayLayerId = stroke.layerId;
  }
  stroke.lastPt = pt;
  requestRender();
}

/* Отправка накопленных точек. final=true — последняя пачка мазка (bit1). */
function flushStroke(final) {
  if (!stroke.active && !final) return;

  const open = !!ws && ws.readyState === WebSocket.OPEN;
  const now = performance.now();

  if (!open) {
    stroke.pending.length = 0;
    if (final) { clearOverlay(); endStrokeLocal(); }
    return;
  }

  if (!final && now - stroke.lastFlush < FLUSH_MIN_MS) {
    if (stroke.pending.length) requestRender();   // повторить в следующем кадре
    return;
  }

  // Троттлинг: сокет забит — выбрасываем старые точки, оставляем новейшую.
  if (ws.bufferedAmount > BUFFER_HIGH_WATER && stroke.pending.length > 1) {
    const keep = stroke.pending[stroke.pending.length - 1];
    const dropped = stroke.pending.length - 1;
    stroke.pending = [{ x: keep.x, y: keep.y, p: keep.p, brk: 1 }];
    log('Троттлинг: буфер ' + ws.bufferedAmount + ' Б — отброшено ' + dropped +
        ' точек (рисуем дальше с новейшей)', 'warn');
  }

  let pts = stroke.pending;
  if (pts.length > MAX_BATCH_POINTS) {
    const extra = pts.length - MAX_BATCH_POINTS;
    pts = pts.slice(extra);              // оставляем новейшие
    log('Троттлинг: пачка обрезана до ' + MAX_BATCH_POINTS + ' точек (−' + extra + ')', 'warn');
  }

  if (final && pts.length === 0 && stroke.lastWire) {
    pts = [{ x: stroke.lastWire.x, y: stroke.lastWire.y, p: stroke.lastWire.p, brk: 0 }];
  }
  if (pts.length === 0) {
    if (final) endStrokeLocal();
    return;
  }

  sendStrokeBatch(pts, !stroke.firstSent, final, false);
  stroke.firstSent = true;
  stroke.lastFlush = now;
  stroke.pending = [];
  if (final) endStrokeLocal();
}

function endStrokeLocal() {
  stroke.active = false;
  stroke.pointerId = null;
  stroke.pointerType = null;
  stroke.lastPt = null;
  stroke.acc = 0;
  stroke.pending = [];
  stroke.lastWire = null;
}

/* Отмена мазка (Esc) — flags bit2. */
function cancelStroke() {
  if (!stroke.active) return;
  const pts = stroke.pending.length ? stroke.pending
            : (stroke.lastWire ? [stroke.lastWire] : []);
  if (pts.length && ws && ws.readyState === WebSocket.OPEN) {
    sendStrokeBatch(pts, !stroke.firstSent, false, true);
  }
  stroke.pending = [];
  endStrokeLocal();
  clearOverlay();
  log('Мазок #' + stroke.id + ' отменён (flags bit2)', 'warn');
}

/* -----------------------------------------------------------------------------
   10. БИНАРНЫЙ ПАКЕТ ТОЧЕК (§4)
   Заголовок 9 байт: u8 op=1 | u32 strokeId | u8 flags | u8 layerId | u16 count
   Точка 8 байт:     u16 x | u16 y | u8 pressure | u8 flags | u16 резерв
   Всё little-endian.
   -------------------------------------------------------------------------- */

function sendStrokeBatch(pts, first, final, cancel) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;

  let layerId = stroke.layerId;
  if (layerId > 255) {
    log('Слой ' + layerId + ' не влезает в u8 протокола — отправляю 255', 'warn');
    layerId = 255;
  }

  const count = Math.min(pts.length, MAX_BATCH_POINTS);
  const buf = new ArrayBuffer(9 + count * 8);
  const dv = new DataView(buf);

  dv.setUint8(0, 1);                       // op = точки мазка
  dv.setUint32(1, stroke.id, true);        // strokeId
  let flags = 0;
  if (first) flags |= 1;                   // bit0 — начало мазка
  if (final) flags |= 2;                   // bit1 — конец мазка
  if (cancel) flags |= 4;                  // bit2 — отмена мазка
  dv.setUint8(5, flags);
  dv.setUint8(6, layerId & 0xFF);
  dv.setUint16(7, count, true);

  let o = 9;
  for (let i = 0; i < count; i++) {
    const p = pts[i];
    dv.setUint16(o, p.x, true);
    dv.setUint16(o + 2, p.y, true);
    dv.setUint8(o + 4, p.p);
    dv.setUint8(o + 5, p.brk ? 1 : 0);
    dv.setUint16(o + 6, 0, true);          // резерв
    o += 8;
  }

  try {
    ws.send(buf);
    log('Мазок #' + stroke.id + ': ' + count + ' точек' +
        (first ? ', начало' : '') + (final ? ', конец' : '') +
        (cancel ? ', отмена' : ''), 'tx');
  } catch (e) {
    log('Ошибка отправки точек: ' + e.message, 'err');
  }
}

/* -----------------------------------------------------------------------------
   11. WEBSOCKET
   -------------------------------------------------------------------------- */

function wsUrl() {
  const proto = (location.protocol === 'https:') ? 'wss://' : 'ws://';
  let url = proto + location.host + '/ws';
  let token = null;
  try {
    token = new URLSearchParams(location.search).get('token');
  } catch (e) { token = null; }
  if (token) url += '?token=' + encodeURIComponent(token);
  return url;
}

function connect() {
  if (typeof WebSocket === 'undefined') {
    log('Браузер не поддерживает WebSocket', 'err');
    return;
  }
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  state.wsState = 'connecting';
  updateStatusUi();

  const url = wsUrl();
  log('Открываю WS: ' + url);
  try {
    ws = new WebSocket(url);
  } catch (e) {
    log('Не удалось создать WebSocket: ' + e.message, 'err');
    state.wsState = 'off';
    scheduleReconnect();
    return;
  }

  ws.binaryType = 'arraybuffer';
  ws.onopen = onWsOpen;
  ws.onmessage = onWsMessage;
  ws.onclose = onWsClose;
  ws.onerror = function () { log('Ошибка WebSocket (сервер недоступен?)', 'err'); };
}

function onWsOpen() {
  state.wsState = 'open';
  state.retry = 0;
  state.reconnecting = false;
  state.welcomed = false;
  log('WS открыт: ' + ws.url, 'ok');
  sendHello();
  updateStatusUi();
  updateHistoryButtons();
  requestRender();
}

function onWsClose(ev) {
  log('WS закрыт (код ' + ev.code + (ev.reason ? ', ' + ev.reason : '') + ')', 'warn');
  state.wsState = 'off';
  state.welcomed = false;
  state.rtt = null;
  ws = null;
  pendingHeader = null;
  abortStrokeLocal('связь потеряна');
  scheduleReconnect();
  updateStatusUi();
  updateHistoryButtons();
  requestRender();
}

/* Обрыв связи во время мазка: локальное превью не подтвердить — убираем его. */
function abortStrokeLocal(reason) {
  if (stroke.active) {
    log('Мазок #' + stroke.id + ' прерван: ' + reason, 'warn');
    endStrokeLocal();
  }
  clearOverlay();
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  state.reconnecting = true;
  const delay = Math.min(10000, 500 * Math.pow(2, Math.min(state.retry, 5))) +
                Math.round(Math.random() * 250);
  state.retry++;
  log('Переподключение через ' + delay + ' мс (попытка ' + state.retry + ')', 'warn');
  reconnectTimer = setTimeout(function () {
    reconnectTimer = null;
    connect();
  }, delay);
  updateStatusUi();
}

function sendJson(obj) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  try {
    ws.send(JSON.stringify(obj));
    return true;
  } catch (e) {
    log('Ошибка отправки JSON: ' + e.message, 'err');
    return false;
  }
}

function sendHello() {
  const box = viewBox();
  sendJson({
    t: 'hello',
    w: Math.round(box.w) || window.innerWidth,
    h: Math.round(box.h) || window.innerHeight,
    dpr: window.devicePixelRatio || 1,
    ua: navigator.userAgent
  });
  log('→ hello: ' + Math.round(box.w) + '×' + Math.round(box.h) +
      ', dpr=' + (window.devicePixelRatio || 1), 'tx');
}

function pingTick() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  pingSentAt = Date.now();
  sendJson({ t: 'ping', ts: pingSentAt });
}

/* -----------------------------------------------------------------------------
   12. ПРИЁМ СООБЩЕНИЙ
   -------------------------------------------------------------------------- */

function onWsMessage(ev) {
  if (typeof ev.data === 'string') {
    handleText(ev.data);
    return;
  }
  handleBinary(ev.data);
}

function handleText(raw) {
  let m;
  try {
    m = JSON.parse(raw);
  } catch (e) {
    log('Битый JSON от сервера: ' + String(raw).slice(0, 140), 'err');
    return;
  }
  if (!m || typeof m.t !== 'string') {
    log('Сообщение без поля t: ' + String(raw).slice(0, 140), 'err');
    return;
  }

  switch (m.t) {
    case 'welcome':   onWelcome(m); break;
    case 'layer':     onLayerMessage(m); break;
    case 'tool':      onToolMessage(m); break;
    case 'history':   onHistory(m); break;
    case 'sources':   onSourcesMessage(m); break;
    case 'pong':      onPong(m); break;
    case 'notice':
      // Спокойное уведомление сервера (не ошибка): «слой очищен» и подобное.
      log(String(m.msg || ''), 'ok');
      toast(String(m.msg || ''));
      break;
    case 'error':
      log('Ошибка сервера: ' + (m.msg || '(без текста)'), 'err');
      toast('Сервер: ' + (m.msg || 'ошибка'), 'err');
      break;
    case 'patch':
    case 'sync':
    case 'proxy':
      // Текстовый заголовок, сразу за ним — бинарный кадр (§3.2).
      if (pendingHeader) {
        log('Заголовок «' + pendingHeader.t + '» не получил кадр — перекрыт «' + m.t + '»', 'warn');
      }
      pendingHeader = m;
      break;
    default:
      log('Неизвестный тип сообщения: ' + m.t, 'warn');
  }
}

function handleBinary(data) {
  if (!pendingHeader) {
    // Бинарный кадр без заголовка — отбрасываем (§ТЗ).
    const size = data && data.byteLength ? data.byteLength : 0;
    log('Бинарный кадр без заголовка (' + size + ' Б) — отброшен', 'err');
    return;
  }
  const header = pendingHeader;
  pendingHeader = null;

  const blob = new Blob([data]);
  // Декодируем строго по порядку, иначе патчи могут примениться не в том порядке.
  decodeChain = decodeChain
    .then(function () { return decodeImage(blob); })
    .then(function (img) {
      if (header.t === 'patch') applyPatch(header, img);
      else if (header.t === 'sync') applySync(header, img);
      else applyProxy(header, img);
    })
    .catch(function (err) {
      log('Не удалось декодировать кадр «' + header.t + '»: ' + err.message, 'err');
    });
}

function decodeImage(blob) {
  if (typeof createImageBitmap === 'function') return createImageBitmap(blob);
  // Резерв для браузеров без createImageBitmap.
  return new Promise(function (resolve, reject) {
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = function () { URL.revokeObjectURL(url); resolve(img); };
    img.onerror = function () { URL.revokeObjectURL(url); reject(new Error('битый кадр')); };
    img.src = url;
  });
}

function onWelcome(m) {
  state.welcomed = true;
  state.retry = 0;
  state.reconnecting = false;

  if (m.canvas && m.canvas.w && m.canvas.h) setCanvasSize(m.canvas.w, m.canvas.h);
  state.layers = Array.isArray(m.layers) ? m.layers.slice() : [];
  if (typeof m.patchHz === 'number') state.patchHz = m.patchHz;
  if (typeof m.proxyFps === 'number') state.proxyFps = m.proxyFps;
  if (m.history) {
    state.history.undo = Number(m.history.undo) || 0;
    state.history.redo = Number(m.history.redo) || 0;
  }
  if (m.tool) applyToolFromServer(m.tool);
  ensureTarget();
  renderLayers();
  updateHistoryButtons();
  updateProxyBadges();
  fitView();

  log('welcome: полотно ' + state.canvas.w + '×' + state.canvas.h +
      ', слоёв ' + state.layers.length +
      ', patchHz=' + state.patchHz + ', proxyFps=' + state.proxyFps +
      ', ver=' + (m.ver === undefined ? '?' : m.ver), 'ok');
  updateStatusUi();
  requestRender();

  // Источник приходит отдельным кадром (постер). Если его нет пару секунд —
  // просим заново: повторный hello заставляет сервер снова поставить постер в
  // очередь. Раньше картинка источника могла не появиться до перезагрузки.
  if (sourceWaitTimer) clearTimeout(sourceWaitTimer);
  sourceWaitTimer = setTimeout(function () {
    sourceWaitTimer = null;
    if (state.proxyKind) return;            // уже получили — просить не надо
    log('Картинки источника нет — прошу заново (hello)', 'warn');
    sendHello();
  }, 2500);
}

function onLayerMessage(m) {
  if (!m.layer || m.id === undefined) {
    log('layer: нет id или layer в сообщении', 'warn');
    return;
  }
  let found = false;
  for (let i = 0; i < state.layers.length; i++) {
    if (sameId(state.layers[i].id, m.id)) {
      state.layers[i] = m.layer;      // сервер присылает полный объект слоя
      found = true;
      break;
    }
  }
  if (!found) {
    state.layers.push(m.layer);
    log('Сервер добавил слой ' + m.id + ' («' + m.layer.name + '»)', 'ok');
  }
  log('layer: id=' + m.id + ' op=' + (m.op || '?') +
      (m.prop ? ' prop=' + m.prop + ' value=' + JSON.stringify(m.value) : ''));

  // Пока пользователь тянет ползунок прозрачности — не перестраиваем панель.
  if (performance.now() - lastSliderDragAt > 600) renderLayers();
  else refreshLayerRowValues();
  updateProxyBadges();
  requestRender();
}

function onToolMessage(m) {
  if (!m.tool) return;
  applyToolFromServer(m.tool);
  log('tool: ' + JSON.stringify(m.tool));
}

function onHistory(m) {
  state.history.undo = Number(m.undo) || 0;
  state.history.redo = Number(m.redo) || 0;
  updateHistoryButtons();
  log('history: undo=' + state.history.undo + ', redo=' + state.history.redo);
}

function onSourcesMessage(m) {
  state.sources = Array.isArray(m.list) ? m.list : [];
  state.sourcesAt = performance.now();
  log('sources: ' + state.sources.length + ' шт.');
  if (pickerLayerId !== null && !el.picker.classList.contains('hidden')) {
    renderPickerList(state.sources, null);
  }
}

function onPong(m) {
  if (typeof m.ts === 'number') {
    state.rtt = Math.max(0, Date.now() - m.ts);
    updateStatusUi();
  }
}

/* ------------------------------- Патчи ----------------------------------- */

/* В patch/sync поле layer есть всегда; если сервер его не прислал —
   работаем со слоем рисования, а не создаём «слой undefined». */
function resolvePaintLayerId(id) {
  if (id !== undefined && id !== null) return id;
  const p = anyPaintLayer();
  return p ? p.id : null;
}

/* Пришли ли уже пиксели маски. Пока не пришли, источник рисуется целиком (в TD
   маска при старте залита белым), иначе до первого sync холст был бы чёрным. */
function markMaskReady(id) {
  const L = findLayer(id);
  if (L && L.kind === 'mask') surf.maskReady = true;
}

function applyPatch(h, bmp) {
  const id = resolvePaintLayerId(h.layer);
  const s = (id === null) ? null : paintSurface(id, true);
  if (!s) { closeBitmap(bmp); log('patch без слоя — отброшен', 'err'); return; }
  markMaskReady(id);

  // Патч — уже готовые пиксели слоя: сначала очищаем прямоугольник, потом рисуем.
  s.x.setTransform(1, 0, 0, 1, 0, 0);
  s.x.globalAlpha = 1;
  s.x.globalCompositeOperation = 'source-over';
  s.x.clearRect(h.x, h.y, h.w, h.h);
  s.x.drawImage(bmp, h.x, h.y, h.w, h.h);
  closeBitmap(bmp);

  // Тот же прямоугольник в оверлее: серверные пиксели отменяют локальное превью.
  clearOverlayRect(h.x, h.y, h.w, h.h);

  if (h.final) clearOverlay();

  patchWindow++;
  log('patch #' + h.seq + ' слой ' + h.layer + ' ' + h.w + '×' + h.h +
      ' @' + h.x + ',' + h.y + (h.final ? ' final' : '') +
      ', кадр ' + (bmp.width || '?') + '×' + (bmp.height || '?'));
  requestRender();
}

function applySync(h, bmp) {
  const id = resolvePaintLayerId(h.layer);
  const s = (id === null) ? null : paintSurface(id, true);
  if (!s) { closeBitmap(bmp); log('sync без слоя — отброшен', 'err'); return; }
  markMaskReady(id);

  s.x.setTransform(1, 0, 0, 1, 0, 0);
  s.x.globalAlpha = 1;
  s.x.globalCompositeOperation = 'source-over';
  s.x.clearRect(0, 0, s.c.width, s.c.height);
  s.x.drawImage(bmp, 0, 0, s.c.width, s.c.height);
  closeBitmap(bmp);

  clearOverlay();          // полная синхронизация — превью больше не нужно
  log('sync #' + h.seq + ' слой ' + h.layer + ' ' + h.w + '×' + h.h +
      ' → полотно ' + s.c.width + '×' + s.c.height +
      ', кадр ' + (bmp.width || '?') + '×' + (bmp.height || '?'), 'rx');
  requestRender();
}

function applyProxy(h, bmp) {
  if (sourceWaitTimer) { clearTimeout(sourceWaitTimer); sourceWaitTimer = null; }
  if (!surf.source) {
    // Полотна ещё нет: держим кадр до welcome (его применит setCanvasSize).
    if (lastSourceFrame) closeBitmap(lastSourceFrame.bmp);
    lastSourceFrame = { h: h, bmp: bmp };
    log('Кадр источника пришёл раньше полотна — держу до welcome', 'warn');
    return;
  }
  drawSourceFrame(h, bmp, false);
}

function drawSourceFrame(h, bmp, restore) {
  if (!surf.source) return;
  // Сервер уже вписал картинку в пропорции полотна — просто растягиваем.
  surf.source.x.setTransform(1, 0, 0, 1, 0, 0);
  surf.source.x.globalAlpha = 1;
  surf.source.x.globalCompositeOperation = 'source-over';
  surf.source.x.clearRect(0, 0, state.canvas.w, state.canvas.h);
  surf.source.x.drawImage(bmp, 0, 0, state.canvas.w, state.canvas.h);

  if (!restore) {
    // Держим кадр: welcome может пересоздать полотно и стереть картинку.
    if (lastSourceFrame) closeBitmap(lastSourceFrame.bmp);
    lastSourceFrame = { h: h, bmp: bmp };
  }

  state.proxyKind = h.kind === 'live' ? 'live' : 'poster';
  state.proxySeq = h.seq;
  proxyWindow++;
  updateProxyBadges();
  log((restore ? 'источник восстановлен после смены полотна, кадр #'
               : 'proxy #' + h.seq + ' (' + state.proxyKind + ') ') +
      (restore ? h.seq : h.w + '×' + h.h + ' → растянут на '
                 + state.canvas.w + '×' + state.canvas.h), 'rx');
  requestRender();
}

function closeBitmap(bmp) {
  if (bmp && typeof bmp.close === 'function') {
    try { bmp.close(); } catch (e) { /* ничего */ }
  }
}

/* -----------------------------------------------------------------------------
   13. ИНСТРУМЕНТ, ЦВЕТ, СЛАЙДЕРЫ
   Локальные значения применяются сразу (оптимистично), на сервер уходит
   склеенное сообщение tool не чаще одного раза в ~80 мс.
   -------------------------------------------------------------------------- */

function queueTool(patch, touchedKey) {
  Object.assign(state.tool, patch);
  if (touchedKey) localEditAt[touchedKey] = performance.now();

  toolPending = Object.assign(toolPending || {}, patch);
  applyToolToUi(false);
  flushToolSoon();
  requestRender();   // размер кисти влияет на кольцо курсора
}

function flushToolSoon() {
  const dt = performance.now() - lastToolSent;
  if (dt >= UI_SEND_MS) {
    flushToolNow();
  } else if (!toolTimer) {
    toolTimer = setTimeout(function () {
      toolTimer = null;
      flushToolNow();
    }, Math.max(0, UI_SEND_MS - dt));
  }
}

function flushToolNow() {
  if (!toolPending) return;
  const p = toolPending;
  toolPending = null;
  lastToolSent = performance.now();
  sendJson({ t: 'tool', tool: p });
  log('tool → ' + JSON.stringify(p), 'tx');
}

function applyToolFromServer(t) {
  if (!t || typeof t !== 'object') return;
  if (typeof t.tool === 'string') state.tool.tool = t.tool;
  if (typeof t.color === 'string' && normalizeHex(t.color)) state.tool.color = normalizeHex(t.color);
  if (typeof t.size === 'number' && isFinite(t.size)) state.tool.size = t.size;
  if (typeof t.hardness === 'number' && isFinite(t.hardness)) state.tool.hardness = t.hardness;
  if (typeof t.flow === 'number' && isFinite(t.flow)) state.tool.flow = t.flow;
  if (typeof t.spacing === 'number' && isFinite(t.spacing)) state.tool.spacing = t.spacing;
  if (t.target !== undefined && t.target !== null) state.tool.target = t.target;
  applyToolToUi(true);
  requestRender();
}

/* Обновление элементов управления из state.tool.
   При fromServer=true не перебиваем то, что пользователь только что тронул. */
function applyToolToUi(fromServer) {
  syncRange(el.size, state.tool.size, 'size', fromServer);
  syncRange(el.hardness, state.tool.hardness, 'hardness', fromServer);
  syncRange(el.flow, state.tool.flow, 'flow', fromServer);
  syncRange(el.spacing, state.tool.spacing, 'spacing', fromServer);

  el.sizeOut.textContent = String(Math.round(state.tool.size));
  el.hardnessOut.textContent = Number(state.tool.hardness).toFixed(2);
  el.flowOut.textContent = Number(state.tool.flow).toFixed(2);
  el.spacingOut.textContent = Number(state.tool.spacing).toFixed(2);

  for (let i = 0; i < el.toolButtons.length; i++) {
    const b = el.toolButtons[i];
    b.classList.toggle('on', b.dataset.tool === state.tool.tool);
  }
  el.view.classList.toggle('tool-pan', state.tool.tool === 'pan');

  const hex = normalizeHex(state.tool.color) || '#ff3366';
  if (!fromServer || performance.now() - localEditAt.color > 500) {
    if (el.colorPicker.value.toLowerCase() !== hex) el.colorPicker.value = hex;
    if (document.activeElement !== el.colorHex) el.colorHex.value = hex;
    el.colorHex.classList.remove('bad');
  }
  updateTargetHint();
}

function syncRange(input, value, key, fromServer) {
  if (!input) return;
  if (fromServer && performance.now() - (localEditAt[key] || 0) < 500) return;   // не мешаем тянуть
  if (document.activeElement === input) return;
  const v = String(value);
  if (input.value !== v) input.value = v;
}

function registerSwatch(hex) {
  const h = normalizeHex(hex);
  if (!h) return;
  const arr = state.swatches.filter(function (c) { return c !== h; });
  arr.unshift(h);
  state.swatches = arr.slice(0, SWATCH_MAX);
  saveSwatches();
  renderSwatches();
}

function renderSwatches() {
  if (!el.swatches) return;
  el.swatches.textContent = '';
  if (!state.swatches.length) {
    const d = document.createElement('span');
    d.className = 'swatch swatch--empty';
    d.title = 'Здесь появятся последние цвета';
    el.swatches.appendChild(d);
    return;
  }
  for (let i = 0; i < state.swatches.length; i++) {
    const c = state.swatches[i];
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'swatch';
    b.style.background = c;
    b.title = c;
    b.dataset.color = c;
    el.swatches.appendChild(b);
  }
}

function loadSwatches() {
  try {
    const raw = localStorage.getItem(LS_SWATCHES);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr)) return [];
    return arr.map(normalizeHex).filter(Boolean).slice(0, SWATCH_MAX);
  } catch (e) {
    return [];
  }
}

function saveSwatches() {
  try { localStorage.setItem(LS_SWATCHES, JSON.stringify(state.swatches)); } catch (e) { /* нет доступа */ }
}

function setColor(hex, fromInput) {
  const h = normalizeHex(hex);
  if (!h) {
    el.colorHex.classList.add('bad');
    log('Некорректный HEX: «' + hex + '»', 'warn');
    return false;
  }
  el.colorHex.classList.remove('bad');
  const wasDifferent = state.tool.color !== h;
  queueTool({ color: h }, 'color');
  if (wasDifferent && !fromInput) registerSwatch(h);
  return true;
}

function setTool(name) {
  if (name !== 'brush' && name !== 'eraser' && name !== 'pan') return;
  queueTool({ tool: name });
  if (name !== 'pan') registerSwatch(state.tool.color);
  log('Инструмент: ' + name);
}

/* Переключателя «рисовать / маска» больше нет: кисть и ластик работают по
   выбранному слою. По слою «Краска» рисуется краска, по слою «Источник» —
   маска источника. Поэтому здесь только выбор слоя-цели. */
function selectLayer(id) {
  state.tool.target = id;
  sendJson({ t: 'tool', tool: { target: id } });
  renderLayers();
  requestRender();
  const L = findLayer(id);
  log('Цель рисования: ' + (L ? L.name : id) +
      (L && Number(L.drawInto) === 2 ? ' (маска источника)' : ' (краска)'));
}

function updateHistoryButtons() {
  el.undoBtn.disabled = !(isConnected() && state.history.undo > 0);
  el.redoBtn.disabled = !(isConnected() && state.history.redo > 0);
  el.clearBtn.disabled = !isConnected();
  el.undoBtn.title = 'Отменить (' + state.history.undo + ') · Ctrl+Z';
  el.redoBtn.title = 'Вернуть (' + state.history.redo + ') · Ctrl+Shift+Z';
}

function doUndo() {
  clearOverlay();
  sendJson({ t: 'undo' });
  log('→ undo', 'tx');
}

function doRedo() {
  clearOverlay();
  sendJson({ t: 'redo' });
  log('→ redo', 'tx');
}

function doClear() {
  const L = anyPaintLayer();
  if (!L) { toast('Нет слоя краски для очистки', 'warn'); return; }
  clearOverlay();
  sendJson({ t: 'clear', layer: L.id });
  log('→ clear слой ' + L.id, 'tx');
}

/* -----------------------------------------------------------------------------
   14. ПАНЕЛЬ СЛОЁВ
   Панель строится из state.layers — любое количество слоёв, без хардкода двух.
   -------------------------------------------------------------------------- */

function ensureTarget() {
  const cur = drawTargetLayer();
  if (cur) return;
  const p = anyPaintLayer();
  if (p) state.tool.target = p.id;
  else if (state.layers.length) state.tool.target = state.layers[0].id;
}

function setTarget(id) {
  selectLayer(id);
}

/* Подпись под панелью: куда сейчас пойдёт мазок. Раньше тут был переключатель
   «Рисовать / Маска», и было непонятно, что произойдёт при выборе «Источника». */
function updateTargetHint() {
  if (!el.targetHint) return;
  const L = findLayer(state.tool.target);
  if (!L) { el.targetHint.textContent = ''; return; }
  const isMask = Number(L.drawInto) === 2;
  el.targetHint.textContent = 'Мазок идёт в слой «' + (L.name || L.id) + '»: ' +
    (isMask ? 'кисть проявляет источник, ластик прячет его.'
            : 'кисть рисует краску, ластик стирает её.');
}

function setLayerProp(id, prop, value) {
  const L = findLayer(id);
  if (L) L[prop] = value;                       // локально сразу
  const key = id + ':' + prop;
  const now = performance.now();
  const last = layerSendLast.get(key) || 0;

  if (now - last >= UI_SEND_MS) {
    sendLayerProp(id, prop, value);
  } else {
    const old = layerSendTimers.get(key);
    if (old) clearTimeout(old);
    layerSendTimers.set(key, setTimeout(function () {
      layerSendTimers.delete(key);
      sendLayerProp(id, prop, value);
    }, Math.max(0, UI_SEND_MS - (now - last))));
  }
}

function sendLayerProp(id, prop, value) {
  layerSendLast.set(id + ':' + prop, performance.now());
  sendJson({ t: 'layer', op: 'prop', id: id, prop: prop, value: value });
  log('layer → id=' + id + ' ' + prop + '=' + JSON.stringify(value), 'tx');
}

function renderLayers() {
  if (!el.layersList) return;
  sourceBadgeEls = [];
  el.layersList.textContent = '';

  if (!state.layers.length) {
    const d = document.createElement('div');
    d.className = 'empty';
    d.textContent = state.welcomed ? 'Сервер не прислал ни одного слоя.' :
      'Слои появятся после подключения к TouchDesigner.';
    el.layersList.appendChild(d);
    updateProxyBadges();
    return;
  }

  for (let i = 0; i < state.layers.length; i++) {
    const L = state.layers[i];
    // Слои с ui:0 — служебные: буфер маски нужен для отрисовки, но своей строки
    // в панели у него нет (маска — часть слоя «Источник»).
    if (L && L.ui === 0) continue;
    el.layersList.appendChild(buildLayerRow(L));
  }
  updateProxyBadges();
  updateTargetHint();
}

function buildLayerRow(L) {
  const row = document.createElement('div');
  const isTarget = sameId(L.id, state.tool.target);
  row.className = 'layer' + (isTarget ? ' layer--target' : '') + (L.visible ? '' : ' layer--hidden');
  row.dataset.id = String(L.id);

  /* --- строка заголовка: глаз, имя, тип, маркер цели --- */
  const head = document.createElement('div');
  head.className = 'layer-head';

  const eye = document.createElement('button');
  eye.type = 'button';
  eye.className = 'eye' + (L.visible ? ' on' : '');
  eye.title = L.visible ? 'Скрыть слой' : 'Показать слой';
  eye.innerHTML = L.visible ? EYE_ON_SVG : EYE_OFF_SVG;
  eye.addEventListener('click', function (e) {
    e.stopPropagation();
    setLayerProp(L.id, 'visible', L.visible ? 0 : 1);
    renderLayers();
  });

  const name = document.createElement('div');
  name.className = 'layer-name';
  name.textContent = L.name || ('Слой ' + L.id);

  const chip = document.createElement('span');
  chip.className = 'kind-chip';
  chip.textContent = (L.kind === 'source') ? 'источник · маска' : 'краска';

  head.append(eye, name, chip);
  if (isTarget) {
    const mark = document.createElement('span');
    mark.className = 'target-mark';
    mark.textContent = '● цель';
    head.appendChild(mark);
  }
  row.appendChild(head);

  /* --- прозрачность --- */
  const opacRow = document.createElement('div');
  opacRow.className = 'layer-row';
  const opLabel = document.createElement('span');
  opLabel.className = 'row-label';
  opLabel.textContent = 'Интенсивность';

  const opRange = document.createElement('input');
  opRange.type = 'range';
  opRange.min = '0';
  opRange.max = '1';
  opRange.step = '0.01';
  const opVal = clamp(typeof L.opacity === 'number' ? L.opacity : 1, 0, 1);
  opRange.value = String(opVal);

  const opOut = document.createElement('output');
  opOut.textContent = Math.round(opVal * 100) + '%';

  opRange.addEventListener('input', function () {
    lastSliderDragAt = performance.now();
    opOut.textContent = Math.round(parseFloat(opRange.value) * 100) + '%';
    setLayerProp(L.id, 'opacity', parseFloat(opRange.value));
  });
  opRange.addEventListener('change', function () { lastSliderDragAt = NEVER; });
  // клик по ползунку не должен пересобирать строку слоя (click всплывает на строку)
  opRange.addEventListener('click', function (e) { e.stopPropagation(); });

  opacRow.append(opLabel, opRange, opOut);
  row.appendChild(opacRow);

  /* --- свойства, зависящие от типа слоя --- */
  if (L.kind === 'paint') {
    // Режима «Рисовать / Маска» у слоя краски больше нет: маска — это отдельный
    // буфер и отдельный пункт панели (слой «Источник»). Здесь только подсказка,
    // что именно делает кисть по этому слою.
    const hint = document.createElement('p');
    hint.className = 'layer-hint';
    hint.textContent = 'Кисть рисует краску, ластик стирает её.';
    row.appendChild(hint);
  }

  if (L.kind === 'source') {
    const srcRow = document.createElement('div');
    srcRow.className = 'layer-row';

    const srcName = document.createElement('span');
    srcName.className = 'src-name';
    srcName.textContent = L.srcName || 'источник не выбран';
    srcName.title = L.srcPath || '';

    const badge = document.createElement('span');
    badge.className = 'badge-pill';
    badge.textContent = proxyBadgeText();
    sourceBadgeEls.push(badge);

    srcRow.append(srcName, badge);
    row.appendChild(srcRow);

    const btnRow = document.createElement('div');
    btnRow.className = 'layer-row';

    const pickBtn = document.createElement('button');
    pickBtn.type = 'button';
    pickBtn.className = 'mini-btn';
    pickBtn.style.flex = '1 1 auto';
    pickBtn.textContent = 'Источник…';
    pickBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      openSourcePicker(L, pickBtn);
    });

    const upLabel = document.createElement('label');
    upLabel.className = 'mini-btn';
    upLabel.style.flex = '0 0 auto';
    upLabel.style.display = 'inline-flex';
    upLabel.style.alignItems = 'center';
    upLabel.textContent = 'Загрузить…';
    const fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.accept = 'image/*,video/*';
    fileInput.addEventListener('click', function (e) { e.stopPropagation(); });
    fileInput.addEventListener('change', function () {
      const f = fileInput.files && fileInput.files[0];
      fileInput.value = '';
      if (f) uploadFile(f, L.id);
    });
    upLabel.appendChild(fileInput);

    btnRow.append(pickBtn, upLabel);
    row.appendChild(btnRow);

    /* --- как источник вписывается в кадр (то же меню, что у fitTOP в TD) --- */
    const fitRow = document.createElement('div');
    fitRow.className = 'layer-row';
    const fitLabel = document.createElement('span');
    fitLabel.className = 'row-label';
    fitLabel.textContent = 'Вставка';
    const fitSel = document.createElement('select');
    fitSel.title = 'Как картинка вписывается в полотно (fitTOP в TD)';
    const modes = Array.isArray(L.fitModes) && L.fitModes.length
      ? L.fitModes : DEFAULT_FIT_MODES;
    for (let i = 0; i < modes.length; i++) {
      const o = document.createElement('option');
      o.value = modes[i].key;
      o.textContent = modes[i].label;
      fitSel.appendChild(o);
    }
    fitSel.value = L.fit || 'best';
    fitSel.addEventListener('click', function (e) { e.stopPropagation(); });
    fitSel.addEventListener('change', function () {
      setLayerProp(L.id, 'fit', fitSel.value);
      requestRender();
    });
    fitRow.append(fitLabel, fitSel);
    row.appendChild(fitRow);
  }

  /* --- клик по строке делает слой целью рисования --- */
  row.addEventListener('click', function () { setTarget(L.id); });

  return row;
}

/* Лёгкое обновление значений ползунков без перестройки строк. */
function refreshLayerRowValues() {
  const rows = el.layersList.querySelectorAll('.layer');
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    const L = findLayer(r.dataset.id);
    if (!L) continue;
    const range = r.querySelector('input[type="range"]');
    const out = r.querySelector('output');
    if (range && document.activeElement !== range) {
      const v = clamp(typeof L.opacity === 'number' ? L.opacity : 1, 0, 1);
      range.value = String(v);
      if (out) out.textContent = Math.round(v * 100) + '%';
    }
    const sel = r.querySelector('select');
    if (sel) sel.value = (L.mode === 'mask') ? 'mask' : 'paint';
    r.classList.toggle('layer--hidden', !L.visible);
  }
}

function hasSourceLayer() {
  for (let i = 0; i < state.layers.length; i++) {
    if (state.layers[i].kind === 'source') return true;
  }
  return false;
}

function proxyBadgeText() {
  if (state.proxyKind === 'live') return 'прокси: live';
  if (state.proxyKind === 'poster') return 'прокси: постер';
  return 'прокси: нет';
}

function updateProxyBadges() {
  const text = proxyBadgeText();
  const cls = state.proxyKind === 'live' ? 'badge-pill live' :
              (state.proxyKind === 'poster' ? 'badge-pill poster' : 'badge-pill');
  for (let i = 0; i < sourceBadgeEls.length; i++) {
    sourceBadgeEls[i].textContent = text;
    sourceBadgeEls[i].className = cls;
  }
  if (el.proxyBadge) {
    el.proxyBadge.textContent = text;
    el.proxyBadge.classList.toggle('hidden', !hasSourceLayer());
  }
}

/* -----------------------------------------------------------------------------
   15. ИСТОЧНИКИ: СПИСОК И ЗАГРУЗКА
   GET  /api/sources  → {"list":[{"name","path","type"}]}
   POST /api/upload?name=… (сырые байты) → {"ok":true,"name","path"}
   -------------------------------------------------------------------------- */

/* -----------------------------------------------------------------------------
   16. ЗАПУСК БЕЗ TERMINAL: КНОПКИ ДЛЯ TOUCHDESIGNER
   POST /api/rebuild | /api/selftest | /api/open
   Нужны, чтобы первая сборка и проверка не требовали Textport: TD почти всегда
   собирает себя сам (см. Runtime.autostart), а это — ручной повтор.
   -------------------------------------------------------------------------- */

function tdCall(what) {
  const names = { rebuild: 'пересборка', selftest: 'самопроверка', open: 'открытие на ПК' };
  const title = names[what] || what;
  log('TD: прошу ' + title, 'tx');
  toast('TD: ' + title + '…');
  // Сначала POST, но если сервер отвечает 404/405 (в TD так бывает: POST
  // доходит не во всех сборках), повторяем тем же адресом через GET — наши
  // обработчики метод не проверяют, им важен только путь.
  return fetch('/api/' + what, { method: 'POST', cache: 'no-store' })
    .then(function (r) {
      if (r.ok) return r.json();
      if (r.status === 404 || r.status === 405) {
        log('TD: POST ' + what + ' → ' + r.status + ', пробую GET', 'warn');
        return fetch('/api/' + what + '?t=' + Date.now(), { cache: 'no-store' })
          .then(function (r2) {
            if (!r2.ok) throw new Error('HTTP ' + r2.status + ' (GET)');
            return r2.json();
          });
      }
      throw new Error('HTTP ' + r.status);
    })
    .then(function (j) {
      if (what === 'selftest') {
        log('TD: самопроверка запущена — сводка появится в paint/tmp/run_summary.txt', 'rx');
        toast('Проверка идёт: сводка будет в paint/tmp/run_summary.txt');
      } else if (what === 'rebuild') {
        log('TD: пересборка ' + (j && j.ok ? 'запущена' : 'не потребовалась'), 'rx');
        toast((j && j.ok) ? 'TD пересобирает компонент…' : 'Пересборка не нужна');
      } else {
        log('TD: страница открыта на ПК', 'rx');
        toast('Открыл страницу на ПК с TouchDesigner');
      }
      return j;
    })
    .catch(function (err) {
      log('TD: не получилось (' + err.message + ')', 'err');
      toast('Не получилось: ' + err.message);
      return null;
    });
}

async function fetchSources(force) {
  const now = performance.now();
  if (!force && state.sources && now - state.sourcesAt < 5000) return state.sources;
  const r = await fetch('/api/sources', { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const j = await r.json();
  const list = (j && Array.isArray(j.list)) ? j.list : [];
  state.sources = list;
  state.sourcesAt = now;
  return list;
}

function openSourcePicker(L, anchor) {
  pickerLayerId = L.id;
  pickerAnchor = anchor;
  el.picker.classList.remove('hidden');
  el.picker.textContent = '';
  positionPicker(anchor);

  const title = document.createElement('div');
  title.className = 'pk-title';
  title.textContent = 'Доступные источники';
  el.picker.appendChild(title);

  const msg = document.createElement('div');
  msg.className = 'pk-msg';
  msg.textContent = 'Загружаю список…';
  el.picker.appendChild(msg);

  fetchSources(true)
    .then(function (list) { renderPickerList(list, null); })
    .catch(function (err) {
      log('Не удалось получить /api/sources: ' + err.message, 'err');
      renderPickerList(null, 'Не удалось получить список: ' + err.message);
    });
}

function renderPickerList(list, errText) {
  if (!el.picker || el.picker.classList.contains('hidden')) return;   // попап уже закрыт
  el.picker.textContent = '';
  const title = document.createElement('div');
  title.className = 'pk-title';
  title.textContent = 'Доступные источники';
  el.picker.appendChild(title);

  if (errText) {
    const m = document.createElement('div');
    m.className = 'pk-msg err';
    m.textContent = errText;
    el.picker.appendChild(m);
    return;
  }
  if (!list || !list.length) {
    const m = document.createElement('div');
    m.className = 'pk-msg';
    m.textContent = 'Список пуст. Загрузите файл кнопкой «Загрузить…».';
    el.picker.appendChild(m);
    return;
  }

  for (let i = 0; i < list.length; i++) {
    const s = list[i];
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'pk-item';

    const nm = document.createElement('span');
    nm.className = 'pk-name';
    nm.textContent = s.name || s.path || '(без имени)';

    const tp = document.createElement('span');
    tp.className = 'pk-type';
    tp.textContent = (s.type === 'video') ? 'видео' : 'картинка';

    b.append(nm, tp);
    b.addEventListener('click', function () {
      chooseSource(s.path, s.name);
      closeSourcePicker();
    });
    el.picker.appendChild(b);
  }
}

function positionPicker(anchor) {
  const r = anchor.getBoundingClientRect();
  const w = 280;
  let left = Math.min(r.left, window.innerWidth - w - 8);
  left = Math.max(8, left);
  let top = r.bottom + 6;
  const maxH = 320;
  if (top + maxH > window.innerHeight - 8) top = Math.max(8, r.top - maxH - 6);
  el.picker.style.left = left + 'px';
  el.picker.style.top = top + 'px';
}

function closeSourcePicker() {
  el.picker.classList.add('hidden');
  el.picker.textContent = '';
  pickerLayerId = null;
  pickerAnchor = null;
}

/* Выбор источника: по протоколу достаточно прислать путь. */
function chooseSource(path, name) {
  if (!path) return;
  sendJson({ t: 'src', path: path });
  log('→ src: ' + path, 'tx');

  const L = findLayer(pickerLayerId !== null ? pickerLayerId : state.tool.target);
  if (L) {
    L.srcPath = path;
    if (name) L.srcName = name;
    renderLayers();
  }
  state.proxyKind = null;     // ждём новый прокси
  state.proxySeq = 0;
  updateProxyBadges();
  toast('Источник: ' + (name || path), 'ok');
}

/* Загрузка файла: сырые байты в POST, затем src с полученным путём. */
async function uploadFile(file, layerId) {
  log('Загружаю файл «' + file.name + '» (' + file.size + ' Б)');
  toast('Загрузка «' + file.name + '»…');
  try {
    const r = await fetch('/api/upload?name=' + encodeURIComponent(file.name), {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: file
    });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    let j = null;
    try { j = await r.json(); } catch (e) { j = null; }
    const path = (j && j.path) ? j.path : file.name;
    const name = (j && j.name) ? j.name : file.name;
    log('Файл загружен: ' + path, 'ok');

    const L = findLayer(layerId);
    if (L) { L.srcPath = path; L.srcName = name; }

    sendJson({ t: 'src', path: path });
    log('→ src: ' + path, 'tx');
    state.proxyKind = null;
    state.proxySeq = 0;
    renderLayers();
    updateProxyBadges();
    toast('Источник установлен: ' + name, 'ok');
  } catch (err) {
    log('Загрузка не удалась: ' + err.message, 'err');
    toast('Не удалось загрузить файл: ' + err.message, 'err');
  }
}

/* -----------------------------------------------------------------------------
   16. ВВОД: POINTER EVENTS (мышь, палец, стилус — без дублирования событий)
   -------------------------------------------------------------------------- */

function onPointerDown(e) {
  if (e.pointerType === 'mouse' && e.button !== 0 && e.button !== 1) return;

  const scr = screenPointOf(e);
  pointers.set(e.pointerId, {
    id: e.pointerId,
    type: e.pointerType,
    screen: scr,
    canvas: screenToCanvas(scr),
    pressure: e.pressure
  });

  cursor.x = scr.x;
  cursor.y = scr.y;
  cursor.inside = true;
  if (e.pointerType !== 'touch') cursor.visible = true;
  else cursor.visible = false;                 // палец: кольцо не показываем

  try { el.view.setPointerCapture(e.pointerId); } catch (err) { /* не критично */ }

  // Второй палец → жест панорамы/зума.
  if (pointers.size >= 2 && countTouch() >= 2) {
    if (stroke.active) flushStroke(true);      // доводим начатый мазок
    panState = null;
    startGesture();
    e.preventDefault();
    requestRender();
    return;
  }

  const wantPan = (state.tool.tool === 'pan') || spaceDown ||
                  (e.pointerType === 'mouse' && e.button === 1);

  if (wantPan) {
    panState = { id: e.pointerId, start: scr, tx: view.tx, ty: view.ty };
    el.view.classList.add('panning');
  } else if (e.button === 0 || e.pointerType !== 'mouse') {
    if (!beginStroke(e.pointerId, e.pointerType, screenToCanvas(scr), e.pressure)) {
      pointers.delete(e.pointerId);
    }
  }

  e.preventDefault();
  requestRender();
}

function onPointerMove(e) {
  const rec = pointers.get(e.pointerId);
  const scr = screenPointOf(e);

  if (e.pointerType !== 'touch') {
    cursor.x = scr.x;
    cursor.y = scr.y;
    cursor.inside = true;
    cursor.visible = true;
  }

  if (!rec) {
    // Наведение без нажатия — только кольцо курсора.
    requestRender();
    return;
  }

  rec.screen = scr;
  rec.canvas = screenToCanvas(scr);
  rec.pressure = e.pressure;

  // Жест двумя пальцами: пан + зум относительно средней точки.
  if (gesture && gesture.active && pointers.size >= 2) {
    updateGesture();
    e.preventDefault();
    return;
  }

  // Панорамирование.
  if (panState && panState.id === e.pointerId) {
    view.tx = panState.tx + (scr.x - panState.start.x);
    view.ty = panState.ty + (scr.y - panState.start.y);
    requestRender();
    e.preventDefault();
    return;
  }

  // Мазок.
  if (stroke.active && stroke.pointerId === e.pointerId) {
    const events = (typeof e.getCoalescedEvents === 'function') ? e.getCoalescedEvents() : null;
    if (events && events.length) {
      for (let i = 0; i < events.length; i++) {
        const ce = events[i];
        const cp = screenPointOf(ce);
        addStrokePoint(e.pointerId, screenToCanvas(cp), ce.pressure, false);
      }
    } else {
      addStrokePoint(e.pointerId, rec.canvas, e.pressure, false);
    }
    e.preventDefault();
  } else {
    requestRender();
  }
}

function onPointerUp(e) {
  const wasStroke = stroke.active && stroke.pointerId === e.pointerId;

  if (wasStroke) {
    // Остаток буфера — отдельной пачкой с флагом «конец мазка».
    flushStroke(true);
  }
  if (panState && panState.id === e.pointerId) {
    panState = null;
    el.view.classList.remove('panning');
  }

  pointers.delete(e.pointerId);
  if (gesture && pointers.size < 2) gesture = null;
  if (pointers.size === 0 && e.pointerType === 'touch') {
    cursor.visible = false;
    cursor.inside = false;
  }
  try { el.view.releasePointerCapture(e.pointerId); } catch (err) { /* уже отпущен */ }
  requestRender();
}

function onPointerCancel(e) {
  // Отмена указа браузером: мазок не выбрасываем, а корректно завершаем.
  onPointerUp(e);
}

function onPointerLeave() {
  cursor.inside = false;
  cursor.visible = false;
  requestRender();
}

function onPointerEnter(e) {
  cursor.inside = true;
  if (e && e.pointerType && e.pointerType !== 'touch') cursor.visible = true;
  requestRender();
}

function countTouch() {
  let n = 0;
  pointers.forEach(function (p) { if (p.type === 'touch') n++; });
  return n;
}

function firstTwoTouch() {
  const arr = [];
  pointers.forEach(function (p) {
    if (p.type === 'touch' && arr.length < 2) arr.push(p);
  });
  return arr.length === 2 ? arr : null;
}

function startGesture() {
  const two = firstTwoTouch();
  if (!two) return;
  const a = two[0].screen;
  const b = two[1].screen;
  const dist = Math.max(1, Math.hypot(b.x - a.x, b.y - a.y));
  const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  gesture = {
    active: true,
    dist0: dist,
    mid0: mid,
    scale0: view.scale,
    tx0: view.tx,
    ty0: view.ty
  };
  cursor.visible = false;
}

function updateGesture() {
  const two = firstTwoTouch();
  if (!two || !gesture) return;
  const a = two[0].screen;
  const b = two[1].screen;
  const dist = Math.max(1, Math.hypot(b.x - a.x, b.y - a.y));
  const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };

  const ns = clamp(gesture.scale0 * (dist / gesture.dist0), MIN_SCALE, MAX_SCALE);
  // Точка полотна под начальной серединой остаётся под текущей серединой.
  const cx = (gesture.mid0.x - gesture.tx0) / gesture.scale0;
  const cy = (gesture.mid0.y - gesture.ty0) / gesture.scale0;
  view.scale = ns;
  view.tx = mid.x - cx * ns;
  view.ty = mid.y - cy * ns;
  requestRender();
}

function onWheel(e) {
  e.preventDefault();
  const scr = screenPointOf(e);
  const dy = clamp(e.deltaY, -240, 240);
  const factor = Math.exp(-dy * 0.0015);
  zoomAt(scr, factor);
}

function onContextMenu(e) {
  if (e.target === el.view) e.preventDefault();
}

/* -----------------------------------------------------------------------------
   17. КЛАВИАТУРА
   -------------------------------------------------------------------------- */

function isTypingTarget(t) {
  if (!t) return false;
  const tag = (t.tagName || '').toUpperCase();
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t.isContentEditable === true;
}

function onKeyDown(e) {
  if (e.key === 'Escape' && !el.picker.classList.contains('hidden')) {
    closeSourcePicker();
    return;
  }
  if (isTypingTarget(e.target)) return;

  if (e.code === 'Space' && !spaceDown) {
    spaceDown = true;
    el.view.classList.add('tool-pan');
    e.preventDefault();
    return;
  }

  const ctrl = e.ctrlKey || e.metaKey;

  if (ctrl && e.key.toLowerCase() === 'z') {
    e.preventDefault();
    if (e.shiftKey) doRedo(); else doUndo();
    return;
  }
  if (ctrl && e.key.toLowerCase() === 'y') {
    e.preventDefault();
    doRedo();
    return;
  }
  if (ctrl) return;   // остальные сочетания не перехватываем

  switch (e.key.toLowerCase()) {
    case 'b': setTool('brush'); e.preventDefault(); break;
    case 'e': setTool('eraser'); e.preventDefault(); break;
    case 'h': setTool('pan'); e.preventDefault(); break;
    case '[': {
      const v = clamp(Math.round(state.tool.size * 0.85), 1, 400);
      queueTool({ size: v }, 'size');
      e.preventDefault();
      break;
    }
    case ']': {
      const v = clamp(Math.round(state.tool.size * 1.18) + (state.tool.size < 8 ? 1 : 0), 1, 400);
      queueTool({ size: v }, 'size');
      e.preventDefault();
      break;
    }
    case 'escape':
      if (stroke.active) { cancelStroke(); e.preventDefault(); }
      break;
    default:
      break;
  }
}

function onKeyUp(e) {
  if (e.code === 'Space') {
    spaceDown = false;
    el.view.classList.toggle('tool-pan', state.tool.tool === 'pan');
    if (panState) { panState = null; el.view.classList.remove('panning'); }
  }
}

/* -----------------------------------------------------------------------------
   18. СТАТУС, ПАНЕЛИ, ЗАПУСК
   -------------------------------------------------------------------------- */

function isConnected() {
  return state.wsState === 'open';
}

function updateStatusUi() {
  if (!el.statusPill) return;
  let text;
  let cls = 'pill--off';

  if (state.wsState === 'open' && state.welcomed) {
    text = 'онлайн';
    cls = 'pill--on';
  } else if (state.wsState === 'open') {
    text = 'подключено, ждём welcome';
    cls = 'pill--wait';
  } else if (state.wsState === 'connecting') {
    text = 'подключение…';
    cls = 'pill--wait';
  } else if (state.reconnecting) {
    text = 'переподключение…';
    cls = 'pill--wait';
  } else {
    text = 'нет связи';
    cls = 'pill--off';
  }

  const rtt = (state.rtt === null) ? 'RTT —' : ('RTT ' + state.rtt + ' мс');
  el.statusPill.className = 'pill ' + cls;
  el.statusPill.textContent = text + ' · ' + rtt + ' · патчи ' + state.patchRate + '/с';
  el.statusPill.title = 'Патчи: сервер до ' + state.patchHz + '/с, прокси ' + state.proxyFps + '/с' +
    (state.proxyRate ? ', принято ' + state.proxyRate + ' кадр/с' : '');
}

function rateTick() {
  state.patchRate = patchWindow;
  state.proxyRate = proxyWindow;
  patchWindow = 0;
  proxyWindow = 0;
  updateStatusUi();
}

function toggleLogPanel(forceOpen) {
  const collapsed = el.logPanel.classList.contains('collapsed');
  const open = (forceOpen === true) ? true : (forceOpen === false ? false : collapsed);
  el.logPanel.classList.toggle('collapsed', !open);
  el.logToggle.title = open ? 'Свернуть' : 'Развернуть';
}

function isNarrow() {
  return window.matchMedia('(max-width: 900px)').matches;
}

function layersPanelOpen() {
  return isNarrow() ? el.layersPanel.classList.contains('open')
                    : !el.layersPanel.classList.contains('panel-hidden');
}

/* Кнопка «Слои» всё время показывает, открыта панель или нет: иначе после
   случайного нажатия на «✕» непонятно, чем её вернуть. */
function syncLayersButton() {
  if (!el.layersBtn) return;
  const open = layersPanelOpen();
  el.layersBtn.classList.toggle('on', open);
  el.layersBtn.title = open ? 'Скрыть панель слоёв' : 'Показать панель слоёв';
}

function toggleLayersPanel() {
  if (isNarrow()) {
    const open = el.layersPanel.classList.toggle('open');
    el.drawerBackdrop.classList.toggle('hidden', !open);
    el.drawerTab.classList.toggle('hidden', open);
  } else {
    el.layersPanel.classList.toggle('panel-hidden');
  }
  syncLayersButton();
  requestRender();
}

function bindUi() {
  /* инструменты */
  for (let i = 0; i < el.toolButtons.length; i++) {
    el.toolButtons[i].addEventListener('click', function () { setTool(this.dataset.tool); });
  }

  /* цвет */
  el.colorPicker.addEventListener('input', function () {
    localEditAt.color = performance.now();
    setColor(el.colorPicker.value, true);
  });
  el.colorPicker.addEventListener('change', function () {
    if (setColor(el.colorPicker.value, false)) registerSwatch(el.colorPicker.value);
  });
  el.colorHex.addEventListener('input', function () {
    if (normalizeHex(el.colorHex.value)) {
      localEditAt.color = performance.now();
      setColor(el.colorHex.value, true);
    } else {
      el.colorHex.classList.add('bad');
    }
  });
  el.colorHex.addEventListener('change', function () {
    if (setColor(el.colorHex.value, false)) registerSwatch(el.colorHex.value);
    else el.colorHex.value = state.tool.color;
  });
  el.colorHex.addEventListener('blur', function () {
    if (!normalizeHex(el.colorHex.value)) el.colorHex.value = state.tool.color;
    el.colorHex.classList.remove('bad');
  });
  el.swatches.addEventListener('click', function (e) {
    const b = e.target.closest ? e.target.closest('.swatch') : null;
    if (b && b.dataset.color) {
      localEditAt.color = performance.now();
      setColor(b.dataset.color, false);
    }
  });

  /* слайдеры кисти */
  bindRange(el.size, 'size', parseFloat, 1, 400);
  bindRange(el.hardness, 'hardness', parseFloat, 0, 1);
  bindRange(el.flow, 'flow', parseFloat, 0.05, 1);
  bindRange(el.spacing, 'spacing', parseFloat, 0.05, 0.6);

  /* история */
  el.undoBtn.addEventListener('click', doUndo);
  el.redoBtn.addEventListener('click', doRedo);
  el.clearBtn.addEventListener('click', doClear);

  /* запуск без Textport: те же действия можно попросить прямо со страницы */
  if (el.rebuildBtn) el.rebuildBtn.addEventListener('click', function () { tdCall('rebuild'); });
  if (el.selftestBtn) el.selftestBtn.addEventListener('click', function () { tdCall('selftest'); });
  if (el.openBtn) el.openBtn.addEventListener('click', function () { tdCall('open'); });
  // «Обновить из git»: TD делает git pull в папке исходников; если файлы
  // изменились, автопересборка соберёт компонент сама — жать «Пересобрать» не надо.
  if (el.gitBtn) el.gitBtn.addEventListener('click', function () { tdCall('git'); });
  el.fitBtn.addEventListener('click', fitView);
  el.layersBtn.addEventListener('click', toggleLayersPanel);
  el.logBtn.addEventListener('click', function () {
    const collapsed = el.logPanel.classList.contains('collapsed');
    toggleLogPanel(collapsed);
  });

  /* панели */
  el.layersClose.addEventListener('click', function () {
    if (isNarrow()) {
      el.layersPanel.classList.remove('open');
      el.drawerBackdrop.classList.add('hidden');
      el.drawerTab.classList.remove('hidden');
    } else {
      el.layersPanel.classList.add('panel-hidden');
    }
    syncLayersButton();
    requestRender();
  });
  el.drawerTab.addEventListener('click', function () {
    el.layersPanel.classList.add('open');
    el.drawerBackdrop.classList.remove('hidden');
    el.drawerTab.classList.add('hidden');
    syncLayersButton();
    requestRender();
  });
  el.drawerBackdrop.addEventListener('click', function () {
    el.layersPanel.classList.remove('open');
    el.drawerBackdrop.classList.add('hidden');
    el.drawerTab.classList.remove('hidden');
    syncLayersButton();
    requestRender();
  });
  syncLayersButton();

  /* лог */
  el.logToggle.addEventListener('click', function () { toggleLogPanel(); });
  el.logCopy.addEventListener('click', copyLog);
  el.logClear.addEventListener('click', function () {
    logBuf.length = 0;
    logDirty = true;
    flushLogRender();
    log('Лог очищен');
  });

  /* попап источников */
  document.addEventListener('pointerdown', function (e) {
    if (el.picker.classList.contains('hidden')) return;
    if (el.picker.contains(e.target)) return;
    if (pickerAnchor && pickerAnchor.contains(e.target)) return;
    closeSourcePicker();
  });
  window.addEventListener('resize', function () {
    if (pickerAnchor && !el.picker.classList.contains('hidden')) positionPicker(pickerAnchor);
  });

  /* канвас */
  el.view.addEventListener('pointerdown', onPointerDown);
  el.view.addEventListener('pointermove', onPointerMove);
  el.view.addEventListener('pointerup', onPointerUp);
  el.view.addEventListener('pointercancel', onPointerCancel);
  el.view.addEventListener('pointerleave', onPointerLeave);
  el.view.addEventListener('pointerenter', onPointerEnter);
  el.view.addEventListener('wheel', onWheel, { passive: false });
  el.view.addEventListener('contextmenu', onContextMenu);
  el.view.addEventListener('dragstart', function (e) { e.preventDefault(); });

  window.addEventListener('keydown', onKeyDown);
  window.addEventListener('keyup', onKeyUp);
  window.addEventListener('blur', function () {
    spaceDown = false;
    el.view.classList.toggle('tool-pan', state.tool.tool === 'pan');
  });

  /* при возврате на вкладку — сразу пытаемся соединиться */
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && state.wsState === 'off') {
      state.retry = 0;
      if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      connect();
    }
  });

  /* размер бокса канваса */
  if (typeof ResizeObserver === 'function') {
    const ro = new ResizeObserver(function () { resizeView(); });
    ro.observe(el.canvasWrap);
    ro.observe(el.logPanel);
    ro.observe(el.layersPanel);
  }
  window.addEventListener('resize', resizeView);
  window.addEventListener('orientationchange', function () { setTimeout(resizeView, 200); });
}

function bindRange(input, key, parse, lo, hi) {
  input.addEventListener('input', function () {
    const v = clamp(parse(input.value), lo, hi);
    const patch = {};
    patch[key] = v;
    queueTool(patch, key);      // локально сразу, на сервер — склеенно (≤80 мс)
  });
}

/* -----------------------------------------------------------------------------
   ЗАПУСК
   -------------------------------------------------------------------------- */

function boot() {
  try {
    cacheDom();
    state.swatches = loadSwatches();
    bindUi();
    renderSwatches();
    renderLayers();
    updateTargetHint();
    updateHistoryButtons();
    updateStatusUi();
    updateProxyBadges();
    applyToolToUi(false);

    setCanvasSize(DEFAULT_CANVAS.w, DEFAULT_CANVAS.h);   // пока не пришёл welcome
    resizeView();
    fitView();

    log('PixelFlow PaintWeb запущен. Протокол v1.');
    log('Экран ' + window.innerWidth + '×' + window.innerHeight +
        ', dpr=' + (window.devicePixelRatio || 1));

    connect();
    setInterval(pingTick, PING_MS);
    setInterval(rateTick, 1000);
    requestRender();
  } catch (err) {
    log('Ошибка запуска: ' + (err && err.message ? err.message : String(err)), 'err');
    if (window.console && console.error) console.error(err);
  }
}

/* Ошибки страницы — в лог-панель, чтобы их можно было переслать. */
window.addEventListener('error', function (e) {
  log('JS-ошибка: ' + (e.message || 'неизвестно') +
      (e.lineno ? ' (' + e.lineno + ':' + e.colno + ')' : ''), 'err');
});
window.addEventListener('unhandledrejection', function (e) {
  const r = e.reason;
  log('Необработанный отказ промиса: ' + (r && r.message ? r.message : String(r)), 'err');
});

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
