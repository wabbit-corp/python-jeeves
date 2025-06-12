from __future__ import annotations
from typing import List, Tuple, TypeAlias, ClassVar
from dataclasses import dataclass


class SExpr:
    Atom: ClassVar[type["SAtom"]] = None  # type: ignore
    Str: ClassVar[type["SStr"]] = None  # type: ignore
    Group: ClassVar[type["SGroup"]] = None  # type: ignore
    Seq: ClassVar[type["SSeq"]] = None  # type: ignore
    Map: ClassVar[type["SMap"]] = None  # type: ignore


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
