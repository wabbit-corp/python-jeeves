from typing import List
from clj.types import SExpr, SAtom, SStr, SGroup, SSeq, SMap


class _Input:
    EOS = '\0'

    def __init__(self, str: str):
        self.str = str
        self.current = _Input.EOS if len(str) == 0 else str[0]
        self.line_number = 1
        self.line_offset = 0
        self.index = 0

    def next(self):
        if self.index < len(self.str):
            self.index += 1
            self.current = _Input.EOS if self.index >= len(self.str) else self.str[self.index]
        else:
            self.current = _Input.EOS

        if self.current == '\n':
            self.line_number += 1
            self.line_offset = 0

    def __repr__(self) -> str:
        start_index = max(0, self.index - 8)
        end_index = min(len(self.str), self.index + 8)

        chars = []
        for i in range(start_index, end_index):
            char = self.str[i]
            if char == ' ':
                char = '·'
            if char == '\n':
                char = '\\n'
            elif char == '\t':
                char = '\\t'
            elif not char.isprintable():
                char = f"\\x{ord(char):02x}"

            if i == self.index:
                chars.append(f"[{char}]")
            else:
                chars.append(char)

        snippet = ' '.join(chars)

        return f"Input({snippet})"

DEBUG_ENABLED = False
DEBUG_DEPTH = 0

# debug decorator
def debug(func):
    if not DEBUG_ENABLED:
        return func

    def wrapper(*args, **kwargs):
        global DEBUG_DEPTH
        saved_depth = DEBUG_DEPTH
        prefix = '  ' * DEBUG_DEPTH
        print(f"{prefix}{func.__name__}({', '.join(repr(x) for x in args)}, {kwargs}) {{")
        try:
            DEBUG_DEPTH += 1
            result = func(*args, **kwargs)
            return result
        except Exception as e:
            exception = e
            raise e
        finally:
            if 'exception' in locals():
                print(f"{prefix}}}raise {exception}")
            else:
                print(f"{'  ' * DEBUG_DEPTH}return {repr(result)}")
            DEBUG_DEPTH -= 1
            assert saved_depth == DEBUG_DEPTH
            print(f"{prefix}}}")
    return wrapper

@debug
def _parse_one_sexpr(input: _Input) -> SExpr:
    _skip_whitespace(input)
    c = input.current
    if   c == '(': return _parse_group(input)
    elif c == '[': return _parse_list(input)
    elif c == '"': return _parse_string(input)
    elif c == '#': return _parse_raw_string(input)
    elif c == '{': return _parse_map(input)
    else:          return _parse_atom(input)

@debug
def _skip_whitespace(input: _Input):
    while True:
        if input.current == _Input.EOS:
            break
        if input.current.isspace():
            input.next()
        elif input.current == ';':
            while input.current != '\n':
                input.next()
            input.next()
        else:
            break

@debug
def _parse_group(input: _Input) -> SGroup:
    assert input.current == '('
    input.next()
    values = []
    while input.current != ')':
        if input.current == _Input.EOS:
            break
        values.append(_parse_one_sexpr(input))
        _skip_whitespace(input)
    input.next()
    return SGroup(values)

@debug
def _parse_list(input: _Input) -> SSeq:
    assert input.current == '['
    input.next()
    values = []
    while input.current != ']':
        if input.current == _Input.EOS:
            raise ValueError("Unexpected end of input")
        values.append(_parse_one_sexpr(input))

        _skip_whitespace(input)
        if input.current == ',':
            input.next()
            _skip_whitespace(input)
    input.next()
    return SSeq(values)

@debug
def _parse_atom(input: _Input) -> SAtom:
    assert input.current not in [_Input.EOS, '(', ')', '[', ']', ':', ','], f'Unexpected character: {input.current} at {input.line_number}:{input.line_offset}'
    assert not input.current.isspace()

    value = ''
    while input.current not in [_Input.EOS, '(', ')', '[', ']', ':', ','] and not input.current.isspace():
        value += input.current
        input.next()

    _skip_whitespace(input)

    return SAtom(value)

@debug
def _parse_string(input: _Input) -> SStr:
    assert input.current == '"'
    input.next()

    value = ''
    while input.current != '"':
        if input.current == _Input.EOS:
            raise ValueError("Unexpected end of input")
        if input.current == '\\':
            input.next()
            match input.current:
                case 'n': value += '\n'
                case 't': value += '\t'
                case 'r': value += '\r'
                case '0': value += '\0'
                case '\\': value += '\\'
                case '"': value += '"'
                case _:
                    raise ValueError(f"Invalid escape sequence: '\\{input.current}'")
            input.next()
        else:
            value += input.current
            input.next()

    assert input.current == '"'
    input.next()

    _skip_whitespace(input)
    return SStr(value)

@debug
def _parse_raw_string(input: _Input) -> SStr:
    assert input.current == '#'
    input.next()
    tag = ''
    while input.current.isalnum():
        tag += input.current
        input.next()
    assert input.current == '"'
    input.next()
    value = ''

    while True:
        if input.current == _Input.EOS:
            raise ValueError("Unexpected end of input")
        if input.current != '"':
            value += input.current
            input.next()
            continue
        else:
            input.next()
            if tag == '': break

            count = 0
            while input.current == tag[count]:
                count += 1
                input.next()
                if count == len(tag):
                    break

            if count == len(tag):
                break

            value += '"' + tag[:count]
            continue

    _skip_whitespace(input)
    return SStr(value)

@debug
def _parse_map(input: _Input) -> SMap:
    assert input.current == '{'
    input.next()
    values = []
    while input.current != '}':
        if input.current == _Input.EOS:
            raise ValueError("Unexpected end of input")

        key = _parse_one_sexpr(input)

        _skip_whitespace(input)

        assert input.current == ':'
        input.next()

        value = _parse_one_sexpr(input)
        values.append((key, value))

        _skip_whitespace(input)
        if input.current == ',':
            input.next()
            _skip_whitespace(input)

    assert input.current == '}'
    input.next()
    _skip_whitespace(input)

    return SMap(values)

@debug
def sexpr(input: str) -> List[SExpr]:
    top_level: List[SExpr] = []
    input_r = _Input(input)
    _skip_whitespace(input_r)
    while input_r.current != _Input.EOS:
        top_level.append(_parse_one_sexpr(input_r))
        _skip_whitespace(input_r)
    return top_level

assert sexpr('(a b c)') == [SGroup([SAtom('a'), SAtom('b'), SAtom('c')])]
assert sexpr('[a b c]') == [SSeq([SAtom('a'), SAtom('b'), SAtom('c')])]
assert sexpr('a b c') == [SAtom('a'), SAtom('b'), SAtom('c')]
assert sexpr('a b (c d)') == [SAtom('a'), SAtom('b'), SGroup([SAtom('c'), SAtom('d')])]
assert sexpr('a b [c d]') == [SAtom('a'), SAtom('b'), SSeq([SAtom('c'), SAtom('d')])]
assert sexpr('a b (c d) [e f]') == [SAtom('a'), SAtom('b'), SGroup([SAtom('c'), SAtom('d')]), SSeq([SAtom('e'), SAtom('f')])]
assert sexpr('{ a : b }') == [SMap([(SAtom('a'), SAtom('b'))])]
assert sexpr('{ a : b, (a) : 1 }') == [SMap([
    (SAtom('a'), SAtom('b')),
    (SGroup([SAtom('a')]), SAtom('1'))
])]

assert sexpr('a "b c"') == [SAtom('a'), SStr('b c')]
assert sexpr('a "b c" d') == [SAtom('a'), SStr('b c'), SAtom('d')]
assert sexpr('a "b\\"c" d') == [SAtom('a'), SStr('b"c'), SAtom('d')]

assert sexpr('a #"b" c') == [SAtom('a'), SStr('b'), SAtom('c')]
assert sexpr('a #"" c') == [SAtom('a'), SStr(''), SAtom('c')]
assert sexpr('a #x"b""x c') == [SAtom('a'), SStr('b"'), SAtom('c')]
assert sexpr('a #tag"b""tag d') == [SAtom('a'), SStr('b"'), SAtom('d')]
