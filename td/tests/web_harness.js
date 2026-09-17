/**
 * Прогон веб-клиента (paint/web/app.js) в Node с ручной заглушкой DOM.
 *
 *     node paint/td/tests/web_harness.js
 *
 * Зачем: клиент — половина системы, и до этого теста он ни разу не исполнялся.
 * Здесь он загружается целиком, здоровается, принимает welcome, «рисует» мышью,
 * принимает патчи и sync — а проверяются байты протокола и порядок работы с
 * пикселями (clearRect перед drawImage, destination-in для маски).
 *
 * Заглушка намеренно минимальна: если app.js начнёт использовать что-то ещё,
 * тест упадёт с понятной ошибкой — это и есть сигнал дополнить заглушку.
 */

'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const RESULTS = [];
let NOW = 1000000;

function check(name, cond, extra) {
  const ok = !!cond;
  RESULTS.push({ ok, name, extra });
  console.log('  ' + (ok ? 'ok  ' : 'ПРОВАЛ') + ' ' + name +
    (!ok && extra !== undefined ? '   ' + JSON.stringify(extra) : ''));
}

/* ------------------------------------------------------------------ DOM ---- */

class ClassList {
  constructor() { this.set = new Set(); }
  add(...c) { c.forEach((x) => x && this.set.add(x)); }
  remove(...c) { c.forEach((x) => this.set.delete(x)); }
  contains(c) { return this.set.has(c); }
  toggle(c, force) {
    const on = force === undefined ? !this.set.has(c) : !!force;
    if (on) this.set.add(c); else this.set.delete(c);
    return on;
  }
}

const ALL_LISTENERS = [];
const FETCHES = [];             // все fetch-запросы клиента (url + метод)

class Ctx2D {
  constructor(canvas) {
    this.canvas = canvas;
    this.calls = [];
    this.fillStyle = '#000';
    this.strokeStyle = '#000';
    this.lineWidth = 1;
    this.globalAlpha = 1;
    this.imageSmoothingEnabled = true;
    this.filter = 'none';
    this.font = '10px sans-serif';
    this.textAlign = 'left';
    this.textBaseline = 'top';
    // смена режима наложения фиксируется как вызов, иначе проверку маски
    // (destination-in) сделать нечем
    let gco = 'source-over';
    Object.defineProperty(this, 'globalCompositeOperation', {
      get() { return gco; },
      set(v) { gco = v; this.calls.push({ op: 'gco', args: [v] }); },
    });
  }
  _rec(op, args) { this.calls.push({ op, args: Array.prototype.slice.call(args) }); }
  clearRect() { this._rec('clearRect', arguments); }
  fillRect() { this._rec('fillRect', arguments); }
  strokeRect() { this._rec('strokeRect', arguments); }
  drawImage() { this._rec('drawImage', arguments); }
  beginPath() { this._rec('beginPath', arguments); }
  closePath() { this._rec('closePath', arguments); }
  arc() { this._rec('arc', arguments); }
  arcTo() { this._rec('arcTo', arguments); }
  ellipse() { this._rec('ellipse', arguments); }
  fill() { this._rec('fill', arguments); }
  stroke() { this._rec('stroke', arguments); }
  moveTo() { this._rec('moveTo', arguments); }
  lineTo() { this._rec('lineTo', arguments); }
  rect() { this._rec('rect', arguments); }
  clip() { this._rec('clip', arguments); }
  save() { this._rec('save', arguments); }
  restore() { this._rec('restore', arguments); }
  setTransform() { this._rec('setTransform', arguments); }
  transform() { this._rec('transform', arguments); }
  translate() { this._rec('translate', arguments); }
  scale() { this._rec('scale', arguments); }
  rotate() { this._rec('rotate', arguments); }
  setLineDash() { this._rec('setLineDash', arguments); }
  fillText() { this._rec('fillText', arguments); }
  strokeText() { this._rec('strokeText', arguments); }
  measureText(t) { return { width: String(t).length * 6 }; }
  getImageData(x, y, w, h) {
    const data = new Uint8ClampedArray(Math.max(4, w * h * 4));
    return { width: w, height: h, data };
  }
  putImageData() { this._rec('putImageData', arguments); }
  createImageData(w, h) { return { width: w, height: h, data: new Uint8ClampedArray(w * h * 4) }; }
  createRadialGradient() {
    this._rec('createRadialGradient', arguments);
    return { addColorStop() {} };
  }
  createLinearGradient() {
    this._rec('createLinearGradient', arguments);
    return { addColorStop() {} };
  }
  createPattern() {
    this._rec('createPattern', arguments);
    return { __pattern: true };
  }
  resetTransform() { this._rec('resetTransform', arguments); }
  isPointInPath() { return false; }
}

let CANVAS_SEQ = 0;
const ALL_EL = [];              // все созданные элементы: среди них и рабочие канвасы

class El {
  constructor(tag, id) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.id = id || '';
    this.__canvasId = 'c' + (++CANVAS_SEQ);
    ALL_EL.push(this);
    this.style = {};
    this.classList = new ClassList();
    this.dataset = {};
    this.children = [];
    this.parentNode = null;
    this._l = {};
    this._attrs = {};
    this.textContent = '';
    this.innerHTML = '';
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.width = 300;
    this.height = 150;
    this.offsetWidth = 300;
    this.offsetHeight = 150;
    this.clientWidth = 300;
    this.clientHeight = 150;
    this.scrollTop = 0;
    this.scrollHeight = 0;
  }
  addEventListener(type, fn) {
    (this._l[type] = this._l[type] || []).push(fn);
    ALL_LISTENERS.push({ target: this, type, fn });
  }
  removeEventListener(type, fn) {
    const a = this._l[type] || [];
    const i = a.indexOf(fn);
    if (i >= 0) a.splice(i, 1);
  }
  dispatchEvent(ev) {
    fireOn(this, ev.type, ev);
    return true;
  }
  click() {
    fireOn(this, 'click', { type: 'click', target: this, preventDefault() {}, stopPropagation() {} });
  }
  append(...nodes) { nodes.forEach((n) => { if (n) this.appendChild(n); }); return this; }
  prepend(...nodes) { nodes.forEach((n) => { if (n) this.insertBefore(n); }); return this; }
  appendChild(c) {
    // как в DOM: фрагмент вставляется своими детьми, а не самим собой
    if (c && c.tagName === '#FRAGMENT') {
      for (const k of c.children.slice()) this.appendChild(k);
      c.children.length = 0;
      return c;
    }
    this.children.push(c);
    c.parentNode = this;
    return c;
  }
  insertBefore(c) { this.children.unshift(c); c.parentNode = this; return c; }
  removeChild(c) {
    const i = this.children.indexOf(c);
    if (i >= 0) this.children.splice(i, 1);
    return c;
  }
  // like the DOM: первый дочерний узел. Без него код, который чистит список
  // через firstChild, в заглушке зацикливался (и прогон висел намертво).
  get firstChild() { return this.children.length ? this.children[0] : null; }
  get firstElementChild() { return this.children.length ? this.children[0] : null; }
  get lastChild() { return this.children.length ? this.children[this.children.length - 1] : null; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  setAttribute(k, v) { this._attrs[k] = String(v); }
  getAttribute(k) { return this._attrs[k] === undefined ? null : this._attrs[k]; }
  removeAttribute(k) { delete this._attrs[k]; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  contains(node) {
    let p = node;
    while (p) {
      if (p === this) return true;
      p = p.parentNode;
    }
    return false;
  }
  focus() { DOC.activeElement = this; }
  blur() { DOC.activeElement = null; }
  scrollIntoView() {}
  getBoundingClientRect() {
    return { left: 0, top: 0, right: this.width, bottom: this.height,
             width: this.width, height: this.height, x: 0, y: 0 };
  }
  getContext(kind) {
    if (kind !== '2d') return null;
    if (!this._ctx) this._ctx = new Ctx2D(this);
    return this._ctx;
  }
  toBlob(cb) { if (cb) cb(new Blob(['png'], { type: 'image/png' })); }
  toDataURL() { return 'data:image/png;base64,'; }
}

function fireOn(target, type, ev) {
  const list = ALL_LISTENERS.filter((l) => l.type === type);
  for (const l of list) {
    if (l.target !== target && l.target !== DOC && l.target !== WIN) continue;
    // как в DOM: this внутри обработчика — это сам элемент
    try {
      l.fn.call(l.target, ev);
    } catch (e) {
      check('обработчик ' + type + ' не упал', false, String(e && e.stack || e));
    }
  }
}

function fireAny(type, ev) {                      // всем слушателям такого типа
  for (const l of ALL_LISTENERS.filter((x) => x.type === type)) {
    try {
      l.fn.call(l.target, ev);
    } catch (e) {
      check('обработчик ' + type + ' не упал', false, String(e && e.stack || e));
    }
  }
}

/** Собрать то, что клиент написал в панель «Лог» — он подробно себя документирует. */
function clientLog(limit) {
  const out = [];
  const walk = (el) => {
    if (!el) return;
    const t = String(el.textContent || '').trim();
    if (t && (!el.children || !el.children.length)) out.push(t);
    for (const k of (el.children || [])) walk(k);
  };
  walk(DOC.getElementById('logView'));
  return out.slice(-(limit || 12));
}

const DOC = {
  readyState: 'loading',
  hidden: false,
  activeElement: null,
  _byId: {},
  body: null,
  documentElement: null,
  getElementById(id) {
    if (!this._byId[id]) this._byId[id] = new El('div', id);
    return this._byId[id];
  },
  createElement(tag) { return new El(tag); },
  createDocumentFragment() { return new El('#fragment'); },
  addEventListener(type, fn) { ALL_LISTENERS.push({ target: this, type, fn }); },
  removeEventListener() {},
  querySelectorAll(sel) {
    // важно возвращать ОДНИ И ТЕ ЖЕ элементы: приложение вешает на них
    // обработчики при загрузке, а тест потом «щёлкает» по ним
    if (SEG_BUTTONS[sel]) return SEG_BUTTONS[sel];
    if (sel === '#toolButtons .seg-btn') {
      SEG_BUTTONS[sel] = ['brush', 'eraser', 'pan'].map((t) => {
        const b = new El('button');
        b.dataset.tool = t;
        b.classList.add('seg-btn');
        return b;
      });
      return SEG_BUTTONS[sel];
    }
    if (sel === '#modeButtons .seg-btn') {
      SEG_BUTTONS[sel] = ['paint', 'mask'].map((t) => {
        const b = new El('button');
        b.dataset.mode = t;
        b.classList.add('seg-btn');
        return b;
      });
      return SEG_BUTTONS[sel];
    }
    UNKNOWN_SELECTORS.push(String(sel));
    return [];
  },
  querySelector() { return null; },
  execCommand() { return true; },
};
DOC.body = new El('body');
DOC.documentElement = new El('html');

const UNKNOWN_SELECTORS = [];
const SEG_BUTTONS = {};

/** Прокрутить микрозадачи: клиент декодирует PNG через createImageBitmap (Promise). */
function settle(n) {
  let p = Promise.resolve();
  for (let i = 0; i < (n || 4); i++) p = p.then(() => new Promise((r) => setImmediate(r)));
  return p;
}

/* -------------------------------------------------------------- браузер ---- */

let RAF_QUEUE = [];
let TIMERS = [];
let SOCKETS = [];

function flushRaf(n) {
  for (let i = 0; i < (n || 4); i++) {
    const q = RAF_QUEUE;
    RAF_QUEUE = [];
    NOW += 16;
    for (const fn of q) {
      try {
        fn(NOW);
      } catch (e) {
        check('кадр отрисовки не упал', false, String(e && e.stack || e));
      }
    }
    if (!q.length) break;
  }
}

function flushTimers(ms) {
  const limit = NOW + (ms === undefined ? 5000 : ms);
  for (let guard = 0; guard < 200; guard++) {
    const due = TIMERS.filter((t) => t.at <= limit).sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    TIMERS = TIMERS.filter((t) => t !== due);
    if (due.interval) TIMERS.push({ fn: due.fn, at: due.at + due.interval, interval: due.interval });
    NOW = Math.max(NOW, due.at);
    try {
      due.fn();
    } catch (e) {
      check('таймер не упал', false, String(e && e.stack || e));
    }
  }
  NOW = Math.max(NOW, limit);
}

class FakeBlob {
  constructor(parts, opts) {
    this.parts = parts || [];
    this.type = (opts && opts.type) || '';
    this.size = this.parts.reduce((s, p) => s + (p && p.byteLength ? p.byteLength : String(p).length), 0);
  }
}

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 0;                       // CONNECTING
    this.bufferedAmount = 0;
    this.sent = [];
    this._l = {};
    SOCKETS.push(this);
  }
  addEventListener(type, fn) { (this._l[type] = this._l[type] || []).push(fn); }
  removeEventListener() {}  _emit(type, ev) {
    if (type === 'open') this.readyState = 1;
    if (type === 'close') this.readyState = 3;
    // приложение пользуется свойством-стилем (ws.onopen/onmessage/onclose),
    // поэтому поддерживаем и его, и addEventListener
    const prop = this['on' + type];
    if (typeof prop === 'function') prop.call(this, ev || {});
    for (const fn of (this._l[type] || [])) fn(ev || {});
  }
  send(data) { this.sent.push(data); }
  close() { this.lastState = 3; this.readyState = 3; this._emit('close', {}); }
  /* помощники для теста */
  open() { this.readyState = 1; this._emit('open', {}); }
  text(obj) {
    try {
      this._emit('message', { data: typeof obj === 'string' ? obj : JSON.stringify(obj) });
    } catch (e) {
      check('обработчик текстового сообщения не упал', false, String(e && e.stack || e));
    }
  }
  bin(buf) {
    try {
      this._emit('message', { data: buf });
    } catch (e) {
      check('обработчик бинарного сообщения не упал', false, String(e && e.stack || e));
    }
  }
}

// реальный WebSocket имеет статические константы, и клиент сверяется с ними
// (ws.readyState !== WebSocket.OPEN) — без них он не отправит ни байта
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSING = 2;
FakeWebSocket.CLOSED = 3;

const WIN = {
  innerWidth: 1200,
  innerHeight: 800,
  devicePixelRatio: 2,
  console,
  addEventListener(type, fn) { ALL_LISTENERS.push({ target: this, type, fn }); },
  removeEventListener() {},
  matchMedia() { return { matches: false, addEventListener() {}, removeEventListener() {} }; },
  setTimeout(fn, ms) { return setTimeoutX(fn, ms); },
  clearTimeout(id) { TIMERS = TIMERS.filter((t) => t.id !== id); },
  setInterval(fn, ms) { return setIntervalX(fn, ms); },
  clearInterval(id) { TIMERS = TIMERS.filter((t) => t.id !== id); },
  requestAnimationFrame(fn) { RAF_QUEUE.push(fn); return RAF_QUEUE.length; },
  cancelAnimationFrame() {},
  location: null,
};

let TIMER_ID = 0;
function setTimeoutX(fn, ms) {
  const id = ++TIMER_ID;
  TIMERS.push({ id, fn, at: NOW + (ms || 0) });
  return id;
}
function setIntervalX(fn, ms) {
  const id = ++TIMER_ID;
  TIMERS.push({ id, fn, at: NOW + (ms || 1), interval: ms || 1 });
  return id;
}

// Дата на виртуальных часах: приложение использует new Date() для отметок в логе
// и Date.now() для троттлинга, и то и другое должно идти по нашему времени.
class FakeDate extends Date {
  constructor(...args) {
    if (args.length === 0) super(NOW);
    else super(...args);
  }
  static now() { return NOW; }
}

const LOCATION = {
  protocol: 'http:',
  host: '192.168.1.50:9980',
  href: 'http://192.168.1.50:9980/',
  search: '',
  pathname: '/',
};

const SANDBOX = {
  document: DOC,
  window: WIN,
  navigator: {
    userAgent: 'HarnessAgent/1.0',
    clipboard: { writeText() { return Promise.resolve(); } },
  },
  location: LOCATION,
  localStorage: {
    _d: {},
    getItem(k) { return this._d[k] === undefined ? null : this._d[k]; },
    setItem(k, v) { this._d[k] = String(v); },
    removeItem(k) { delete this._d[k]; },
  },
  WebSocket: FakeWebSocket,
  Blob: FakeBlob,
  URLSearchParams,
  URL,
  console,
  Date: FakeDate,
  performance: { now: () => NOW },
  requestAnimationFrame: WIN.requestAnimationFrame,
  cancelAnimationFrame: WIN.cancelAnimationFrame,
  setTimeout: setTimeoutX,
  clearTimeout: WIN.clearTimeout,
  setInterval: setIntervalX,
  clearInterval: WIN.clearInterval,
  devicePixelRatio: 2,
  matchMedia: WIN.matchMedia,
  fetch: (url, opts) => {
    // Запоминаем запросы: кнопки «Пересобрать/Проверка/Открыть на ПК» работают
    // именно через HTTP, и проверить это можно только по списку вызовов.
    const method = (opts && opts.method) || 'GET';
    FETCHES.push({ url: String(url), method: method });
    // Подсунутое поведение живого TD: POST на /api/selftest не доходит (404),
    // значит клиент обязан повторить запрос через GET.
    if (method === 'POST' && String(url).indexOf('/api/selftest') === 0) {
      return Promise.resolve({
        ok: false, status: 404,
        json: () => Promise.resolve({ error: 'not found' }),
        text: () => Promise.resolve('not found'),
        blob: () => Promise.resolve(new FakeBlob(['x'])),
      });
    }
    return Promise.resolve({
      ok: true, status: 200,
      json: () => Promise.resolve({ ok: true, list: [{ name: 'sample.png', path: 'uploads/sample.png', type: 'image' }] }),
      text: () => Promise.resolve('{"list":[]}'),
      blob: () => Promise.resolve(new FakeBlob(['x'])),
    });
  },
  Image: class { constructor() { this.width = 16; this.height = 16; } set src(v) { this._s = v; if (this.onload) this.onload(); } },
  createImageBitmap: (blob) => Promise.resolve({ width: 120, height: 80, close() {} }),
  alert: () => {},
};
SANDBOX.window = WIN;
SANDBOX.globalThis = SANDBOX;
WIN.document = DOC;
WIN.location = LOCATION;
WIN.navigator = SANDBOX.navigator;
WIN.localStorage = SANDBOX.localStorage;
WIN.WebSocket = FakeWebSocket;

/* ------------------------------------------------------------- прогон ------ */

function decodeFrames(frames) {
  return frames.map((buf) => {
    const dv = new DataView(buf.buffer || buf);
    return {
      op: dv.getUint8(0),
      strokeId: dv.getUint32(1, true),
      flags: dv.getUint8(5),
      layer: dv.getUint8(6),
      count: dv.getUint16(7, true),
      len: dv.byteLength,
      points: (function () {
        const out = [];
        for (let i = 0; i < dv.getUint16(7, true); i++) {
          const o = 9 + i * 8;
          out.push({ x: dv.getUint16(o, true), y: dv.getUint16(o + 2, true),
                     pr: dv.getUint8(o + 4), brk: dv.getUint8(o + 5),
                     pad: dv.getUint16(o + 6, true) });
        }
        return out;
      })(),
    };
  });
}

function main() {
  return run();
}

async function run() {
  const appPath = path.join(__dirname, '..', '..', 'web', 'app.js');
  const src = fs.readFileSync(appPath, 'utf8');

  console.log('\n=== веб-клиент PaintWeb в Node =================================');

  const ctx = vm.createContext(SANDBOX);
  let threw = null;
  try {
    vm.runInContext(src, ctx, { filename: 'app.js' });
  } catch (e) {
    threw = e;
  }
  check('app.js загружается без ошибок', !threw, threw && String(threw.stack || threw));
  if (threw) return finish();

  try {
    DOC.readyState = 'interactive';
    fireAny('DOMContentLoaded', { type: 'DOMContentLoaded' });
    flushRaf(2);
  } catch (e) {
    check('запуск (boot) без исключений', false, String(e.stack || e));
  }
  check('boot прошёл без исключений', true);
  check('создан один WebSocket', SOCKETS.length === 1, SOCKETS.length);

  const ws = SOCKETS[0];
  if (!ws) return finish();
  check('адрес ws: /ws и хост из location',
    ws.url === 'ws://192.168.1.50:9980/ws', ws.url);

  ws.open();
  const hello = ws.sent.map(safeJson).filter(Boolean).find((m) => m.t === 'hello');
  check('после открытия отправлен hello', !!hello, ws.sent.length);
  check('в hello есть размеры окна и dpr',
    hello && typeof hello.w === 'number' && typeof hello.h === 'number'
    && typeof hello.dpr === 'number', hello);

  /* ---- кадр источника приходит РАНЬШЕ welcome ---- */
  // Welcome пересоздаёт полотно, то есть стирает уже показанную картинку
  // источника, а сервер повторно её не присылает. Раньше из-за этого источник
  // пропадал до перезагрузки страницы.
  ws.text({ t: 'proxy', kind: 'poster', seq: 1, w: 1920, h: 1080, fmt: 'png' });
  ws.bin(new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0, 0, 0, 0]).buffer);
  await settle(4);
  flushTimers(400);          // дать панели лога перерисоваться (она на таймере)
  check('кадр источника принят до welcome',
    clientLog(60).some((s) => s.indexOf('proxy #1') >= 0), clientLog(12).slice(-4));

  ws.text({
    t: 'welcome', ver: 1, canvas: { w: 1920, h: 1080 },
    layers: [
      { id: 0, name: 'Источник', kind: 'source', visible: 1, opacity: 1, drawInto: 2, srcType: 'image', srcName: 'sample.png' },
      { id: 1, name: 'Краска', kind: 'paint', visible: 1, opacity: 1, drawInto: 1 },
      { id: 2, name: 'Маска источника', kind: 'mask', visible: 1, opacity: 1, ui: 0 },
    ],
    tool: { tool: 'brush', color: '#ff3366', size: 40, hardness: 0.7, flow: 0.9, spacing: 0.15, target: 1 },
    patchHz: 30, proxyFps: 2, history: { undo: 0, redo: 0 },
  });
  flushRaf(2);
  flushTimers(400);
  check('welcome принят без ошибок', true);
  check('картинка источника не потерялась при welcome',
    clientLog(60).some((s) => s.indexOf('proxy #1') >= 0), clientLog(14).slice(-6));
  check('неизвестных CSS-селекторов не запрашивали', UNKNOWN_SELECTORS.length === 0,
    UNKNOWN_SELECTORS.slice(0, 3));

  /* ---- панель слоёв: кнопка «Слои» показывает, открыта панель или нет ---- */
  const layersBtn = DOC.getElementById('layersBtn');
  const layersPanel = DOC.getElementById('layersPanel');
  DOC.getElementById('layersClose').click();
  const closedState = layersBtn.classList.contains('on');
  flushRaf(1);
  layersBtn.click();
  const reopened = !layersPanel.classList.contains('panel-hidden');
  check('крестик скрывает панель, кнопка «Слои» гаснет', !closedState,
    { panelHidden: layersPanel.classList.contains('panel-hidden') });
  check('кнопка «Слои» возвращает панель и подсвечивается',
    reopened && layersBtn.classList.contains('on'),
    { reopened: reopened, on: layersBtn.classList.contains('on') });

  /* ---- рисование мышью ---- */
  const view = DOC.getElementById('view');
  // button обязателен: клиент справедливо игнорирует мышь без button 0/1
  const base = { pointerId: 1, pointerType: 'mouse', buttons: 1, button: 0, pressure: 0,
                 preventDefault() {}, stopPropagation() {}, isPrimary: true };
  fireOn(view, 'pointerdown', Object.assign({ type: 'pointerdown', clientX: 100, clientY: 100 },
    base, { target: view }));
  for (let i = 1; i <= 6; i++) {
    fireOn(view, 'pointermove', Object.assign({ type: 'pointermove',
      clientX: 100 + i * 20, clientY: 100 + i * 5 }, base,
      { button: -1, target: view }));
    NOW += 16;
    flushRaf(1);
  }
  fireOn(view, 'pointerup', Object.assign({ type: 'pointerup', clientX: 220, clientY: 130 },
    base, { buttons: 0, target: view }));
  flushRaf(2);

  const binFrames = ws.sent.filter((s) => typeof s !== 'string');
  check('точки мазка ушли бинарными кадрами', binFrames.length > 0, binFrames.length);
  const frames = decodeFrames(binFrames);
  const strokeIds = Array.from(new Set(frames.map((f) => f.strokeId)));
  if (!frames.length) {
    check('мазок отправлен (иначе остальные проверки бессмысленны)', false,
      ws.sent.map((s) => (typeof s === 'string' ? safeJson(s) : 'binary')));
    return finish();
  }
  check('strokeId один на мазок', strokeIds.length === 1, strokeIds);
  check('первый кадр помечен началом (bit0)', (frames[0].flags & 1) === 1, frames[0].flags);
  check('последний кадр помечен концом (bit1)',
    (frames[frames.length - 1].flags & 2) === 2, frames[frames.length - 1].flags);
  check('мазок адресован слою краски (1)', frames.every((f) => f.layer === 1),
    frames.map((f) => f.layer));
  check('opcode = 1', frames.every((f) => f.op === 1));
  check('длина кадра = 9 + 8·count',
    frames.every((f) => f.len === 9 + 8 * f.count), frames.map((f) => f.len));
  check('count в пределах 1..127',
    frames.every((f) => f.count >= 1 && f.count <= 127), frames.map((f) => f.count));
  const pts = frames.reduce((a, f) => a.concat(f.points), []);
  check('точки в диапазоне u16', pts.every((p) => p.x >= 0 && p.x <= 65535
    && p.y >= 0 && p.y <= 65535), pts.slice(0, 2));
  check('давление мыши = 128', pts.every((p) => p.pr === 128), pts.slice(0, 3).map((p) => p.pr));
  check('резервные байты нулевые', pts.every((p) => p.pad === 0));
  check('точки монотонны по X', pts.every((p, i) => i === 0 || p.x >= pts[i - 1].x - 1),
    pts.map((p) => p.x));

  /* ---- патч ---- */
  const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0, 0, 0, 0]).buffer;
  ws.text({ t: 'patch', layer: 1, x: 40, y: 50, w: 120, h: 80, seq: 1, final: 1 });
  ws.bin(png);
  await settle(6);                     // клиент декодирует PNG асинхронно
  flushRaf(3);
  const ctxs = allContexts();
  check('патч: clearRect перед drawImage с тем же прямоугольником',
    ctxs.some((c) => hasClearThenDraw(c, 40, 50, 120, 80)),
    ctxs.map((c) => c.calls.slice(-4).map((x) => x.op)));
  const logAfterPatch = clientLog(60);
  check('клиент записал в лог применение патча',
    logAfterPatch.some((l) => /patch #1/.test(l)), logAfterPatch.slice(-4));

  /* ---- sync ---- */
  ws.text({ t: 'sync', layer: 1, seq: 2, w: 1920, h: 1080 });
  ws.bin(png);
  await settle(6);
  flushRaf(3);
  check('sync: очистка всего слоя и отрисовка',
    allContexts().some((c) => c.calls.some((x) => x.op === 'clearRect' && x.args[2] === 1920)),
    allContexts().map((c) => c.calls.filter((x) => x.op === 'clearRect').slice(-1)[0]));

  /* ---- инструменты, режим, undo/redo/clear ---- */
  ws.sent.length = 0;
  DOC.querySelectorAll('#toolButtons .seg-btn')[1].click();      // ластик
  flushTimers(200);
  const toolMsg = ws.sent.map(safeJson).filter(Boolean).filter((m) => m.t === 'tool').pop();
  check('клик «Ластик» отправил tool=eraser',
    toolMsg && toolMsg.tool && toolMsg.tool.tool === 'eraser', toolMsg);

  // Мазок не несёт в себе инструмент, поэтому сообщение tool обязано уйти ДО
  // первого бинарного кадра. Иначе сервер считал мазок нарисованным кистью —
  // «ластик красит вместо стирания».
  ws.sent.length = 0;
  DOC.querySelectorAll('#toolButtons .seg-btn')[0].click();      // кисть (в дебаунсе)
  fireOn(view, 'pointerdown', Object.assign({ type: 'pointerdown', clientX: 200, clientY: 200 },
    base, { target: view }));                                    // сразу рисуем, таймер не ждём
  fireOn(view, 'pointermove', Object.assign({ type: 'pointermove', clientX: 220, clientY: 200 },
    base, { button: -1, target: view }));
  NOW += 16;
  flushRaf(1);
  const firstTool = ws.sent.map(safeJson).findIndex((m) => m && m.t === 'tool');
  fireOn(view, 'pointerup', Object.assign({ type: 'pointerup', clientX: 230, clientY: 200 },
    base, { buttons: 0, target: view }));
  flushRaf(2);
  const firstBin = ws.sent.findIndex((s) => typeof s !== 'string');
  check('инструмент уходит на сервер раньше первого кадра мазка',
    firstTool >= 0 && firstBin >= 0 && firstTool < firstBin,
    { tool: firstTool, bin: firstBin, sent: ws.sent.map((s) => (typeof s === 'string' ? safeJson(s) && safeJson(s).t : 'bin')) });
  DOC.querySelectorAll('#toolButtons .seg-btn')[1].click();      // снова ластик
  flushTimers(200);

  // Ластик обязан стирать краску в локальной копии слоя сразу, а не только в
  // оверлее превью: оверлей лежит ПОВЕРХ слоя, и destination-out внутри него
  // ничего не гасит — до патча с сервера не менялось бы ничего (симптом
  // «стираю, а изменения видно только после отпускания мыши»).
  const marks = allContexts().map((c) => ({ c: c, from: c.calls.length }));
  fireOn(view, 'pointerdown', Object.assign({ type: 'pointerdown', clientX: 120, clientY: 120 },
    base, { target: view }));
  for (let i = 1; i <= 4; i++) {
    fireOn(view, 'pointermove', Object.assign({ type: 'pointermove',
      clientX: 120 + i * 15, clientY: 120 }, base, { button: -1, target: view }));
    NOW += 16;
    flushRaf(1);
  }
  fireOn(view, 'pointerup', Object.assign({ type: 'pointerup', clientX: 180, clientY: 120 },
    base, { buttons: 0, target: view }));
  flushRaf(2);
  const erasedCtxs = marks.filter((m) => {
    const ops = m.c.calls;
    for (let i = m.from; i < ops.length - 1; i++) {
      if (ops[i].op === 'gco' && ops[i].args[0] === 'destination-out' &&
          ops[i + 1].op === 'drawImage') return true;
    }
    return false;
  });
  check('ластик гасит краску в двух полотнах (оверлей превью + сам слой)',
    erasedCtxs.length >= 2, erasedCtxs.length);
  check('ластик реально отправил мазок',
    ws.sent.some((s) => typeof s !== 'string'), ws.sent.length);

  /* ---- выбор слоя рисования вместо переключателя «Рисовать / Маска» ---- */
  // Переключателя режима больше нет: куда попадёт мазок, решает выбранный слой.
  // И выбор НЕ должен сбрасываться, когда начинаешь рисовать (это был баг).
  const srcRow = layerRowById(0);
  const paintRow = layerRowById(1);
  check('в панели есть строки «Источник» и «Краска»', !!srcRow && !!paintRow,
    { source: !!srcRow, paint: !!paintRow });

  ws.sent.length = 0;
  if (srcRow) srcRow.click();
  flushTimers(200);
  const targetMsg = ws.sent.map(safeJson).filter(Boolean)
    .filter((m) => m.t === 'tool' && m.tool && m.tool.target !== undefined).pop();
  check('клик по «Источнику» выбирает его целью рисования',
    targetMsg && Number(targetMsg.tool.target) === 0, targetMsg);

  // Рисуем по «Источнику» — мазок обязан уехать в буфер МАСКИ (слой 2),
  // и цель не должна перескочить на «Краску».
  ws.sent.length = 0;
  const base2 = { pointerId: 7, pointerType: 'mouse', buttons: 1, button: 0, pressure: 0,
                  preventDefault() {}, stopPropagation() {}, isPrimary: true };
  fireOn(view, 'pointerdown', Object.assign({ type: 'pointerdown', clientX: 140, clientY: 140 },
    base2, { target: view }));
  fireOn(view, 'pointermove', Object.assign({ type: 'pointermove', clientX: 160, clientY: 140 },
    base2, { button: -1, target: view }));
  NOW += 16;
  flushRaf(1);
  fireOn(view, 'pointerup', Object.assign({ type: 'pointerup', clientX: 170, clientY: 140 },
    base2, { buttons: 0, target: view }));
  flushRaf(2);
  const maskFrames = decodeFrames(ws.sent.filter((s) => typeof s !== 'string'));
  check('мазок по «Источнику» адресован буферу маски (слой 2)',
    maskFrames.length > 0 && maskFrames.every((f) => f.layer === 2),
    maskFrames.map((f) => f.layer));
  const afterTarget = ws.sent.map(safeJson).filter(Boolean)
    .filter((m) => m.t === 'tool' && m.tool && m.tool.target !== undefined).pop();
  check('цель рисования НЕ сбросилась на «Краску»',
    !afterTarget || Number(afterTarget.tool.target) === 0, afterTarget);

  /* ---- режим вставки источника выбирается в строке слоя ---- */
  ws.sent.length = 0;
  const fitSel = findInRow(srcRow, 'SELECT');
  const fitOptions = fitSel ? Array.from(fitSel.children).map((o) => o.value) : [];
  check('в строке источника есть выбор вставки со всеми режимами',
    !!fitSel && fitOptions.length === 6 && fitOptions.indexOf('fill') >= 0
    && fitOptions.indexOf('nativeres') >= 0, fitOptions);
  if (fitSel) {
    fitSel.value = 'fill';
    fireOn(fitSel, 'change', { type: 'change', target: fitSel });
  }
  flushTimers(200);
  const fitMsg = ws.sent.map(safeJson).filter(Boolean)
    .filter((m) => m.t === 'layer' && m.prop === 'fit').pop();
  check('выбор режима вставки уходит на сервер',
    fitMsg && Number(fitMsg.id) === 0 && fitMsg.value === 'fill', fitMsg);

  /* ---- применение маски в отрисовке ---- */
  // Маска приходит отдельным слоем (kind:mask) и применяется к источнику
  // через destination-in. Без неё источник рисуется как есть.
  ws.text({ t: 'sync', layer: 2, seq: 20, w: 1920, h: 1080 });
  ws.bin(png);
  await settle(6);
  flushRaf(3);
  check('маска применяется к источнику (destination-in)',
    allContexts().some((c) => c.calls.some((x) => x.op === 'gco'
      && x.args[0] === 'destination-in')),
    allContexts().map((c) => c.calls.filter((x) => x.op === 'gco')
      .map((x) => x.args[0]).slice(-3)));

  ws.sent.length = 0;
  DOC.getElementById('undoBtn').click();
  DOC.getElementById('redoBtn').click();
  DOC.getElementById('clearBtn').click();
  flushTimers(200);
  const types = ws.sent.map(safeJson).filter(Boolean).map((m) => m.t);
  check('кнопки Отменить/Вернуть/Очистить шлют свои сообщения',
    types.includes('undo') && types.includes('redo') && types.includes('clear'), types);

  /* ---- прочие сообщения ---- */
  ws.sent.length = 0;
  ws.text({ t: 'history', undo: 3, redo: 1 });
  ws.text({ t: 'pong', ts: 1 });
  ws.text({ t: 'sources', list: [{ name: 'a.png', path: 'uploads/a.png', type: 'image' }] });
  ws.text({ t: 'error', msg: 'тестовая ошибка' });
  ws.text({ t: 'совсем-неизвестное', foo: 1 });
  ws.bin(png);                                     // бинарь без заголовка
  await settle(4);
  flushRaf(2);
  check('прочие сообщения и мусор не ломают клиент', true);

  /* ---- переподключение ---- */
  const n0 = SOCKETS.length;
  ws.close();
  flushTimers(3000);
  check('после обрыва связи клиент переподключается', SOCKETS.length > n0,
    { было: n0, стало: SOCKETS.length });

  /* ---- кнопки запуска TD (без Textport) ---- */
  FETCHES.length = 0;
  DOC.getElementById('rebuildBtn').click();
  DOC.getElementById('selftestBtn').click();
  DOC.getElementById('openBtn').click();
  DOC.getElementById('gitBtn').click();
  await settle(4);
  const urls = FETCHES.map((f) => f.url + ' ' + f.method);
  check('«Пересобрать» просит TD пересобраться',
    FETCHES.some((f) => f.url === '/api/rebuild' && f.method === 'POST'), urls);
  check('«Обновить из git» просит TD подтянуть исходники',
    FETCHES.some((f) => f.url === '/api/git' && f.method === 'POST'), urls);
  check('«Проверка» запускает самопроверку',
    FETCHES.some((f) => f.url === '/api/selftest' && f.method === 'POST'), urls);
  check('если POST не дошёл — повторяем через GET',
    FETCHES.some((f) => f.url.indexOf('/api/selftest') === 0 && f.method === 'GET'),
    urls);
  check('«Открыть на ПК» просит открыть страницу',
    FETCHES.some((f) => f.url === '/api/open' && f.method === 'POST'), urls);

  /* ---- смена размера полотна не должна стирать источник ---- */
  // welcome с другим размером пересоздаёт полотно: без восстановления картинка
  // источника пропадала бы до перезагрузки (сервер повторно её не присылает).
  ws.text({
    t: 'welcome', ver: 1, canvas: { w: 1280, h: 720 },
    layers: [
      { id: 0, name: 'Источник', kind: 'source', visible: 1, opacity: 1, drawInto: 2, srcType: 'image', srcName: 'sample.png' },
      { id: 1, name: 'Краска', kind: 'paint', visible: 1, opacity: 1, drawInto: 1 },
      { id: 2, name: 'Маска источника', kind: 'mask', visible: 1, opacity: 1, ui: 0 },
    ],
    tool: { tool: 'brush', color: '#ff3366', size: 40, hardness: 0.7, flow: 0.9, spacing: 0.15, target: 1 },
    patchHz: 30, proxyFps: 2, history: { undo: 0, redo: 0 },
  });
  flushRaf(2);
  flushTimers(400);
  check('после смены размера полотна источник восстановлен',
    clientLog(80).some((s) => s.indexOf('возвращаю картинку источника') >= 0),
    clientLog(14).slice(-6));

  /* ---- накопленный лог ---- */
  const logView = DOC.getElementById('logView');
  check('в панели «Лог» есть записи', String(logView.textContent || '').length > 0
    || (logView.children || []).length > 0);
  return finish();
}

function safeJson(s) {
  if (typeof s !== 'string') return null;
  try { return JSON.parse(s); } catch (e) { return null; }
}

function allContexts() {
  // именно ALL_EL: рабочие канвасы слоёв клиент создаёт через createElement,
  // и они не попадают в _byId
  return ALL_EL.filter((el) => el && el._ctx).map((el) => el._ctx);
}

/* Первый элемент строки слоя с нужным тегом (например SELECT режима вставки). */
function findInRow(row, tag) {
  const stack = row ? [row] : [];
  while (stack.length) {
    const el = stack.shift();
    if (el !== row && el.tagName === tag) return el;
    for (const c of (el.children || [])) stack.push(c);
  }
  return null;
}

/* Строка панели слоёв по id слоя: клиент помечает её dataset.id и классом layer. */
function layerRowById(id) {
  for (const el of ALL_EL) {
    if (!el || !el.dataset) continue;
    if (String(el.dataset.id) !== String(id)) continue;
    if (String(el.className || '').split(' ').indexOf('layer') >= 0) return el;
  }
  return null;
}

function hasClearThenDraw(c, x, y, w, h) {
  for (let i = 0; i < c.calls.length; i++) {
    const a = c.calls[i];
    if (a.op !== 'clearRect') continue;
    if (a.args[0] !== x || a.args[1] !== y || a.args[2] !== w || a.args[3] !== h) continue;
    for (let j = i + 1; j < c.calls.length; j++) {
      const b = c.calls[j];
      if (b.op === 'drawImage') {
        // drawImage(bitmap, x, y, w, h) — тот же прямоугольник
        return b.args[1] === x && b.args[2] === y && b.args[3] === w && b.args[4] === h;
      }
      if (b.op === 'clearRect') break;         // это уже другой прямоугольник
    }
  }
  return false;
}

function finish() {
  console.log('\n--- последние записи лога клиента ------------------------------');
  for (const l of clientLog(14)) console.log('   ' + l);
  console.log('\n===============================================================');
  const bad = RESULTS.filter((r) => !r.ok);
  console.log('проверок: ' + RESULTS.length + ', провалов: ' + bad.length);
  for (const b of bad) console.log('  ПРОВАЛ: ' + b.name + '   ' + JSON.stringify(b.extra));
  process.exitCode = bad.length ? 1 : 0;
}

Promise.resolve(main()).catch((e) => {
  console.log('harness упал: ' + (e && e.stack || e));
  process.exitCode = 1;
});
