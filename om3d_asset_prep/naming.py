# SPDX-License-Identifier: GPL-3.0-or-later
"""Naming convention helpers.

Pure Python (no bpy), so this module can be tested anywhere.
Default convention: SM_<Asset>_<Part>_low / SM_<Asset>_<Part>_high[_<Tag>]
<Part> is optional so single-piece props (SM_Crate_low) still pass.
"""
import re
from dataclasses import dataclass

TOKEN = r"[A-Za-z0-9]+"

DUP_SUFFIX = re.compile(r"\.\d{3,}$")

# Unreal collision prefixes. These follow Unreal's own rule, not ours.
COLLISION = re.compile(r"^U(CX|BX|SP|CP)_")

# Names other DCCs and importers hand out. Checked after stripping .001.
JUNK_PATTERNS = (
    # Maya
    re.compile(r"^(polySurface|pCube|pSphere|pCylinder|pPlane|pTorus|pCone|pPipe|"
               r"pHelix|pPyramid|pPrism|pDisc|pSolid|transform|group|null)\d*$"),
    # 3ds Max (Box001, Sphere012)
    re.compile(r"^(Box|Sphere|Cylinder|Plane|Teapot|Object|Line|Shape|Group|Torus|Cone)\d{2,}$"),
    # Generic importer names
    re.compile(r"^(Mesh|mesh|Geometry|geometry|Default|default|Node|node|Object|object|"
               r"Model|model|Untitled|untitled)([_.]?\d*)$"),
)

BLENDER_DEFAULTS = re.compile(
    r"^(Cube|Sphere|UVSphere|Cylinder|Plane|Icosphere|Torus|Cone|Circle|Suzanne|Grid|Monkey)$")


def strip_dup_suffix(name):
    return DUP_SUFFIX.sub("", name)


def has_dup_suffix(name):
    return bool(DUP_SUFFIX.search(name))


def is_collision_name(name):
    return bool(COLLISION.match(name))


def is_junk_name(name, include_blender_defaults=True):
    base = strip_dup_suffix(name)
    if any(p.match(base) for p in JUNK_PATTERNS):
        return True
    return include_blender_defaults and bool(BLENDER_DEFAULTS.match(base))


@dataclass(frozen=True)
class Convention:
    prefix: str = "SM_"
    sep: str = "_"
    low: str = "low"
    high: str = "high"
    require_suffix: bool = True

    def _parts(self):
        sep = self.sep or "_"
        low = self.low or "low"
        high = self.high or "high"
        return sep, low, high

    def regex(self):
        sep, low, high = self._parts()
        s, lo, hi = re.escape(sep), re.escape(low), re.escape(high)
        # A token that isn't the low/high keyword sitting on its own.
        word = rf"(?!(?:{lo}|{hi})(?:{s}|$)){TOKEN}"
        body = rf"(?P<asset>{word})(?:{s}(?P<part>{word}))?"
        suffix = rf"(?:(?P<low>{lo})|(?P<high>{hi})(?:{s}(?P<tag>{TOKEN}))?)"
        tail = rf"{s}{suffix}" if self.require_suffix else rf"(?:{s}{suffix})?"
        return re.compile(rf"^{re.escape(self.prefix)}{body}{tail}$")

    def parse(self, name):
        """Return {asset, part, role, tag} or None if the name doesn't fit."""
        m = self.regex().match(name)
        if not m:
            return None
        role = "low" if m.group("low") else ("high" if m.group("high") else None)
        return {"asset": m.group("asset"), "part": m.group("part"),
                "role": role, "tag": m.group("tag")}

    def matches(self, name):
        return self.parse(name) is not None

    def make(self, asset, part=None, role="low", tag=None):
        sep, low, high = self._parts()
        bits = [asset] + ([part] if part else [])
        if role:
            bits.append(low if role == "low" else high)
            if role == "high" and tag:
                bits.append(tag)
        return self.prefix + sep.join(bits)

    def describe(self):
        sep, low, high = self._parts()
        return f"{self.prefix}<Asset>{sep}<Part>{sep}{low} / {high}"
