from typing import List, Tuple, TypeAlias
from dataclasses import dataclass


class SExpr:
    Atom: type['SAtom'] = None # type: ignore
    Str: type['SStr'] = None # type: ignore
    Group: type['SGroup'] = None # type: ignore
    Seq: type['SSeq'] = None # type: ignore
    Map: type['SMap'] = None # type: ignore

# a | ab | define-test | 123 | 123.456 | 123. | .456
@dataclass
class SAtom(SExpr):
    value: str

# "a" | "ab" | "define-test" | "123" | "123.456" | "123." | ".456"
# #"\d+" | #"\d+.\*" ... (raw)
@dataclass
class SStr(SExpr):
    value: str

# (a b c)
@dataclass
class SGroup(SExpr):
    values: List[SExpr]

# [a b c] | [a, b, c] | []
@dataclass
class SSeq(SExpr):
    values: List[SExpr]

# { a : b, c : d }
@dataclass
class SMap(SExpr):
    values: List[Tuple[SExpr, SExpr]]

SExpr.Atom = SAtom
SExpr.Str = SStr
SExpr.Group = SGroup
SExpr.Seq = SSeq
SExpr.Map = SMap
