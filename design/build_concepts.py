"""Draw the three concept boards and the font specimen from committed artifacts.

Every figure is read from the canonical snapshot or the value-of-speed artifact;
nothing is typed by hand. Fonts are SIL OFL packages from ``node_modules``,
subset to the characters used and embedded in each SVG as WOFF2.

    python design/build_concepts.py      # writes design/concepts/*.svg + index.html
    node design/render.mjs               # PNG exports
"""

from __future__ import annotations

import base64
import glob
import html
import io
import json
import math
import re
from pathlib import Path

import numpy as np
from fontTools import subset

ROOT = Path(__file__).resolve().parents[1]
DESIGN = ROOT / "design"
OUT = DESIGN / "concepts"
FONTS = DESIGN / "node_modules" / "@fontsource"
SNAPSHOT_DIR = ROOT / "artifacts" / "snapshots" / "snap-a4c99d5a49a64ffd"
CANONICAL = "scn-07c647ec1459"
W = 1440

# ---------------------------------------------------------------------- data


def load() -> dict:
    snap = json.loads((SNAPSHOT_DIR / "snapshot.json").read_text(encoding="utf-8"))
    speed_path = Path(glob.glob(str(ROOT / "artifacts" / "structure" / "speed-*" / "speed.json"))[0])
    speed = json.loads(speed_path.read_text(encoding="utf-8"))
    merchants = {m["merchant_id"]: m for m in snap["merchants"]}
    scenario = next(s for s in snap["scenarios"] if s["scenario_id"] == CANONICAL)
    sp = next(s for s in speed["scenarios"] if s["scenario_id"] == CANONICAL)
    plan = sp["delays"][0]["frozen"]["actions"]
    keystones = [m["merchant_id"] for m in sorted(
        (m for m in merchants.values() if m["systemic_rank"] is not None),
        key=lambda m: m["systemic_rank"])[:5]]
    return {
        "snapshot_id": snap["provenance"]["run_id"],
        "n_scenarios": len(snap["scenarios"]),
        "speed_id": speed_path.parent.name,
        "speed_n": len(speed["scenarios"]),
        "speed_with_plan": sum(1 for s in speed["scenarios"] if s["has_plan"]),
        "merchants": merchants,
        "deps": snap["dependencies"],
        "scenario": scenario,
        "shock": scenario["shock"],
        "affected": scenario["affected_merchants"],
        "cf": scenario["counterfactual"],
        "plan": plan,
        "speed": sp,
        "points": speed["points"],
        "keystones": keystones,
    }


# ---------------------------------------------------------------- formatting


def crore(x: float) -> str:
    return f"₹{x / 1e7:,.2f} Cr"


def lakh(x: float) -> str:
    return f"₹{x / 1e5:,.2f} L"


def indian(x: float) -> str:
    s = str(int(round(x)))
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join([*groups, tail]) if groups else tail


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def short(h: str) -> str:
    return h[:17] if h.startswith("snap-") else h[:15]


# --------------------------------------------------------------------- fonts

SERIF = {
    "A": ("Source Serif 4", "source-serif-4"),
    "B": ("Libre Caslon Text", "libre-caslon-text"),
    "C": ("Spectral", "spectral"),
}
MONO = ("IBM Plex Mono", "ibm-plex-mono")
ASCII = "".join(chr(c) for c in range(0x20, 0x7F))


def _subset(path: Path, text: str) -> tuple[str, list[int]] | None:
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["*"]
    opts.name_IDs = ["*"]
    font = subset.load_font(str(path), opts)
    cmap = font.getBestCmap()
    cps = sorted({ord(ch) for ch in text} & set(cmap))
    if not cps:
        return None
    sub = subset.Subsetter(opts)
    sub.populate(unicodes=cps)
    sub.subset(font)
    buf = io.BytesIO()
    subset.save_font(font, buf, opts)
    return base64.b64encode(buf.getvalue()).decode(), cps


def font_faces(faces: list[tuple[str, str, int, str]], text: str) -> str:
    """``faces`` = (family, package, weight, style). One @font-face per subset file."""
    text = text + ASCII
    rules = []
    for family, pkg, weight, style in faces:
        for part in ("latin", "latin-ext"):
            path = FONTS / pkg / "files" / f"{pkg}-{part}-{weight}-{style}.woff2"
            if not path.exists():
                continue
            got = _subset(path, text)
            if got is None:
                continue
            data, cps = got
            ranges = ",".join(f"U+{c:04X}" for c in cps)
            rules.append(
                f"@font-face{{font-family:'{family}';font-weight:{weight};font-style:{style};"
                f"unicode-range:{ranges};src:url(data:font/woff2;base64,{data}) format('woff2')}}"
            )
    return "\n".join(rules)


def visible_text(svg: str) -> str:
    return html.unescape("".join(re.findall(r">([^<>]+)<", svg)))


def document(body: str, *, height: int, faces: list, style: str, title: str) -> str:
    probe = visible_text(body)
    css = font_faces(faces, probe)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{height}" '
        f'viewBox="0 0 {W} {height}" role="img" aria-label="{esc(title)}">\n'
        f"<title>{esc(title)}</title>\n<style>\n{css}\n{style}\n</style>\n{body}\n</svg>\n"
    )


# ------------------------------------------------------------------- drawing


def text(x, y, s, cls, anchor="start", extra=""):
    return f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}" text-anchor="{anchor}"{extra}>{esc(s)}</text>'


def line(x1, y1, x2, y2, cls="rule", extra=""):
    return f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" class="{cls}"{extra}/>'


def rect(x, y, w, h, cls, extra=""):
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" class="{cls}"{extra}/>'


GRAIN = """
<filter id="paper" x="0" y="0" width="100%" height="100%">
  <feTurbulence type="fractalNoise" baseFrequency="0.8" numOctaves="3" seed="2" result="n"/>
  <feColorMatrix in="n" type="luminanceToAlpha" result="a"/>
  <feComponentTransfer in="a" result="b"><feFuncA type="linear" slope="0.10" intercept="-0.02"/></feComponentTransfer>
  <feFlood flood-color="{ink}" result="c"/>
  <feComposite in="c" in2="b" operator="in"/>
</filter>
<filter id="grain" x="-2%" y="-2%" width="104%" height="104%">
  <feTurbulence type="fractalNoise" baseFrequency="0.045 0.16" numOctaves="4" seed="9" result="n"/>
  <feColorMatrix in="n" type="luminanceToAlpha" result="a"/>
  <feComponentTransfer in="a" result="b"><feFuncA type="linear" slope="0.85" intercept="-0.18"/></feComponentTransfer>
  <feFlood flood-color="{ink}" result="c"/>
  <feComposite in="c" in2="b" operator="in" result="speck"/>
  <feComposite in="speck" in2="SourceGraphic" operator="in" result="clipped"/>
  <feMerge><feMergeNode in="SourceGraphic"/><feMergeNode in="clipped"/></feMerge>
</filter>
"""


def hatch(pid: str, color: str, gap: int = 5, angle: int = 45, width: float = 1.0) -> str:
    return (f'<pattern id="{pid}" width="{gap}" height="{gap}" patternUnits="userSpaceOnUse" '
            f'patternTransform="rotate({angle})"><line x1="0" y1="0" x2="0" y2="{gap}" '
            f'stroke="{color}" stroke-width="{width}"/></pattern>')


# ---------------------------------------------------------------- the wall

COURSES = [("anchor", "I"), ("large", "II"), ("medium", "III"), ("small", "IV"), ("micro", "V")]


def wall_layout(d: dict, x0: float, y_base: float, width: float, scale: float = 1.0):
    """Stones in courses by tier, foundation at the bottom. Returns stones and course rows."""
    heights = {"anchor": 92, "large": 70, "medium": 58, "small": 46, "micro": 36}
    gap = 4 * scale
    stones, rows = [], []
    y = y_base
    for tier, numeral in COURSES:
        members = sorted((m for m in d["merchants"].values() if m["tier"] == tier),
                         key=lambda m: m["merchant_id"])
        chunks = [members] if len(members) <= 26 else [members[: len(members) // 2 + 1],
                                                       members[len(members) // 2 + 1:]]
        for i, chunk in enumerate(chunks):
            h = heights[tier] * scale
            y -= h + gap
            weights = [math.log10(max(m["liquidity_buffer"], 1e4)) - 3.2 for m in chunk]
            total = sum(weights)
            # stagger the joints of every other course, as masons do; the course is
            # laid wider by the offset and trimmed flush at both ends
            offset = (width / len(chunk)) * 0.35 if (len(rows) % 2) else 0.0
            avail = width + offset - gap * (len(chunk) - 1)
            x = x0 - offset
            row = []
            for m, wgt in zip(chunk, weights, strict=True):
                w = avail * wgt / total
                row.append({"id": m["merchant_id"], "x": x, "y": y, "w": w, "h": h, "m": m})
                x += w + gap
            stones.extend(row)
            rows.append({"tier": tier, "numeral": numeral if i == 0 else "", "y": y, "h": h,
                         "row": row})
    return stones, rows


def clip_wall(stones, x0, width):
    """Trim the staggered overhang so the wall has straight ends."""
    out = []
    for s in stones:
        x1, x2 = max(s["x"], x0), min(s["x"] + s["w"], x0 + width)
        if x2 - x1 > 3:
            out.append({**s, "x": x1, "w": x2 - x1})
    return out


def stone_path(s, r=2.2):
    x, y, w, h = s["x"], s["y"], s["w"], s["h"]
    # slightly irregular corners: deterministic per id
    k = int(s["id"][1:])
    j = [((k * 37 + i * 11) % 7) / 7.0 - 0.5 for i in range(4)]
    return (f"M{x + r:.1f},{y + j[0]:.1f} L{x + w - r:.1f},{y - j[1] * 0.6:.1f} "
            f"Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} L{x + w + j[2] * 0.6:.1f},{y + h - r:.1f} "
            f"Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} L{x + r:.1f},{y + h + j[3] * 0.6:.1f} "
            f"Q{x:.1f},{y + h:.1f} {x:.1f},{y + h - r:.1f} L{x:.1f},{y + r:.1f} Q{x:.1f},{y:.1f} "
            f"{x + r:.1f},{y:.1f} Z")


# ============================================================ BOARD A ======


def board_a(d: dict) -> str:
    ink, paper, accent, slate, bronze = "#15161d", "#eeeef4", "#b5462a", "#5d6474", "#8a6a3f"
    H = 2600
    parts = [f'<defs>{GRAIN.format(ink=ink)}{hatch("strain", ink, 5, 45, 1.1)}'
             f'{hatch("gapfill", accent, 6, -45, 0.8)}</defs>',
             rect(0, 0, W, H, "paper"), rect(0, 0, W, H, "paper", ' filter="url(#paper)"')]
    L, R = 96, W - 96

    # ---- masthead
    parts += [text(L, 70, "KEYSTONE", "eyebrow"),
              text(R, 70, "STRUCTURAL SURVEY OF A PAYMENT NETWORK  ·  SHEET 1 OF 5", "eyebrow", "end"),
              line(L, 88, R, 88, "rule-strong")]

    # ---- hero
    parts += [text(L, 200, "A payment network,", "h1"), text(L, 290, "surveyed as a structure.", "h1"),
              text(L, 350, "Merchants are stones, laid in courses by size. The bearings between them are",
                   "lede"),
              text(L, 384, "inferred from payment history alone. Pull one, and see what moves.", "lede")]
    wx, ww, wbase = 220, 1000, 940
    stones, rows = wall_layout(d, wx, wbase, ww)
    stones = clip_wall(stones, wx, ww)
    ks = set(d["keystones"])
    tone = {"anchor": slate, "large": "#6b7282", "medium": "#7a8090", "small": "#8a8f9d",
            "micro": "#9a9eab"}
    g = []
    for s in stones:
        fill = bronze if s["id"] in ks else tone[s["m"]["tier"]]
        g.append(f'<path d="{stone_path(s)}" fill="{fill}"/>')
    parts.append(f'<g filter="url(#grain)">{"".join(g)}</g>')
    for s in stones:
        if s["id"] in ks:
            cx = s["x"] + s["w"] / 2
            parts.append(f'<path d="M{cx - 8:.1f},{wbase + 34:.1f} L{cx + 8:.1f},{wbase + 34:.1f} '
                         f'L{cx:.1f},{wbase + 22:.1f} Z" class="mark"/>')
            parts.append(text(cx, s["y"] + s["h"] / 2 + 5, s["id"], "stone-label", "middle"))
    # ground line and course dimensions
    parts.append(line(wx - 40, wbase, wx + ww + 40, wbase, "rule-strong"))
    for i in range(14):
        x = wx - 40 + i * (ww + 80) / 13
        parts.append(line(x, wbase, x - 12, wbase + 12, "rule"))
    for row in rows:
        if row["numeral"]:
            top = row["y"]
            bottom = next(r for r in reversed(rows) if r["tier"] == row["tier"])
            y2 = row["y"] + row["h"] if bottom is row else row["y"] + row["h"]
            span = [r for r in rows if r["tier"] == row["tier"]]
            y_top = min(r["y"] for r in span)
            y_bot = max(r["y"] + r["h"] for r in span)
            parts += [line(wx - 54, y_top, wx - 54, y_bot, "dim"),
                      line(wx - 60, y_top, wx - 48, y_top, "dim"),
                      line(wx - 60, y_bot, wx - 48, y_bot, "dim"),
                      text(wx - 70, (y_top + y_bot) / 2 + 4, f"{row['numeral']} · {row['tier']}",
                           "course", "end")]
            del top, bottom, y2
    # keystone note in the right margin
    kx = wx + ww + 30
    parts += [text(kx, 470, "▲ Keystones", "note-head"),
              text(kx, 494, "The five stones whose", "note"),
              text(kx, 514, "failure the rest of the", "note"),
              text(kx, 534, "wall feels most.", "note"),
              text(kx, 562, "Systemic importance,", "note-i"),
              text(kx, 580, "replayed offline.", "note-i")]
    parts += [line(L, 1000, R, 1000, "rule"),
              text(L, 1026, "Fig. 1   Elevation, foundation to coping. Courses by tier; stone width by liquidity "
                   "buffer on a log scale; keystones by systemic importance.", "caption"),
              seal(R - 300, 1012, "INFERRED", f"{short(d['snapshot_id'])} · dependencies", accent)]

    # ---- plate II
    y0 = 1110
    sc = d["shock"]
    parts += [line(L, y0, R, y0, "rule-strong"),
              text(L, y0 + 40, "PLATE II", "eyebrow"),
              text(L, y0 + 100, "Pull a stone", "h2"),
              text(L, y0 + 140, f"{sc['origin_merchants'][0]} never receives {lakh(sc['magnitude'])} from "
                   f"m0008, due at hour {sc['onset_hours']:.0f}.", "lede")]
    # small wall with the pulled stone, strain and shoring
    px, pw, pbase = L, 700, y0 + 640
    stones2, rows2 = wall_layout(d, px, pbase, pw, scale=0.72)
    stones2 = clip_wall(stones2, px, pw)
    pulled = sc["origin_merchants"][0]
    shored = {a["merchant_id"]: a for a in d["plan"]}
    g = []
    for s in stones2:
        if s["id"] == pulled:
            continue
        fill = bronze if s["id"] in ks else tone[s["m"]["tier"]]
        g.append(f'<path d="{stone_path(s, 1.6)}" fill="{fill}"/>')
    parts.append(f'<g filter="url(#grain)">{"".join(g)}</g>')
    for s in stones2:
        if s["m"]["vulnerable"] and s["id"] != pulled:
            parts.append(f'<path d="{stone_path(s, 1.6)}" fill="url(#strain)" opacity="0.75"/>')
        if s["id"] == pulled:
            parts.append(f'<path d="{stone_path(s, 1.6)}" class="void"/>')
            parts.append(line(s["x"] + s["w"] / 2, s["y"] - 6, s["x"] + s["w"] / 2, s["y"] - 70, "leader"))
            parts.append(text(s["x"] + s["w"] / 2, s["y"] - 78, f"{pulled} pulled", "callout", "middle"))
        if s["id"] in shored:
            # a raking shore: a timber from a sole plate in front of the wall to the stone
            cx, cy = s["x"] + s["w"] / 2, s["y"] + s["h"] * 0.35
            foot = s["x"] + s["w"] + 34
            timber = f"M{foot:.1f},{pbase:.1f} L{cx + 10:.1f},{cy:.1f}"
            parts.append(f'<path d="{timber}" class="shore-halo"/><path d="{timber}" class="shore"/>')
            parts.append(f'<circle cx="{cx + 10:.1f}" cy="{cy:.1f}" r="4" class="shore-cleat"/>')
            parts.append(line(foot - 14, pbase, foot + 14, pbase, "shore-plate"))
            parts.append(text(cx - 6, s["y"] + s["h"] - 10, s["id"], "stone-label", "middle"))
    parts.append(line(px - 20, pbase, px + pw + 40, pbase, "rule-strong"))
    lx, ly = px, pbase + 46
    parts += [rect(lx, ly - 12, 22, 14, "legend-strain"), text(lx + 32, ly, "cover below 1 before the shock",
                                                                "legend"),
              f'<path d="M{lx + 280:.1f},{ly + 2:.1f} L{lx + 300:.1f},{ly - 12:.1f}" class="shore"/>',
              text(lx + 312, ly, "shoring: the recommended plan", "legend"),
              rect(lx + 560, ly - 12, 22, 14, "void"), text(lx + 592, ly, "pulled", "legend")]

    # result panel
    rx, ry, rw = 860, y0 + 200, R - 860
    cf = d["cf"]
    first = d["affected"][0]
    rows_a = [
        ("Merchants affected by this shock", f"{len(d['affected'])}"),
        ("Payments it delays", lakh(d["scenario"]["projected_impact"]["disrupted_value"])),
        ("First constraint", f"{first['time_to_constraint_hours']:.1f} h after"),
    ]
    rows_b = [
        ("Merchants missing a payment", f"{cf['baseline_affected']}"),
        ("— of which, without the shock", f"{cf['baseline_affected'] - len(d['affected'])}"),
    ]
    rows_c = [(f"Shoring  {a['merchant_id']}  (keystone, cover "
               f"{d['merchants'][a['merchant_id']]['cover_ratio']:.2f})", crore(a["amount_inr"]))
              for a in d["plan"]]
    rows_d = [
        ("Network disruption index", f"−{cf['disruption_reduction_pct']:.1f} %"),
        ("Payments kept on time", crore(cf["commerce_preserved"])),
        ("Merchants protected", f"{cf['merchants_protected']}"),
        ("Gap to the exact optimum", f"{100 * cf['optimality_gap']:.1f} %"),
    ]
    y = ry
    parts.append(text(rx, y, "THIS SHOCK", "eyebrow"))
    y += 14
    for label, value in rows_a:
        y += 34
        parts += [text(rx, y, label, "row"), text(rx + rw, y, value, "fig", "end"),
                  line(rx, y + 12, rx + rw, y + 12, "hair")]
    parts.append(text(rx, y + 34, "Point estimate; this snapshot carries no interval.", "note-i"))
    y += 82
    parts.append(text(rx, y, "THE WEEK AROUND IT", "eyebrow"))
    y += 14
    for label, value in rows_b + rows_c + rows_d:
        y += 34
        sub = label.startswith("— ")
        parts += [text(rx + (22 if sub else 0), y, label[2:] if sub else label,
                       "row-sub" if sub else "row"),
                  text(rx + rw, y, value, "fig", "end"),
                  line(rx, y + 12, rx + rw, y + 12, "hair")]
    parts.append(seal(rx, y + 30, "REPLAYED OFFLINE", f"{short(d['snapshot_id'])} · {d['n_scenarios']} scenarios",
                      accent))
    parts += [text(L, y0 + 744, "MARGIN NOTE", "eyebrow"),
              text(L, y0 + 770, "The bearings were estimated only from payments before hour 19;",
                   "note-i"),
              text(L, y0 + 790, "nothing the shock did is visible to the model that drew them.",
                   "note-i")]

    # cost of waiting
    parts.append(line(L, y0 + 850, R, y0 + 850, "rule"))
    parts += cost_chart(d, L, y0 + 900, R - L, ink=ink, accent=accent)
    parts += [line(L, H - 70, R, H - 70, "rule"),
              text(L, H - 40, "Synthetic network. Every figure is replayed offline inside the synthetic "
                   "generator; ₹ are analytic rupees; the disruption index is unitless.", "foot")]
    style = f"""
.paper{{fill:{paper}}}
.eyebrow{{font:500 12px 'IBM Plex Mono';letter-spacing:.16em;fill:{ink}}}
.h1{{font:600 76px 'Source Serif 4';letter-spacing:-.01em;fill:{ink}}}
.h2{{font:600 52px 'Source Serif 4';fill:{ink}}}
.lede{{font:italic 400 24px 'Source Serif 4';fill:#383a47}}
.rule{{stroke:{ink};stroke-width:.6;opacity:.55}}
.rule-strong{{stroke:{ink};stroke-width:1.2}}
.hair{{stroke:{ink};stroke-width:.4;opacity:.35}}
.dim{{stroke:{ink};stroke-width:.7}}
.course{{font:400 14px 'IBM Plex Mono';fill:{ink}}}
.stone-label{{font:500 11px 'IBM Plex Mono';fill:#f6f3ea}}
.mark{{fill:{accent}}}
.note-head{{font:600 16px 'Source Serif 4';fill:{ink}}}
.note{{font:400 15px 'Source Serif 4';fill:#383a47}}
.note-i{{font:italic 400 15px 'Source Serif 4';fill:#555867}}
.caption{{font:italic 400 15px 'Source Serif 4';fill:#383a47}}
.void{{fill:{paper};stroke:{ink};stroke-width:1;stroke-dasharray:4 3}}
.leader{{stroke:{ink};stroke-width:.8}}
.callout{{font:500 13px 'IBM Plex Mono';fill:{ink}}}
.shore{{stroke:{accent};stroke-width:4;fill:none;stroke-linecap:round}}
.legend{{font:400 14px 'Source Serif 4';fill:#383a47}}
.legend-strain{{fill:url(#strain);stroke:{ink};stroke-width:.6}}
.row{{font:400 16px 'Source Serif 4';fill:{ink}}}
.row-sub{{font:italic 400 15px 'Source Serif 4';fill:#555867}}
.shore-plate{{stroke:{accent};stroke-width:4}}
.shore-halo{{stroke:{paper};stroke-width:9;fill:none;stroke-linecap:round}}
.shore-cleat{{fill:{accent};stroke:{paper};stroke-width:2}}
.fig{{font:500 16px 'IBM Plex Mono';font-feature-settings:'tnum' 1;fill:{ink}}}
.foot{{font:italic 400 14px 'Source Serif 4';fill:#555867}}
.seal-ring{{fill:none;stroke:{accent};stroke-width:1.2}}
.seal-k{{font:600 10.5px 'IBM Plex Mono';letter-spacing:.14em;fill:{accent}}}
.seal-v{{font:400 10.5px 'IBM Plex Mono';fill:{accent}}}
.axis{{font:400 13px 'IBM Plex Mono';fill:#383a47}}
.frozen{{stroke:{ink};stroke-width:2;fill:none}}
.replanned{{stroke:{accent};stroke-width:2;fill:none;stroke-dasharray:6 4}}
.gap{{fill:url(#gapfill);opacity:.9}}
.dot-f{{fill:{ink}}}
.dot-r{{fill:{paper};stroke:{accent};stroke-width:2}}
.chart-title{{font:600 26px 'Source Serif 4';fill:{ink}}}
"""
    faces = [("Source Serif 4", "source-serif-4", 400, "normal"),
             ("Source Serif 4", "source-serif-4", 400, "italic"),
             ("Source Serif 4", "source-serif-4", 600, "normal"),
             ("IBM Plex Mono", "ibm-plex-mono", 400, "normal"),
             ("IBM Plex Mono", "ibm-plex-mono", 500, "normal"),
             ("IBM Plex Mono", "ibm-plex-mono", 600, "normal")]
    return document("\n".join(parts), height=H, faces=faces, style=style,
                    title="Concept A, The Structural Report")


def seal(x: float, y: float, kind: str, ref: str, color: str) -> str:
    """The provenance seal: a ruled tag naming the kind and the artifact."""
    w = 8 + 7.0 * max(len(kind) + 2, len(ref))
    return (f'<g><rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="38" rx="2" class="seal-ring"/>'
            f'<text x="{x + 10:.1f}" y="{y + 15:.1f}" class="seal-k">{esc("◆ " + kind)}</text>'
            f'<text x="{x + 10:.1f}" y="{y + 30:.1f}" class="seal-v">{esc(ref)}</text></g>')


def cost_chart(d, x, y, w, *, ink, accent):
    """Plate III in miniature: the canonical plan, frozen and re-planned, on a discrete rail."""
    sp = d["speed"]["delays"]
    delays = [p["delay_hours"] for p in sp]
    fz = [p["frozen"]["disruption_cut_pct"] for p in sp]
    rp = [p["replanned"]["disruption_cut_pct"] for p in sp]
    out = [text(x, y, "THE COST OF WAITING", "eyebrow"),
           text(x, y + 42, "The same plan, landing later", "chart-title"),
           line(x, y + 72, x + 34, y + 72, "frozen"),
           text(x + 44, y + 77, "plan as chosen at the shock", "legend"),
           line(x + 300, y + 72, x + 334, y + 72, "replanned"),
           text(x + 344, y + 77, "re-planned for the delay", "legend")]
    cx, cy, cw, ch = x + 50, y + 120, w - 480, 250
    ymax = 45.0

    def px(i):
        return cx + i * cw / (len(delays) - 1)

    def py(v):
        return cy + ch - v / ymax * ch

    for v in (0, 10, 20, 30, 40):
        out += [line(cx, py(v), cx + cw, py(v), "hair"), text(cx - 12, py(v) + 4, f"{v}", "axis", "end")]
    out.append(text(cx - 12, cy - 16, "% cut", "axis", "end"))
    poly = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(rp)) + " " + " ".join(
        f"{px(i):.1f},{py(v):.1f}" for i, v in reversed(list(enumerate(fz))))
    out.append(f'<polygon points="{poly}" class="gap"/>')
    out.append('<polyline points="' + " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(fz))
               + '" class="frozen"/>')
    out.append('<polyline points="' + " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(rp))
               + '" class="replanned"/>')
    for i, (a, b) in enumerate(zip(fz, rp, strict=True)):
        out += [f'<circle cx="{px(i):.1f}" cy="{py(a):.1f}" r="4" class="dot-f"/>',
                f'<rect x="{px(i) - 4:.1f}" y="{py(b) - 4:.1f}" width="8" height="8" class="dot-r"/>']
    # the rail: discrete stops, evenly spaced, because they are choices, not a time axis
    out.append(line(cx, cy + ch + 28, cx + cw, cy + ch + 28, "rule-strong"))
    for i, dl in enumerate(delays):
        out += [line(px(i), cy + ch + 22, px(i), cy + ch + 34, "rule-strong"),
                text(px(i), cy + ch + 54, f"{dl:g} h", "axis", "middle")]
    out.append(text(cx, cy + ch + 84, "Delay before the money lands: a scenario parameter, not a production "
                    "timing.", "note-i"))
    # annotation at 48 h, written in the empty field under the curve
    i48 = delays.index(48.0)
    at48 = sp[i48]["frozen"]
    share = at48["reduction"]["delay"] / at48["reduction"]["total"]
    ax, ay = px(i48), py(fz[i48])
    tx, ty = px(1) - 10, py(16)
    out += [f'<path d="M{ax - 6:.1f},{ay + 4:.1f} Q{ax - 60:.1f},{ty - 4:.1f} {tx + 330:.1f},{ty - 4:.1f}" '
            f'class="leader" fill="none"/>',
            text(tx, ty, f"At 48 h: {fz[i48]:.1f} % cut, and nothing kept on time.", "note"),
            text(tx, ty + 20, f"Payments made less late carry {100 * share:.0f} % of what remains.", "note-i")]
    # aggregate column
    lx = cx + cw + 70
    agg = {p["delay_hours"]: p for p in d["points"]}
    out += [text(lx, cy + 4, f"Across the {d['speed_with_plan']} benchmark scenarios", "note"),
            text(lx, cy + 24, "with a plan: mean cut, as chosen", "note"),
            text(lx, cy + 44, "[95 % interval over scenarios]", "note-i")]
    for k, dl in enumerate((0.0, 6.0, 24.0, 48.0, 72.0)):
        iv = agg[dl]["frozen_cut_pct"]
        out.append(text(lx, cy + 80 + 24 * k, f"{dl:>4g} h  {iv['mean']:5.1f} %  [{iv['low']:.1f}, {iv['high']:.1f}]",
                        "fig", extra=' xml:space="preserve"'))
    out.append(seal(lx, cy + 214, "REPLAYED OFFLINE", f"{d['speed_id']} · {d['speed_n']} scenarios", accent))
    return out


# ============================================================ BOARD B ======

TIER_ORDER = ["anchor", "large", "medium", "small", "micro"]
NUMERAL = dict(COURSES)

STAMP = """
<filter id="stamp" x="-5%" y="-20%" width="110%" height="140%">
  <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" seed="5" result="n"/>
  <feColorMatrix in="n" type="luminanceToAlpha" result="a"/>
  <feComponentTransfer in="a" result="m"><feFuncA type="discrete" tableValues="1 1 1 0.55 1 1 0.2 1"/></feComponentTransfer>
  <feComposite in="SourceGraphic" in2="m" operator="in"/>
</filter>
"""


def stamp(x, y, kind, ref, angle=-2.0):
    w = 24 + 7.4 * max(len(kind) + 2, len(ref))
    return (f'<g transform="rotate({angle} {x:.1f} {y:.1f})" filter="url(#stamp)">'
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="46" class="stamp-o"/>'
            f'<rect x="{x + 4:.1f}" y="{y + 4:.1f}" width="{w - 8:.1f}" height="38" class="stamp-i"/>'
            f'<text x="{x + w / 2:.1f}" y="{y + 20:.1f}" class="stamp-k" text-anchor="middle">{esc(kind)}</text>'
            f'<text x="{x + w / 2:.1f}" y="{y + 35:.1f}" class="stamp-v" text-anchor="middle">{esc(ref)}</text>'
            f'</g>')


def board_b(d: dict) -> str:
    ink, paper, blue, red, pencil = "#1f2128", "#f1ecdc", "#9fb5c6", "#a23b2c", "#6d6a62"
    H = 2680
    L, R = 120, W - 96
    parts = [f'<defs>{GRAIN.format(ink=ink)}{STAMP}</defs>',
             rect(0, 0, W, H, "paper"), rect(0, 0, W, H, "paper", ' filter="url(#paper)"'),
             line(78, 0, 78, H, "margin"), line(83, 0, 83, H, "margin")]
    parts += [text(L, 60, "KEYSTONE", "eyebrow"),
              text(R, 60, "GENERAL LEDGER OF A PAYMENT NETWORK  ·  FOLIO 1", "eyebrow", "end"),
              line(L, 76, R, 76, "red-rule"), line(L, 80, R, 80, "red-rule")]

    # ---- hero
    parts += [text(L, 196, "The books of a", "h1"), text(L, 284, "payment network.", "h1"),
              text(L, 344, "Every merchant an account; every dependency a standing entry, inferred from",
                   "lede"),
              text(L, 376, "payment history alone. Post a shock, and the books must reconcile.", "lede")]
    ordered = sorted(d["merchants"].values(),
                     key=lambda m: (TIER_ORDER.index(m["tier"]), m["merchant_id"]))
    top, pitch = 480, 5.7
    row_y = {m["merchant_id"]: top + i * pitch for i, m in enumerate(ordered)}
    xl, xr = 360, 1060
    ks = set(d["keystones"])
    parts += [text(xl - 4, top - 22, "ACCOUNT", "colhead", "end"),
              text(xr + 4, top - 22, "BEARS ON", "colhead")]
    for i in range(0, 104, 4):
        y = top - 2 + i * pitch
        parts.append(line(xl + 10, y, xr - 10, y, "ledger-rule"))
    plain, keyed = [], []
    for e in d["deps"]:
        y1, y2 = row_y[e["source_id"]] + 1.6, row_y[e["target_id"]] + 1.6
        seg = f"M{xl:.1f},{y1:.1f} L{xr:.1f},{y2:.1f}"
        (keyed if (e["source_id"] in ks or e["target_id"] in ks) else plain).append(seg)
    parts.append(f'<path d="{" ".join(plain)}" class="tie"/>')
    parts.append(f'<path d="{" ".join(keyed)}" class="tie-key"/>')
    for m in ordered:
        y = row_y[m["merchant_id"]]
        cls = "acct-key" if m["merchant_id"] in ks else "acct"
        parts += [rect(xl - 34, y, 30, 3.4, cls), rect(xr + 4, y, 30, 3.4, cls)]
    ky = [row_y[k] for k in sorted(ks)]
    parts += [line(xr + 40, min(ky), xr + 40, max(ky) + 3.4, "bracket-red"),
              text(xr + 50, min(ky) + 4, "keystone accounts", "acct-label"),
              text(xr + 50, min(ky) + 20, " · ".join(sorted(ks)[:3]), "acct-ids"),
              text(xr + 50, min(ky) + 35, " · ".join(sorted(ks)[3:]), "acct-ids")]
    # class brackets on the left, thumb-index tabs on the right
    for tier in TIER_ORDER:
        ys = [row_y[m["merchant_id"]] for m in ordered if m["tier"] == tier]
        y1, y2 = min(ys), max(ys) + 3.4
        parts += [line(250, y1, 250, y2, "bracket"), line(250, y1, 258, y1, "bracket"),
                  line(250, y2, 258, y2, "bracket"),
                  text(238, (y1 + y2) / 2 + 4, f"{NUMERAL[tier]}  {tier}", "class", "end",
                       ' xml:space="preserve"')]
        k = TIER_ORDER.index(tier)
        parts += [rect(R - 46, y1 - 1, 46, y2 - y1 + 2, "tab" if k % 2 == 0 else "tab-alt"),
                  text(R - 23, (y1 + y2) / 2 + 5, NUMERAL[tier], "tab-label", "middle")]
    parts += [line(L, 1090, R, 1090, "rule"),
              text(L, 1116, "Folio 1. Tie-out of standing entries. Each line is an inferred dependency: a "
                   "shortfall at the account on the left", "caption"),
              text(L, 1136, "passes to the account on the right. In red, the entries of the five keystone "
                   "accounts.", "caption"),
              stamp(R - 330, 1104, "INFERRED", f"{short(d['snapshot_id'])} · dependencies")]

    # ---- plate: the journal
    y0 = 1210
    sc, cf = d["shock"], d["cf"]
    pulled = sc["origin_merchants"][0]
    parts += [line(L, y0, R, y0, "red-rule"), line(L, y0 + 4, R, y0 + 4, "red-rule"),
              text(L, y0 + 44, "JOURNAL", "eyebrow"),
              text(L, y0 + 104, "Posting a shock", "h2"),
              text(L, y0 + 144, f"{pulled} never receives {lakh(sc['magnitude'])} from m0008, due at hour "
                   f"{sc['onset_hours']:.0f}.", "lede")]
    ty = y0 + 200
    cols = {"hour": L, "part": L + 96, "folio": 930, "dr": 1170, "cr": R}
    parts += [text(cols["hour"], ty, "HOUR", "colhead"), text(cols["part"], ty, "PARTICULARS", "colhead"),
              text(cols["folio"], ty, "FOLIO", "colhead"), text(cols["dr"], ty, "DR  ₹", "colhead", "end"),
              text(cols["cr"], ty, "CR  ₹", "colhead", "end")]
    onset = sc["onset_hours"]
    first = d["affected"][0]
    entries = [(f"{onset:.1f}", "Receivable from m0008 due, not received", pulled, "",
                indian(sc["magnitude"]))]
    for a in d["plan"]:
        cover = d["merchants"][a["merchant_id"]]["cover_ratio"]
        entries.append((f"{a['lands_at_hours']:.1f}",
                        f"Shoring: liquidity to {a['merchant_id']} (keystone, cover {cover:.2f})",
                        a["merchant_id"], indian(a["amount_inr"]), ""))
    entries.append((f"{onset + first['time_to_constraint_hours']:.1f}",
                    f"Memo: {pulled} first constrained, {first['time_to_constraint_hours']:.1f} h after the "
                    "shock", pulled, "", ""))
    y = ty + 14
    parts.append(line(L, y, R, y, "blue-rule"))
    for hour, part, folio, dr, cr in entries:
        y += 38
        cls = "entry-memo" if part.startswith("Memo") else "entry"
        parts += [text(cols["hour"], y - 12, hour, "fig"), text(cols["part"], y - 12, part, cls),
                  text(cols["folio"], y - 12, folio, "fig"),
                  text(cols["dr"], y - 12, dr, "fig", "end"), text(cols["cr"], y - 12, cr, "fig", "end"),
                  line(L, y, R, y, "blue-rule")]
    total = sum(a["amount_inr"] for a in d["plan"])
    y += 38
    parts += [text(cols["part"], y - 12, "Total shoring", "entry-total"),
              text(cols["dr"], y - 12, indian(total), "fig-total", "end"),
              line(cols["dr"] - 150, y - 34, cols["dr"], y - 34, "ink-rule"),
              line(cols["dr"] - 150, y, cols["dr"], y, "ink-rule"),
              line(cols["dr"] - 150, y + 4, cols["dr"], y + 4, "ink-rule")]
    for x in (cols["part"] - 12, cols["folio"] - 12, cols["dr"] - 140, cols["dr"] + 14):
        parts.append(line(x, ty - 20, x, y + 4, "red-col"))

    # reconciliation
    ry = y + 90
    parts += [text(L, ry, "RECONCILIATION", "eyebrow"),
              text(L, ry + 36, "The same week, twice, with the same random draws", "h3")]
    c1, c2, c3 = 860, 1100, R
    hy = ry + 80
    parts += [text(c1, hy, "WITHOUT SHORING", "colhead", "end"), text(c2, hy, "WITH SHORING", "colhead", "end"),
              text(c3, hy, "DIFFERENCE", "colhead", "end"), line(L, hy + 14, R, hy + 14, "blue-rule")]
    recon = [
        ("Payments late, ₹", indian(cf["baseline_disrupted_value"]),
         indian(cf["with_intervention_disrupted_value"]), f"{indian(cf['commerce_preserved'])} on time"),
        ("Merchants missing a payment", f"{cf['baseline_affected']}", f"{cf['with_intervention_affected']}",
         f"{cf['merchants_protected']} protected"),
        ("Network disruption index, units", f"{cf['baseline_disruption'] / 1e6:,.1f} M",
         f"{cf['with_intervention_disruption'] / 1e6:,.1f} M", f"−{cf['disruption_reduction_pct']:.1f} %"),
        ("Shoring, ₹", "—", indian(cf["cost"]), f"optimum gap {100 * cf['optimality_gap']:.1f} %"),
    ]
    y = hy + 14
    for label, a, b, c in recon:
        y += 38
        parts += [text(L, y - 12, label, "entry"), text(c1, y - 12, a, "fig", "end"),
                  text(c2, y - 12, b, "fig", "end"), text(c3, y - 12, c, "fig-red", "end"),
                  line(L, y, R, y, "blue-rule")]
    parts += [line(L, y + 4, R, y + 4, "ink-rule"),
              text(L, y + 34, "✓  Reconciled: the replay agrees with the snapshot exactly.", "check"),
              stamp(R - 340, y + 20, "REPLAYED OFFLINE",
                    f"{short(d['snapshot_id'])} · {d['n_scenarios']} scenarios", 1.5)]

    # schedule B: the cost of waiting
    sy = y + 130
    sp = d["speed"]["delays"]
    parts += [text(L, sy, "SCHEDULE B", "eyebrow"),
              text(L, sy + 36, "The cost of waiting", "h3"),
              text(L + 300, sy + 36, "Delay is a scenario parameter, not a production timing.", "caption")]
    hy = sy + 80
    sc_cols = {"delay": L, "fz": 470, "rp": 680, "kept": 920, "bar": 990}
    parts += [text(sc_cols["delay"], hy, "DELAY BEFORE THE MONEY LANDS", "colhead"),
              text(sc_cols["fz"], hy, "CUT, AS CHOSEN", "colhead", "end"),
              text(sc_cols["rp"], hy, "CUT, RE-PLANNED", "colhead", "end"),
              text(sc_cols["kept"], hy, "KEPT ON TIME  ₹", "colhead", "end"),
              line(L, hy + 14, R, hy + 14, "blue-rule")]
    y = hy + 14
    bw = R - sc_cols["bar"]
    for p in sp:
        y += 36
        f, r = p["frozen"], p["replanned"]
        hot = p["delay_hours"] == 48.0
        parts += [text(sc_cols["delay"], y - 11, f"{p['delay_hours']:g} h", "fig"),
                  text(sc_cols["fz"], y - 11, f"{f['disruption_cut_pct']:.2f} %", "fig", "end"),
                  text(sc_cols["rp"], y - 11, f"{r['disruption_cut_pct']:.2f} %", "fig", "end"),
                  text(sc_cols["kept"], y - 11, indian(f["payments_kept_on_time_inr"]),
                       "fig-red" if hot else "fig", "end"),
                  rect(sc_cols["bar"], y - 24, bw * f["disruption_cut_pct"] / 45, 6, "bar-f"),
                  rect(sc_cols["bar"], y - 15, bw * r["disruption_cut_pct"] / 45, 6, "bar-r"),
                  line(L, y, R, y, "blue-rule")]
    at48 = next(p["frozen"] for p in sp if p["delay_hours"] == 48.0)
    share = at48["reduction"]["delay"] / at48["reduction"]["total"]
    agg = {p["delay_hours"]: p["frozen_cut_pct"] for p in d["points"]}
    parts += [line(L, y + 4, R, y + 4, "ink-rule"),
              text(L, y + 34, f"At 48 h nothing is kept on time, yet the cut is "
                   f"{at48['disruption_cut_pct']:.1f} %: the payments are still late, but less late "
                   f"({100 * share:.0f} % of it).", "entry-red"),
              text(L, y + 60, f"Across the {d['speed_with_plan']} benchmark scenarios with a plan, the mean cut "
                   f"as chosen falls from {agg[0.0]['mean']:.1f} % [{agg[0.0]['low']:.1f}, "
                   f"{agg[0.0]['high']:.1f}] at 0 h to {agg[48.0]['mean']:.1f} % [{agg[48.0]['low']:.1f}, "
                   f"{agg[48.0]['high']:.1f}] at 48 h.", "caption"),
              rect(sc_cols["bar"], sy + 26, 26, 6, "bar-f"),
              text(sc_cols["bar"] + 34, sy + 33, "as chosen", "caption"),
              rect(sc_cols["bar"] + 140, sy + 26, 26, 6, "bar-r"),
              text(sc_cols["bar"] + 174, sy + 33, "re-planned", "caption"),
              stamp(R - 380, y + 84, "REPLAYED OFFLINE", f"{d['speed_id']} · {d['speed_n']} scenarios", -1.0)]
    parts += [line(L, H - 66, R, H - 66, "red-rule"),
              text(L, H - 38, "Synthetic network. Every figure is replayed offline inside the synthetic "
                   "generator; ₹ are analytic rupees in Indian grouping; the disruption index is unitless.",
                   "foot")]
    style = f"""
.paper{{fill:{paper}}}
.margin{{stroke:{red};stroke-width:1;opacity:.7}}
.eyebrow{{font:500 12px 'IBM Plex Mono';letter-spacing:.16em;fill:{ink}}}
.h1{{font:400 80px 'Libre Caslon Text';letter-spacing:-.01em;fill:{ink}}}
.h2{{font:400 54px 'Libre Caslon Text';fill:{ink}}}
.h3{{font:400 28px 'Libre Caslon Text';fill:{ink}}}
.lede{{font:italic 400 23px 'Libre Caslon Text';fill:#3d3c39}}
.rule{{stroke:{ink};stroke-width:.6;opacity:.5}}
.red-rule{{stroke:{red};stroke-width:1}}
.red-col{{stroke:{red};stroke-width:.8;opacity:.75}}
.blue-rule{{stroke:{blue};stroke-width:.8}}
.ledger-rule{{stroke:{blue};stroke-width:.5;opacity:.45}}
.ink-rule{{stroke:{ink};stroke-width:1}}
.colhead{{font:500 11px 'IBM Plex Mono';letter-spacing:.12em;fill:{pencil}}}
.tie{{stroke:{pencil};stroke-width:.5;opacity:.42;fill:none}}
.tie-key{{stroke:{red};stroke-width:.9;opacity:.8;fill:none}}
.acct{{fill:{ink};opacity:.75}}
.acct-key{{fill:{red}}}
.acct-label{{font:italic 400 14px 'Libre Caslon Text';fill:{red}}}
.acct-ids{{font:500 11px 'IBM Plex Mono';fill:{red}}}
.bracket-red{{stroke:{red};stroke-width:1}}
.bracket{{stroke:{ink};stroke-width:.8}}
.class{{font:400 13px 'IBM Plex Mono';fill:{ink}}}
.tab{{fill:#e2dcc6;stroke:{ink};stroke-width:.5}}
.tab-alt{{fill:#d8d1b7;stroke:{ink};stroke-width:.5}}
.tab-label{{font:400 15px 'Libre Caslon Text';fill:{ink}}}
.caption{{font:italic 400 15px 'Libre Caslon Text';fill:#4a4843}}
.entry{{font:400 17px 'Libre Caslon Text';fill:{ink}}}
.entry-memo{{font:italic 400 17px 'Libre Caslon Text';fill:#4a4843}}
.entry-total{{font:700 17px 'Libre Caslon Text';fill:{ink}}}
.entry-red{{font:italic 400 17px 'Libre Caslon Text';fill:{red}}}
.fig{{font:400 16px 'IBM Plex Mono';font-feature-settings:'tnum' 1;fill:{ink}}}
.fig-total{{font:600 16px 'IBM Plex Mono';font-feature-settings:'tnum' 1;fill:{ink}}}
.fig-red{{font:500 16px 'IBM Plex Mono';font-feature-settings:'tnum' 1;fill:{red}}}
.check{{font:italic 400 17px 'Libre Caslon Text';fill:{red}}}
.bar-f{{fill:{ink}}}
.bar-r{{fill:none;stroke:{red};stroke-width:1.2}}
.stamp-o{{fill:none;stroke:{red};stroke-width:2}}
.stamp-i{{fill:none;stroke:{red};stroke-width:.8}}
.stamp-k{{font:600 12px 'IBM Plex Mono';letter-spacing:.18em;fill:{red}}}
.stamp-v{{font:400 11px 'IBM Plex Mono';fill:{red}}}
.foot{{font:italic 400 14px 'Libre Caslon Text';fill:#4a4843}}
"""
    faces = [("Libre Caslon Text", "libre-caslon-text", 400, "normal"),
             ("Libre Caslon Text", "libre-caslon-text", 400, "italic"),
             ("Libre Caslon Text", "libre-caslon-text", 700, "normal"),
             ("IBM Plex Mono", "ibm-plex-mono", 400, "normal"),
             ("IBM Plex Mono", "ibm-plex-mono", 500, "normal"),
             ("IBM Plex Mono", "ibm-plex-mono", 600, "normal")]
    return document("\n".join(parts), height=H, faces=faces, style=style,
                    title="Concept B, The Ledger Room")


# ============================================================ BOARD C ======

_LAYOUT: dict[str, tuple[float, float]] = {}


def chart_layout(d: dict) -> dict[str, tuple[float, float]]:
    """Unit-square positions by payment distance: classical MDS on hop counts.

    Coordinates are blended with their ranks so dense clusters open up; the
    chart says plainly that positions are not geography.
    """
    if _LAYOUT:
        return _LAYOUT
    touched = {e["source_id"] for e in d["deps"]} | {e["target_id"] for e in d["deps"]}
    ids = sorted(m for m in d["merchants"] if m in touched)
    n, idx = len(ids), {m: i for i, m in enumerate(ids)}
    adj: list[set[int]] = [set() for _ in ids]
    for e in d["deps"]:
        a, b = idx[e["source_id"]], idx[e["target_id"]]
        adj[a].add(b)
        adj[b].add(a)
    dist = np.full((n, n), np.inf)
    for s0 in range(n):
        dist[s0, s0], frontier, k = 0, [s0], 0
        while frontier:
            k += 1
            nxt = []
            for u in frontier:
                for v in adj[u]:
                    if dist[s0, v] == np.inf:
                        dist[s0, v] = k
                        nxt.append(v)
            frontier = nxt
    finite = dist[np.isfinite(dist)].max()
    dist[~np.isfinite(dist)] = finite + 2
    j = np.eye(n) - 1.0 / n
    b = -0.5 * j @ (dist ** 2) @ j
    vals, vecs = np.linalg.eigh(b)
    order = np.argsort(vals)[::-1][:2]
    x = vecs[:, order] * np.sqrt(np.maximum(vals[order], 0))
    for k in range(2):
        col = x[:, k]
        if col[idx[d["keystones"][0]]] > np.median(col):  # fix the sign so the chart is stable
            col = -col
        z = (col - col.min()) / (col.max() - col.min())
        r = np.argsort(np.argsort(col, kind="stable"), kind="stable") / (n - 1)
        x[:, k] = 0.4 * z + 0.6 * r
    _LAYOUT.update({m: (float(x[idx[m], 0]), float(x[idx[m], 1])) for m in ids})
    return _LAYOUT


def chart_body(d, fx, fy, fw, fh, *, pad=60, currents=True, labels=True, sigma=0.07, cls=""):
    """Shoals, contours, currents and soundings inside one frame. Returns (parts, positions)."""
    import contourpy

    unit = chart_layout(d)
    pos = {m: (fx + pad + u * (fw - 2 * pad), fy + pad + v * (fh - 2 * pad)) for m, (u, v) in unit.items()}
    islands = sorted(m for m in d["merchants"] if m not in unit)
    xs = np.linspace(fx, fx + fw, 220)
    ys = np.linspace(fy, fy + fh, int(220 * fh / fw))
    gx, gy = np.meshgrid(xs, ys)
    z = np.zeros_like(gx)
    s2 = (sigma * fw) ** 2
    for m, (px_, py_) in pos.items():
        cover = d["merchants"][m]["cover_ratio"]
        strain = 0.0 if cover is None else max(0.0, 1.0 - cover)
        if strain:
            z += strain * np.exp(-((gx - px_) ** 2 + (gy - py_) ** 2) / (2 * s2))
    gen = contourpy.contour_generator(xs, ys, z, fill_type="OuterCode", line_type="Separate")
    levels = [f * z.max() for f in (0.12, 0.28, 0.5, 0.75)]
    out = []
    tints = ["shoal-1", "shoal-2", "shoal-3", "shoal-4"]
    for k, lo in enumerate(levels):
        hi = levels[k + 1] if k + 1 < len(levels) else z.max() * 1.01
        points, codes = gen.filled(lo, hi)
        for pts, cds in zip(points, codes, strict=True):
            seg = []
            for (px_, py_), c in zip(pts, cds, strict=True):
                seg.append(("M" if c == 1 else "L") + f"{px_:.1f},{py_:.1f}")
                if c == 79:
                    seg.append("Z")
            out.append(f'<path d="{"".join(seg)}" class="{tints[k]}" fill-rule="evenodd"/>')
    for lv in levels:
        for ln in gen.lines(lv):
            out.append('<polyline points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in ln)
                       + '" class="contour"/>')
    if currents:
        strong = sorted(e["pass_through"] for e in d["deps"])[len(d["deps"]) // 2]
        for e in d["deps"]:
            (x1, y1), (x2, y2) = pos[e["source_id"]], pos[e["target_id"]]
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            dx, dy = x2 - x1, y2 - y1
            cx_, cy_ = mx - dy * 0.18, my + dx * 0.18
            wdt = 0.4 + 2.0 * e["pass_through"]
            out.append(f'<path d="M{x1:.1f},{y1:.1f} Q{cx_:.1f},{cy_:.1f} {x2:.1f},{y2:.1f}" '
                       f'class="current{cls}" stroke-width="{wdt:.2f}"/>')
            if e["pass_through"] >= strong:
                t = 0.58
                qx = (1 - t) ** 2 * x1 + 2 * (1 - t) * t * cx_ + t ** 2 * x2
                qy = (1 - t) ** 2 * y1 + 2 * (1 - t) * t * cy_ + t ** 2 * y2
                tx = 2 * (1 - t) * (cx_ - x1) + 2 * t * (x2 - cx_)
                ty = 2 * (1 - t) * (cy_ - y1) + 2 * t * (y2 - cy_)
                ang = math.degrees(math.atan2(ty, tx))
                out.append(f'<path d="M-4,-3.5 L2,0 L-4,3.5" class="chevron" '
                           f'transform="translate({qx:.1f},{qy:.1f}) rotate({ang:.1f})"/>')
    radius = {"anchor": 4.6, "large": 3.8, "medium": 3.1, "small": 2.5, "micro": 2.0}
    ks = set(d["keystones"])
    # merchants with no inferred dependency: a row of islands, labelled as such
    ix, iy = fx + 26, fy + fh - 30
    for k, m in enumerate(islands):
        out.append(f'<circle cx="{ix + k * 11:.1f}" cy="{iy:.1f}" r="{radius[d["merchants"][m]["tier"]]}" '
                   f'class="island"/>')
    out.append(text(ix, iy - 12, "no dependency inferred", "island-label"))
    for m, (px_, py_) in pos.items():
        if m in ks:
            continue
        out.append(f'<circle cx="{px_:.1f}" cy="{py_:.1f}" r="{radius[d["merchants"][m]["tier"]]}" '
                   f'class="sounding"/>')
    for m in sorted(ks):
        px_, py_ = pos[m]
        star = " ".join(f"{px_ + (9 if i % 2 == 0 else 3.6) * math.sin(math.pi * i / 5):.1f},"
                        f"{py_ - (9 if i % 2 == 0 else 3.6) * math.cos(math.pi * i / 5):.1f}"
                        for i in range(10))
        out.append(f'<path d="M{px_:.1f},{py_:.1f} q10,-4 22,-20 q-16,6 -22,20 z" class="flare"/>')
        out.append(f'<polygon points="{star}" class="light"/>')
        if labels:
            out.append(text(px_ + 12, py_ + 18, m, "light-label"))
    return out, pos


def neatline(fx, fy, fw, fh, step=52):
    out = [rect(fx - 12, fy - 12, fw + 24, fh + 24, "neat-outer"), rect(fx, fy, fw, fh, "neat-inner")]
    k = 0
    for x in np.arange(fx, fx + fw, step):
        w = min(step, fx + fw - x)
        if k % 2 == 0:
            out += [rect(x, fy - 8, w, 4, "neat-bar"), rect(x, fy + fh + 4, w, 4, "neat-bar")]
        k += 1
    k = 0
    for y in np.arange(fy, fy + fh, step):
        h = min(step, fy + fh - y)
        if k % 2 == 0:
            out += [rect(fx - 8, y, 4, h, "neat-bar"), rect(fx + fw + 4, y, 4, h, "neat-bar")]
        k += 1
    for x in np.arange(fx + 2 * step, fx + fw, 2 * step):
        out.append(line(x, fy, x, fy + fh, "graticule"))
    for y in np.arange(fy + 2 * step, fy + fh, 2 * step):
        out.append(line(fx, y, fx + fw, y, "graticule"))
    return out


def compass(cx, cy, r):
    out = [f'<circle cx="{cx}" cy="{cy}" r="{r}" class="rose"/>',
           f'<circle cx="{cx}" cy="{cy}" r="{r - 10}" class="rose"/>']
    for i in range(72):
        a = math.radians(i * 5)
        l1 = r - (10 if i % 6 == 0 else 5)
        out.append(line(cx + l1 * math.sin(a), cy - l1 * math.cos(a), cx + r * math.sin(a),
                        cy - r * math.cos(a), "rose-tick"))
    for i in range(8):
        a = math.radians(i * 45)
        long_ = r - 14 if i % 2 == 0 else r * 0.55
        side = 7 if i % 2 == 0 else 5
        tip = (cx + long_ * math.sin(a), cy - long_ * math.cos(a))
        l_ = (cx + side * math.sin(a - math.pi / 2), cy - side * math.cos(a - math.pi / 2))
        r_ = (cx + side * math.sin(a + math.pi / 2), cy - side * math.cos(a + math.pi / 2))
        out.append(f'<polygon points="{tip[0]:.1f},{tip[1]:.1f} {l_[0]:.1f},{l_[1]:.1f} {cx},{cy}" '
                   f'class="rose-dark"/>')
        out.append(f'<polygon points="{tip[0]:.1f},{tip[1]:.1f} {r_[0]:.1f},{r_[1]:.1f} {cx},{cy}" '
                   f'class="rose-light"/>')
    out.append(text(cx, cy - r - 10, "N", "rose-n", "middle"))
    return out


def board_c(d: dict) -> str:
    ink, paper, accent, sepia, blue = "#1d2430", "#efeee8", "#a23b2c", "#7a5c3e", "#3f6880"
    H = 2900
    L, R = 96, W - 96
    parts = [f'<defs>{GRAIN.format(ink=ink)}</defs>', rect(0, 0, W, H, "paper"),
             rect(0, 0, W, H, "paper", ' filter="url(#paper)"'),
             text(L, 58, "KEYSTONE", "eyebrow"),
             text(R, 58, "SURVEY CHART OF A PAYMENT NETWORK  ·  SHEET 1", "eyebrow", "end")]
    # ---- hero: the chart
    parts += [text(W / 2, 150, "SURVEY CHART", "carto-k", "middle"),
              text(W / 2, 222, "of a Payment Network", "carto-t", "middle"),
              line(W / 2 - 220, 252, W / 2 + 220, 252, "rule"),
              text(W / 2, 284, "Positions by payment distance, not geography. Shoals gather where merchants are "
                   "short of cover;", "carto-n", "middle"),
              text(W / 2, 308, "currents are inferred dependencies; lights mark the keystones.", "carto-n",
                   "middle")]
    fx, fy, fw, fh = L + 12, 360, R - L - 24, 820
    parts.append(rect(fx, fy, fw, fh, "water"))
    parts += compass(fx + fw - 110, fy + 120, 72)
    body, _ = chart_body(d, fx, fy, fw, fh, pad=70)
    parts += body + neatline(fx, fy, fw, fh)
    parts += [text(L, fy + fh + 52, "Chart 1. Shoal tints are contours of how far merchants fall short of "
                   "cover, smoothed; currents are heavier where more of a shortfall passes on.", "caption"),
              seal(R - 300, fy + fh + 30, "INFERRED", f"{short(d['snapshot_id'])} · dependencies", accent)]

    # ---- plate: track of a shock
    y0 = 1320
    sc, cf = d["shock"], d["cf"]
    pulled = sc["origin_merchants"][0]
    parts += [line(L, y0, R, y0, "rule-strong"), text(L, y0 + 40, "SHEET 2", "eyebrow"),
              text(L, y0 + 100, "Track of a shock", "h2"),
              text(L, y0 + 140, f"{pulled} never receives {lakh(sc['magnitude'])} from m0008, due at hour "
                   f"{sc['onset_hours']:.0f}.", "lede")]
    px0, py0, pw, ph = L + 12, y0 + 190, 700, 600
    parts.append(rect(px0, py0, pw, ph, "water"))
    body, pos = chart_body(d, px0, py0, pw, ph, pad=46, labels=False, cls="-faint")
    parts += body + neatline(px0, py0, pw, ph, step=40)
    wx, wy = pos[pulled]
    parts += [f'<circle cx="{wx:.1f}" cy="{wy:.1f}" r="17" class="danger"/>',
              line(wx - 11, wy, wx + 11, wy, "wreck"),
              *[line(wx + k, wy - 5, wx + k, wy + 5, "wreck") for k in (-6, 0, 6)],
              text(wx + 22, wy - 12, f"{pulled}", "wreck-label"),
              text(wx + 22, wy + 4, "receivable lost, hour 19", "wreck-note")]
    hx, hy = px0 + 22, py0 + ph * 0.5
    parts += [rect(hx - 8, hy - 8, 16, 16, "harbour"), text(hx - 6, hy + 28, "funding", "wreck-note")]
    for a in d["plan"]:
        tx_, ty_ = pos[a["merchant_id"]]
        mx_, my_ = (hx + tx_) / 2, (hy + ty_) / 2 + (40 if ty_ > hy else -40)
        parts += [f'<path d="M{hx:.1f},{hy:.1f} Q{mx_:.1f},{my_:.1f} {tx_:.1f},{ty_:.1f}" class="route"/>',
                  f'<circle cx="{tx_:.1f}" cy="{ty_:.1f}" r="11" class="route-end"/>',
                  rect(tx_ - 150, ty_ + 14, 138, 20, "route-tag"),
                  text(tx_ - 144, ty_ + 28, f"{a['merchant_id']}  {crore(a['amount_inr'])}", "route-label",
                       extra=' xml:space="preserve"')]
    parts.append(text(px0, py0 + ph + 40, "Dashed: settlement routes of the recommended plan, to two keystones "
                      "already short of cover.", "caption"))

    # notice to mariners
    nx, ny, nw = 860, y0 + 190, R - 860
    parts += [rect(nx - 18, ny - 18, nw + 36, 640, "notice"),
              text(nx, ny + 14, "NOTICE TO MARINERS", "eyebrow")]
    first = d["affected"][0]
    rows = [("This shock", None),
            ("Merchants affected", f"{len(d['affected'])}"),
            ("Payments it delays", lakh(d["scenario"]["projected_impact"]["disrupted_value"])),
            ("First constraint", f"{first['time_to_constraint_hours']:.1f} h after"),
            ("The week around it", None),
            ("Merchants missing a payment", f"{cf['baseline_affected']}"),
            ("  without the shock", f"{cf['baseline_affected'] - len(d['affected'])}"),
            *[(f"Settlement to {a['merchant_id']}", crore(a["amount_inr"])) for a in d["plan"]],
            ("Network disruption index", f"−{cf['disruption_reduction_pct']:.1f} %"),
            ("Payments kept on time", crore(cf["commerce_preserved"])),
            ("Merchants protected", f"{cf['merchants_protected']}"),
            ("Gap to the exact optimum", f"{100 * cf['optimality_gap']:.1f} %")]
    y = ny + 30
    for label, value in rows:
        if value is None:
            y += 40
            parts.append(text(nx, y, label, "notice-h"))
            continue
        y += 32
        parts += [text(nx + (18 if label.startswith("  ") else 0), y, label.strip(),
                       "row-i" if label.startswith("  ") else "row"),
                  text(nx + nw, y, value, "fig", "end"), line(nx, y + 11, nx + nw, y + 11, "hair")]
    parts += [text(nx, y + 36, "First constraint is a point estimate; no interval.", "row-i"),
              seal(nx, y + 56, "REPLAYED OFFLINE", f"{short(d['snapshot_id'])} · {d['n_scenarios']} scenarios",
                   accent)]

    # tide table: the cost of waiting
    ty0 = y0 + 900
    sp = d["speed"]["delays"]
    parts += [line(L, ty0, R, ty0, "rule"), text(L, ty0 + 40, "TIDE TABLE", "eyebrow"),
              text(L, ty0 + 84, "The cost of waiting", "h3"),
              text(L + 330, ty0 + 84, "Delay before the money lands: a scenario parameter, not a production "
                   "timing.", "caption")]
    gx0, gy0, gw, gh = L + 220, ty0 + 120, R - L - 300, 190
    cols = [gx0 + i * gw / (len(sp) - 1) for i in range(len(sp))]

    def yv(v):
        return gy0 + gh - v / 45 * gh

    fz = [p["frozen"]["disruption_cut_pct"] for p in sp]
    rp = [p["replanned"]["disruption_cut_pct"] for p in sp]
    for v in (0, 20, 40):
        parts += [line(gx0, yv(v), gx0 + gw, yv(v), "hair"), text(gx0 - 14, yv(v) + 4, f"{v} %", "axis", "end")]
    area = (" ".join(f"{x:.1f},{yv(v):.1f}" for x, v in zip(cols, fz, strict=True))
            + f" {cols[-1]:.1f},{yv(0):.1f} {cols[0]:.1f},{yv(0):.1f}")
    parts += [f'<polygon points="{area}" class="tide"/>',
              '<polyline points="' + " ".join(f"{x:.1f},{yv(v):.1f}" for x, v in zip(cols, fz, strict=True))
              + '" class="tide-line"/>',
              '<polyline points="' + " ".join(f"{x:.1f},{yv(v):.1f}" for x, v in zip(cols, rp, strict=True))
              + '" class="tide-replan"/>']
    rows_t = [("DELAY", [f"{p['delay_hours']:g} h" for p in sp], "fig-b"),
              ("CUT, AS CHOSEN", [f"{v:.1f} %" for v in fz], "fig"),
              ("CUT, RE-PLANNED", [f"{v:.1f} %" for v in rp], "fig-2"),
              ("KEPT ON TIME", [crore(p["frozen"]["payments_kept_on_time_inr"])
                                if p["frozen"]["payments_kept_on_time_inr"] >= 1 else "nothing"
                                for p in sp], "fig")]
    y = gy0 + gh + 50
    for label, vals, cls in rows_t:
        parts.append(text(L, y, label, "colhead"))
        for x, v in zip(cols, vals, strict=True):
            parts.append(text(x, y, v, cls, "middle"))
        parts.append(line(L, y + 12, R, y + 12, "hair"))
        y += 34
    at48 = next(p["frozen"] for p in sp if p["delay_hours"] == 48.0)
    share = at48["reduction"]["delay"] / at48["reduction"]["total"]
    agg = {p["delay_hours"]: p["frozen_cut_pct"] for p in d["points"]}
    parts += [text(L, y + 16, f"At 48 h nothing is kept on time, yet {at48['disruption_cut_pct']:.1f} % of the "
                   f"disruption is cut: payments still late, but less late ({100 * share:.0f} % of it).", "note"),
              text(L, y + 42, f"Across the {d['speed_with_plan']} benchmark scenarios with a plan: "
                   f"{agg[0.0]['mean']:.1f} % [{agg[0.0]['low']:.1f}, {agg[0.0]['high']:.1f}] at 0 h, "
                   f"{agg[48.0]['mean']:.1f} % [{agg[48.0]['low']:.1f}, {agg[48.0]['high']:.1f}] at 48 h, as "
                   "chosen.", "caption"),
              seal(R - 330, y + 66, "REPLAYED OFFLINE", f"{d['speed_id']} · {d['speed_n']} scenarios", accent),
              line(L, H - 66, R, H - 66, "rule"),
              text(L, H - 38, "Synthetic network. Every figure is replayed offline inside the synthetic "
                   "generator; ₹ are analytic rupees; the disruption index is unitless.", "foot")]
    style = f"""
.paper{{fill:{paper}}}
.water{{fill:#f6f6f1}}
.shoal-1{{fill:#dfe9ec}}
.shoal-2{{fill:#c8dbe2}}
.shoal-3{{fill:#adc9d5}}
.shoal-4{{fill:#8fb3c4}}
.contour{{fill:none;stroke:{blue};stroke-width:.7;opacity:.8}}
.current{{fill:none;stroke:{sepia};opacity:.55}}
.current-faint{{fill:none;stroke:{sepia};opacity:.25}}
.chevron{{fill:none;stroke:{sepia};stroke-width:1.1;opacity:.8}}
.sounding{{fill:{ink}}}
.light{{fill:{ink};stroke:#f6f6f1;stroke-width:1}}
.flare{{fill:{accent};opacity:.8}}
.light-label{{font:italic 400 15px 'Spectral';fill:{ink}}}
.neat-outer{{fill:none;stroke:{ink};stroke-width:1.6}}
.neat-inner{{fill:none;stroke:{ink};stroke-width:.8}}
.neat-bar{{fill:{ink}}}
.graticule{{stroke:{blue};stroke-width:.4;opacity:.35}}
.cartouche{{fill:{paper};stroke:{ink};stroke-width:1.4}}
.cartouche-in{{fill:none;stroke:{ink};stroke-width:.6}}
.carto-k{{font:600 17px 'Spectral';letter-spacing:.42em;fill:{ink}}}
.carto-t{{font:italic 300 44px 'Spectral';fill:{ink}}}
.carto-n{{font:italic 400 15px 'Spectral';fill:#3c4350}}
.rose{{fill:none;stroke:{ink};stroke-width:.8}}
.rose-tick{{stroke:{ink};stroke-width:.6}}
.rose-dark{{fill:{ink}}}
.rose-light{{fill:#f6f6f1;stroke:{ink};stroke-width:.5}}
.rose-n{{font:600 18px 'Spectral';fill:{ink}}}
.eyebrow{{font:500 12px 'IBM Plex Mono';letter-spacing:.16em;fill:{ink}}}
.h2{{font:300 56px 'Spectral';fill:{ink}}}
.h3{{font:400 28px 'Spectral';fill:{ink}}}
.lede{{font:italic 300 24px 'Spectral';fill:#3c4350}}
.caption{{font:italic 400 15px 'Spectral';fill:#3c4350}}
.note{{font:400 16px 'Spectral';fill:{ink}}}
.rule{{stroke:{ink};stroke-width:.6;opacity:.55}}
.rule-strong{{stroke:{ink};stroke-width:1.2}}
.hair{{stroke:{ink};stroke-width:.4;opacity:.35}}
.danger{{fill:none;stroke:{accent};stroke-width:1.4;stroke-dasharray:2 3}}
.wreck{{stroke:{accent};stroke-width:2}}
.wreck-label{{font:500 13px 'IBM Plex Mono';fill:{accent}}}
.wreck-note{{font:italic 400 14px 'Spectral';fill:{accent}}}
.harbour{{fill:{paper};stroke:{ink};stroke-width:1.4}}
.route{{fill:none;stroke:{accent};stroke-width:2;stroke-dasharray:7 5}}
.route-end{{fill:none;stroke:{accent};stroke-width:1.6}}
.route-label{{font:500 13px 'IBM Plex Mono';fill:{accent}}}
.notice{{fill:#f6f6f1;stroke:{ink};stroke-width:.8}}
.notice-h{{font:italic 400 18px 'Spectral';fill:{ink}}}
.row{{font:400 16px 'Spectral';fill:{ink}}}
.row-i{{font:italic 400 15px 'Spectral';fill:#3c4350}}
.fig{{font:400 15px 'IBM Plex Mono';font-feature-settings:'tnum' 1;fill:{ink}}}
.fig-b{{font:600 15px 'IBM Plex Mono';fill:{ink}}}
.fig-red{{font:400 15px 'IBM Plex Mono';font-feature-settings:'tnum' 1;fill:{accent}}}
.fig-2{{font:400 15px 'IBM Plex Mono';font-feature-settings:'tnum' 1;fill:#5a6170}}
.colhead{{font:500 11px 'IBM Plex Mono';letter-spacing:.12em;fill:#5a6170}}
.island{{fill:none;stroke:{ink};stroke-width:.9}}
.island-label{{font:italic 400 13px 'Spectral';fill:#3c4350}}
.route-tag{{fill:#f6f6f1;stroke:{accent};stroke-width:.6}}
.axis{{font:400 12px 'IBM Plex Mono';fill:#3c4350}}
.tide{{fill:#c8dbe2;opacity:.8}}
.tide-line{{fill:none;stroke:{blue};stroke-width:2}}
.tide-replan{{fill:none;stroke:{accent};stroke-width:1.6;stroke-dasharray:5 4}}
.seal-ring{{fill:none;stroke:{accent};stroke-width:1.2}}
.seal-k{{font:600 10.5px 'IBM Plex Mono';letter-spacing:.14em;fill:{accent}}}
.seal-v{{font:400 10.5px 'IBM Plex Mono';fill:{accent}}}
.foot{{font:italic 400 14px 'Spectral';fill:#3c4350}}
"""
    faces = [("Spectral", "spectral", 300, "normal"), ("Spectral", "spectral", 300, "italic"),
             ("Spectral", "spectral", 400, "normal"), ("Spectral", "spectral", 400, "italic"),
             ("Spectral", "spectral", 600, "normal"),
             ("IBM Plex Mono", "ibm-plex-mono", 400, "normal"),
             ("IBM Plex Mono", "ibm-plex-mono", 500, "normal"),
             ("IBM Plex Mono", "ibm-plex-mono", 600, "normal")]
    return document("\n".join(parts), height=H, faces=faces, style=style,
                    title="Concept C, The Survey Chart")


# ========================================================== SPECIMEN ======

CANDIDATES = [
    ("A", "The Structural Report", "Source Serif 4", "source-serif-4", "Pull a stone", 600),
    ("B", "The Ledger Room", "Libre Caslon Text", "libre-caslon-text", "Posting a shock", 400),
    ("C", "The Survey Chart", "Spectral", "spectral", "Track of a shock", 300),
]
MEASURED_MONO = ["ibm-plex-mono", "jetbrains-mono", "dm-mono", "courier-prime"]


def _version(pkg: str) -> str:
    return json.loads((FONTS / pkg / "package.json").read_text(encoding="utf-8"))["version"]


def coverage(pkg: str) -> dict[str, bool]:
    """Measured from the font files: the rupee sign, tabular figures, small capitals."""
    from fontTools.ttLib import TTFont

    rupee, features = False, set()
    for part in ("latin", "latin-ext"):
        path = FONTS / pkg / "files" / f"{pkg}-{part}-400-normal.woff2"
        if not path.exists():
            continue
        font = TTFont(str(path))
        rupee |= 0x20B9 in font.getBestCmap()
        if "GSUB" in font:
            features |= {fr.FeatureTag for fr in font["GSUB"].table.FeatureList.FeatureRecord}
    return {"rupee": rupee, "tnum": "tnum" in features, "smcp": "smcp" in features}


def specimen(d: dict) -> str:
    ink, paper, accent = "#1b1d24", "#f2f1ec", "#a23b2c"
    H = 1460
    L, R = 96, W - 96
    sc, cf = d["shock"], d["cf"]
    pulled = sc["origin_merchants"][0]
    parts = [rect(0, 0, W, H, "paper"),
             text(L, 60, "KEYSTONE", "eyebrow"),
             text(R, 60, "FONT SPECIMEN  ·  THREE SIL OFL CANDIDATES", "eyebrow", "end"),
             line(L, 78, R, 78, "rule-strong"),
             text(L, 150, "One display serif per concept; one figure face for all three.", "intro"),
             text(L, 182, "Every figure on a plate is set in IBM Plex Mono: it is the only monospace candidate "
                  "measured to carry the rupee sign.", "intro-i")]
    y = 230
    for key, concept, family, pkg, sample, weight in CANDIDATES:
        cov = coverage(pkg)
        parts += [line(L, y, R, y, "rule"),
                  text(L, y + 36, f"{key} · {concept.upper()}", "eyebrow"),
                  text(L, y + 64, family, "name"),
                  text(L, y + 88, f"@fontsource/{pkg} {_version(pkg)}", "meta"),
                  text(L, y + 108, "SIL Open Font License 1.1", "meta"),
                  text(L, y + 136, f"₹ U+20B9   {'yes' if cov['rupee'] else 'no — falls back'}", "meta",
                       extra=' xml:space="preserve"'),
                  text(L, y + 156, f"small caps {'yes' if cov['smcp'] else 'no'}", "meta"),
                  f'<text x="460" y="{y + 92}" style="font:{weight} 72px \'{family}\';fill:{ink}">'
                  f'{esc(sample)}</text>',
                  f'<text x="460" y="{y + 138}" style="font:italic 400 24px \'{family}\';fill:#3c3f4a">'
                  f'{esc(f"{pulled} never receives its receivable from m0008, due at hour 19.")}</text>',
                  f'<text x="460" y="{y + 178}" style="font:400 18px \'{family}\';fill:#3c3f4a">'
                  f'{esc("Merchants, courses, bearings, shoring, settlement — 0123456789")}</text>',
                  text(460, y + 214, f"{crore(cf['commerce_preserved'])} kept on time  ·  "
                       f"−{cf['disruption_reduction_pct']:.2f} %  ·  108.4 h  ·  {indian(sc['magnitude'])}",
                       "figs", extra=' xml:space="preserve"')]
        y += 260
    # measured coverage of the monospace candidates
    parts += [line(L, y, R, y, "rule-strong"),
              text(L, y + 40, "MONOSPACE CANDIDATES, MEASURED", "eyebrow"),
              text(L, y + 70, "Checked in the WOFF2 files: a cmap entry for U+20B9 and a 'tnum' GSUB feature.",
                   "intro-i")]
    y += 110
    parts += [text(L, y, "PACKAGE", "colhead"), text(700, y, "₹ U+20B9", "colhead", "end"),
              text(900, y, "'TNUM' FEATURE", "colhead", "end"), text(R, y, "USED", "colhead", "end"),
              line(L, y + 12, R, y + 12, "rule")]
    for pkg in MEASURED_MONO:
        y += 36
        cov = coverage(pkg)
        used = "figures, all boards" if pkg == "ibm-plex-mono" else "—"
        parts += [text(L, y, f"@fontsource/{pkg} {_version(pkg)}", "figs"),
                  text(700, y, "yes" if cov["rupee"] else "no", "figs", "end"),
                  text(900, y, "yes" if cov["tnum"] else "no", "figs", "end"),
                  text(R, y, used, "figs", "end"), line(L, y + 12, R, y + 12, "hair")]
    parts += [text(L, y + 46, "A monospace face is fixed-width, so its figures line up without a 'tnum' "
                   "feature. Libre Caslon Text has no ₹: on Board B a rupee", "intro-i"),
              text(L, y + 68, "sign inside serif text falls back to a system face; figures stay in Plex Mono "
                   "either way.", "intro-i"),
              line(L, H - 66, R, H - 66, "rule"),
              text(L, H - 38, "All four faces are self-hosted from npm @fontsource packages, subset and embedded "
                   "in each SVG. Licence texts: design/fonts/licenses/.", "foot")]
    style = f"""
.paper{{fill:{paper}}}
.eyebrow{{font:500 12px 'IBM Plex Mono';letter-spacing:.16em;fill:{ink}}}
.colhead{{font:500 11px 'IBM Plex Mono';letter-spacing:.12em;fill:#5a5d68}}
.intro{{font:400 26px 'Source Serif 4';fill:{ink}}}
.intro-i{{font:italic 400 17px 'Source Serif 4';fill:#3c3f4a}}
.name{{font:600 22px 'Source Serif 4';fill:{ink}}}
.meta{{font:400 13px 'IBM Plex Mono';fill:#3c3f4a}}
.figs{{font:400 17px 'IBM Plex Mono';font-feature-settings:'tnum' 1;fill:{ink}}}
.rule{{stroke:{ink};stroke-width:.6;opacity:.5}}
.rule-strong{{stroke:{ink};stroke-width:1.2}}
.hair{{stroke:{ink};stroke-width:.4;opacity:.35}}
.foot{{font:italic 400 14px 'Source Serif 4';fill:#3c3f4a}}
"""
    faces = [("Source Serif 4", "source-serif-4", 400, "normal"),
             ("Source Serif 4", "source-serif-4", 400, "italic"),
             ("Source Serif 4", "source-serif-4", 600, "normal"),
             ("Libre Caslon Text", "libre-caslon-text", 400, "normal"),
             ("Libre Caslon Text", "libre-caslon-text", 400, "italic"),
             ("Spectral", "spectral", 300, "normal"), ("Spectral", "spectral", 400, "normal"),
             ("Spectral", "spectral", 400, "italic"),
             ("IBM Plex Mono", "ibm-plex-mono", 400, "normal"),
             ("IBM Plex Mono", "ibm-plex-mono", 500, "normal")]
    return document("\n".join(parts), height=H, faces=faces, style=style,
                    title="Font specimen, three SIL OFL candidates")


# ------------------------------------------------------------------- index

BOARDS = [
    ("board-a-structural-report", "A", "The Structural Report",
     "An engineer's survey in numbered plates. Merchants are stones laid in courses by tier; keystones sit in "
     "the foundation; the plan is drawn as shoring. The most literal reading of the brief, and the one that "
     "carries Plates I to V most naturally.",
     "Strong at a glance; the wall replaces the hundred-dot grid (exception E4). Risk: the metaphor can read "
     "as decoration if the plates lose their dimension lines and captions."),
    ("board-b-ledger-room", "B", "The Ledger Room",
     "A printed double-entry ledger. All 216 inferred dependencies as a tie-out between account columns; the "
     "shock posted as journal entries; the same week twice as a reconciliation; the cost of waiting as a "
     "schedule.",
     "Most honest about numbers and closest to how a finance reader thinks; the execution ledger in Plate IV "
     "would feel native. Risk: dense, and the hero is less inviting to touch than A or C."),
    ("board-c-survey-chart", "C", "The Survey Chart",
     "A nautical survey. Merchants placed by payment distance (not geography); shoals where cover is short; "
     "dependencies as currents; keystones as lights; the shock as a wreck with settlement routes.",
     "The most striking hero and the best at showing where strain gathers. Risk: positions are a layout, not "
     "a measurement, and need that said every time; the 15 merchants with no inferred dependency sit apart."),
]


def index_html(d: dict) -> str:
    cards = "\n".join(
        f"""<article>
  <a href="{slug}.svg"><img src="{slug}.png" alt="Concept {key}, {title}" loading="lazy"></a>
  <h2><span>{key}</span> {esc(title)}</h2>
  <p>{esc(what)}</p>
  <p class="crit"><b>Critique.</b> {esc(crit)}</p>
  <p class="links"><a href="{slug}.svg">SVG (fonts embedded)</a> · <a href="{slug}.png">PNG</a></p>
</article>""" for slug, key, title, what, crit in BOARDS)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Keystone concept boards</title>
<style>
  :root {{ color-scheme: light; --paper:#f2f1ec; --ink:#1b1d24; --ink-2:#4a4d58; --rule:#cfcdc4; --accent:#a23b2c; }}
  body {{ margin:0; background:var(--paper); color:var(--ink); font:16px/1.55 Georgia, 'Times New Roman', serif; }}
  main {{ max-width:1320px; margin:0 auto; padding:48px 16px 80px; }}
  .eyebrow {{ font:500 12px/1 ui-monospace, 'Cascadia Mono', Menlo, monospace; letter-spacing:.16em; text-transform:uppercase; }}
  h1 {{ font-weight:400; font-size:clamp(34px,5vw,56px); line-height:1.1; margin:.4em 0 .3em; }}
  .lede {{ font-style:italic; color:var(--ink-2); max-width:62ch; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:40px 32px; margin-top:40px; }}
  article img {{ width:100%; height:auto; border:1px solid var(--rule); display:block; background:#fff; }}
  h2 {{ font-weight:400; font-size:26px; margin:18px 0 6px; }}
  h2 span {{ font:600 14px ui-monospace, Menlo, monospace; border:1px solid var(--ink); padding:2px 7px; margin-right:6px; vertical-align:middle; }}
  .crit {{ color:var(--ink-2); }}
  .links a, a {{ color:var(--accent); }}
  .pick {{ margin-top:48px; padding:20px 24px; border:1px solid var(--ink); max-width:70ch; }}
  footer {{ margin-top:40px; color:var(--ink-2); font-size:14px; border-top:1px solid var(--rule); padding-top:14px; }}
</style>
</head>
<body>
<main>
  <p class="eyebrow">Keystone · Step C0 · concept boards</p>
  <h1>Three ways to draw the same evidence</h1>
  <p class="lede">Each board is a hero (no metrics) and one plate, drawn by code from the committed snapshot
  {esc(d['snapshot_id'])} and the value-of-speed artifact {esc(d['speed_id'])}. The numbers are identical on all
  three; only the visual language differs. Click a board for the full-size SVG.</p>
  <div class="grid">
{cards}
  </div>
  <section class="pick">
    <p class="eyebrow">Your pick</p>
    <p>Reply with <b>A</b>, <b>B</b> or <b>C</b> (or a mix, e.g. "C's hero with B's ledger for Plate IV").
    Phase 2 builds the chosen concept end to end. Fonts: see the
    <a href="font-specimen.svg">specimen</a> (<a href="font-specimen.png">PNG</a>).</p>
  </section>
  <footer>Synthetic network; every figure is replayed offline inside the synthetic generator. Fonts are SIL OFL
  (IBM Plex Mono, Source Serif 4, Libre Caslon Text, Spectral), self-hosted and embedded.</footer>
</main>
</body>
</html>
"""


def main() -> None:
    d = load()
    OUT.mkdir(parents=True, exist_ok=True)
    outputs = {
        "board-a-structural-report.svg": board_a(d),
        "board-b-ledger-room.svg": board_b(d),
        "board-c-survey-chart.svg": board_c(d),
        "font-specimen.svg": specimen(d),
        "index.html": index_html(d),
    }
    for name, content in outputs.items():
        (OUT / name).write_text(content, encoding="utf-8", newline="\n")
        print("wrote", name)


if __name__ == "__main__":
    main()
