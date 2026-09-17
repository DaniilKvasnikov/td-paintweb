// PaintWeb — восстановление прямоугольной области слоя из PNG-патча (undo / redo).
//
// Входы:  sTD2DInputs[0] = текущее состояние слоя
//         sTD2DInputs[1] = патч области (картинка ровно того же размера, что uRect)
//
// Область внутри uRect заменяется целиком — вместе с альфой. Именно поэтому undo
// работает по маленькому PNG-региону, а не по полному снапшоту полотна.
//
// Ориентация — так же, как в brush.glsl: знак uRes.y задаёт вертикаль.

uniform vec4 uRes;    // (W, H*(±1), 1/W, 1/H)
uniform vec4 uRect;   // (x, y, w, h) в пикселях, ЛЕВЫЙ ВЕРХ

out vec4 fragColor;

float pwY(float t){
    return uRes.y < 0.0 ? t : 1.0 - t;
}

void main(){
    float W = abs(uRes.x), H = abs(uRes.y);
    vec2 p = vec2(vUV.s * W, pwY(vUV.t) * H);
    vec4 o = texture(sTD2DInputs[0], vUV.st);

    if (p.x >= uRect.x && p.y >= uRect.y &&
        p.x <  uRect.x + uRect.z && p.y < uRect.y + uRect.w){
        vec2 q = (p - uRect.xy) / uRect.zw;             // 0..1 внутри области, сверху вниз
        o = texture(sTD2DInputs[1], vec2(q.x, pwY(q.y)));
    }

    fragColor = o;
}
