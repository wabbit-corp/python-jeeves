from typing import List, Tuple, Dict, Any, NewType
import typing

from dataclasses import dataclass
import dataclasses
import re
from collections import OrderedDict

from clj.types import SExpr, SAtom, SStr, SGroup, SSeq, SMap

@dataclass
class ExecutionContext:
    env: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def register(self, fn, name=None):
        import typing
        import inspect

        assert isinstance(fn, typing.Callable)
        # print(typing.get_type_hints(fn))
        # sig = inspect.signature(fn)
        # print(sig.parameters)
        name = name or fn.__name__
        self.env[name] = fn

Quoted = NewType('Quoted', SExpr)

@dataclass
class NativeFunction:
    fn: typing.Callable
    def __call__(self, ctx: ExecutionContext, *args: Any) -> Any:
        evaluated = [eval_sexpr(ctx, x) for x in args]
        return self.fn(*evaluated)


def eval_sexpr(ctx: ExecutionContext, e: SExpr | List[SExpr]) -> Any:
    if isinstance(e, list):
        return [eval_sexpr(ctx, x) for x in e]

    match e:
        case SAtom(a):
            if a.startswith('py.'):
                a = a[3:]
                module, attr = a.rsplit('/', 1)
                import importlib
                module_obj = importlib.import_module(module)
                result = getattr(module_obj, attr)
                if hasattr(result, '__call__'):
                    return NativeFunction(result)
                return result

            return ctx.env[a]

        case SStr(s):
            return s

        case SSeq(s):
            return [eval_sexpr(ctx, e) for e in s]

        case SMap(s):
            result = OrderedDict()
            for k, v in s:
                result[eval_sexpr(ctx, k)] = eval_sexpr(ctx, v)
            return result

        case SGroup(g):
            assert len(g) > 0
            fn = eval_sexpr(ctx, g[0])
            args = g[1:]
            return fn(ctx, *args)

