#!/usr/bin/env python3
"""Put the hat on Limelight's SystemCore model and check the fit.

    pip install cadquery-ocp numpy pillow
    kicad-cli pcb export step --subst-models --user-origin 150x150mm -f -o build/systemcore-hat.step systemcore-hat.kicad_pcb
    python fit_check.py SYSTEMCORE_SIMPLIFIED_STEP.step build/systemcore-hat.step [--render build/]

SYSTEMCORE_SIMPLIFIED_STEP.step: https://downloads.limelightvision.io/cad/SYSTEMCORE_SIMPLIFIED_STEP.step
The simplified model draws each SL header as a solid block (no cavity, no pins), so the sockets
are checked against the cavity from Molex drawing 1719710000-SD instead of the model.
"""
import math
import sys

import numpy as np
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepGProp import BRepGProp
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.GProp import GProp_GProps
from OCP.gp import gp_Trsf, gp_Vec
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS

sys.path.insert(0, __file__.rsplit("/", 1)[0])
sys.path.append("/usr/lib/python3/dist-packages")    # KiCad's pcbnew, needed by gen_hat's imports
import gen_hat as G  # noqa: E402  (geometry constants only)


def load(path):
    r = STEPControl_Reader()
    r.ReadFile(path)
    r.TransferRoots()
    return r.OneShape()


def solids(shape):
    out, e = [], TopExp_Explorer(shape, TopAbs_SOLID)
    while e.More():
        out.append(TopoDS.Solid(e.Current()))
        e.Next()
    return out


def bbox(s):
    b = Bnd_Box()
    BRepBndLib.Add_s(s, b)
    p, q = b.CornerMin(), b.CornerMax()
    return np.array([p.X(), p.Y(), p.Z(), q.X(), q.Y(), q.Z()])


def volume(s):
    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(s, g)
    return g.Mass()


def moved(s, dz):
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(0, 0, dz))
    return BRepBuilderAPI_Transform(s, t, True).Shape()


def classify(b, z_bot, z_top):
    if b[3] - b[0] > 100:
        return "board"
    if b[2] < z_bot - 5.0:
        return "socket"           # hangs below the board into SystemCore
    if b[2] >= z_top - 4.0 and b[5] > z_top + 10:
        return "lever"
    return "other"


def mesh(shape, tol=0.05):
    BRepMesh_IncrementalMesh(shape, tol, False, 0.3, True)
    V, F = [], []
    e = TopExp_Explorer(shape, TopAbs_FACE)
    while e.More():
        f = TopoDS.Face(e.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(f, loc)
        if tri is not None:
            tr, off = loc.Transformation(), len(V)
            for k in range(1, tri.NbNodes() + 1):
                p = tri.Node(k).Transformed(tr)
                V.append((p.X(), p.Y(), p.Z()))
            for k in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(k).Get()
                t = (a - 1 + off, b - 1 + off, c - 1 + off)
                F.append(t if f.Orientation() != TopAbs_REVERSED else (t[0], t[2], t[1]))
        e.Next()
    return np.array(V, float), np.array(F, int)


def render(parts, path, elev, azim, size=(1800, 1200), zoom=1.0, target=(0, 0, 12)):
    """Tiny z-buffer rasterizer: parts = [(V, F, rgb)]."""
    from PIL import Image
    W, H = size
    el, az = math.radians(elev), math.radians(azim)
    fwd = np.array([math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)])  # eye direction
    right = np.cross([0, 0, 1], fwd)
    if np.linalg.norm(right) < 1e-6:          # looking straight down: +X right, +Y up
        right = np.array([1.0, 0, 0])
    right /= np.linalg.norm(right)
    up = np.cross(fwd, right)
    light = fwd * 0.6 + up * 0.5 + right * 0.3
    light /= np.linalg.norm(light)
    scale = min(W, H) / 150.0 * zoom
    zbuf = np.full((H, W), -1e9)
    img = np.full((H, W, 3), 250.0)
    T = np.array(target, float)
    for V, F, rgb in parts:
        P = V - T
        sx = P @ right * scale + W / 2
        sy = H / 2 - P @ up * scale
        sz = P @ fwd
        tri = V[F]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
        shade = 0.35 + 0.65 * np.abs(n @ light)
        for i, (a, b, c) in enumerate(F):
            xs, ys, zs = sx[[a, b, c]], sy[[a, b, c]], sz[[a, b, c]]
            x0, x1 = max(int(xs.min()), 0), min(int(xs.max()) + 1, W - 1)
            y0, y1 = max(int(ys.min()), 0), min(int(ys.max()) + 1, H - 1)
            if x1 < x0 or y1 < y0:
                continue
            gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
            d = (ys[1] - ys[2]) * (xs[0] - xs[2]) + (xs[2] - xs[1]) * (ys[0] - ys[2])
            if abs(d) < 1e-9:
                continue
            l1 = ((ys[1] - ys[2]) * (gx - xs[2]) + (xs[2] - xs[1]) * (gy - ys[2])) / d
            l2 = ((ys[2] - ys[0]) * (gx - xs[2]) + (xs[0] - xs[2]) * (gy - ys[2])) / d
            l3 = 1 - l1 - l2
            m = (l1 >= 0) & (l2 >= 0) & (l3 >= 0)
            z = l1 * zs[0] + l2 * zs[1] + l3 * zs[2]
            sub = zbuf[y0:y1 + 1, x0:x1 + 1]
            u = m & (z > sub)
            sub[u] = z[u]
            img[y0:y1 + 1, x0:x1 + 1][u] = np.array(rgb) * shade[i]
    Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).save(path)
    print("rendered", path)


def main(sc_path, hat_path, render_dir=None):
    z0 = G.Z_HAT_BOTTOM
    sc = load(sc_path)
    raw = load(hat_path)
    # Seat the hat the way it really sits: socket open faces resting on the SL cavity floors.
    # (KiCad's STEP puts bottom-side parts on the outer copper/mask surface, ~0.09 mm below its 1.51 mm core.)
    hat = moved(raw, G.Z_SL_FLOOR - bbox(raw)[2])
    hb = bbox(hat)
    print(f"hat at z {hb[2]:.2f} .. {hb[5]:.2f}; board underside (outer surface) at {z0:.2f}")
    z_top = z0 + G.HAT_T
    groups = {"board": [], "socket": [], "lever": [], "other": []}
    for s in solids(hat):
        groups[classify(bbox(s), z0, z_top)].append(s)
    print({k: len(v) for k, v in groups.items()})
    ok = True

    # 1. nothing but sockets may touch SystemCore
    for kind in ("board", "lever", "other"):
        for s in groups[kind]:
            v = volume(BRepAlgoAPI_Common(s, sc).Shape())
            if v > 1e-3:
                ok = False
                print(f"  COLLISION {kind} bbox={np.round(bbox(s), 2)} volume={v:.3f} mm^3")
    print("1. board/levers vs SystemCore: " + ("no collisions" if ok else "COLLISIONS"))

    # 2. each socket must sit inside the matching SL header cavity (Molex 1719710000-SD)
    cav_w, wall_end = 3.07, 0.5
    floor = G.Z_SL_FLOOR
    socks = sorted((bbox(s) for s in groups["socket"]), key=lambda b: (b[0], b[1]))
    ports = sorted(G.SL_PORTS, key=lambda p: (p["X"], p["Yc"]))
    worst = 9
    for b, p in zip(socks, ports):
        n = len(p["pins"])
        A = {2: 7.38, 3: 10.08, 4: 12.62}[n]
        cx0, cx1 = p["X"] - cav_w / 2, p["X"] + cav_w / 2
        cy0, cy1 = p["Yc"] - (A / 2 - wall_end), p["Yc"] + (A / 2 - wall_end)
        side = min(b[0] - cx0, cx1 - b[3])
        end = min(b[1] - cy0, cy1 - b[4])
        worst = min(worst, side, end)
        good = side > 0.05 and end > 0.05
        ok &= good
        print(f"  {p['name']:5s} socket x[{b[0]:.2f},{b[3]:.2f}] y[{b[1]:.2f},{b[4]:.2f}] bottom z={b[2]:.2f} "
              f"| side clearance {side:.2f}  end clearance {end:.2f} {'ok' if good else 'TOO TIGHT'}")
    print(f"2. sockets inside SL cavities: worst clearance {worst:.2f} mm")
    sb = min(b[2] for b in socks)
    print(f"   socket open face at z={sb:.2f}, cavity floor z={floor:.2f}, "
          f"pin tip z={G.Z_SC_PCB + 9.0:.2f} -> engagement {G.Z_SC_PCB + 9.0 - sb:.2f} mm")

    # 3. height of SystemCore under the hat (everything the board does not have a cut-out for)
    V, F = mesh(sc, 0.05)
    tops = V[:, 2]
    print(f"3. SystemCore max z = {tops.max():.2f} (Ethernet/USB, under cut-outs); hat underside {z0:.2f}")
    print("RESULT:", "PASS" if ok else "FAIL")

    if render_dir:
        hv, hf = mesh(hat, 0.05)
        parts = [(V, F, (70, 74, 80))]
        colors = {"board": (30, 110, 60), "socket": (25, 25, 25), "lever": (215, 215, 210), "other": (200, 160, 60)}
        for kind, ss in groups.items():
            for s in ss:
                v, f = mesh(s, 0.05)
                parts.append((v, f, colors[kind]))
        render(parts, f"{render_dir}/fit_iso.png", 35, -60)
        render(parts, f"{render_dir}/fit_iso_back.png", 30, 120)
        render(parts, f"{render_dir}/fit_side.png", 0, -90, zoom=1.0)
        render(parts, f"{render_dir}/fit_top.png", 90, -90, zoom=1.05)
    return ok


if __name__ == "__main__":
    rd = sys.argv[sys.argv.index("--render") + 1] if "--render" in sys.argv else None
    sys.exit(0 if main(sys.argv[1], sys.argv[2], rd) else 1)
