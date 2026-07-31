"""
templates.py

Парсинг и рендер шаблонов строк L1/L2/L3.

Синтаксис:
    Обычный текст, {имя_переменной} для подстановки значения.
    {имя_переменной:N}     - обрезать/дополнить пробелами до ровно N символов
                              (для строк - "Rend" из "Konstantin" через {stream_user:4})
    {имя_переменной:.2f}   - любой другой спецификатор идёт напрямую в Python format()
                              (числа: .2f, 03d и т.п.)

Примеры:
    "Added: {recent_ago}"
    "{stream_user:4} {stream_progress}% {stream_mode}"
    "Movies {plex_movies}"
    "↓ {net1_rx}"

Если хоть одна переменная в шаблоне не резолвится (None - например net2 не
выбран, или это индекс за пределами активных стримов) - render() возвращает
all_resolved=False, чтобы screens.py мог решить не показывать такой экран.
"""

import re

import variables

TOKEN_RE = re.compile(r"\{([a-zA-Z0-9_]+)(?::([^}]*))?\}")


def parse_template(template: str):
    """Возвращает список токенов: ('text', str) или ('var', name, spec|None)."""
    tokens = []
    pos = 0
    for m in TOKEN_RE.finditer(template):
        if m.start() > pos:
            tokens.append(("text", template[pos:m.start()]))
        name = m.group(1)
        spec = m.group(2)
        tokens.append(("var", name, spec))
        pos = m.end()
    if pos < len(template):
        tokens.append(("text", template[pos:]))
    return tokens


def format_value(value, spec):
    if spec is None or spec == "":
        return str(value)
    if spec.isdigit():
        n = int(spec)
        s = str(value)
        return s[:n] if len(s) > n else s.ljust(n)
    try:
        return format(value, spec)
    except (ValueError, TypeError):
        return str(value)


def render(template: str, context: dict, index=None):
    """Рендерит шаблон. Возвращает (готовая_строка, all_resolved: bool)."""
    if not template:
        return "", True

    tokens = parse_template(template)
    out = []
    all_resolved = True

    for tok in tokens:
        if tok[0] == "text":
            out.append(tok[1])
        else:
            _, name, spec = tok
            value = variables.resolve(name, context, index)
            if value is None:
                all_resolved = False
                out.append("")
            else:
                out.append(format_value(value, spec))

    return "".join(out), all_resolved


def used_variables(template: str):
    """Список имён переменных, встречающихся в шаблоне - для валидации в веб-интерфейсе."""
    return [tok[1] for tok in parse_template(template) if tok[0] == "var"]


def validate_template(template: str):
    """Список опечаток - переменных, которых нет в реестре. Пустой список = всё ок."""
    return [name for name in used_variables(template) if name not in variables.VARIABLES]


def template_group(template: str):
    """
    Определяет, к какой "повторяющейся" группе относится шаблон (stream/recent/qbt),
    либо None, если шаблон использует только скалярные переменные (обычный,
    неповторяющийся экран). Если в одном шаблоне намешаны переменные из РАЗНЫХ
    повторяющихся групп - это ошибка конфигурации, возвращаем список для диагностики.
    """
    groups = set()
    for name in used_variables(template):
        spec = variables.VARIABLES.get(name)
        if spec and spec["group"] != "scalar":
            groups.add(spec["group"])
    return groups
