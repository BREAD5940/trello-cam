#!/usr/bin/env python3
"""Generate the SystemCore lever-terminal hat (KiCad 9).

Run with the Python that ships KiCad's pcbnew module:
    /usr/bin/python3 gen_hat.py            # place parts, outline, labels -> unrouted board + DSN
    /usr/bin/python3 gen_hat.py --ses X    # same, then import a Freerouting session and save routed board

Coordinates below are SystemCore coordinates in mm, taken from Limelight's
SYSTEMCORE_SIMPLIFIED_STEP.step (2026-11-19): origin at the centre of the box,
+X toward the Ethernet jack, +Y toward the CAN row, viewed from above.
KiCad's board frame is x = OX + X, y = OY - Y.
"""
import math
import os
import sys

import pcbnew
from pcbnew import VECTOR2I, FromMM, ToMM

HERE = os.path.dirname(os.path.abspath(__file__))
KICAD_FP = "/usr/share/kicad/footprints"
NAME = "systemcore-hat"
OX, OY = 150.0, 150.0

# --------------------------------------------------------------------------
# Assumptions to check before ordering (see README "Check before ordering")
# --------------------------------------------------------------------------
# The simplified STEP draws the 2-pin CAN headers as plain 6.6 x 7.38 mm blocks,
# so it does not show which side their latch ramp is on. Every SL header whose
# latch side is visible (6x SmartIO, 2x I2C, RSL) has it on +X, so the CAN ports
# are assumed to match. If yours face the other way, set this to -1.
CAN_LATCH_SIDE = +1

# SystemCore PCB top is at z = 11.29; Molex SL 171971 cavity floor is 2.9 above it.
# An 11.0 mm socket bottoming on that floor puts the hat underside at z = 25.18,
# 0.5 mm above the tallest rim (24.68). Kept here for the README and the 3D check.
Z_SC_PCB = 11.29
Z_SL_FLOOR = Z_SC_PCB + 2.89
SOCKET_H = 11.0
Z_HAT_BOTTOM = Z_SL_FLOOR + SOCKET_H
HAT_T = 1.6

# Board extents (SystemCore frame)
LEFT, RIGHT = -67.75, 67.75
TOP, BOT = 49.5, -51.1
CORNER_R = 2.0
TOP_STRIP_IN = 35.0     # inner edge of the top lever strip bodies
BOT_STRIP_IN = -36.6    # inner edge of the bottom lever strip bodies

# --------------------------------------------------------------------------
# SystemCore ports: pin-row X, centre Y, pin count, pin names (pin 1 first).
# Pin 1 sits at the low-Y end when the latch faces +X (Molex 1719710000-SD).
# --------------------------------------------------------------------------
CAN_BLOCK_CX = [-4.905, 8.795, 22.495, 36.195, 49.90]   # centre of 6.6 mm wide block
SL_PORTS = []
for i, cx in enumerate(CAN_BLOCK_CX):
    SL_PORTS.append(dict(name=f"CAN{i}", X=cx - 0.75 * CAN_LATCH_SIDE, Yc=28.545,
                         dirn=CAN_LATCH_SIDE, pins=["H", "L"]))
SL_PORTS.append(dict(name="RSL", X=-60.50, Yc=-21.72, dirn=+1, pins=["GND", "10V"]))
for i, (x, yc) in enumerate(zip([-47.42, -40.25, -33.08, -25.91, -18.74, -11.57],
                                [-27.50, -27.50, -27.50, -27.485, -27.50, -27.50])):
    SL_PORTS.append(dict(name=f"IO{i}", X=x, Yc=yc, dirn=+1, pins=["GND", "3V3", "SIG"]))
for i, x in enumerate([51.53, 58.70]):
    SL_PORTS.append(dict(name=f"I2C{i}", X=x, Yc=-18.94, dirn=+1, pins=["GND", "3V3", "SDA", "SCL"]))

BRIDGE_NETS = ["BR_CANH", "BR_CANL", "BR_VIN", "BR_GND"]
POWER_NETS = ["BR_VIN", "BR_GND"]

# Lever blocks: (ref, port, poles, side, body left X, nets left->right, pole letters)
LETTERS = {"H": "H", "L": "L", "GND": "G", "3V3": "V", "SIG": "S", "SDA": "D", "SCL": "C", "10V": "+"}
BLOCKS = []
for p in SL_PORTS:
    if p["name"].startswith("CAN"):
        BLOCKS.append((p["name"], 2, "top", p["X"] - 4.25,
                       [f"{p['name']}_{n}" for n in p["pins"]], [LETTERS[n] for n in p["pins"]]))
BLOCKS.append(("BRIDGE", 4, "top", -60.0, BRIDGE_NETS, ["H", "L", "+", "G"]))
BLOCKS.append(("RSL", 2, "bottom", -66.0, ["RSL_GND", "RSL_10V"], ["G", "+"]))
for i in range(6):
    BLOCKS.append((f"IO{i}", 3, "bottom", -55.5 + 14.0 * i,
                   [f"IO{i}_{n}" for n in ("GND", "3V3", "SIG")], ["G", "V", "S"]))
BLOCKS.append(("I2C0", 4, "bottom", 30.5, [f"I2C0_{n}" for n in ("GND", "3V3", "SDA", "SCL")], ["G", "V", "D", "C"]))
BLOCKS.append(("I2C1", 4, "bottom", 49.5, [f"I2C1_{n}" for n in ("GND", "3V3", "SDA", "SCL")], ["G", "V", "D", "C"]))

# BRIDGE pigtail solder pads (a cut-in-half official Micro-Fit+ device cable)
PIGTAIL_X = [-38.5, -35.0, -31.5, -28.0]
PIGTAIL_Y = 40.5
PIGTAIL_COLORS = ["YEL", "GRN", "RED", "BLK"]
ZIPTIE_HOLES = [(-42.5, 37.0), (-24.0, 37.0)]

# Cut-outs (SystemCore frame). Rects: (x0, x1, y0, y1, corner r)
WINDOWS = {
    "USB-A": (3.1, 47.4, -32.7, -14.5, 1.0),
    "DISPLAY": (-32.6, -3.4, -2.9, 14.9, 1.0),
    "BRIDGE + LINK": (-44.2, -12.8, 18.1, 34.3, 1.0),
}
ETH_NOTCH = (40.9, -5.3, 17.9)          # x_left, y_bottom, y_top; open to the right edge
BUTTON_HOLE = (-50.0, 28.34, 3.0)       # status light pipe / button on the lid
MOUNT_HOLES = [(sx * 64.0, sy * 32.0) for sx in (-1, 1) for sy in (-1, 1)]
MOUNT_DRILL = 4.5

# --------------------------------------------------------------------------


def P(x, y):
    return VECTOR2I(FromMM(OX + x), FromMM(OY - y))


def kxy(v):
    return ToMM(v.x) - OX, OY - ToMM(v.y)


board = None
nets = {}


def net(name):
    if name not in nets:
        n = pcbnew.NETINFO_ITEM(board, name)
        board.Add(n)
        nets[name] = n
    return nets[name]


def seg(a, b, layer=pcbnew.Edge_Cuts, w=0.1):
    s = pcbnew.PCB_SHAPE(board)
    s.SetShape(pcbnew.SHAPE_T_SEGMENT)
    s.SetStart(P(*a))
    s.SetEnd(P(*b))
    s.SetLayer(layer)
    s.SetWidth(FromMM(w))
    board.Add(s)


def arc(a, m, b, layer=pcbnew.Edge_Cuts, w=0.1):
    s = pcbnew.PCB_SHAPE(board)
    s.SetShape(pcbnew.SHAPE_T_ARC)
    s.SetArcGeometry(P(*a), P(*m), P(*b))
    s.SetLayer(layer)
    s.SetWidth(FromMM(w))
    board.Add(s)


def circle(c, r, layer=pcbnew.Edge_Cuts, w=0.1):
    s = pcbnew.PCB_SHAPE(board)
    s.SetShape(pcbnew.SHAPE_T_CIRCLE)
    s.SetCenter(P(*c))
    s.SetEnd(P(c[0] + r, c[1]))
    s.SetLayer(layer)
    s.SetWidth(FromMM(w))
    board.Add(s)


def rounded_poly(pts, radii, layer=pcbnew.Edge_Cuts, w=0.1):
    """Closed polygon of axis-aligned edges with a fillet radius per vertex."""
    n = len(pts)
    tangents = []
    for i in range(n):
        v = pts[i]
        a, b = pts[i - 1], pts[(i + 1) % n]
        r = radii[i]
        din = ((v[0] - a[0]), (v[1] - a[1]))
        dout = ((b[0] - v[0]), (b[1] - v[1]))
        li, lo = math.hypot(*din), math.hypot(*dout)
        din = (din[0] / li, din[1] / li)
        dout = (dout[0] / lo, dout[1] / lo)
        if r <= 0:
            tangents.append((v, v, None))
            continue
        t1 = (v[0] - r * din[0], v[1] - r * din[1])
        t2 = (v[0] + r * dout[0], v[1] + r * dout[1])
        c = (t1[0] + r * dout[0], t1[1] + r * dout[1])
        d = (v[0] - c[0], v[1] - c[1])
        dl = math.hypot(*d)
        mid = (c[0] + r * d[0] / dl, c[1] + r * d[1] / dl)
        tangents.append((t1, t2, mid))
    for i in range(n):
        t1, t2, mid = tangents[i]
        if mid is not None:
            arc(t1, mid, t2, layer, w)
        nxt = tangents[(i + 1) % n][0]
        if math.hypot(nxt[0] - t2[0], nxt[1] - t2[1]) > 1e-6:
            seg(t2, nxt, layer, w)


def rect(x0, x1, y0, y1, r, layer=pcbnew.Edge_Cuts, w=0.1):
    rounded_poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], [r] * 4, layer, w)


def text(s, x, y, size=1.0, layer=pcbnew.F_SilkS, rot=0, thick=None, just=None):
    t = pcbnew.PCB_TEXT(board)
    t.SetText(s)
    t.SetPosition(P(x, y))
    t.SetLayer(layer)
    t.SetTextSize(VECTOR2I(FromMM(size), FromMM(size)))
    t.SetTextThickness(FromMM(thick if thick else max(0.15, size * 0.15)))
    if rot:
        t.SetTextAngleDegrees(rot)
    if layer in (pcbnew.B_SilkS, pcbnew.B_Fab):
        t.SetMirrored(True)
    if just == "left":
        t.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_LEFT)
    elif just == "right":
        t.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_RIGHT)
    board.Add(t)
    return t


def set_model(fp, path, offset=(0, 0, 0), rot=(0, 0, 0)):
    models = fp.Models()
    while len(models):
        models.pop()
    m = pcbnew.FP_3DMODEL()
    m.m_Filename = path
    m.m_Offset = pcbnew.VECTOR3D(*offset)
    m.m_Rotation = pcbnew.VECTOR3D(*rot)
    m.m_Show = True
    models.append(m)


def hide_ref(fp):
    fp.Reference().SetVisible(False)


def make_hole_fp(mount):
    """NPTH hole: 4.5 mm for the M4/#8 SystemCore screws (Ø8 head keepout), or 2.5 mm zip-tie hole."""
    fp = pcbnew.FOOTPRINT(board)
    name = "MountingHole_4.5mm_Head8mm" if mount else "ZipTie_Hole_2.5mm"
    fp.SetFPID(pcbnew.LIB_ID("hat", name))
    fp.SetValue("M4/#8 screw" if mount else "zip tie")
    d = MOUNT_DRILL if mount else 2.5
    pad = pcbnew.PAD(fp)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(VECTOR2I(FromMM(d), FromMM(d)))
    pad.SetDrillSize(VECTOR2I(FromMM(d), FromMM(d)))
    pad.SetLayerSet(pad.UnplatedHoleMask())
    fp.Add(pad)
    cy = pcbnew.PCB_SHAPE(fp)
    cy.SetShape(pcbnew.SHAPE_T_CIRCLE)
    cy.SetCenter(VECTOR2I(0, 0))
    cy.SetEnd(VECTOR2I(FromMM(4.0 if mount else 1.5), 0))
    cy.SetLayer(pcbnew.F_CrtYd)
    cy.SetWidth(FromMM(0.05))
    fp.Add(cy)
    return fp


def make_pigtail_fp():
    """Four 1.5 mm plated holes (20/18 AWG) at 3.5 mm pitch for the BRIDGE pigtail wires."""
    fp = pcbnew.FOOTPRINT(board)
    fp.SetFPID(pcbnew.LIB_ID("hat", "Pigtail_1x04_P3.5mm_D1.5mm"))
    fp.SetValue("BRIDGE pigtail")
    for i in range(4):
        pad = pcbnew.PAD(fp)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetShape(pcbnew.PAD_SHAPE_RECT if i == 0 else pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(VECTOR2I(FromMM(2.8), FromMM(2.8)))
        pad.SetDrillSize(VECTOR2I(FromMM(1.5), FromMM(1.5)))
        pad.SetLayerSet(pad.PTHMask())
        pad.SetNumber(str(i + 1))
        pad.SetFPRelativePosition(VECTOR2I(FromMM(3.5 * i), 0))
        fp.Add(pad)
    cy = pcbnew.PCB_SHAPE(fp)
    cy.SetShape(pcbnew.SHAPE_T_RECT)
    cy.SetStart(VECTOR2I(FromMM(-1.65), FromMM(-1.65)))
    cy.SetEnd(VECTOR2I(FromMM(3.5 * 3 + 1.65), FromMM(1.65)))
    cy.SetLayer(pcbnew.F_CrtYd)
    cy.SetWidth(FromMM(0.05))
    fp.Add(cy)
    return fp


def write_project_library():
    """Save the custom footprints to ./hat.pretty and register it in the project fp-lib-table."""
    lib = os.path.join(HERE, "hat.pretty")
    os.makedirs(lib, exist_ok=True)
    io = pcbnew.PCB_IO_KICAD_SEXPR()
    for fp in (make_hole_fp(True), make_hole_fp(False), make_pigtail_fp()):
        fp.SetReference("REF**")
        io.FootprintSave(lib, fp)
    with open(os.path.join(HERE, "fp-lib-table"), "w") as f:
        f.write('(fp_lib_table\n  (version 7)\n  (lib (name "hat")(type "KiCad")(uri "${KIPRJMOD}/hat.pretty")'
                '(options "")(descr "SystemCore hat custom footprints"))\n)\n')


def build(grow=0.0):
    """grow > 0 enlarges the internal cut-outs; used only for the copy handed to the autorouter,
    which applies its 0.2 mm wire clearance to cut-outs, so the real board keeps >= 0.3 mm."""
    global board, nets
    nets = {}
    board = pcbnew.NewBoard(os.path.join(HERE, NAME + ".kicad_pcb"))
    ds = board.GetDesignSettings()
    ds.SetBoardThickness(FromMM(HAT_T))
    ds.m_TrackMinWidth = FromMM(0.2)
    ds.m_MinClearance = FromMM(0.2)
    ds.m_ViasMinSize = FromMM(0.6)
    ds.m_MinThroughDrill = FromMM(0.3)
    ds.m_CopperEdgeClearance = FromMM(0.3)    # JLC/PCBWay routed-edge minimum is 0.2-0.3 mm
    ds.m_HoleClearance = FromMM(0.25)
    ds.m_HoleToHoleMin = FromMM(0.25)
    ds.m_SilkClearance = FromMM(0.0)

    # ---- outline ------------------------------------------------------------
    ex, ey0, ey1 = ETH_NOTCH
    pts = [(LEFT, BOT), (RIGHT, BOT), (RIGHT, ey0), (ex, ey0), (ex, ey1), (RIGHT, ey1), (RIGHT, TOP), (LEFT, TOP)]
    radii = [CORNER_R, CORNER_R, 0.6, 1.0, 1.0, 0.6, CORNER_R, CORNER_R]
    rounded_poly(pts, radii)
    for name, (x0, x1, y0, y1, r) in WINDOWS.items():
        rect(x0 - grow, x1 + grow, y0 - grow, y1 + grow, r + grow)
    circle(BUTTON_HOLE[:2], BUTTON_HOLE[2] + grow)

    # ---- mounting + zip-tie holes -------------------------------------------
    for i, (x, y) in enumerate(MOUNT_HOLES + ZIPTIE_HOLES):
        fp = make_hole_fp(i < len(MOUNT_HOLES))
        fp.SetReference(f"H{i + 1}")
        fp.SetPosition(P(x, y))
        hide_ref(fp)
        board.Add(fp)

    # ---- sockets (bottom side, plug into SystemCore) -------------------------
    sock_lib = os.path.join(KICAD_FP, "Connector_PinSocket_2.54mm.pretty")
    socket_pins = {}
    for k, p in enumerate(SL_PORTS):
        n = len(p["pins"])
        fp = pcbnew.FootprintLoad(sock_lib, f"PinSocket_1x{n:02d}_P2.54mm_Vertical")
        fp.SetReference(f"J{k + 1}")
        fp.SetValue(f"1x{n} H11")
        y1 = p["Yc"] - p["dirn"] * (n - 1) / 2 * 2.54
        fp.SetOrientationDegrees(180 if p["dirn"] == +1 else 0)
        fp.SetPosition(P(p["X"], y1))
        set_model(fp, f"${{KIPRJMOD}}/3d/PinSocket_1x{n:02d}_P2.54mm_H11.step")
        board.Add(fp)
        fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        hide_ref(fp)
        for pad in fp.Pads():
            i = int(pad.GetNumber()) - 1
            pad.SetNet(net(f"{p['name']}_{p['pins'][i]}"))
            want = (p["X"], p["Yc"] + p["dirn"] * (i - (n - 1) / 2) * 2.54)
            got = kxy(pad.GetPosition())
            assert abs(got[0] - want[0]) < 1e-3 and abs(got[1] - want[1]) < 1e-3, (p["name"], i, got, want)
            socket_pins[(p["name"], p["pins"][i])] = got
        # bottom silk: port name beside the socket
        text(p["name"], p["X"] + (-3.2 if p["name"] == "I2C0" else 3.2), p["Yc"], 1.0, pcbnew.B_SilkS,
             rot=90)

    # ---- lever blocks (top side) --------------------------------------------
    wago_lib = os.path.join(KICAD_FP, "TerminalBlock_WAGO.pretty")
    block_info = []
    for k, (gname, n, side, xl, nets_lr, letters) in enumerate(BLOCKS):
        fp = pcbnew.FootprintLoad(wago_lib, f"TerminalBlock_WAGO_2601-11{n:02d}_1x{n:02d}_P3.50mm_Horizontal")
        fp.SetReference(f"TB{k + 1}")
        fp.SetValue(f"WAGO 2601-11{n:02d}")
        if side == "bottom":
            ox, oy, rot = xl + 2.44, BOT + 5.35, 0
            pole_x = [ox + 3.5 * j for j in range(n)]
            pole_net = {j + 1: nets_lr[j] for j in range(n)}
        else:
            ox, oy, rot = xl + 3.5 * (n - 1) + 2.56, TOP - 5.35, 180
            pole_x = [ox - 3.5 * j for j in range(n)]
            pole_net = {j + 1: nets_lr[n - 1 - j] for j in range(n)}
        fp.SetOrientationDegrees(rot)
        fp.SetPosition(P(ox, oy))
        set_model(fp, f"${{KIPRJMOD}}/3d/WAGO_2601-11{n:02d}.step")
        board.Add(fp)
        hide_ref(fp)
        for pad in fp.Pads():
            pad.SetNet(net(pole_net[int(pad.GetNumber())]))
        # the entry face sits on the board edge: drop the footprint silk drawn along it and
        # stop the side lines 0.3 mm short of the edge
        lim_hi, lim_lo = TOP - 0.3, BOT + 0.3
        for item in list(fp.GraphicalItems()):
            if item.GetLayer() != pcbnew.F_SilkS or item.GetClass() != "PCB_SHAPE":
                continue
            a, b = kxy(item.GetStart()), kxy(item.GetEnd())
            out = [y > lim_hi or y < lim_lo for y in (a[1], b[1])]
            if all(out) or item.GetShape() != pcbnew.SHAPE_T_SEGMENT:
                if any(out):
                    fp.Remove(item)
                continue
            for setter, pt, o in ((item.SetStart, a, out[0]), (item.SetEnd, b, out[1])):
                if o:
                    setter(P(pt[0], min(max(pt[1], lim_lo), lim_hi)))
        xs = sorted(pole_x)
        block_info.append((gname, side, xs, letters, xl, xl + 3.5 * (n - 1) + 5.0))

    # ---- BRIDGE pigtail pads -------------------------------------------------
    fp = make_pigtail_fp()
    fp.SetReference("J15")
    fp.SetPosition(P(PIGTAIL_X[0], PIGTAIL_Y))
    board.Add(fp)
    hide_ref(fp)
    for i, pad in enumerate(sorted(fp.Pads(), key=lambda q: int(q.GetNumber()))):
        pad.SetNet(net(BRIDGE_NETS[i]))

    # ---- labels -------------------------------------------------------------
    for gname, side, xs, letters, x0, x1 in block_info:
        cx = (x0 + x1) / 2
        if side == "bottom":
            if gname == "RSL":
                text("RSL", -59.0, -33.55, 1.0, thick=0.2)
            else:
                text(gname.replace("IO", "IO ").replace("I2C", "I2C "), cx, -33.55, 1.1, thick=0.2)
            for x, l in zip(xs, letters):
                text(l, x, -35.55, 1.0, thick=0.2)
        else:
            text(gname.replace("CAN", "CAN "), cx, 32.15, 1.1, thick=0.2)
            for x, l in zip(xs, letters):
                text(l, x, 34.0, 1.0, thick=0.2)
    for x, c, l in zip(PIGTAIL_X, PIGTAIL_COLORS, ["H", "L", "+", "G"]):
        text(c, x, 43.4, 0.9, thick=0.18)
        text(l, x, 44.9, 0.9, thick=0.18)
    text("BRIDGE PIGTAIL", (PIGTAIL_X[0] + PIGTAIL_X[-1]) / 2, 46.9, 1.0, thick=0.18)
    text("solder from cut Micro-Fit+ cable", (PIGTAIL_X[0] + PIGTAIL_X[-1]) / 2, 48.4, 0.8, thick=0.15)

    text("SYSTEMCORE LEVER HAT", -50.0, 12.0, 1.6, thick=0.3)
    text("v1  2026-11  untested", -50.0, 9.8, 1.0, thick=0.18)
    legend = ["G = GND     V = 3.3 V out", "S = signal  D = SDA  C = SCL", "H / L = CAN high / low",
              "+ = 10 V (RSL) / power in (BRIDGE)", "Strip 8-9 mm. 26-16 AWG."]
    for i, s in enumerate(legend):
        text(s, -64.5, 6.4 - 1.7 * i, 0.9, thick=0.16, just="left")
    text("DISPLAY", -18.0, -4.4, 1.0, thick=0.18)
    text("USB", 25.25, -12.8, 1.0, thick=0.18)
    text("ETHERNET", 54.0, 19.5, 1.0, thick=0.18)
    text("BRIDGE / LINK", -28.5, 16.6, 1.0, thick=0.18)
    text("SYSTEMCORE SIDE: sockets plug into the ports", -10.0, -7.0, 1.2, pcbnew.B_SilkS)
    text("2.0 mm spacers between lid and hat at the 4 screws", -10.0, -9.4, 1.0, pcbnew.B_SilkS)

    # SystemCore outline: full on F.Fab; on the bottom silk, broken where the Ethernet notch is
    rounded_poly([(-67.3, -35.3), (67.3, -35.3), (67.3, 35.3), (-67.3, 35.3)], [3.0] * 4, pcbnew.F_Fab, 0.12)
    sx, sy, r = 67.0, 35.0, 3.0
    for a, b in [((-sx + r, -sy), (sx - r, -sy)), ((-sx + r, sy), (sx - r, sy)), ((-sx, -sy + r), (-sx, sy - r)),
                 ((sx, -sy + r), (sx, ETH_NOTCH[1] - 0.6)), ((sx, ETH_NOTCH[2] + 0.6), (sx, sy - r))]:
        seg(a, b, pcbnew.B_SilkS, 0.12)
    k = r * (1 - 1 / math.sqrt(2))
    for cx, cy in [(-1, -1), (1, -1), (1, 1), (-1, 1)]:
        arc((cx * sx, cy * (sy - r)), (cx * (sx - k), cy * (sy - k)), (cx * (sx - r), cy * sy), pcbnew.B_SilkS, 0.12)

    # ---- zones: none. Every lever connects only to its own socket pin (1:1). --
    pcbnew.SaveBoard(os.path.join(HERE, NAME + ".kicad_pcb"), board)
    return socket_pins


SIGNAL_W_UM, POWER_W_UM = 300, 1500     # 0.3 mm signals; 1.5 mm for BRIDGE power (~4 A on 1 oz)


def export_dsn():
    """Export Specctra DSN, with a 'power' class (wider tracks) for the BRIDGE supply nets."""
    import re
    path = os.path.join(HERE, "build", NAME + ".dsn")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    assert pcbnew.ExportSpecctraDSN(board, path), "DSN export failed"
    s = open(path).read()
    s = s.replace("(rule\n      (width 200)", f"(rule\n      (width {SIGNAL_W_UM})", 1)
    m = re.search(r"\(class kicad_default (.*?)\(circuit", s, re.S)
    start, depth, i = m.start(), 0, m.start()
    while True:
        depth += {"(": 1, ")": -1}.get(s[i], 0)
        if depth == 0 and s[i] == ")":
            break
        i += 1
    sig = [n for n in m.group(1).split() if n not in POWER_NETS]
    default = s[start:i + 1].replace(m.group(1), " ".join(sig) + "\n      ")
    default = default.replace("(width 200)", f"(width {SIGNAL_W_UM})")
    power = (f"(class power {' '.join(POWER_NETS)}\n      (circuit\n        (use_via \"Via[0-1]_600:300_um\")\n"
             f"      )\n      (rule\n        (width {POWER_W_UM})\n        (clearance 300)\n      )\n    )")
    open(path, "w").write(s[:start] + default + "\n    " + power + s[i + 1:])
    return path


def route(dsn, jar):
    """Auto-route with Freerouting (tested with 2.1.0 on Java 21)."""
    import subprocess
    ses = dsn[:-4] + ".ses"
    with open(dsn[:-4] + ".freerouting.log", "w") as log:
        subprocess.run(["java", "-jar", jar, "-de", dsn, "-do", ses, "-mp", "40", "--gui.enabled=false"],
                       check=True, stdout=log, stderr=subprocess.STDOUT)
    return ses


if __name__ == "__main__":
    # usage: gen_hat.py [--route path/to/freerouting-2.1.0.jar]
    build(grow=0.12)
    dsn = export_dsn()
    print("DSN:", dsn)
    pins = build()
    write_project_library()
    print("placed", len(pins), "socket pins,", len(BLOCKS), "lever blocks")
    if "--route" in sys.argv:
        ses = route(dsn, sys.argv[sys.argv.index("--route") + 1])
        assert pcbnew.ImportSpecctraSES(board, ses), "SES import failed"
        pcbnew.SaveBoard(os.path.join(HERE, NAME + ".kicad_pcb"), board)
        print("routed and saved")
