"""Статическая проверка шейдеров: опечатки, скобки, точки с запятой.

    python paint/td/tests/test_shaders.py

Зачем: ошибка компиляции GLSL в TD выглядит как «ничего не рисуется», а поймать
её снаружи нечем — компилятора GLSL под рукой нет. Зато можно поймать то, что
даёт 90% таких ошибок: неизвестный идентификатор (опечатка в имени uniform-а или
переменной), несбалансированные скобки, пропущенная точка с запятой.

Проверяльщик сам проверяется на «сломанных» шейдерах-образцах: он обязан находить
подсунутые ошибки. Иначе он бесполезен.
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PAINT = os.path.dirname(os.path.dirname(HERE))

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

RESULTS = []


def check(name, cond, extra=''):
    RESULTS.append((bool(cond), name, extra))
    print('  %s %s%s' % ('ok  ' if cond else 'ПРОВАЛ', name,
                         ('   ' + str(extra)) if extra and not cond else ''))


TYPES = {'void', 'float', 'int', 'uint', 'bool', 'double',
         'vec2', 'vec3', 'vec4', 'ivec2', 'ivec3', 'ivec4',
         'bvec2', 'bvec3', 'bvec4', 'uvec2', 'uvec3', 'uvec4',
         'mat2', 'mat3', 'mat4', 'mat2x2', 'mat3x3', 'mat4x4',
         'sampler2D', 'sampler3D', 'sampler2DArray', 'samplerCube'}

KEYWORDS = {'if', 'else', 'for', 'while', 'do', 'break', 'continue', 'return',
            'discard', 'const', 'uniform', 'in', 'out', 'inout', 'layout',
            'struct', 'switch', 'case', 'default', 'true', 'false',
            'lowp', 'mediump', 'highp', 'inverse'}

BUILTINS = {
    'mix', 'smoothstep', 'clamp', 'step', 'distance', 'length', 'dot', 'cross',
    'normalize', 'abs', 'min', 'max', 'floor', 'ceil', 'fract', 'mod', 'pow',
    'exp', 'exp2', 'log', 'log2', 'sqrt', 'inversesqrt', 'sin', 'cos', 'tan',
    'asin', 'acos', 'atan', 'sinh', 'cosh', 'tanh', 'sign', 'round', 'trunc',
    'texture', 'textureLod', 'textureGrad', 'texelFetch', 'textureSize',
    'dFdx', 'dFdy', 'fwidth', 'all', 'any', 'not', 'isnan', 'isinf',
    'reflect', 'refract', 'faceforward', 'transpose', 'inverse', 'determinant',
    'radians', 'degrees', 'main',
    # встроенные имена TD, которые подставляет сам движок
    'sTD2DInputs', 'sTD3DInputs', 'sTD2DInputsSize', 'vUV', 'uTDOutputInfo',
    'uTDTexCoordInfo', 'uTDMat', 'TDOutputSwizzle', 'TDAlphaSwizzle',
    'gl_FragCoord', 'gl_FragDepth', 'gl_Position', 'gl_VertexID',
    'gl_InstanceID', 'gl_PointSize', 'gl_FrontFacing',
}

IDENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def strip_comments(src):
    src = re.sub(r'/\*.*?\*/', ' ', src, flags=re.S)
    src = re.sub(r'//[^\n]*', ' ', src)
    return src


TYPES_ALT = '|'.join(sorted(TYPES))
TYPE_HEAD = re.compile(r'\b(?:%s)\s+' % TYPES_ALT)


def _split_top_level(text):
    """Разбить по запятым верхнего уровня (запятые внутри скобок не считаются)."""
    parts, depth, cur = [], 0, ''
    for ch in text:
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(cur)
            cur = ''
        else:
            cur += ch
    parts.append(cur)
    return parts


def declared_names(src):
    """Имена, объявленные в шейдере: uniform-ы, локалки, функции, параметры, #define.

    Для каждого места, где встречается тип, берём текст до конца оператора и в
    каждой части через запятую смотрим ПЕРВЫЙ идентификатор — это позиция
    объявляемого имени. Так `float W = f(), H = g();` даёт и W, и H, а выражения
    в инициализаторах в «объявленные» не попадают.
    """
    names = set()
    for line in src.splitlines():
        m = re.match(r'\s*#\s*define\s+([A-Za-z_][A-Za-z0-9_]*)', line)
        if m:
            names.add(m.group(1))
    for m in re.finditer(r'\buniform\s+[A-Za-z_][A-Za-z0-9_]*\s+([A-Za-z_][A-Za-z0-9_]*)',
                         src):
        names.add(m.group(1))
    for m in TYPE_HEAD.finditer(src):
        stmt = src[m.end():].split(';')[0][:400]
        for part in _split_top_level(stmt):
            mm = re.match(r'\s*([A-Za-z_][A-Za-z0-9_]*)', part)
            if mm:
                names.add(mm.group(1))
    return names


def used_identifiers(src):
    """Идентификаторы кода: без чисел, без полей после точки, без директив."""
    out = []
    for line in src.splitlines():
        if line.lstrip().startswith('#'):
            continue
        for m in IDENT.finditer(line):
            start = m.start()
            prev = line[start - 1] if start > 0 else ''
            if prev == '.' or prev.isdigit():      # .xy / 1.0f — не идентификатор
                continue
            out.append(m.group(0))
    return out


def check_shader(text, label='shader'):
    """Вернуть список проблем в шейдере."""
    problems = []
    src = strip_comments(text)

    # скобки
    for opener, closer in (('{', '}'), ('(', ')'), ('[', ']')):
        if src.count(opener) != src.count(closer):
            problems.append('%s: несбалансированные %s%s (%d против %d)'
                            % (label, opener, closer, src.count(opener),
                               src.count(closer)))

    # точки с запятой: строка кода обязана кончаться на ; { } или ) { ... }
    # Незакрытая скобка к концу строки означает продолжение оператора на
    # следующей строке — такие строки не проверяем (многострочные условия if).
    depth = 0
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        depth += s.count('(') - s.count(')')
        if depth > 0:
            continue
        if s.endswith((';', '{', '}', ',')):
            continue
        if s.endswith(')') and ('if' in s or 'for' in s or 'while' in s):
            continue
        if s.startswith(('if', 'for', 'while', 'else')) and s.endswith(')'):
            continue
        problems.append('%s:%d: строка не заканчивается на ; { } — «%s»'
                        % (label, i, s[:60]))

    # обязательные части пиксельного шейдера
    if not re.search(r'\bout\s+vec4\s+fragColor\s*;', src):
        problems.append('%s: нет объявления "out vec4 fragColor;"' % label)
    if not re.search(r'\bvoid\s+main\s*\(', src):
        problems.append('%s: нет функции main()' % label)

    # неизвестные идентификаторы
    known = declared_names(src) | TYPES | KEYWORDS | BUILTINS
    unknown = []
    for name in used_identifiers(src):
        if name not in known and name not in unknown:
            unknown.append(name)
    for name in unknown:
        problems.append('%s: неизвестный идентификатор «%s» (опечатка?)' % (label, name))

    return problems


GOOD = """
#define MAXDABS 8
uniform vec4 uRes;
out vec4 fragColor;
float helper(float t){
    return 1.0 - t;
}
void main(){
    vec2 p = vec2(vUV.s, helper(vUV.t)) * uRes.xy;
    vec4 prev = texture(sTD2DInputs[0], vUV.st);
    float acc = 0.0;
    for (int i = 0; i < MAXDABS; i++){
        if (i >= 3) break;
        acc = acc + 1.0;
    }
    fragColor = prev + vec4(p, acc, 0.0);
}
"""

BROKEN_IDENT = GOOD.replace('uRes.xy', 'uRess.xy')
BROKEN_SEMI = GOOD.replace('float acc = 0.0;', 'float acc = 0.0')
BROKEN_BRACE = GOOD.replace('void main(){', 'void main(){ {')
BROKEN_NOOUT = GOOD.replace('out vec4 fragColor;', '')


def main():
    print('\n=== статическая проверка шейдеров =============================')
    print('\n[1] сами шейдеры проекта')
    # Шейдеров пять: кисть (ею же рисуется маска), восстановление области,
    # снятие премультипликации для картинок в браузер, применение маски к
    # источнику и слой монотонного цвета по маске. Композита среди них нет: он
    # собран встроенным overTOP.
    KNOWN_SHADERS = ('brush.glsl', 'restore.glsl', 'unpremult.glsl',
                     'maskapply.glsl', 'colapply.glsl')
    for name in KNOWN_SHADERS:
        path = os.path.join(PAINT, 'td', 'runtime', name)
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
        problems = check_shader(text, name)
        for p in problems:
            print('      ' + p)
        check('%s: чисто' % name, not problems, problems)
    extra = [n for n in os.listdir(os.path.join(PAINT, 'td', 'runtime'))
             if n.endswith('.glsl') and n not in KNOWN_SHADERS]
    check('лишних шейдеров не осталось', not extra, extra)

    print('\n[2] проверяльщик обязан находить подсунутые ошибки')
    check('образец без ошибок проходит', check_shader(GOOD, 'good') == [],
          check_shader(GOOD, 'good'))
    p = check_shader(BROKEN_IDENT, 'ident')
    check('опечатка в имени uniform-а найдена',
          any('uRess' in x for x in p), p)
    p = check_shader(BROKEN_SEMI, 'semi')
    check('пропущенная точка с запятой найдена',
          any('не заканчивается' in x for x in p), p)
    p = check_shader(BROKEN_BRACE, 'brace')
    check('несбалансированные скобки найдены',
          any('несбалансированные' in x for x in p), p)
    p = check_shader(BROKEN_NOOUT, 'noout')
    check('отсутствие fragColor найдено',
          any('fragColor' in x for x in p), p)
    p = check_shader(GOOD.replace('void main()', 'void mainn()'), 'nomain')
    check('отсутствие main найдено', any('main()' in x for x in p), p)

    print('\n[3] что проверяльщик намеренно не считает ошибкой')
    tricky = """
#define A 1
uniform vec4 uRes;   // комментарий с "кавычками" и {скобкой}
out vec4 fragColor;
void main(){
    float x = 1.0f;              // суффикс типа
    vec2 uv = vUV.st;            // поле .st
    ivec2 q = ivec2(0, 0);
    float v = texelFetch(sTD2DInputs[1], q, 0).r;
    if (x > 0.5) x = x * 2.0;
    for (int i = 0; i < A; i++) { x += 0.1; }
    fragColor = vec4(uv, v, x);
}
"""
    p = check_shader(tricky, 'tricky')
    check('комментарии, суффиксы, поля, циклы — без ложных срабатываний',
          not p, p)

    print('\n===============================================================')
    bad = [r for r in RESULTS if not r[0]]
    print('проверок: %d, провалов: %d' % (len(RESULTS), len(bad)))
    for _, name, extra in bad:
        print('  ПРОВАЛ: %s   %s' % (name, extra))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
