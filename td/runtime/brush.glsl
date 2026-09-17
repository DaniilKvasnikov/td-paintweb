// PaintWeb — v2 кисть.
//
// Одна пачка штампов за кадр накладывается в ping-pong буфер слоя.
// Входы:  sTD2DInputs[0] = предыдущее состояние слоя (feedback)
//         sTD2DInputs[1] = текстура штампов
//
// Текстура штампов — крошечный PNG (512x1, RGBA8), который каждым кадром пишет
// Python и подхватывает moviefileinTOP. На один штамп 4 тексела:
//    +0 : x_hi, x_lo, y_hi, y_lo       (координаты, 1/8 пикселя)
//    +1 : rad_hi, rad_lo, hardness, flow
//    +2 : r, g, b, 0
//    +3 : резерв
// Почему так, а не scriptTOP: вызов колбэков scriptTOP в конкретной сборке TD
// не подтвердился (текстура оставалась пустой), а этот путь состоит только из
// операторов, которые в этой же сборке уже работают.
//
// Ранний выход за пределами uRect: полный кадр не крутит цикл по всем штампам,
// а просто копирует предыдущее состояние.
//
// ОРИЕНТАЦИЯ. vUV в TOP-ах TD отсчитывается снизу (v=0 — низ изображения), а весь
// протокол клиента — сверху. Пересчёт делает pwY(). Признак переворота передаётся
// ЗНАКОМ uRes.y: положительный — обычная ориентация, отрицательный — инверсная.

#define MAXDABS 128

uniform vec4 uRes;    // (W, H*(±1), 1/W, 1/H)  — знак H задаёт вертикальную ориентацию
uniform vec4 uCount;  // (сколько штампов, режим, служебный флаг, множитель непрозрачности)
                      //  режим: 0 = краска, 1 = маска слоя «Источник», 2 = ластик
                      //  служебный флаг: 1 = очистить слой, 2 = залить маску целиком
uniform vec4 uColor;  // (r, g, b, a)
uniform vec4 uRect;   // (x, y, w, h) — изменённая область, пиксели, ЛЕВЫЙ ВЕРХ

out vec4 fragColor;

float pwY(float t){
    return uRes.y < 0.0 ? t : 1.0 - t;
}

// байт из текстуры -> 0..255
float b255(float v){
    return floor(clamp(v, 0.0, 1.0) * 255.0 + 0.5);
}

// два байта -> значение с шагом 1/8 (см. упаковку в paint_runtime.push_dabs)
float unpack16(float hi, float lo){
    return (b255(hi) * 256.0 + b255(lo)) * 0.125;
}

void main(){
    float W = abs(uRes.x), H = abs(uRes.y);
    vec2 p = vec2(vUV.s * W, pwY(vUV.t) * H);     // координаты пикселя, левый верх
    vec4 prev = texture(sTD2DInputs[0], vUV.st);

    if (uCount.z > 1.5){                          // заливка маски: «источник виден весь»
        fragColor = vec4(1.0);                    // белый с альфой 1 (премультиплицировано)
        return;
    }
    if (uCount.z > 0.5){                          // очистка слоя: ровно один кадр нулей
        fragColor = vec4(0.0);
        return;
    }
    if (p.x < uRect.x || p.y < uRect.y ||
        p.x > uRect.x + uRect.z || p.y > uRect.y + uRect.w){
        fragColor = prev;                         // вне изменённой области — не трогаем
        return;
    }

    int n = int(uCount.x + 0.5);
    float acc = 0.0;
    for (int i = 0; i < MAXDABS; i++){
        if (i >= n) break;
        int base = i * 4;
        vec4 t0 = texelFetch(sTD2DInputs[1], ivec2(base + 0, 0), 0);
        vec4 t1 = texelFetch(sTD2DInputs[1], ivec2(base + 1, 0), 0);
        vec4 t2 = texelFetch(sTD2DInputs[1], ivec2(base + 2, 0), 0);

        vec2 c = vec2(unpack16(t0.r, t0.g), unpack16(t0.b, t0.a));
        float rad = unpack16(t1.r, t1.g);
        float hardness = b255(t1.b) / 255.0;
        float flow = b255(t1.a) / 255.0;

        float d = distance(p, c);
        if (d <= rad && rad > 0.0){
            float inner = rad * mix(0.0, 0.98, clamp(hardness, 0.0, 1.0));
            float a = 1.0 - smoothstep(inner, rad, d);
            a *= flow;
            acc = acc + a * (1.0 - acc);           // накопление, как повторный source-over
        }
    }

    float op = uCount.w;
    vec4 o = prev;
    if (uCount.y < 0.5){                          // краска
        o.rgb = mix(prev.rgb, uColor.rgb, acc * uColor.a * op);
        o.a   = prev.a + acc * uColor.a * op * (1.0 - prev.a);
    } else if (uCount.y < 1.5){                   // маска слоя «Источник»
        // Маска хранится как премультиплицированный БЕЛЫЙ: RGB = альфа. Иначе
        // цвет кисти попал бы в RGB, а композит умножает источник на маску — и
        // картинка окрашивалась бы в цвет кисти. Цвету тут взяться неоткуда.
        float na = prev.a + acc * op * (1.0 - prev.a);
        o.rgb = vec3(na);
        o.a   = na;
    } else {                                      // ластик
        // Слой хранится с ПРЕМУЛЬТИПЛИРОВАННОЙ альфой: смешивание от
        // прозрачного чёрного даёт ровно rgb = color * a и rgb = color * a.
        // Поэтому стирать одному альфа-каналу нельзя: RGB останется на полную
        // силу, и композит (overTOP считает premultiplied) покажет краску как
        // будто ластик не сработал. Гасим обе части одинаково.
        float f = 1.0 - acc * uColor.a * op;
        o.rgb = prev.rgb * f;
        o.a   = prev.a * f;
    }
    fragColor = o;
}
