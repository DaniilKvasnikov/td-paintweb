# -*- coding: utf-8 -*-
"""Разбор PNG-дампов PaintWeb: что именно лежит в слое (форма, альфа, цвета).

Печатает карту альфы символами: ' ' пусто, '.' слабо, '+' средне, '#' плотно.
Так видно, мазок это, картинка источника или пустота, — без картинок на экране.
"""
import struct
import sys
import zlib

try:
    import numpy as np
except Exception:
    np = None


def read_png(path):
    with open(path, 'rb') as f:
        blob = f.read()
    if blob[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('не PNG')
    p = 8
    w = h = depth = ctype = None
    idat = bytearray()
    while p < len(blob):
        ln = struct.unpack('>I', blob[p:p + 4])[0]
        tag = blob[p + 4:p + 8]
        data = blob[p + 8:p + 8 + ln]
        p += 12 + ln
        if tag == b'IHDR':
            w, h, depth, ctype = struct.unpack('>IIBB', data[:10])
        elif tag == b'IDAT':
            idat += data
        elif tag == b'IEND':
            break
    raw = zlib.decompress(bytes(idat))
    nch = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    bpp = max(1, (depth * nch) // 8)
    stride = w * bpp
    out = bytearray()
    prev = bytearray(stride)
    q = 0
    for _y in range(h):
        ft = raw[q]
        q += 1
        line = bytearray(raw[q:q + stride])
        q += stride
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
            # дампы TD бывают 16-битными: для разбора формы хватает старшего байта
            out += bytes(line[i] for i in range(0, stride, 2))
        else:
            out += line
        prev = line
    return w, h, nch, bytes(out)


def report(path):
    print('=' * 78)
    print(path)
    try:
        w, h, nch, px = read_png(path)
    except Exception as ex:
        print('  не прочитался: %s' % ex)
        return
    print('  размер %dx%d, каналов %d' % (w, h, nch))
    if np is None:
        print('  numpy нет — только размер')
        return
    a = np.frombuffer(px, dtype=np.uint8).reshape(h, w, nch).astype(np.int32)
    alpha = a[:, :, 3] if nch == 4 else np.full((h, w), 255, dtype=np.int32)
    print('  альфа: min=%d max=%d среднее=%.2f, ненулевых пикселей %.2f%%'
          % (alpha.min(), alpha.max(), alpha.mean(),
             100.0 * (alpha > 8).sum() / float(w * h)))
    if nch >= 3:
        nz = alpha > 8
        if nz.any():
            rgb = a[:, :, :3]
            sel = rgb[nz]
            print('  цвет внутри пятна (среднее RGB): %.0f, %.0f, %.0f'
                  % (sel[:, 0].mean(), sel[:, 1].mean(), sel[:, 2].mean()))
            print('  доля пикселей с альфой < 250 внутри пятна: %.1f%%'
                  % (100.0 * (alpha[nz] < 250).mean()))
    # карта альфы
    gh, gw = 18, 60
    print('  карта альфы (%dx%d):' % (gw, gh))
    for gy in range(gh):
        line = ''
        for gx in range(gw):
            y0, y1 = gy * h // gh, max(gy * h // gh + 1, (gy + 1) * h // gh)
            x0, x1 = gx * w // gw, max(gx * w // gw + 1, (gx + 1) * w // gw)
            m = alpha[y0:y1, x0:x1].mean()
            line += ' ' if m < 4 else ('.' if m < 60 else ('+' if m < 180 else '#'))
        print('  |%s|' % line)
    # вертикальный профиль «где краска» по строкам, в процентах высоты
    prof = (alpha > 8).mean(axis=1)
    top = int(np.argmax(prof)) if prof.size else -1
    print('  самая плотная строка: %d из %d (%.0f%% высоты), покрытие %.1f%%'
          % (top, h, 100.0 * top / max(1, h - 1), 100.0 * prof[top]))


def main():
    for path in sys.argv[1:]:
        report(path)


main()
