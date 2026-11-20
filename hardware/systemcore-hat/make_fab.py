#!/usr/bin/env python3
"""Fab outputs for JLCPCB / PCBWay from systemcore-hat.kicad_pcb (needs kicad-cli 9).

    python3 make_fab.py   -> fab/systemcore-hat-gerbers.zip, fab/bom.csv, fab/bom-jlc.csv, fab/cpl-jlc.csv,
                             fab/check-print-1to1.pdf
"""
import csv
import os
import shutil
import subprocess
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PCB = os.path.join(HERE, "systemcore-hat.kicad_pcb")
FAB = os.path.join(HERE, "fab")
GERB = os.path.join(FAB, "gerbers")

# value prefix -> (description, LCSC part, manufacturer part, alternates)
PARTS = {
    "1x2 H11": ("Female header 1x2 2.54 mm, 11.0 mm insulator, gold", "C41427517", "hanxia HX PM2.54-1x2P ZC H11"),
    "1x3 H11": ("Female header 1x3 2.54 mm, 11.0 mm insulator, gold", "C41427518", "hanxia HX PM2.54-1x3P ZC H11"),
    "1x4 H11": ("Female header 1x4 2.54 mm, 11.0 mm insulator, gold", "C41427519", "hanxia HX PM2.54-1x4P ZC H11"),
    "WAGO 2601-1102": ("Lever PCB terminal block, 2-pole, 3.5 mm, 26-16 AWG", "C7509568", "WAGO 2601-1102"),
    "WAGO 2601-1103": ("Lever PCB terminal block, 3-pole, 3.5 mm, 26-16 AWG", "C7472698", "WAGO 2601-1103"),
    "WAGO 2601-1104": ("Lever PCB terminal block, 4-pole, 3.5 mm, 26-16 AWG", "C7472748", "WAGO 2601-1104"),
}


def run(*args):
    subprocess.run(["kicad-cli", *args], check=True, stdout=subprocess.DEVNULL)


def main():
    shutil.rmtree(GERB, ignore_errors=True)
    os.makedirs(GERB)
    run("pcb", "export", "gerbers", "--layers", "F.Cu,B.Cu,F.Mask,B.Mask,F.SilkS,B.SilkS,Edge.Cuts",
        "--no-protel-ext", "-o", GERB + "/", PCB)
    run("pcb", "export", "drill", "--format", "excellon", "--drill-origin", "absolute", "--excellon-units", "mm",
        "--excellon-separate-th", "--generate-map", "--map-format", "gerberx2", "-o", GERB + "/", PCB)
    zpath = os.path.join(FAB, "systemcore-hat-gerbers.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(GERB)):
            z.write(os.path.join(GERB, f), f)
    print("wrote", zpath)

    # positions (also used for the BOM): KiCad's own export, then reshaped for JLC
    pos = os.path.join(FAB, "positions-kicad.csv")
    run("pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both", "--use-drill-file-origin",
        "-o", pos, PCB)
    rows = list(csv.DictReader(open(pos)))
    parts = [r for r in rows if any(r["Val"].startswith(k) for k in PARTS)]
    with open(os.path.join(FAB, "cpl-jlc.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        for r in parts:
            w.writerow([r["Ref"], r["PosX"] + "mm", r["PosY"] + "mm", "Top" if r["Side"] == "top" else "Bottom",
                        r["Rot"]])
    groups = {}
    for r in parts:
        key = next(k for k in PARTS if r["Val"].startswith(k))
        groups.setdefault(key, []).append(r["Ref"])
    with open(os.path.join(FAB, "bom-jlc.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Comment", "Designator", "Footprint", "LCSC Part #"])
        for key, refs in groups.items():
            fp = next(r["Package"] for r in parts if r["Ref"] == refs[0])
            w.writerow([PARTS[key][2], ",".join(refs), fp, PARTS[key][1]])
    with open(os.path.join(FAB, "bom.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Qty", "References", "Part", "Description", "LCSC", "Side"])
        for key, refs in groups.items():
            side = next(r["Side"] for r in parts if r["Ref"] == refs[0])
            w.writerow([len(refs), " ".join(refs), PARTS[key][2], PARTS[key][0], PARTS[key][1], side])
        w.writerow([1, "J15", "BRIDGE pigtail", "Half of a Micro-Fit+ MFP-R-4_MFP-R-4_DEVICE cable, soldered", "", "top"])
        w.writerow([4, "H1-H4", "2.0 mm spacer, M4/#8", "Between SystemCore lid and hat at the 4 corner screws", "", ""])
        w.writerow([1, "H5-H6", "Zip tie, <= 2.5 mm wide", "Strain relief for the BRIDGE pigtail", "", ""])
    os.remove(pos)
    print("wrote bom.csv, bom-jlc.csv, cpl-jlc.csv")

    # 1:1 print to lay on a SystemCore before ordering (top view: sockets, outline, cut-outs)
    run("pcb", "export", "pdf", "--layers", "Edge.Cuts,F.Fab,B.Fab,F.Cu", "--mode-single", "--black-and-white",
        "-o", os.path.join(FAB, "check-print-1to1.pdf"), PCB)
    print("wrote check-print-1to1.pdf")


if __name__ == "__main__":
    main()
