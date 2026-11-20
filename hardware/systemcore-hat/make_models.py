#!/usr/bin/env python3
"""Simple STEP models for parts that have none in KiCad 9's library.

Needs OpenCascade's Python bindings:  pip install cadquery-ocp
    python make_models.py            -> 3d/*.step

They are envelope models for fit checks and renders, not manufacturer CAD.
KiCad model frame: x as in the footprint, y = -footprint y, z up from the board surface.
"""
import os

from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "3d")


def box(x0, y0, z0, x1, y1, z1):
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), gp_Pnt(x1, y1, z1)).Shape()


def fuse(a, b):
    return BRepAlgoAPI_Fuse(a, b).Shape()


def cut(a, b):
    return BRepAlgoAPI_Cut(a, b).Shape()


def save(shape, name):
    w = STEPControl_Writer()
    w.Transfer(shape, STEPControl_AsIs)
    assert w.Write(os.path.join(OUT, name)) == IFSelect_RetDone
    print("wrote", name)


def socket(n, h=11.0):
    """2.54 mm single-row female header, insulator height h, pin 1 at the origin."""
    yb = -(2.54 * (n - 1) + 1.27)
    s = box(-1.25, yb, 0, 1.25, 1.27, h)
    for k in range(n):
        y = -2.54 * k
        s = cut(s, box(-0.5, y - 0.5, h - 6.5, 0.5, y + 0.5, h + 0.1))       # contact entry
        s = fuse(s, box(-0.32, y - 0.32, -3.2, 0.32, y + 0.32, 0.0))         # tail
    return s


def wago_2601_11(n):
    """WAGO 2601-11xx envelope: 3.5 mm pitch, 12.75 mm tall, wire entry on footprint +y (model -y)."""
    x0, x1 = -2.44, 3.5 * (n - 1) + 2.56
    s = box(x0, -5.35, 0, x1, 9.15, 12.75)
    for k in range(n):
        x = 3.5 * k
        s = cut(s, box(x - 1.2, -5.45, 1.5, x + 1.2, 1.5, 7.4))              # conductor entry
        s = fuse(s, box(x - 1.3, -7.43, 10.9, x + 1.3, 2.0, 12.75))          # lever, closed
        for y in (0.0, 5.0):
            s = fuse(s, box(x - 0.5, y - 0.25, -3.6, x + 0.5, y + 0.25, 0.0))
    return s


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for n in (2, 3, 4):
        save(socket(n), f"PinSocket_1x{n:02d}_P2.54mm_H11.step")
        save(wago_2601_11(n), f"WAGO_2601-11{n:02d}.step")
