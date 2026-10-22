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
            avail = width - gap * (len(chunk) - 1)
            # stagger the joints of every other course, as masons do
            offset = (avail / len(chunk)) * 0.35 if (len(rows) % 2) else 0.0
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
    H = 2280
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
            parts.append(f'<path d="M{cx - 9:.1f},{s["y"] - 16:.1f} L{cx + 9:.1f},{s["y"] - 16:.1f} '
                         f'L{cx:.1f},{s["y"] - 4:.1f} Z" class="mark"/>')
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
    parts += [text(kx, 470, "▼ Keystones", "note-head"),
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
            cx, cy = s["x"] + s["w"] / 2, s["y"] + s["h"] / 2
            parts.append(f'<path d="M{s["x"] + 6:.1f},{pbase + 26:.1f} L{cx - 8:.1f},{cy:.1f} M{s["x"] + s["w"] - 6:.1f},'
                         f'{pbase + 26:.1f} L{cx + 8:.1f},{cy:.1f}" class="shore"/>')
            parts.append(text(cx, cy + 4, s["id"], "stone-label", "middle"))
    parts.append(line(px - 20, pbase, px + pw + 20, pbase, "rule-strong"))
    parts.append(line(px - 20, pbase + 26, px + pw + 20, pbase + 26, "rule"))
    lx, ly = px, pbase + 66
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
        ("  of which, without the shock", f"{cf['baseline_affected'] - len(d['affected'])}"),
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
        parts += [text(rx, y, label, "row"), text(rx + rw, y, value, "fig", "end"),
                  line(rx, y + 12, rx + rw, y + 12, "hair")]
    parts.append(seal(rx, y + 30, "REPLAYED OFFLINE", f"{short(d['snapshot_id'])} · {d['n_scenarios']} scenarios",
                      accent))
    parts += [text(L, y0 + 760, "Margin note.", "note-head"),
              text(L + 92, y0 + 760, "The bearings were estimated only from payments before hour 19; "
                   "nothing the shock did is visible to the model.", "note-i")]

    # cost of waiting
    parts += cost_chart(d, L, y0 + 830, R - L, 330, ink=ink, accent=accent, cls_prefix="")
    parts.append(text(L, H - 40, "Synthetic network. Every figure is replayed offline inside the synthetic "
                      "generator; ₹ are analytic rupees; the disruption index is unitless.", "foot"))
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
.shore{{stroke:{accent};stroke-width:3;fill:none;stroke-linecap:round}}
.legend{{font:400 14px 'Source Serif 4';fill:#383a47}}
.legend-strain{{fill:url(#strain);stroke:{ink};stroke-width:.6}}
.row{{font:400 16px 'Source Serif 4';fill:{ink}}}
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


def cost_chart(d, x, y, w, h, *, ink, accent, cls_prefix):
    """Plate III in miniature: the canonical plan, frozen and re-planned, on a discrete rail."""
    sp = d["speed"]["delays"]
    delays = [p["delay_hours"] for p in sp]
    fz = [p["frozen"]["disruption_cut_pct"] for p in sp]
    rp = [p["replanned"]["disruption_cut_pct"] for p in sp]
    out = [text(x, y, "THE COST OF WAITING", "eyebrow"),
           text(x, y + 40, "The same plan, landing later", "chart-title")]
    cx, cy, cw, ch = x + 70, y + 80, w - 470, h - 120
    ymax = 45.0

    def px(i):
        return cx + i * cw / (len(delays) - 1)

    def py(v):
        return cy + ch - v / ymax * ch

    for v in (0, 10, 20, 30, 40):
        out += [line(cx, py(v), cx + cw, py(v), "hair"), text(cx - 12, py(v) + 4, f"{v}", "axis", "end")]
    out.append(text(cx - 12, cy - 14, "% cut", "axis", "end"))
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
    # the rail
    out.append(line(cx, cy + ch + 26, cx + cw, cy + ch + 26, "rule-strong"))
    for i, dl in enumerate(delays):
        out += [line(px(i), cy + ch + 20, px(i), cy + ch + 32, "rule-strong"),
                text(px(i), cy + ch + 52, f"{dl:g} h", "axis", "middle")]
    out.append(text(cx, cy + ch + 78, "Delay before the money lands: a scenario parameter, not a production "
                    "timing.", "note-i"))
    # annotation at 48 h
    i48 = delays.index(48.0)
    at48 = sp[i48]["frozen"]
    share = at48["reduction"]["delay"] / at48["reduction"]["total"]
    ax, ay = px(i48), py(fz[i48])
    out += [line(ax, ay - 8, ax + 60, ay - 70, "leader"),
            text(ax + 66, ay - 92, f"48 h: {fz[i48]:.1f} % cut, {crore(at48['payments_kept_on_time_inr'])} kept on time.",
                 "note"),
            text(ax + 66, ay - 72, f"Payments made less late carry {100 * share:.0f} % of it.", "note-i")]
    # legend + aggregate
    lx = cx + cw + 60
    agg = {p["delay_hours"]: p for p in d["points"]}
    out += [line(lx, cy + 6, lx + 34, cy + 6, "frozen"), text(lx + 44, cy + 11, "plan as chosen at the shock",
                                                                 "legend"),
            line(lx, cy + 34, lx + 34, cy + 34, "replanned"), text(lx + 44, cy + 39, "re-planned for the delay",
                                                                      "legend"),
            text(lx, cy + 86, f"Across the {d['speed_with_plan']} benchmark scenarios", "note"),
            text(lx, cy + 106, "with a plan, mean cut [95 % interval]:", "note"),
            text(lx, cy + 136, f"0 h    {agg[0.0]['frozen_cut_pct']['mean']:5.1f} %  "
                 f"[{agg[0.0]['frozen_cut_pct']['low']:.1f}, {agg[0.0]['frozen_cut_pct']['high']:.1f}]", "fig"),
            text(lx, cy + 160, f"24 h   {agg[24.0]['frozen_cut_pct']['mean']:5.1f} %  "
                 f"[{agg[24.0]['frozen_cut_pct']['low']:.1f}, {agg[24.0]['frozen_cut_pct']['high']:.1f}]", "fig"),
            text(lx, cy + 184, f"48 h   {agg[48.0]['frozen_cut_pct']['mean']:5.1f} %  "
                 f"[{agg[48.0]['frozen_cut_pct']['low']:.1f}, {agg[48.0]['frozen_cut_pct']['high']:.1f}]", "fig"),
            seal(lx, cy + 206, "REPLAYED OFFLINE", f"{d['speed_id']} · {d['speed_n']} scenarios", accent)]
    return out
