# indoorgml_viewer_final.py
from __future__ import annotations

VERSION = "2026-04-21 20:27"

import copy
import csv
import json
import math
import os
import re
import warnings
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Set

import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.path import Path as MplPath
from matplotlib.collections import LineCollection, PolyCollection
import matplotlib.colors as mcolors
import matplotlib.image as mpimg

# Silence matplotlib's "Ignoring fixed y limits to fulfill fixed data aspect
# with adjustable data limits." warning. Our axes use
# `set_aspect('equal', adjustable='datalim')` so the fitted limits already
# match the canvas aspect ratio — the warning is purely cosmetic noise that
# fires on every set_xlim/set_ylim. We pre-compute matching limits in
# fit_to_extent, so the warning never indicates a real problem here.
warnings.filterwarnings(
    "ignore",
    message=r".*Ignoring fixed [xy] limits to fulfill fixed data aspect.*",
    category=UserWarning,
)


# ── Canvas font ────────────────────────────────────────────────────────────
# Tk widgets inherit TkDefaultFont and render CJK fine, but the Matplotlib
# canvas does not go through Tk: it defaults to DejaVu Sans, which carries no
# Hangul (or CJK) glyphs at all. Cell names such as "전기실" therefore came
# out as tofu boxes, and every draw emitted a "Glyph … missing from font(s)"
# warning per character.
#
# Pick the first installed font that actually covers the scripts in use.
# Candidates are ordered per-platform, then a shared fallback list catches
# the open-source families that ship with many Linux distributions.
_CJK_FONT_CANDIDATES = (
    # Windows
    "Malgun Gothic", "맑은 고딕", "Gulim", "Batang", "Microsoft YaHei",
    # macOS
    "Apple SD Gothic Neo", "AppleGothic", "PingFang SC", "Hiragino Sans",
    # Linux / cross-platform open source
    "Noto Sans CJK KR", "Noto Sans KR", "NanumGothic", "Nanum Gothic",
    "Noto Sans CJK JP", "Noto Sans CJK SC", "Source Han Sans KR",
    "UnDotum", "Baekmuk Gulim", "WenQuanYi Zen Hei",
)


def _font_covers(path: str, sample: str) -> bool:
    """True when the font file at `path` has a glyph for every char in sample.

    Checking the actual cmap beats trusting a family name: the name a font
    registers under varies by platform and locale (a Korean Windows may list
    Malgun Gothic as '맑은 고딕'), and a name match tells us nothing about
    coverage anyway.
    """
    try:
        from fontTools.ttLib import TTFont, TTCollection
    except Exception:
        return True          # cannot verify — assume the name match was right
    try:
        if path.lower().endswith(".ttc"):
            faces = TTCollection(path, lazy=True).fonts
        else:
            faces = [TTFont(path, lazy=True, fontNumber=0)]
        for face in faces:
            cmap = face.getBestCmap()
            if all(ord(ch) in cmap for ch in sample):
                return True
    except Exception:
        return False
    return False


def _configure_canvas_font() -> Optional[str]:
    """Prepend an installed CJK-capable font to Matplotlib's sans-serif list.

    Returns the chosen family name, or None when the system has none — in
    that case Latin text still renders correctly and only CJK degrades, so
    this is a warning rather than a fatal condition.
    """
    try:
        from matplotlib import font_manager, rcParams
    except Exception:
        return None

    # "한글" — if a face carries these it carries Hangul generally.
    sample = "\uD55C\uAE00"

    by_name: Dict[str, str] = {}
    for f in font_manager.fontManager.ttflist:
        by_name.setdefault(f.name, f.fname)

    # 1) Preferred families, in platform order, verified against the cmap.
    chosen = None
    for name in _CJK_FONT_CANDIDATES:
        path = by_name.get(name)
        if path and _font_covers(path, sample):
            chosen = name
            break

    # 2) Nothing from the list matched. Rather than give up — the list cannot
    #    name every CJK font on every system, and a stale font cache can hide
    #    a freshly installed one — scan whatever is registered for coverage.
    if chosen is None:
        for name, path in sorted(by_name.items()):
            if _font_covers(path, sample):
                chosen = name
                break

    if chosen is None:
        return None

    # Keep DejaVu Sans behind it: the CJK font supplies Hangul, DejaVu still
    # covers any symbol the CJK family happens to lack.
    existing = [f for f in rcParams.get("font.sans-serif", []) if f != chosen]
    rcParams["font.sans-serif"] = [chosen] + existing
    rcParams["font.family"] = "sans-serif"
    # A CJK face usually has no real italic; Matplotlib would fall back to a
    # Latin-only font for minus signs otherwise.
    rcParams["axes.unicode_minus"] = False
    return chosen


CANVAS_FONT = _configure_canvas_font()

# Matplotlib emits one "Glyph NNNNN (...) missing from font(s)" warning per
# *character* per draw. On a floor with a few hundred Korean labels that is
# thousands of lines per redraw, which buries anything else the console has
# to say. The condition is already reported once, in the status bar, so
# silence the flood.
warnings.filterwarnings(
    "ignore",
    message=r".*Glyph \d+ .* missing from font.*",
    category=UserWarning,
)


# =========================================================
# Data classes
# =========================================================
@dataclass
class LayerFeature:
    layer_id: str
    theme: str = "Navigation"     # e.g. Navigation, Sensor, Topographic, Zone, Floor …
    source_element: Optional[ET.Element] = None


@dataclass
class SpaceFeature:
    feature_id: str
    name: str = ""
    cls: str = ""
    level: Optional[str] = None
    function: str = ""          # GeneralSpace function value (navi:function)
    polygons_3d: List[List[Tuple[float, float, float]]] = field(default_factory=list)
    centers: List[Tuple[float, float, float]] = field(default_factory=list)
    source_element: Optional[ET.Element] = None
    duality: str = ""           # node_id of the dual Node in DualSpaceLayer
    layer_id: str = ""          # owning ThematicLayer id
    is_poi: bool = False        # core:poi — default false
    is_virtual: bool = False    # virtual CellSpace (no geometry) — dual to an orphan node


@dataclass
class NodeFeature:
    node_id: str
    coord: Optional[Tuple[float, float, float]] = None
    connects: List[str] = field(default_factory=list)
    source_element: Optional[ET.Element] = None
    level: Optional[str] = None  # floor level, set when manually created
    duality: str = ""            # space_id of the dual CellSpace in PrimalSpaceLayer
    layer_id: str = ""           # owning ThematicLayer id


@dataclass
class EdgeFeature:
    edge_id: str
    coords: List[Tuple[float, float, float]] = field(default_factory=list)
    connects: List[str] = field(default_factory=list)
    source_element: Optional[ET.Element] = None
    layer_id: str = ""           # owning ThematicLayer id
    weight: float = 1.0          # core:weight — default 1.0 for new edges
    duality: str = ""            # CellBoundary id this edge is dual to


@dataclass
class InterLayerConnection:
    ilc_id: str
    space1_id: str                    # space in layer1
    space2_id: str                    # space in layer2
    layer1_id: str = ""
    layer2_id: str = ""
    type_of_topo_expr: str = "dummy"
    source_element: Optional[ET.Element] = None


class _SpaceDict(dict):
    """Dict subclass that auto-invalidates the owner's floor index on every
    mutation. Avoids having to sprinkle `invalidate_floor_index()` calls at
    every place that adds/removes a CellSpace."""
    __slots__ = ("_owner",)

    def __init__(self, owner=None):
        super().__init__()
        self._owner = owner

    def _invalidate(self):
        if self._owner is not None:
            self._owner._floor_index = None

    def __setitem__(self, k, v):
        super().__setitem__(k, v)
        self._invalidate()

    def __delitem__(self, k):
        super().__delitem__(k)
        self._invalidate()

    def pop(self, k, *a):
        r = super().pop(k, *a)
        self._invalidate()
        return r

    def clear(self):
        super().clear()
        self._invalidate()

    def update(self, *a, **kw):
        super().update(*a, **kw)
        self._invalidate()


def _make_space_dict():
    return _SpaceDict()


@dataclass
class IndoorModel:
    spaces: Dict[str, SpaceFeature] = field(default_factory=_make_space_dict)
    nodes: Dict[str, NodeFeature] = field(default_factory=dict)
    edges: Dict[str, EdgeFeature] = field(default_factory=dict)
    layers: Dict[str, LayerFeature] = field(default_factory=dict)  # ThematicLayer id → LayerFeature
    inter_layer_connections: Dict[str, InterLayerConnection] = field(default_factory=dict)
    source_tree: Optional[ET.ElementTree] = None
    source_path: str = ""
    # Non-fatal problems detected while parsing the source file, e.g. two
    # <Edge> elements sharing one id (only the last survives in `edges`).
    # `open_file` surfaces these so a silent data loss never goes unnoticed.
    # Each entry: (kind, feature_id, copies_found).
    load_conflicts: List[Tuple[str, str, int]] = field(default_factory=list)
    # Cache: floor (level string) → set of space ids. Built lazily on first
    # query; auto-invalidated by `_SpaceDict` mutations. If a SpaceFeature's
    # `level` is mutated in place (rare), call `invalidate_floor_index()`.
    _floor_index: Optional[Dict[str, Set[str]]] = None

    def __post_init__(self):
        # Hook the spaces dict so it can auto-invalidate the cache.
        if isinstance(self.spaces, _SpaceDict):
            self.spaces._owner = self
        else:
            d = _SpaceDict(self)
            d.update(self.spaces)
            self.spaces = d

    def invalidate_floor_index(self) -> None:
        self._floor_index = None

    def floor_index(self) -> Dict[str, Set[str]]:
        if self._floor_index is None:
            idx: Dict[str, Set[str]] = {}
            for sp_id, sp in self.spaces.items():
                if sp.level is not None and sp.level.strip():
                    idx.setdefault(sp.level.strip(), set()).add(sp_id)
            self._floor_index = idx
        return self._floor_index

# =========================================================
# XML / GML helpers
# =========================================================
def local_name(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def find_any_id(elem: ET.Element) -> Optional[str]:
    """Return the gml:id (or any {ns}id) attribute value of elem.

    Uses local-name == 'id' check so that attributes like 'uuid' or 'objectid'
    (whose full name also ends with 'id') are NOT accidentally matched.
    Attribute order is not guaranteed after ElementTree write/read cycles,
    so we must match by exact local name rather than endswith().
    """
    for k, v in elem.attrib.items():
        local = k.split("}", 1)[1] if "}" in k else k
        if local == "id":
            return v
    return None


def parse_floats(text: str) -> List[float]:
    vals = re.split(r"[\s,]+", text.strip())
    return [float(v) for v in vals if v]


def detect_srs_dimension(elem: ET.Element) -> int:
    dim = elem.attrib.get("srsDimension")
    if dim and dim.isdigit():
        return int(dim)
    return 3


def tuples_from_flat(values: List[float], dim: int) -> List[Tuple[float, float, float]]:
    pts: List[Tuple[float, float, float]] = []
    if dim <= 0:
        dim = 3

    for i in range(0, len(values), dim):
        chunk = values[i:i + dim]
        if len(chunk) >= 3:
            pts.append((chunk[0], chunk[1], chunk[2]))
        elif len(chunk) == 2:
            pts.append((chunk[0], chunk[1], 0.0))
    return pts


def parse_pos(elem: ET.Element) -> Optional[Tuple[float, float, float]]:
    if elem.text and elem.text.strip():
        vals = parse_floats(elem.text)
        pts = tuples_from_flat(vals, detect_srs_dimension(elem))
        return pts[0] if pts else None
    return None


def parse_poslist(elem: ET.Element) -> List[Tuple[float, float, float]]:
    if elem.text and elem.text.strip():
        vals = parse_floats(elem.text)
        return tuples_from_flat(vals, detect_srs_dimension(elem))
    return []


def extract_all_pos_points(elem: ET.Element) -> List[Tuple[float, float, float]]:
    points: List[Tuple[float, float, float]] = []
    for sub in elem.iter():
        ln = local_name(sub.tag)
        if ln == "pos":
            p = parse_pos(sub)
            if p is not None:
                points.append(p)
        elif ln == "posList":
            points.extend(parse_poslist(sub))
    return points


def extract_linestrings(elem: ET.Element) -> List[List[Tuple[float, float, float]]]:
    lines: List[List[Tuple[float, float, float]]] = []
    for sub in elem.iter():
        if local_name(sub.tag) == "LineString":
            pts = extract_all_pos_points(sub)
            if len(pts) >= 2:
                lines.append(pts)
    return lines


def extract_polygons(elem: ET.Element) -> List[List[Tuple[float, float, float]]]:
    polygons: List[List[Tuple[float, float, float]]] = []

    for sub in elem.iter():
        if local_name(sub.tag) == "Polygon":
            rings: List[List[Tuple[float, float, float]]] = []
            for ring in sub.iter():
                if local_name(ring.tag) in ("LinearRing", "Ring"):
                    pts = extract_all_pos_points(ring)
                    if len(pts) >= 3:
                        rings.append(pts)
            if rings:
                polygons.append(rings[0])

    if not polygons:
        for sub in elem.iter():
            if local_name(sub.tag) in ("LinearRing", "Ring"):
                pts = extract_all_pos_points(sub)
                if len(pts) >= 3:
                    polygons.append(pts)

    return polygons


def polygon_centroid(poly: List[Tuple[float, float, float]]) -> Optional[Tuple[float, float, float]]:
    if not poly:
        return None
    return (
        sum(p[0] for p in poly) / len(poly),
        sum(p[1] for p in poly) / len(poly),
        sum(p[2] for p in poly) / len(poly),
    )


def first_text(elem: ET.Element, names: List[str]) -> Optional[str]:
    wanted = set(names)
    for sub in elem.iter():
        if local_name(sub.tag) in wanted and sub.text and sub.text.strip():
            return sub.text.strip()
    return None


def get_direct_child_text_by_localname(elem: ET.Element, child_name: str) -> Optional[str]:
    for child in elem:
        if local_name(child.tag) == child_name and child.text and child.text.strip():
            return child.text.strip()
    return None


def href_to_id(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value[1:] if value.startswith("#") else value


# =========================================================
# Tag hints
# =========================================================
SPACE_TAG_HINTS = {
    "CellSpace",
    "CellSpaceType",
    "NavigableSpace",
    "GeneralSpace",
    "TransferSpace",
    "AnchorSpace",
    "ConnectionSpace",
    "TransitionSpace",
    "NonNavigableSpace",
}

NODE_TAG_HINTS = {"Node", "State"}
EDGE_TAG_HINTS = {"Edge", "Transition"}


# =========================================================
# Parser
# =========================================================
def _read_xml_tree(xml_path: str) -> ET.ElementTree:
    """Parse an XML file robustly.

    Python's expat-based ElementTree refuses multi-byte encodings declared in
    the XML prolog (e.g. ``encoding='cp949'``, ``euc-kr``, ``shift_jis``,
    ``gb2312``…), failing with ``"multi-byte encodings are not supported"``.
    To handle such files, we read raw bytes, detect the declared encoding via
    Python codecs, decode to a UTF-8 string, rewrite the prolog encoding to
    UTF-8, and then feed the result to ``ET.fromstring``.
    """
    try:
        return ET.parse(xml_path)
    except (ET.ParseError, ValueError) as e:
        msg = str(e).lower()
        if "multi-byte" not in msg and "multi-bytes" not in msg \
                and "unknown encoding" not in msg \
                and "encoding" not in msg:
            raise

    # Fallback path: re-read & decode manually
    import re as _re
    with open(xml_path, "rb") as f:
        raw = f.read()

    declared_enc = "utf-8"
    head = raw[:200].decode("ascii", errors="ignore")
    m = _re.search(r"encoding\s*=\s*['\"]([^'\"]+)['\"]", head)
    if m:
        declared_enc = m.group(1).strip()

    # Strip optional BOM, then decode
    try:
        text = raw.decode(declared_enc)
    except (LookupError, UnicodeDecodeError):
        # Last-ditch: try UTF-8 then cp949
        for enc in ("utf-8-sig", "utf-8", "cp949", "euc-kr", "latin-1"):
            try:
                text = raw.decode(enc)
                declared_enc = enc
                break
            except UnicodeDecodeError:
                continue
        else:
            raise

    # Rewrite the encoding in the XML declaration to UTF-8 so downstream
    # writers don't re-emit the broken declaration.
    text = _re.sub(
        r"(<\?xml[^?]*?encoding\s*=\s*)['\"][^'\"]+['\"]",
        r"\1'UTF-8'",
        text, count=1)

    root = ET.fromstring(text)
    return ET.ElementTree(root)


def parse_indoorgml(xml_path: str) -> IndoorModel:
    tree = _read_xml_tree(xml_path)
    root = tree.getroot()

    model = IndoorModel(source_tree=tree, source_path=xml_path)

    # ── ThematicLayers ───────────────────────────────────────────────────────
    # Build a mapping: element → layer_id for spaces / nodes / edges
    _elem_to_layer: Dict[ET.Element, str] = {}

    for tl_elem in root.iter():
        if local_name(tl_elem.tag) != "ThematicLayer":
            continue
        lid = find_any_id(tl_elem) or f"layer_{len(model.layers)+1}"
        theme = first_text(tl_elem, ["theme"]) or "Navigation"
        model.layers[lid] = LayerFeature(
            layer_id=lid, theme=theme, source_element=tl_elem
        )
        for child in tl_elem.iter():
            _elem_to_layer[child] = lid

    # Helper: find the layer_id for a given element
    def _layer_of(elem: ET.Element) -> str:
        return _elem_to_layer.get(elem, "")

    # ── Spaces ───────────────────────────────────────────────────────────────
    for elem in root.iter():
        ln = local_name(elem.tag)
        if ln in SPACE_TAG_HINTS:
            fid = find_any_id(elem) or f"space_{len(model.spaces)+1}"
            # The writer emits the human-readable name as `<cellSpaceName>`
            # (per IndoorGML 2.0). Older files / other tools sometimes
            # use a plain `<name>` instead, so we accept both.
            name = (first_text(elem, ["cellSpaceName"])
                    or first_text(elem, ["name"]) or "")
            level = get_direct_child_text_by_localname(elem, "level") or first_text(elem, ["level"])
            function = get_direct_child_text_by_localname(elem, "function") or first_text(elem, ["function"]) or ""
            polygons = extract_polygons(elem)

            centers: List[Tuple[float, float, float]] = []
            for poly in polygons:
                c = polygon_centroid(poly)
                if c is not None:
                    centers.append(c)

            # Extract <duality xlink:href="#nodeId"> — only consider direct
            # children of this CellSpace so we don't accidentally capture
            # a duality element of a nested feature.
            duality: str = ""
            for sub in elem:
                if local_name(sub.tag) != "duality":
                    continue
                for k, v in sub.attrib.items():
                    if k.endswith("href"):
                        rid = href_to_id(v)
                        if rid:
                            duality = rid
                        break
                if duality:
                    break

            # IndoorGML 2.0 <poi> element (boolean text, lowercased)
            is_poi_text = get_direct_child_text_by_localname(elem, "poi")
            is_poi_flag = (is_poi_text or "").strip().lower() == "true"

            # Heuristic: any cell named "VS_<...>" with no geometry is a
            # virtual cell created by this editor. Restoring the flag
            # here keeps later checks (Find Cells without Node, etc.)
            # consistent across save → reload round-trips.
            is_virtual_flag = (fid.startswith("VS_") and not polygons)

            if fid in model.spaces:
                model.load_conflicts.append((ln, fid, 2))

            model.spaces[fid] = SpaceFeature(
                feature_id=fid,
                name=name,
                cls=ln,
                level=level,
                function=function,
                polygons_3d=polygons,
                centers=centers,
                source_element=elem,
                layer_id=_layer_of(elem),
                duality=duality,
                is_poi=is_poi_flag,
                is_virtual=is_virtual_flag,
            )

    # ── Nodes ────────────────────────────────────────────────────────────────
    for elem in root.iter():
        ln = local_name(elem.tag)
        if ln in NODE_TAG_HINTS:
            nid = find_any_id(elem) or f"node_{len(model.nodes)+1}"
            coord: Optional[Tuple[float, float, float]] = None

            for sub in elem.iter():
                if local_name(sub.tag) == "Point":
                    pts = extract_all_pos_points(sub)
                    if pts:
                        coord = pts[0]
                        break

            if coord is None:
                pts = extract_all_pos_points(elem)
                if pts:
                    coord = pts[0]

            connects: List[str] = []
            duality: str = ""
            for sub in elem.iter():
                sub_ln = local_name(sub.tag)
                for k, v in sub.attrib.items():
                    if k.endswith("href"):
                        rid = href_to_id(v)
                        if rid:
                            if sub_ln == "duality":
                                duality = rid
                            else:
                                connects.append(rid)

            if nid in model.nodes:
                model.load_conflicts.append(("Node", nid, 2))

            model.nodes[nid] = NodeFeature(
                node_id=nid,
                coord=coord,
                connects=list(dict.fromkeys(connects)),
                source_element=elem,
                duality=duality,
                layer_id=_layer_of(elem),
            )

    # ── Edges ────────────────────────────────────────────────────────────────
    for elem in root.iter():
        ln = local_name(elem.tag)
        if ln in EDGE_TAG_HINTS:
            eid = find_any_id(elem) or f"edge_{len(model.edges)+1}"

            coords: List[Tuple[float, float, float]] = []
            line_candidates = extract_linestrings(elem)
            if line_candidates:
                coords = line_candidates[0]
            else:
                pts = extract_all_pos_points(elem)
                if len(pts) >= 2:
                    coords = pts

            # Only <connects> feeds the endpoint list. Scooping up *every*
            # descendant href (as an earlier version did) also swallowed the
            # sibling <duality> reference, so an edge carrying a duality came
            # out with three "endpoints" — the third one dangling, because an
            # Edge's duality points at a CellBoundary, not a Node. That bogus
            # third entry then reached the XML writer and the renderer.
            # An Edge has exactly two endpoints; keep both even when they are
            # equal, so a self-loop in the source stays visible to Check
            # rather than being silently collapsed by de-duplication.
            connects: List[str] = []
            duality: str = ""
            for sub in elem.iter():
                sub_ln = local_name(sub.tag)
                if sub_ln not in ("connects", "duality"):
                    continue
                for k, v in sub.attrib.items():
                    if k.endswith("href"):
                        rid = href_to_id(v)
                        if rid:
                            if sub_ln == "duality":
                                duality = rid
                            else:
                                connects.append(rid)

            # Parse <core:weight> child if present; default 1.0 otherwise
            weight_val = 1.0
            for sub in elem:
                if local_name(sub.tag) == "weight" and sub.text and sub.text.strip():
                    try:
                        weight_val = float(sub.text.strip())
                    except ValueError:
                        pass
                    break

            if eid in model.edges:
                model.load_conflicts.append(("Edge", eid, 2))

            model.edges[eid] = EdgeFeature(
                edge_id=eid,
                coords=coords,
                connects=connects,
                source_element=elem,
                layer_id=_layer_of(elem),
                weight=weight_val,
                duality=duality,
            )

    fill_missing_node_coords_from_spaces(model)
    fill_missing_node_coords_from_edges(model)
    fill_node_levels_from_edges_and_spaces(model)
    space_refs = sum(1 for e in model.edges.values()
                     for r in e.connects if r in model.spaces)
    normalize_edge_connects_to_node_ids(model)
    if space_refs:
        # Not a duplicate-id conflict, but the same class of problem: the
        # file on disk says something the model does not. Recorded so the
        # UI can tell the user the file violates the Edge→Node constraint
        # (the XSD will not, since xlink:href is just an anyURI).
        model.load_conflicts.append(("connects→CellSpace", "", space_refs))
    rebuild_all_edge_coords_from_nodes(model)

    # Cross-link: set space.duality from node.duality (reverse direction)
    for node in model.nodes.values():
        if node.duality and node.duality in model.spaces:
            model.spaces[node.duality].duality = node.node_id

    # ── InterLayerConnections ─────────────────────────────────────────────
    for elem in root.iter():
        if local_name(elem.tag) != "InterLayerConnection":
            continue
        ilc_id = find_any_id(elem) or f"ILC_{len(model.inter_layer_connections)+1}"
        topo_expr = first_text(elem, ["typeOfTopoExpression"]) or "dummy"
        space_ids: List[str] = []
        for sub in elem.iter():
            for k, v in sub.attrib.items():
                if k.endswith("href"):
                    rid = href_to_id(v)
                    if rid and rid in model.spaces:
                        space_ids.append(rid)
        if len(space_ids) >= 2:
            s1, s2 = space_ids[0], space_ids[1]
            model.inter_layer_connections[ilc_id] = InterLayerConnection(
                ilc_id=ilc_id,
                space1_id=s1,
                space2_id=s2,
                layer1_id=model.spaces[s1].layer_id,
                layer2_id=model.spaces[s2].layer_id,
                type_of_topo_expr=topo_expr,
                source_element=elem,
            )

    return model


def resolve_connect_ref_to_node_id(model: IndoorModel, ref: str) -> str:
    """Map one `connects` reference onto a Node id, if that is possible.

    An IndoorGML Edge connects Nodes. Converters routinely write the dual
    CellSpace id instead ('cell_X' where the node is 'S_cell_X'), and the
    XSD cannot catch it: `xlink:href` is declared `xsd:anyURI`, so a
    validator only checks that the string looks like a URI, never what kind
    of element it points at. Such a file validates cleanly and still breaks
    every router that dereferences the endpoint.

    Resolution order:
      1. already a Node — return unchanged;
      2. a CellSpace whose `duality` names a Node — return that Node;
      3. anything else — return unchanged, so the caller can preserve it
         verbatim and let Check → Find Broken Edge Connections report it.

    Step 2 is deliberately keyed off the *duality link* rather than an
    'S_' + id naming convention: this file contains cells whose dual node
    does not follow that pattern (VS_S_2d0c0 → S_2d0c0), and guessing from
    the id would silently mis-wire them.
    """
    if ref in model.nodes:
        return ref
    space = model.spaces.get(ref)
    if space is not None and space.duality and space.duality in model.nodes:
        return space.duality
    return ref


def normalize_edge_connects_to_node_ids(model: IndoorModel) -> None:
    """Replace space-ID references in edge.connects with the corresponding node IDs.

    IndoorGML 2 Edge.connects typically references CellSpace IDs (e.g. 'cell_XXX'),
    while the dual Node has ID 'S_cell_XXX'.  After normalization, edge.connects
    uses node IDs directly, so drag / selection logic can match them without a
    separate duality lookup.
    """
    space_to_node: Dict[str, str] = {}
    for node in model.nodes.values():
        if node.duality:
            space_to_node[node.duality] = node.node_id
        else:
            # Fallback: connects list of a node typically contains its dual space ID
            for ref in node.connects:
                if ref in model.spaces:
                    space_to_node[ref] = node.node_id
    for edge in model.edges.values():
        edge.connects = [
            space_to_node.get(ref) or resolve_connect_ref_to_node_id(model, ref)
            for ref in edge.connects
        ]


def _sync_edge_connects_in_xml(model: IndoorModel) -> None:
    """Write `edge.connects` back to each Edge's `<connects>` elements.

    Two problems are fixed here, both of which used to survive a round trip
    because the writer only touched geometry and never revisited an existing
    Edge's references.

    *Endpoint edits were lost.* Re-pointing an edge in the editor changed
    `edge.connects` in memory only; the file kept the old endpoints.

    *Mixed reference conventions.* An IndoorGML Edge connects Nodes, but
    converters commonly emit the dual CellSpace id instead ('cell_X' where
    the node is 'S_cell_X'). `parse_indoorgml` resolves those on load via
    `normalize_edge_connects_to_node_ids`, so the editor works either way —
    but the file stayed inconsistent, and edges created in the editor were
    written with node ids, leaving one file with both conventions (in the
    hospital test model: 16523 cell-style vs 6629 node-style references,
    with 2081 edges mixing the two within a single element). Normalising on
    save makes every endpoint a Node reference.

    A reference that resolves to nothing is left exactly as-is rather than
    dropped, so Check → Find Broken Edge Connections can still report it.
    """
    for edge in model.edges.values():
        elem = edge.source_element
        if elem is None:
            continue

        pos_elems = [c for c in elem if local_name(c.tag) == "connects"]
        if not pos_elems:
            continue

        # Resolve each endpoint independently. An earlier version bailed out
        # unless *every* endpoint resolved, which meant one broken reference
        # kept its whole edge on the old cell-style convention.
        refs = [resolve_connect_ref_to_node_id(model, r) for r in edge.connects]

        for ce, ref in zip(pos_elems, refs):
            for k in list(ce.attrib):
                if k.endswith("href"):
                    ce.attrib[k] = f"#{ref}"
                    break
            else:
                ce.attrib[f"{XLINK}href"] = f"#{ref}"

        # An edge whose endpoint count changed in the editor needs the
        # element list resized, not just rewritten.
        if len(refs) > len(pos_elems):
            template = pos_elems[-1]
            idx = list(elem).index(template)
            for extra in refs[len(pos_elems):]:
                ce = ET.Element(template.tag)
                ce.attrib[f"{XLINK}href"] = f"#{extra}"
                idx += 1
                elem.insert(idx, ce)
        elif len(refs) < len(pos_elems):
            for ce in pos_elems[len(refs):]:
                elem.remove(ce)


def rebuild_all_edge_coords_from_nodes(model: IndoorModel) -> None:
    """After connects are normalized to node IDs, rebuild every edge's coords
    directly from the connected nodes' positions."""
    for edge in model.edges.values():
        pts = [model.nodes[ref].coord for ref in edge.connects
               if ref in model.nodes and model.nodes[ref].coord is not None]
        # A malformed source may point both <connects> at the same cell.
        # Rebuilding from those endpoints would collapse the edge to a
        # zero-length segment and it would vanish from the canvas, so keep
        # whatever geometry the file actually carried and let Check report it.
        if len(pts) >= 2 and pts[0] != pts[-1]:
            edge.coords = [pts[0], pts[-1]]



def fill_missing_node_coords_from_spaces(model: IndoorModel) -> None:
    for node in model.nodes.values():
        if node.coord is not None:
            continue
        for ref in node.connects:
            sp = model.spaces.get(ref)
            if sp and sp.centers:
                node.coord = sp.centers[0]
                break


def fill_missing_node_coords_from_edges(model: IndoorModel) -> None:
    for node_id, node in model.nodes.items():
        if node.coord is not None:
            continue

        candidate_points: List[Tuple[float, float, float]] = []
        for edge in model.edges.values():
            if node_id in edge.connects and len(edge.coords) >= 2:
                candidate_points.append(edge.coords[0])
                candidate_points.append(edge.coords[-1])

        if candidate_points:
            node.coord = candidate_points[0]


def fill_node_levels_from_edges_and_spaces(model: IndoorModel) -> None:
    """Resolve every node's level. Priority order (first match wins):
      1. The dual CellSpace (`node.duality`). Most authoritative —
         IndoorGML 2.0 ties a node and its dual cell to the same floor,
         and cells are routinely tagged with `<level>` even when nodes
         are not.
      2. Any space directly referenced from `node.connects`.
      3. The level of any space referenced by an edge incident on this
         node (legacy heuristic — can pick the WRONG floor when the
         node itself is a vertical-edge endpoint, so it ranks below the
         duality check).
      4. Z-coordinate proximity to a known floor reference Z (final
         fallback).

    Re-runs even when `node.level` is already set IF the duality says
    a different floor — fixes files that arrived with all nodes
    mislabeled but with correct cell-level data.
    """
    # First pass: trust duality unconditionally (overrides existing
    # node.level if the dual cell disagrees, since the cell is the
    # authoritative carrier of `<level>` in IndoorGML 2.0).
    for node in model.nodes.values():
        if node.duality:
            sp = model.spaces.get(node.duality)
            if sp and sp.level:
                node.level = sp.level
                continue
        if node.level is not None:
            continue
        # 2) any space referenced from node.connects
        for ref in node.connects:
            sp = model.spaces.get(ref)
            if sp and sp.level:
                node.level = sp.level
                break
        if node.level is not None:
            continue
        # 3) indirect: spaces referenced by edges incident on this node
        for edge in model.edges.values():
            if node.node_id not in edge.connects:
                continue
            for ref in edge.connects:
                sp = model.spaces.get(ref)
                if sp and sp.level:
                    node.level = sp.level
                    break
            if node.level is not None:
                break

    # Still unresolved: infer from Z coordinate (each floor has a unique Z)
    floor_z: Dict[str, float] = {}
    for sp in model.spaces.values():
        if sp.level and sp.polygons_3d:
            z = sp.polygons_3d[0][0][2]
            floor_z[sp.level] = z  # same Z for all spaces on same floor

    for node in model.nodes.values():
        if node.level is not None or node.coord is None:
            continue
        nz = node.coord[2]
        if floor_z:
            node.level = min(floor_z, key=lambda f: abs(floor_z[f] - nz))


# =========================================================
# Save helpers
# =========================================================
def _update_ring_coords(elem: ET.Element, poly: List[Tuple[float, float, float]]) -> None:
    """Update the first LinearRing's coordinate text inside elem to match poly.

    The list of vertices written to the XML must EXACTLY match `poly`:
      • If the source ring has more `<pos>` (or `<posList>`) entries than the
        new polygon, the trailing entries are removed.
      • If the source ring has fewer entries than the new polygon, additional
        `<pos>` siblings are inserted (cloning the namespace + srsDimension
        of the existing pos elements).
    The previous implementation only overwrote in-place, leaving stale
    trailing vertices when a polygon shrank (causing self-intersecting
    “zombie” geometry on reload) and silently dropping new vertices when a
    polygon grew.

    A `gml:LinearRing` must repeat its first position at the end. Callers
    hold rings in both conventions — cells parsed from XML keep the closing
    duplicate, cells built in the editor drop it — and this function is the
    single point every geometry edit passes through on its way to the XML,
    so the ring is closed here rather than at each call site. Files whose
    rings were already left open are therefore repaired on the next save.
    """
    if len(poly) >= 3 and poly[0][:2] != poly[-1][:2]:
        poly = list(poly) + [poly[0]]

    for ring in elem.iter():
        if local_name(ring.tag) not in ("LinearRing", "Ring"):
            continue

        # posList: a single text element holding the whole flat list
        for child in ring:
            if local_name(child.tag) == "posList":
                dim = detect_srs_dimension(child)
                if dim == 2:
                    child.text = " ".join(f"{p[0]} {p[1]}" for p in poly)
                else:
                    child.text = " ".join(f"{p[0]} {p[1]} {p[2]}" for p in poly)
                return

        # Individual <pos> elements
        pos_elems = [c for c in ring if local_name(c.tag) == "pos"]
        if pos_elems:
            # Determine the namespace tag and srsDimension to use for any
            # newly inserted <pos> children — copy from the existing template.
            template_tag = pos_elems[0].tag
            template_dim = detect_srs_dimension(pos_elems[0])

            n_have = len(pos_elems)
            n_need = len(poly)

            # 1) Overwrite existing pos elements with new coords
            for i in range(min(n_have, n_need)):
                x, y, z = poly[i]
                dim = detect_srs_dimension(pos_elems[i])
                pos_elems[i].text = (
                    f"{x} {y}" if dim == 2 else f"{x} {y} {z}"
                )

            # 2) If the new polygon has fewer vertices, drop the surplus
            if n_have > n_need:
                for stale in pos_elems[n_need:]:
                    ring.remove(stale)

            # 3) If the new polygon has more vertices, append new <pos>
            elif n_need > n_have:
                last_pos = pos_elems[-1]
                last_idx = list(ring).index(last_pos)
                for j in range(n_have, n_need):
                    x, y, z = poly[j]
                    new_pos = ET.Element(template_tag)
                    new_pos.attrib["srsDimension"] = str(template_dim)
                    new_pos.text = (
                        f"{x} {y}" if template_dim == 2 else f"{x} {y} {z}"
                    )
                    ring.insert(last_idx + 1 + (j - n_have), new_pos)
            return

        return  # only touch first ring



def export_indoorgml_floors(model: IndoorModel, floors: List[str],
                            out_path: str) -> str:
    """Export the given floors into a single IndoorGML file.

    Z (height) values of all geometry are preserved as-is — every
    `<gml:pos>` is copied verbatim from the source tree, so the exported
    floors keep their original elevations relative to one another.

    Members on floors NOT in `floors` are removed from a deep copy of the
    source tree:
      • cellSpaceMember whose CellSpace level is not in `floors`
      • nodeMember     whose Node level     is not in `floors`
      • edgeMember     whose Edge endpoints are not all on listed floors
    All other XML structure (namespaces, layer hierarchy, headers, ILCs)
    is preserved. Returns the written path.
    """
    if model.source_tree is None:
        raise ValueError("No source XML tree available.")
    if not floors:
        raise ValueError("No floors selected for export.")

    floor_set = set(floors)
    # Pre-compute the set of in-scope spaces and nodes
    keep_space_ids: Set[str] = set()
    for floor in floors:
        keep_space_ids |= get_space_ids_on_floor(model, floor)
    keep_node_ids = {
        nid for nid, n in model.nodes.items()
        if n.level and n.level in floor_set
    }

    # Edges are tricky: the parser conflates <connects xlink:href> (real
    # endpoints) with <duality xlink:href> (link to a CellSpace) inside the
    # same `e.connects` list, and some IndoorGML files include dangling
    # endpoint references that aren't even in the Node table. A strict
    # endpoint-membership filter therefore drops many legitimate in-floor
    # edges. Use the edge's *Z coordinates* — already reliable in the GML
    # geometry — to decide which floor the edge belongs to.
    floor_z: Dict[str, float] = {}
    for sp in model.spaces.values():
        if sp.level and sp.polygons_3d:
            floor_z[sp.level] = sp.polygons_3d[0][0][2]

    def _floor_at_z(z: float) -> Optional[str]:
        if not floor_z:
            return None
        return min(floor_z.keys(), key=lambda k: abs(floor_z[k] - z))

    keep_edge_ids: Set[str] = set()
    for eid, e in model.edges.items():
        if not e.coords:
            # No geometry — fall back to endpoint-membership heuristic
            if all(c in keep_node_ids or c in keep_space_ids
                   for c in e.connects):
                keep_edge_ids.add(eid)
            continue
        # Vertical edges span two floors, so check BOTH endpoints' Z.
        first_lvl = _floor_at_z(e.coords[0][2])
        last_lvl  = _floor_at_z(e.coords[-1][2])
        if (first_lvl in floor_set) or (last_lvl in floor_set):
            keep_edge_ids.add(eid)

    tree_copy = copy.deepcopy(model.source_tree)
    root = tree_copy.getroot()

    for elem in root.iter():
        ln = local_name(elem.tag)
        if ln == "PrimalSpaceLayer":
            to_remove = []
            for child in elem:
                if local_name(child.tag) != "cellSpaceMember":
                    continue
                space_elem = next(
                    (c for c in child if local_name(c.tag) in SPACE_TAG_HINTS), None)
                if space_elem is not None:
                    sid = find_any_id(space_elem)
                    if sid not in keep_space_ids:
                        to_remove.append(child)
            for ch in to_remove:
                elem.remove(ch)
        elif ln == "DualSpaceLayer":
            to_remove = []
            for child in elem:
                cln = local_name(child.tag)
                if cln == "nodeMember":
                    node_elem = next(
                        (c for c in child if local_name(c.tag) in NODE_TAG_HINTS), None)
                    if node_elem is not None:
                        nid = find_any_id(node_elem)
                        if nid not in keep_node_ids:
                            to_remove.append(child)
                elif cln == "edgeMember":
                    edge_elem = next(
                        (c for c in child if local_name(c.tag) in EDGE_TAG_HINTS), None)
                    if edge_elem is not None:
                        eid = find_any_id(edge_elem)
                        if eid not in keep_edge_ids:
                            to_remove.append(child)
            for ch in to_remove:
                elem.remove(ch)

    _indent_tree(tree_copy)
    tree_copy.write(out_path, encoding="unicode", xml_declaration=True)
    return out_path


def load_function_codes(csv_path: str) -> List[Tuple[str, str]]:
    """Load function code list from CSV. Returns list of (code, display_label).
    CSV format (header row optional):
      code,description
      Administration,행정 공간
      ...
    If only one column, code == display_label.
    """
    codes: List[Tuple[str, str]] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0].strip().startswith("#"):
                continue
            code = row[0].strip()
            desc = row[1].strip() if len(row) > 1 and row[1].strip() else code
            if code:
                codes.append((code, desc))
    return codes


def _build_function_color_map(codes: List[Tuple[str, str]]) -> Dict[str, str]:
    """Assign a distinct hex color to each function code using a fixed palette.

    Uses matplotlib's tab20 colormap (20 colors); cycles if more codes than colors.
    Returns dict mapping code → hex color string (e.g. '#4e79a7').
    """
    import matplotlib.colors as mcolors
    import matplotlib as mpl

    # tab20 gives 20 visually distinct colours
    # matplotlib.colormaps dict available from 3.7+; fall back to cm.get_cmap for older versions
    try:
        cmap = mpl.colormaps["tab20"]
    except AttributeError:
        import matplotlib.cm as cm
        cmap = cm.get_cmap("tab20")
    result: Dict[str, str] = {}
    for i, (code, _) in enumerate(codes):
        rgba = cmap(i % cmap.N)
        result[code] = mcolors.to_hex(rgba)
    return result


def _bgcfg_path(gml_path: str) -> str:
    return gml_path + ".bgcfg.json"


def save_bg_config(gml_path: str, floor: str, image_path: str, extent: List[float]) -> None:
    """Save background config for one floor into the sidecar JSON (merges with existing)."""
    p = _bgcfg_path(gml_path)
    cfg: dict = {}
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    cfg[floor] = {"image_path": image_path, "extent": extent}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def load_bg_config_for_floor(gml_path: str, floor: str) -> Optional[dict]:
    """Return {"image_path": ..., "extent": [...]} for the given floor, or None."""
    p = _bgcfg_path(gml_path)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get(floor)
    except Exception:
        return None


# ── Reference points (file-wide; shared across all floors) ─────────────────
# Stored under the special key "_reference_points" in the same sidecar
# JSON so all per-floor entries and the global anchors live together.
_REFPOINTS_KEY = "_reference_points"


def save_reference_points(gml_path: str,
                          ll: Tuple[float, float],
                          ur: Tuple[float, float]) -> None:
    """Persist the two reference (anchor) points to the sidecar JSON."""
    p = _bgcfg_path(gml_path)
    cfg: dict = {}
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    cfg[_REFPOINTS_KEY] = {"LL": [ll[0], ll[1]], "UR": [ur[0], ur[1]]}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def load_reference_points(gml_path: str
                          ) -> Optional[Tuple[Tuple[float, float],
                                              Tuple[float, float]]]:
    """Return ((LL_x, LL_y), (UR_x, UR_y)) or None."""
    p = _bgcfg_path(gml_path)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        rp = cfg.get(_REFPOINTS_KEY)
        if not rp:
            return None
        ll = rp.get("LL"); ur = rp.get("UR")
        if not ll or not ur or len(ll) < 2 or len(ur) < 2:
            return None
        return ((float(ll[0]), float(ll[1])),
                (float(ur[0]), float(ur[1])))
    except Exception:
        return None


def clear_reference_points(gml_path: str) -> None:
    p = _bgcfg_path(gml_path)
    if not os.path.exists(p):
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if _REFPOINTS_KEY in cfg:
            cfg.pop(_REFPOINTS_KEY)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def _update_node_coord_in_xml(elem: ET.Element, coord: Tuple[float, float, float]) -> None:
    """Update the first pos/posList inside a Node element to the new coordinate."""
    for sub in elem.iter():
        ln = local_name(sub.tag)
        if ln == "pos":
            dim = detect_srs_dimension(sub)
            sub.text = f"{coord[0]} {coord[1]}" if dim == 2 else f"{coord[0]} {coord[1]} {coord[2]}"
            return
        if ln == "posList":
            dim = detect_srs_dimension(sub)
            sub.text = f"{coord[0]} {coord[1]}" if dim == 2 else f"{coord[0]} {coord[1]} {coord[2]}"
            return


def _detect_namespaces(root: ET.Element) -> Tuple[str, str, str]:
    """Return (core_ns, gml_ns, xlink_ns) from the root element's tag and namespace map."""
    core_ns = "http://www.opengis.net/indoorgml/2.0/core"
    gml_ns  = "http://www.opengis.net/gml/3.2"
    xlink_ns = "http://www.w3.org/1999/xlink"
    # Try to infer from root tag
    if "}" in root.tag:
        candidate = root.tag.split("}")[0][1:]
        if "indoorgml" in candidate:
            core_ns = candidate
    return core_ns, gml_ns, xlink_ns


def _detect_id_ns(root: ET.Element, default_ns: str) -> str:
    """Return the namespace URI used by the `id` attribute on existing feature
    elements (CellSpace / Node / Edge / ThematicLayer / …).

    Some IndoorGML 2.0 files bind `id` to a namespace other than GML (often the
    same as `core`). Re-using whatever namespace the source file used keeps
    the prefix consistent after ElementTree re-serialization — otherwise new
    elements get an auto-generated prefix like `ns1:id` while existing ones
    keep `gml:id` (or vice versa).
    """
    FEATURE_LOCAL_NAMES = {
        "CellSpace", "Node", "Edge", "ThematicLayer",
        "PrimalSpaceLayer", "DualSpaceLayer",
    }
    for elem in root.iter():
        if local_name(elem.tag) in FEATURE_LOCAL_NAMES:
            for k in elem.attrib.keys():
                if "}" in k and k.split("}", 1)[1] == "id":
                    return k.split("}")[0][1:]
    return default_ns


def _find_dual_space_layer(root: ET.Element) -> Optional[ET.Element]:
    """Return the first DualSpaceLayer element in the tree."""
    for elem in root.iter():
        if local_name(elem.tag) == "DualSpaceLayer":
            return elem
    return None


def _find_primal_space_layer(root: ET.Element) -> Optional[ET.Element]:
    for elem in root.iter():
        if local_name(elem.tag) == "PrimalSpaceLayer":
            return elem
    return None


def _make_primal_space_layer_elem(CORE: str, ID_NS: str, psl_id: str) -> ET.Element:
    psl = ET.Element(f"{CORE}PrimalSpaceLayer")
    psl.attrib[f"{{{ID_NS}}}id"] = psl_id
    return psl


def _make_dual_space_layer_elem(CORE: str, ID_NS: str, dsl_id: str) -> ET.Element:
    dsl = ET.Element(f"{CORE}DualSpaceLayer")
    dsl.attrib[f"{{{ID_NS}}}id"] = dsl_id
    return dsl


def _find_thematic_layer_by_id(root: ET.Element, layer_id: str) -> Optional[ET.Element]:
    """Return the ThematicLayer element whose gml:id matches layer_id."""
    for elem in root.iter():
        if local_name(elem.tag) == "ThematicLayer":
            if find_any_id(elem) == layer_id:
                return elem
    return None


def _get_or_create_psl_for_layer(root: ET.Element, layer_id: str) -> ET.Element:
    """Return the PrimalSpaceLayer inside the ThematicLayer with the given id.
    Falls back to the first PrimalSpaceLayer if the layer is not found,
    and creates one from scratch if none exists at all."""
    core_ns, gml_ns, _ = _detect_namespaces(root)
    id_ns = _detect_id_ns(root, gml_ns)
    CORE  = f"{{{core_ns}}}"
    ID_NS = f"{{{id_ns}}}"

    tl = _find_thematic_layer_by_id(root, layer_id) if layer_id else None
    if tl is not None:
        for child in tl.iter():
            if local_name(child.tag) == "PrimalSpaceLayer":
                return child
        # PrimalSpaceLayer absent in this ThematicLayer → create it
        primal_wrap = ET.SubElement(tl, f"{CORE}primalSpace")
        psl = ET.SubElement(primal_wrap, f"{CORE}PrimalSpaceLayer")
        psl.attrib[f"{ID_NS}id"] = f"PS_{layer_id}"
        return psl

    # Fallback: no specific layer found, use the first one
    return _get_or_create_primal_space_layer(root)


def _get_or_create_dsl_for_layer(root: ET.Element, layer_id: str) -> ET.Element:
    """Return the DualSpaceLayer inside the ThematicLayer with the given id."""
    core_ns, gml_ns, _ = _detect_namespaces(root)
    id_ns = _detect_id_ns(root, gml_ns)
    CORE  = f"{{{core_ns}}}"
    ID_NS = f"{{{id_ns}}}"

    tl = _find_thematic_layer_by_id(root, layer_id) if layer_id else None
    if tl is not None:
        for child in tl.iter():
            if local_name(child.tag) == "DualSpaceLayer":
                return child
        # DualSpaceLayer absent in this ThematicLayer → create it
        dual_wrap = ET.SubElement(tl, f"{CORE}dualSpace")
        dsl = ET.SubElement(dual_wrap, f"{CORE}DualSpaceLayer")
        dsl.attrib[f"{ID_NS}id"] = f"DS_{layer_id}"
        return dsl

    # Fallback
    return _get_or_create_dual_space_layer(root)


def _get_or_create_primal_space_layer(root: ET.Element) -> ET.Element:
    """Return PrimalSpaceLayer, creating the full layer hierarchy if absent."""
    psl = _find_primal_space_layer(root)
    if psl is not None:
        return psl
    core_ns, gml_ns, _ = _detect_namespaces(root)
    id_ns = _detect_id_ns(root, gml_ns)
    CORE  = f"{{{core_ns}}}"
    ID_NS = f"{{{id_ns}}}"
    # Ensure root has correct namespace tag
    if root.tag == "IndoorFeatures":
        root.tag = f"{CORE}IndoorFeatures"
        root.attrib[f"{ID_NS}id"] = "IF_new"
        root.attrib[f"xmlns:{core_ns.split('/')[-1]}"] = core_ns
    layers = ET.SubElement(root, f"{CORE}layers")
    layer  = ET.SubElement(layers, f"{CORE}ThematicLayer")
    layer.attrib[f"{ID_NS}id"] = "L1"
    primal_wrap = ET.SubElement(layer, f"{CORE}primalSpace")
    psl = ET.SubElement(primal_wrap, f"{CORE}PrimalSpaceLayer")
    psl.attrib[f"{ID_NS}id"] = "PS1"
    return psl


def _get_or_create_dual_space_layer(root: ET.Element) -> ET.Element:
    """Return DualSpaceLayer, creating it (under existing ThematicLayer) if absent."""
    dsl = _find_dual_space_layer(root)
    if dsl is not None:
        return dsl
    core_ns, gml_ns, _ = _detect_namespaces(root)
    id_ns = _detect_id_ns(root, gml_ns)
    CORE  = f"{{{core_ns}}}"
    ID_NS = f"{{{id_ns}}}"
    # Find or create ThematicLayer
    tl = None
    for elem in root.iter():
        if local_name(elem.tag) == "ThematicLayer":
            tl = elem
            break
    if tl is None:
        # Fallback: create full structure
        _get_or_create_primal_space_layer(root)
        for elem in root.iter():
            if local_name(elem.tag) == "ThematicLayer":
                tl = elem
                break
    dual_wrap = ET.SubElement(tl, f"{CORE}dualSpace")
    dsl = ET.SubElement(dual_wrap, f"{CORE}DualSpaceLayer")
    dsl.attrib[f"{ID_NS}id"] = "DS1"
    return dsl


def _get_floor_ceiling_z(model: IndoorModel, floor: str):
    """Return (floor_z, ceiling_z) by inspecting existing spaces on the
    given floor. Z values are in cm (matching the rest of the editor's
    coordinate convention).
    """
    for sp in model.spaces.values():
        if sp.level == floor and len(sp.polygons_3d) >= 2:
            fz = sp.polygons_3d[0][0][2]
            cz = sp.polygons_3d[1][0][2]
            return fz, cz
        if sp.level == floor and len(sp.polygons_3d) == 1:
            fz = sp.polygons_3d[0][0][2]
            return fz, fz + 300        # default 300 cm head height
    return 0.0, 300.0


def _add_new_spaces_to_xml(model: IndoorModel, new_space_ids: Set[str]) -> None:
    if not new_space_ids or model.source_tree is None:
        return
    root = model.source_tree.getroot()

    core_ns, gml_ns, xlink_ns = _detect_namespaces(root)
    id_ns = _detect_id_ns(root, gml_ns)
    CORE  = f"{{{core_ns}}}"
    GML   = f"{{{gml_ns}}}"
    XLINK = f"{{{xlink_ns}}}"
    ID_NS = f"{{{id_ns}}}"

    # Index existing CellSpace elements by id so we never write a duplicate.
    existing_space_by_id: Dict[str, ET.Element] = {}
    for elem in root.iter():
        if local_name(elem.tag) in SPACE_TAG_HINTS:
            for k, v in elem.attrib.items():
                if local_name(k) == "id":
                    existing_space_by_id[v] = elem
                    break

    # Helper: build a parent map once so we can remove stale elements.
    parent_of: Dict[ET.Element, ET.Element] = {}
    for parent in root.iter():
        for child in parent:
            parent_of[child] = parent

    def _remove_existing_cellspace(sid: str) -> None:
        """Excise the existing CellSpace element (and its enclosing
        cellSpaceMember wrapper, if any) from the tree, so we can write
        a fresh one with the new geometry."""
        elem = existing_space_by_id.pop(sid, None)
        if elem is None:
            return
        p = parent_of.get(elem)
        if p is None:
            return
        gp = parent_of.get(p)
        if gp is not None and local_name(p.tag) == "cellSpaceMember":
            try:
                gp.remove(p)
                return
            except ValueError:
                pass
        try:
            p.remove(elem)
        except ValueError:
            pass

    for sid in new_space_ids:
        sp = model.spaces.get(sid)
        if sp is None:
            continue

        # If a CellSpace with this id already exists in the tree we
        # cannot just rebind to it — its `<gml:pos>` values still
        # carry the OLD geometry (Normalize Floor Z, Repair, etc. all
        # rely on regenerating geometry from `sp.polygons_3d`). Drop
        # the stale element and let the rest of this function emit a
        # fresh one with the up-to-date polygons.
        if sid in existing_space_by_id:
            _remove_existing_cellspace(sid)

        # IndoorGML 2.0 carries the cell subclass in the element local
        # name (CellSpace / TransferSpace / ConnectionSpace /
        # AnchorSpace). Pull it from `sp.cls` and fall back to plain
        # CellSpace.
        cls_tag = (sp.cls or "CellSpace").strip() or "CellSpace"

        # Virtual CellSpace (no geometry, no externalReference) —
        # emit a minimal element and move on.
        if sp.is_virtual or not sp.polygons_3d:
            psl = _get_or_create_psl_for_layer(root, sp.layer_id or "")
            member  = ET.SubElement(psl, f"{CORE}cellSpaceMember")
            cs_elem = ET.SubElement(member, f"{CORE}{cls_tag}")
            cs_elem.attrib[f"{ID_NS}id"] = sid
            if sp.level:
                lv = ET.SubElement(cs_elem, f"{CORE}level")
                lv.text = sp.level
            nm = ET.SubElement(cs_elem, f"{CORE}cellSpaceName")
            nm.text = sp.name or ""
            poi = ET.SubElement(cs_elem, f"{CORE}poi")
            poi.text = "true" if sp.is_poi else "false"
            if sp.duality:
                dual_elem = ET.SubElement(cs_elem, f"{CORE}duality")
                dual_elem.attrib[f"{XLINK}href"] = f"#{sp.duality}"
            sp.source_element = cs_elem
            continue

        # Select the PrimalSpaceLayer that belongs to this space's layer
        psl = _get_or_create_psl_for_layer(root, sp.layer_id or "")

        floor_pts = sp.polygons_3d[0]  # 2D footprint (floor polygon)
        floor_z = floor_pts[0][2]
        ceiling_z = sp.polygons_3d[1][0][2] if len(sp.polygons_3d) >= 2 else floor_z + 300

        # Ensure closed ring for floor/ceiling
        ring = list(floor_pts)
        if ring[0] != ring[-1]:
            ring.append(ring[0])

        member  = ET.SubElement(psl, f"{CORE}cellSpaceMember")
        cs_elem = ET.SubElement(member, f"{CORE}{cls_tag}")
        cs_elem.attrib[f"{ID_NS}id"] = sid

        geom_wrap  = ET.SubElement(cs_elem, f"{CORE}cellSpaceGeom")
        csg        = ET.SubElement(geom_wrap, f"{CORE}CellSpaceGeometry")
        geom3d     = ET.SubElement(csg, f"{CORE}Geometry3D")
        solid      = ET.SubElement(geom3d, f"{GML}Solid")
        solid.attrib[f"{GML}id"] = f"{sid}_S"
        exterior   = ET.SubElement(solid, f"{GML}exterior")
        shell      = ET.SubElement(exterior, f"{GML}Shell")

        def _poly_member(poly_id, pts):
            sm = ET.SubElement(shell, f"{GML}surfaceMember")
            pg = ET.SubElement(sm, f"{GML}Polygon")
            pg.attrib[f"{GML}id"] = poly_id
            ext = ET.SubElement(pg, f"{GML}exterior")
            lr  = ET.SubElement(ext, f"{GML}LinearRing")
            for p in pts:
                pos = ET.SubElement(lr, f"{GML}pos")
                pos.attrib["srsDimension"] = "3"
                pos.text = f"{p[0]} {p[1]} {p[2]}"

        # Floor polygon
        floor_ring = [(p[0], p[1], floor_z) for p in ring]
        _poly_member(f"{sid}_F", floor_ring)

        # Ceiling polygon
        ceil_ring = [(p[0], p[1], ceiling_z) for p in ring]
        _poly_member(f"{sid}_C", ceil_ring)

        # Wall polygons (one per edge of the footprint)
        for i in range(len(ring) - 1):
            x0, y0 = ring[i][0], ring[i][1]
            x1, y1 = ring[i + 1][0], ring[i + 1][1]
            wall_pts = [
                (x0, y0, floor_z),
                (x1, y1, floor_z),
                (x1, y1, ceiling_z),
                (x0, y0, ceiling_z),
                (x0, y0, floor_z),
            ]
            _poly_member(f"{sid}_W_{i+1}", wall_pts)

        # Metadata
        if sp.level:
            lv = ET.SubElement(cs_elem, f"{CORE}level")
            lv.text = sp.level
        nm = ET.SubElement(cs_elem, f"{CORE}cellSpaceName")
        nm.text = sp.name or ""

        # Duality reference to dual Node
        if sp.duality:
            dual_elem = ET.SubElement(cs_elem, f"{CORE}duality")
            dual_elem.attrib[f"{{{xlink_ns}}}href"] = f"#{sp.duality}"

        sp.source_element = cs_elem


def _add_new_nodes_to_xml(model: IndoorModel, new_node_ids: Set[str]) -> None:
    if not new_node_ids or model.source_tree is None:
        return
    root = model.source_tree.getroot()

    core_ns, gml_ns, xlink_ns = _detect_namespaces(root)
    id_ns = _detect_id_ns(root, gml_ns)
    CORE  = f"{{{core_ns}}}"
    GML   = f"{{{gml_ns}}}"
    ID_NS = f"{{{id_ns}}}"

    # Index existing Node elements by id so we never write a second copy
    # for the same id (defends against undo/redo races and re-creation
    # under the same auto-generated id).
    existing_node_by_id: Dict[str, ET.Element] = {}
    for elem in root.iter():
        if local_name(elem.tag) == "Node":
            for k, v in elem.attrib.items():
                if local_name(k) == "id":
                    existing_node_by_id[v] = elem
                    break

    for nid in new_node_ids:
        node = model.nodes.get(nid)
        if node is None or node.coord is None:
            continue
        x, y, z = node.coord

        if nid in existing_node_by_id:
            # Already in the tree — update its <pos> (and duality href if
            # present) in place rather than appending a sibling.
            n_elem = existing_node_by_id[nid]
            for sub in n_elem.iter():
                if local_name(sub.tag) == "pos":
                    dim = detect_srs_dimension(sub)
                    sub.text = (f"{x} {y}" if dim == 2
                                else f"{x} {y} {z}")
                    break
            if node.duality:
                # Replace existing duality children, then add a fresh one
                for ch in [c for c in list(n_elem)
                           if local_name(c.tag) == "duality"]:
                    n_elem.remove(ch)
                duality_elem = ET.SubElement(n_elem, f"{CORE}duality")
                duality_elem.attrib[f"{{{xlink_ns}}}href"] = f"#{node.duality}"
            node.source_element = n_elem
            continue

        # Brand-new — append to the appropriate DualSpaceLayer
        dsl = _get_or_create_dsl_for_layer(root, node.layer_id or "")

        member  = ET.SubElement(dsl, f"{CORE}nodeMember")
        n_elem  = ET.SubElement(member, f"{CORE}Node")
        n_elem.attrib[f"{ID_NS}id"] = nid
        geom    = ET.SubElement(n_elem, f"{CORE}geometry")
        pt      = ET.SubElement(geom, f"{GML}Point")
        pt.attrib[f"{GML}id"] = f"P_{nid}"
        pos     = ET.SubElement(pt, f"{GML}pos")
        pos.attrib["srsDimension"] = "3"
        pos.text = f"{x} {y} {z}"

        # Add duality reference: use explicit duality if set, else fall back to any same-floor space
        duality_space_id = node.duality
        if not duality_space_id and node.level:
            for sp_id, sp in model.spaces.items():
                if sp.level == node.level:
                    duality_space_id = sp_id
                    break
        duality_elem = ET.SubElement(n_elem, f"{CORE}duality")
        if duality_space_id:
            duality_elem.attrib[f"{{{xlink_ns}}}href"] = f"#{duality_space_id}"

        node.source_element = n_elem
        existing_node_by_id[nid] = n_elem


def _add_new_edges_to_xml(model: IndoorModel, new_edge_ids: Set[str]) -> None:
    if not new_edge_ids or model.source_tree is None:
        return
    root = model.source_tree.getroot()

    core_ns, gml_ns, xlink_ns = _detect_namespaces(root)
    id_ns = _detect_id_ns(root, gml_ns)
    CORE  = f"{{{core_ns}}}"
    GML   = f"{{{gml_ns}}}"
    XLINK = f"{{{xlink_ns}}}"
    ID_NS = f"{{{id_ns}}}"

    # Index existing Edge elements by id so we never write a duplicate.
    existing_edge_by_id: Dict[str, ET.Element] = {}
    for elem in root.iter():
        if local_name(elem.tag) == "Edge":
            for k, v in elem.attrib.items():
                if local_name(k) == "id":
                    existing_edge_by_id[v] = elem
                    break

    for eid in new_edge_ids:
        edge = model.edges.get(eid)
        if edge is None or len(edge.coords) < 2:
            continue

        if eid in existing_edge_by_id:
            # Already in the tree — leave the structural skeleton intact;
            # the LineString coords are refreshed by the regular update
            # pass elsewhere if needed. Just bind source_element so future
            # updates target the right node, and skip appending a copy.
            edge.source_element = existing_edge_by_id[eid]
            continue

        # Select the DualSpaceLayer that belongs to this edge's layer
        dsl = _get_or_create_dsl_for_layer(root, edge.layer_id or "")

        member = ET.SubElement(dsl, f"{CORE}edgeMember")
        e_elem = ET.SubElement(member, f"{CORE}Edge")
        e_elem.attrib[f"{ID_NS}id"] = eid

        # Weight (default 1.0 for newly created edges)
        weight_elem = ET.SubElement(e_elem, f"{CORE}weight")
        weight_elem.text = f"{getattr(edge, 'weight', 1.0)}"

        # Never emit a CellSpace id here. `edge.connects` normally holds node
        # ids, but normalize_edge_connects_to_node_ids() passes through
        # anything it could not resolve, and that value used to be written
        # verbatim — which is how an editor session could *introduce* the
        # very cell-style reference this file already suffers from. Resolve
        # once more at the point of writing; genuinely unresolvable ids are
        # still written as-is so Check can report them.
        for conn_id in edge.connects:
            ce = ET.SubElement(e_elem, f"{CORE}connects")
            ce.attrib[f"{XLINK}href"] = f"#{resolve_connect_ref_to_node_id(model, conn_id)}"

        duality = ET.SubElement(e_elem, f"{CORE}duality")
        duality.text = ""

        geom = ET.SubElement(e_elem, f"{CORE}geometry")
        ls   = ET.SubElement(geom, f"{GML}LineString")
        ls.attrib[f"{GML}id"] = f"{eid}_LS"
        for coord in edge.coords:
            pos = ET.SubElement(ls, f"{GML}pos")
            pos.attrib["srsDimension"] = "3"
            pos.text = f"{coord[0]} {coord[1]} {coord[2]}"

        edge.source_element = e_elem
        existing_edge_by_id[eid] = e_elem


def _indent_tree(tree: ET.ElementTree) -> None:
    """Add pretty-print indentation to an ElementTree (in-place).

    Uses ET.indent() on Python 3.9+; falls back to a simple recursive
    implementation on older versions so the output is always human-readable.
    """
    try:
        ET.indent(tree.getroot(), space="  ")
    except AttributeError:
        def _indent(elem: ET.Element, level: int = 0) -> None:
            pad = "\n" + "  " * level
            if len(elem):
                if not elem.text or not elem.text.strip():
                    elem.text = pad + "  "
                for child in elem:
                    _indent(child, level + 1)
                if not child.tail or not child.tail.strip():
                    child.tail = pad
            if level and (not elem.tail or not elem.tail.strip()):
                elem.tail = pad
        _indent(tree.getroot())


def save_indoorgml(model: IndoorModel, path: str,
                   new_node_ids: Optional[Set[str]] = None,
                   new_edge_ids: Optional[Set[str]] = None,
                   new_space_ids: Optional[Set[str]] = None) -> None:
    if model.source_tree is None:
        raise ValueError("No source XML tree available.")

    root = model.source_tree.getroot()

    # Update existing space geometry
    for sp in model.spaces.values():
        if sp.source_element is None or not sp.polygons_3d:
            continue
        _update_ring_coords(sp.source_element, sp.polygons_3d[0])

    # Update existing node coordinates
    for node in model.nodes.values():
        if node.source_element is not None and node.coord is not None:
            _update_node_coord_in_xml(node.source_element, node.coord)

    _sync_edge_connects_in_xml(model)

    # Remove deleted spaces from XML
    psl = _find_primal_space_layer(root)
    if psl is not None:
        for member in list(psl):
            if local_name(member.tag) == "cellSpaceMember":
                for child in list(member):
                    if local_name(child.tag) in SPACE_TAG_HINTS:
                        gml_id = next(
                            (v for k, v in child.attrib.items() if local_name(k) == "id"),
                            None
                        )
                        if gml_id and gml_id not in model.spaces:
                            psl.remove(member)
                        break

    # Remove deleted nodes/edges from XML (elements no longer in model)
    dsl = _find_dual_space_layer(root)
    if dsl is not None:
        for member in list(dsl):
            tag = local_name(member.tag)
            if tag == "nodeMember":
                for child in list(member):
                    if local_name(child.tag) == "Node":
                        gml_id = next(
                            (v for k, v in child.attrib.items() if local_name(k) == "id"),
                            None
                        )
                        if gml_id and gml_id not in model.nodes:
                            dsl.remove(member)
                        break
            elif tag == "edgeMember":
                for child in list(member):
                    if local_name(child.tag) == "Edge":
                        gml_id = next(
                            (v for k, v in child.attrib.items() if local_name(k) == "id"),
                            None
                        )
                        if gml_id and gml_id not in model.edges:
                            dsl.remove(member)
                        break

    # Insert newly created spaces, nodes, and edges into the XML tree
    if new_space_ids:
        _add_new_spaces_to_xml(model, new_space_ids)
    if new_node_ids:
        _add_new_nodes_to_xml(model, new_node_ids)
    if new_edge_ids:
        _add_new_edges_to_xml(model, new_edge_ids)

    # Write InterLayerConnections that have no source_element yet
    core_ns, gml_ns, xlink_ns = _detect_namespaces(root)
    id_ns = _detect_id_ns(root, gml_ns)
    CORE  = f"{{{core_ns}}}"
    GML   = f"{{{gml_ns}}}"
    XLINK = f"{{{xlink_ns}}}"
    ID_NS = f"{{{id_ns}}}"
    for ilc in model.inter_layer_connections.values():
        if ilc.source_element is not None:
            continue  # already in XML
        ilc_wrap = ET.SubElement(root, f"{CORE}interLayerConnection")
        ilc_elem = ET.SubElement(ilc_wrap, f"{CORE}InterLayerConnection")
        ilc_elem.attrib[f"{ID_NS}id"] = ilc.ilc_id
        topo = ET.SubElement(ilc_elem, f"{CORE}typeOfTopoExpression")
        topo.text = ilc.type_of_topo_expr
        for sp_id in (ilc.space1_id, ilc.space2_id):
            c = ET.SubElement(ilc_elem, f"{CORE}connects")
            c.attrib[f"{XLINK}href"] = f"#{sp_id}"
        ilc.source_element = ilc_elem

    _indent_tree(model.source_tree)
    model.source_tree.write(path, encoding="unicode", xml_declaration=True)
    model.source_path = path


# =========================================================
# Geometry utilities
# =========================================================


# =========================================================
# Floor graph utilities
# =========================================================
_FLOOR_LABEL_RE = re.compile(r"^\s*(?P<b>[Bb])?\s*(?P<num>\d+)\s*[Ff]?\s*$")


def floor_sort_key(name: Optional[str]) -> Tuple[int, float, str]:
    """Natural ordering key for floor labels.

    Plain ``sorted()`` puts '10F' before '1F' and drops every basement at the
    end, which is unusable for a tower with 17 levels.  This key yields the
    order a building occupant expects::

        B4F < B3F < B2F < B1F < 1F < 2F < ... < 9F < 10F < ... < 13F

    Basements sort by negated number so a deeper basement comes first.
    Labels that do not look like a floor at all (''Mezzanine'', 'Roof', …)
    keep a stable alphabetical order *after* every recognised floor rather
    than being interleaved arbitrarily.
    """
    s = (name or "").strip()
    m = _FLOOR_LABEL_RE.match(s)
    if m:
        num = int(m.group("num"))
        return (0, -num if m.group("b") else num, s)
    return (1, 0.0, s.lower())


def get_available_floors(model: IndoorModel) -> List[str]:
    return sorted(model.floor_index().keys(), key=floor_sort_key)


def get_space_ids_on_floor(model: IndoorModel, floor: str) -> Set[str]:
    # Return a copy so callers can mutate without disturbing the cache.
    return set(model.floor_index().get(floor.strip(), ()))


def expand_floor_graph(model: IndoorModel, floor_space_ids: Set[str],
                       floor: Optional[str] = None) -> Tuple[Set[str], Set[str], Set[str]]:
    """Return (visible_space_ids, visible_node_ids, visible_edge_ids) for the given floor.

    Handles two cases for edge.connects entries:
      (a) node ID  — after successful normalize_edge_connects_to_node_ids()
      (b) space ID — when normalization couldn't find a dual node for that space
          (space has no Node with duality pointing to it)

    Node lookup chain:
      1. node.duality in floor_space_ids  (primary — duality link)
      2. node.level == floor              (fallback — orphan / manually created nodes)

    Edge lookup:
      edge.connects ref is a floor node ID   OR   a floor space ID (un-normalized)
    """
    visible_space_ids = set(floor_space_ids)

    # Step 1: nodes on this floor.
    # Most reliable signal: the node's DUAL CellSpace. If the dual cell
    # is on this floor → include. If the dual cell is on ANY OTHER
    # floor → reject (handles files where every node is mislabeled the
    # same level even though its dual cell is correctly tagged). Only
    # nodes WITHOUT a dual cell fall back to their own `node.level`.
    visible_node_ids: Set[str] = set()
    for nid, node in model.nodes.items():
        dual_level: Optional[str] = None
        if node.duality:
            dual_sp = model.spaces.get(node.duality)
            if dual_sp is not None and dual_sp.level:
                dual_level = dual_sp.level
        if dual_level is not None:
            if dual_level == floor:
                visible_node_ids.add(nid)
            # else: dual cell explicitly on a different floor → skip
            continue
        # No dual cell or it has no level → use legacy heuristics
        if node.duality and node.duality in visible_space_ids:
            visible_node_ids.add(nid)
        elif floor and node.level == floor:
            visible_node_ids.add(nid)

    # Step 2: edges whose connects touch a floor node OR a floor space (un-normalized ref)
    visible_edge_ids: Set[str] = set()
    for eid, edge in model.edges.items():
        for ref in edge.connects:
            if ref in visible_node_ids or ref in visible_space_ids:
                visible_edge_ids.add(eid)
                break

    # Step 3: pull in all endpoint nodes of included edges
    for eid in visible_edge_ids:
        edge = model.edges[eid]
        for ref in edge.connects:
            if ref in model.nodes:
                visible_node_ids.add(ref)

    return visible_space_ids, visible_node_ids, visible_edge_ids


# =========================================================
# Cell split geometry helpers
# =========================================================
def _intersect_line_segment(lp1, lp2, sp1, sp2):
    """Intersect infinite line (lp1→lp2) with segment (sp1→sp2).
    Returns (s, (ix, iy)) where s ∈ [0,1] is position on segment, or None."""
    rx = lp2[0] - lp1[0]
    ry = lp2[1] - lp1[1]
    ex = sp2[0] - sp1[0]
    ey = sp2[1] - sp1[1]
    denom = rx * ey - ry * ex
    if abs(denom) < 1e-10:
        return None
    dx = sp1[0] - lp1[0]
    dy = sp1[1] - lp1[1]
    s = (dx * ry - dy * rx) / denom
    if s < -1e-9 or s > 1 + 1e-9:
        return None
    s = max(0.0, min(1.0, s))
    return s, (sp1[0] + s * ex, sp1[1] + s * ey)


def _poly_open(poly_2d):
    """Return polygon vertices without closing duplicate vertex."""
    pts = list(poly_2d)
    if len(pts) > 1 and abs(pts[0][0]-pts[-1][0]) < 1e-9 and abs(pts[0][1]-pts[-1][1]) < 1e-9:
        pts = pts[:-1]
    return pts


def _split_polygon_by_vertices(poly_2d, vi: int, vj: int):
    """Split polygon along the diagonal from vertex vi to vertex vj.
    Returns (poly_a, poly_b) each as list of (x,y), or None."""
    pts = _poly_open(poly_2d)
    n = len(pts)
    if n < 4:
        return None
    if vi > vj:
        vi, vj = vj, vi
    if vi == vj or (vi == 0 and vj == n - 1):
        return None
    poly_a = [pts[k] for k in range(vi, vj + 1)]
    poly_b = [pts[k] for k in range(vj, n)] + [pts[k] for k in range(0, vi + 1)]
    if len(poly_a) < 3 or len(poly_b) < 3:
        return None
    return poly_a, poly_b


# =========================================================
# Drawing on embedded axes
# =========================================================
def draw_on_axes(ax, model: IndoorModel, floor: Optional[str] = None, show_labels: bool = True,
                 excluded_space_ids: Optional[Set[str]] = None,
                 show_nodes: bool = True,
                 function_color_map: Optional[Dict[str, str]] = None,
                 function_label_map: Optional[Dict[str, str]] = None,
                 excluded_node_ids: Optional[Set[str]] = None,
                 layer_id: Optional[str] = None,
                 bg_layer_id: Optional[str] = None,
                 bg_floor: Optional[str] = None,
                 show_offfloor_anchors: bool = True) -> None:
    ax.clear()
    ax.set_facecolor("black")
    ax.figure.patch.set_facecolor("black")

    if floor is None or floor == "All":
        visible_space_ids = set(model.spaces.keys())
        visible_node_ids = set(model.nodes.keys())
        visible_edge_ids = set(model.edges.keys())
        floor_title = "All"
    else:
        visible_space_ids = get_space_ids_on_floor(model, floor)
        visible_space_ids, visible_node_ids, visible_edge_ids = expand_floor_graph(model, visible_space_ids, floor=floor)
        floor_title = floor

    # BG floor filter — spaces on bg_floor are shown dimly as reference
    bg_space_ids: Set[str] = set()
    if floor and floor != "All" and bg_floor and bg_floor != floor:
        bg_floor_space_ids = get_space_ids_on_floor(model, bg_floor)
        bg_space_ids = bg_floor_space_ids  # shown dimly; not in visible_space_ids

    # Layer filter — restrict to the selected ThematicLayer.
    # Single-pass partition (was three set comprehensions over the same set).
    spaces_dict = model.spaces
    nodes_dict  = model.nodes
    edges_dict  = model.edges
    if layer_id:
        new_visible: Set[str] = set()
        new_bg:      Set[str] = set(bg_space_ids)
        explicit_bg = bool(bg_layer_id and bg_layer_id != layer_id)
        for s in visible_space_ids:
            sp = spaces_dict.get(s)
            if sp is None:
                continue
            lid = sp.layer_id
            if lid == layer_id:
                new_visible.add(s)
            elif explicit_bg:
                if lid == bg_layer_id:
                    new_bg.add(s)
            else:
                new_bg.add(s)
        visible_space_ids = new_visible
        bg_space_ids      = new_bg
        visible_node_ids  = {n for n in visible_node_ids
                             if nodes_dict[n].layer_id == layer_id}
        visible_edge_ids  = {e for e in visible_edge_ids
                             if edges_dict[e].layer_id == layer_id}
    elif bg_layer_id:
        bg_space_ids |= {s for s in visible_space_ids
                         if spaces_dict[s].layer_id == bg_layer_id}

    # Draw background layer dimly first
    if bg_space_ids:
        bg_fill_verts: List = []
        bg_border_segs: List = []
        for sp_id in bg_space_ids:
            sp = model.spaces.get(sp_id)
            if not sp or not sp.polygons_3d:
                continue
            poly = sp.polygons_3d[0]
            verts_2d = [(p[0], p[1]) for p in poly]
            bg_fill_verts.append(verts_2d)
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            if poly and (poly[0][0] != poly[-1][0] or poly[0][1] != poly[-1][1]):
                xs.append(poly[0][0]); ys.append(poly[0][1])
            bg_border_segs.append(list(zip(xs, ys)))
        if bg_fill_verts:
            from matplotlib.collections import PolyCollection as _PC, LineCollection as _LC
            bg_pc = _PC(bg_fill_verts, facecolors=[(1, 1, 1, 0.08)], edgecolors="none", zorder=1)
            ax.add_collection(bg_pc)
            bg_lc = _LC(bg_border_segs, colors=[(0.4, 0.4, 0.4, 0.3)], linewidths=0.8, zorder=2)
            ax.add_collection(bg_lc)

    # CellSpace polygons — batch with PolyCollection + LineCollection.
    # Single-pass polygon extraction: walk poly only once and build both the
    # 2D vertex list (for fill) and the closed-ring (for border) at the same
    # time, instead of the previous three traversals.
    fill_verts: List = []
    fill_facecolors: List = []
    border_segs: List = []

    DEFAULT_RGBA = (1.0, 1.0, 1.0, 0.30)
    fcm = function_color_map
    # Pre-compute alpha-baked RGBA per function code so to_rgba() isn't
    # called once per CellSpace.
    fc_rgba: Dict[str, tuple] = {}
    if fcm:
        for code, color in fcm.items():
            fc_rgba[code] = mcolors.to_rgba(color, 0.35)

    excl_sp = excluded_space_ids or ()
    add_text = ax.text
    add_scatter = ax.scatter
    for sp_id in visible_space_ids:
        if sp_id in excl_sp:
            continue
        sp = spaces_dict.get(sp_id)
        if sp is None or sp.is_virtual:
            continue

        rgba = fc_rgba.get(sp.function, DEFAULT_RGBA) if fc_rgba else DEFAULT_RGBA

        if sp.polygons_3d:
            poly = sp.polygons_3d[0]
            verts = [(p[0], p[1]) for p in poly]
            fill_verts.append(verts)
            fill_facecolors.append(rgba)
            # Close the ring for the border line if not already closed.
            if verts and verts[0] != verts[-1]:
                border_segs.append(verts + [verts[0]])
            else:
                border_segs.append(verts)
        elif sp.centers:
            cx, cy, _ = sp.centers[0]
            add_scatter([cx], [cy], color="white", s=10)

        if show_labels and sp.centers and sp.name.strip():
            cx, cy, _ = sp.centers[0]
            add_text(cx, cy, sp.name, fontsize=7, color="white")

    if fill_verts:
        ax.add_collection(PolyCollection(
            fill_verts, facecolors=fill_facecolors,
            edgecolors="none", zorder=1))
    if border_segs:
        ax.add_collection(LineCollection(
            border_segs, colors="white", linewidths=1.0, zorder=2))

    # ------------------------------------------------------------------
    # Classify vertical edges + their endpoints up front. We need this for
    # both edge coloring (downstair / upstair distinct color) and node
    # coloring (orange lower endpoint, blue current-floor down-link, hidden
    # upper endpoint).
    # ------------------------------------------------------------------
    specific_floor = bool(floor) and floor != "All"
    lower_off_ids: Set[str] = set()
    upper_off_ids: Set[str] = set()
    cur_with_lower: Set[str] = set()
    downstair_edge_ids: Set[str] = set()
    upstair_edge_ids:   Set[str] = set()
    cur_floor_z: Optional[float] = None
    if specific_floor:
        for s_id in visible_space_ids:
            sp = model.spaces.get(s_id)
            if sp is not None and sp.polygons_3d:
                cur_floor_z = sp.polygons_3d[0][0][2]
                break

        Z_EPS = 50.0  # mm — tolerance for "Z equals current floor Z"

        def _on_current_floor(n: NodeFeature) -> bool:
            # Most authoritative: the dual CellSpace's level. Some
            # files mislabel every node "1F" while keeping the cells
            # tagged correctly per floor — using the dual cell here
            # makes vertical-edge classification still work in those
            # files (without it, a 2F node mislabeled "1F" would be
            # treated as a 1F endpoint and the edge would render as a
            # normal same-floor edge instead of a vertical one).
            if n.duality:
                dual_sp = model.spaces.get(n.duality)
                if dual_sp is not None and dual_sp.level:
                    return dual_sp.level == floor
            if n.level == floor:
                return True
            if cur_floor_z is None or n.coord is None:
                return False
            return abs(n.coord[2] - cur_floor_z) < Z_EPS

        for edge_id, edge in model.edges.items():
            if layer_id and edge.layer_id != layer_id:
                continue
            # `e.connects` may contain non-node refs (the parsed
            # `<duality>` xlink:href points to a CellSpace, not a Node).
            # Pull out only entries that resolve to real Nodes.
            node_pairs = [
                (c, model.nodes[c]) for c in edge.connects
                if c in model.nodes
            ]
            if len(node_pairs) < 2:
                continue
            on_cur_ids = [c for c, n in node_pairs if _on_current_floor(n)]
            if not on_cur_ids:
                continue

            edge_goes_down = False
            edge_goes_up   = False
            for c, n in node_pairs:
                if _on_current_floor(n) or n.coord is None:
                    continue
                # Decide above/below via the off-floor node's DUAL CELL
                # (most reliable when node levels are mislabeled). Fall
                # back to the node's own Z otherwise.
                other_z: Optional[float] = None
                if n.duality:
                    dual_sp = model.spaces.get(n.duality)
                    if dual_sp is not None and dual_sp.polygons_3d:
                        other_z = dual_sp.polygons_3d[0][0][2]
                if other_z is None:
                    other_z = n.coord[2]
                if cur_floor_z is None:
                    lower_off_ids.add(c)
                    edge_goes_down = True
                elif other_z < cur_floor_z - Z_EPS:
                    lower_off_ids.add(c)
                    edge_goes_down = True
                elif other_z > cur_floor_z + Z_EPS:
                    upper_off_ids.add(c)
                    edge_goes_up = True
                else:
                    # Off-floor by name but same Z — treat as upper to
                    # be safe (hidden) so a dangling cross-floor edge
                    # doesn't get drawn as a normal same-floor edge.
                    upper_off_ids.add(c)
                    edge_goes_up = True
            if edge_goes_down:
                cur_with_lower.update(on_cur_ids)
                downstair_edge_ids.add(edge_id)
            if edge_goes_up:
                upstair_edge_ids.add(edge_id)

    # Edges — batch with LineCollection (3 buckets: normal, down, up)
    if show_nodes:
        normal_segs: List = []
        down_segs:   List = []
        up_segs:     List = []
        for edge_id in visible_edge_ids:
            edge = edges_dict.get(edge_id)
            if edge is None or len(edge.coords) < 2:
                continue
            seg = [(p[0], p[1]) for p in edge.coords]
            if edge_id in downstair_edge_ids:
                down_segs.append(seg)
            elif edge_id in upstair_edge_ids:
                up_segs.append(seg)
            else:
                normal_segs.append(seg)
        if normal_segs:
            ax.add_collection(LineCollection(
                normal_segs, colors="deepskyblue",
                linewidths=1.0, zorder=3))
        if down_segs:
            # Downstair vertical edges — magenta/salmon family
            ax.add_collection(LineCollection(
                down_segs, colors="orchid",
                linewidths=1.6, zorder=4))
        if up_segs:
            # Upstair vertical edges — lime/green family
            ax.add_collection(LineCollection(
                up_segs, colors="limegreen",
                linewidths=1.6, zorder=4))

    if show_nodes:
        nx: List[float] = []
        ny: List[float] = []
        lower_nx: List[float] = []
        lower_ny: List[float] = []
        cur_blue_nx: List[float] = []
        cur_blue_ny: List[float] = []

        Z_EPS_NODE = 50.0   # mm — same tolerance as vertical-edge logic

        # Classify each node — drop upper-floor vertical endpoints entirely.
        for node_id, node in model.nodes.items():
            if excluded_node_ids and node_id in excluded_node_ids:
                continue
            if node.coord is None:
                continue
            if node_id in upper_off_ids:
                continue                              # hidden
            if node_id in lower_off_ids:
                if show_offfloor_anchors:
                    lower_nx.append(node.coord[0])
                    lower_ny.append(node.coord[1])
                # else: drop the cross-floor anchor entirely
                continue
            if node_id in cur_with_lower:
                if show_offfloor_anchors:
                    cur_blue_nx.append(node.coord[0])
                    cur_blue_ny.append(node.coord[1])
                else:
                    # The blue marker would have flagged a current-floor
                    # node that links downward — without anchors visible
                    # we just render it as a normal current-floor node.
                    nx.append(node.coord[0])
                    ny.append(node.coord[1])
                continue
            if node_id not in visible_node_ids:
                continue
            # Final guard: even if the node sneaked into visible_node_ids
            # (e.g. via expand_floor_graph step 3 pulling in the other
            # endpoint of a vertical edge), drop it when its own floor
            # clearly does NOT match the active floor.
            #
            # Three independent signals are checked, in priority order:
            #   1. The DUAL CellSpace's level. This is the most
            #      authoritative — files often mislabel `node.level`
            #      while keeping `cell.level` correct, so we trust the
            #      dual cell first.
            #   2. The node's own `level` text (`node.level == floor`).
            #   3. The node's Z coordinate matching the active floor's
            #      reference Z (handles legacy nodes with empty level).
            # The node passes only when at least one signal places it on
            # the active floor AND the dual cell does NOT contradict it.
            if specific_floor:
                dual_level: Optional[str] = None
                if node.duality:
                    dual_sp = model.spaces.get(node.duality)
                    if dual_sp is not None and dual_sp.level:
                        dual_level = dual_sp.level
                # Strong rejection: dual cell's level explicitly says
                # the node belongs to another floor — drop it even if
                # its own .level / Z claim otherwise (handles files
                # where every node is mislabelled "1F" but the cells
                # are tagged correctly per floor).
                if dual_level and dual_level != floor:
                    continue
                node_on_floor = (node.level == floor)
                if not node_on_floor and dual_level == floor:
                    node_on_floor = True
                if (not node_on_floor and cur_floor_z is not None
                        and abs(node.coord[2] - cur_floor_z)
                            < Z_EPS_NODE):
                    node_on_floor = True
                if not node_on_floor:
                    continue
            nx.append(node.coord[0])
            ny.append(node.coord[1])

        if nx:
            ax.scatter(nx, ny, color="tomato", s=18, zorder=5)
        if cur_blue_nx:
            # Current-floor node that links down to a lower floor — drawn
            # in dodgerblue and slightly larger so it stands out against
            # surrounding tomato nodes and the deepskyblue edge lines.
            ax.scatter(cur_blue_nx, cur_blue_ny, color="dodgerblue",
                       s=22, zorder=6, alpha=1.0,
                       edgecolors="black", linewidths=0.6)
        if lower_nx:
            ax.scatter(lower_nx, lower_ny, color="orange", s=22, zorder=6,
                       alpha=1.0, edgecolors="black", linewidths=0.6)

    ax.set_title(f"Floor: {floor_title}", color="white", fontsize=9, pad=3)
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.tick_params(colors="white", labelsize=7)
    ax.spines[:].set_edgecolor("white")
    # adjustable='datalim' keeps the axes box filling the full canvas rectangle
    # (unlike 'box' which shrinks the axes to a square when data is roughly
    # square). fit_to_extent pre-computes xlim/ylim to match the canvas aspect,
    # and the resulting matplotlib warning is suppressed at the call site.
    ax.set_aspect('equal', adjustable='datalim')
    ax.grid(False)

    # Function-code legend (shown only when color map is active)
    if function_color_map:
        import matplotlib.patches as mpatches
        used_codes = {sp.function for sp in model.spaces.values()
                      if sp.function and sp.function in function_color_map}
        if used_codes:
            # Show the human-readable label loaded from the function-
            # codes CSV when available; fall back to the bare code if
            # no label was provided for that entry.
            def _legend_label(c: str) -> str:
                if function_label_map:
                    label = function_label_map.get(c)
                    if label and label != c:
                        return label
                return c
            patches = [
                mpatches.Patch(facecolor=function_color_map[code], alpha=0.30,
                               edgecolor="white", linewidth=0.5,
                               label=_legend_label(code))
                for code in sorted(used_codes)
            ]
            leg = ax.legend(handles=patches, loc="upper left", fontsize=7,
                            facecolor="#1a1a1a", edgecolor="#555555",
                            labelcolor="white", framealpha=0.85,
                            title="Function", title_fontsize=7)
            leg.get_title().set_color("white")


# =========================================================
# UI
# =========================================================
class IndoorGMLViewerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        # Showing the resolved canvas font makes a CJK problem self-evident:
        # if labels are boxes, the title says which font was picked (or that
        # none was), instead of the user having to guess.
        _font_note = f"  [font: {CANVAS_FONT}]" if CANVAS_FONT else "  [font: none — CJK may not render]"
        self.root.title(f"IndoorGML Viewer  v{VERSION}{_font_note}")
        self.root.geometry("1200x800")

        self.model: Optional[IndoorModel] = None

        self.file_var = tk.StringVar(value="")
        self.file_name_var = tk.StringVar(value="(no file)")
        self.file_var.trace_add("write", lambda *_: self.file_name_var.set(
            os.path.basename(self.file_var.get()) if self.file_var.get() else "(no file)"))
        self.floor_var    = tk.StringVar(value="All")
        self.bg_floor_var = tk.StringVar(value="None")  # background reference floor
        self.layer_var    = tk.StringVar(value="All")   # active (editable) layer
        self.bg_layer_var = tk.StringVar(value="None")  # background reference layer
        self.label_var = tk.BooleanVar(value=True)
        self.show_nodes_var = tk.BooleanVar(value=True)
        self.show_ilc_var   = tk.BooleanVar(value=True)
        # Off-floor anchor markers (lower-floor vertical-edge endpoints
        # in orange, current-floor nodes that link down in dodgerblue).
        # Default OFF so a single-floor view shows ONLY that floor's
        # nodes; users who want the cross-floor anchors visible can turn
        # this back on from the View menu.
        self.show_offfloor_anchors_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Open an IndoorGML file.")
        self.edit_status_var = tk.StringVar(value="")

        self._edit_mode = False
        self._edit_mode_var = tk.BooleanVar(value=False)
        self._edit_selected_space_id: Optional[str] = None
        self._edit_selected_poly_idx: int = 0
        self._edit_selected_vertex_idx: Optional[int] = None
        self._edit_drag_active = False
        # Function code list (loaded from CSV)
        self._function_codes: List[Tuple[str, str]] = []  # [(code, label), ...]
        self._function_csv_path: str = ""
        self._function_color_map: Dict[str, str] = {}    # code → hex color string

        self._graph_edit_mode = False
        self._graph_edit_mode_var = tk.BooleanVar(value=False)
        self._graph_connect_mode = False
        self._graph_connect_mode_var = tk.BooleanVar(value=False)
        self._space_create_mode_var = tk.BooleanVar(value=False)
        # Rectangle-shortcut variant of New Cell — pick two opposite
        # corners; the editor expands them into a 4-vertex polygon.
        self._rect_create_mode = False
        self._rect_create_mode_var = tk.BooleanVar(value=False)
        self._rect_create_pt1: Optional[Tuple[float, float]] = None
        # Multi-cell function-code apply mode (Function Codes →
        # "Multiple Cells (bulk apply)"). Click cells to add/remove
        # them from a selection set, then Apply to assign one chosen
        # function code to all of them.
        self._multi_apply_mode = False
        self._multi_apply_selected: Set[str] = set()
        self._multi_apply_code: str = ""
        # Multi-cell Class bulk-apply mode (Cell → "Class Bulk Apply")
        self._multi_class_mode = False
        self._multi_class_selected: Set[str] = set()
        self._multi_class_value: str = ""
        self._graph_connect_first: Optional[str] = None
        # Measure distance mode (Misc → Measure Distance)
        self._measure_mode: bool = False
        self._measure_pt1: Optional[Tuple[float, float]] = None
        # Vertical edge mode — connect two nodes on different floors
        self._vertical_edge_mode = False
        self._vertical_edge_mode_var = tk.BooleanVar(value=False)
        self._vertical_edge_first_node: Optional[str] = None
        self._vertical_edge_first_floor: Optional[str] = None
        self._graph_selected_node: Optional[str] = None
        self._graph_drag_node: Optional[str] = None
        self._graph_edit_status_var = tk.StringVar(value="")
        self._new_node_ids: Set[str] = set()   # nodes created in this session
        self._new_edge_ids: Set[str] = set()   # edges created in this session
        self._new_space_ids: Set[str] = set()  # spaces created in this session

        self._space_create_mode = False
        self._space_create_verts: List[Tuple[float, float]] = []
        self._space_create_preview_line = None  # set after canvas init
        self._space_create_last_click_time: float = 0.0  # for manual double-click detection

        self._split_mode = False
        self._split_mode_var = tk.BooleanVar(value=False)
        self._split_space_id: Optional[str] = None
        self._split_vertex_idx: Optional[int] = None   # first selected vertex index
        self._split_pt1: Optional[Tuple[float, float]] = None

        self._door_mode = False
        self._door_mode_var = tk.BooleanVar(value=False)
        self._door_pt1: Optional[Tuple[float, float]] = None
        # Door rectangle thickness in cm (typical ~10–30 cm)
        self._door_thickness_var = tk.DoubleVar(value=20.0)

        # InterLayerConnection mode
        self._ilc_mode = False
        self._ilc_mode_var = tk.BooleanVar(value=False)
        self._ilc_bg_layer: Optional[str] = None   # layer id for the "background" side
        self._ilc_fg_layer: Optional[str] = None   # layer id for the "foreground" side
        self._ilc_space1: Optional[str] = None     # first selected space id
        self._new_ilc_ids: Set[str] = set()        # ILCs created this session

        # View-only selection (when no edit mode is active)
        self._view_selected_type: Optional[str] = None   # "space" | "node" | "edge"
        self._view_selected_id: Optional[str] = None
        # Optional override: when set, the view-selection highlight uses
        # this XY (and Z, if given) instead of looking up the model's
        # cached coord. Needed for "duplicate id" navigation where the
        # picked copy may live at a different location than the one the
        # model dict happens to reference.
        self._view_selected_override_xy: Optional[Tuple[float, float]] = None
        self._view_selected_override_poly: Optional[List[Tuple[float, float]]] = None

        self._undo_stack: List[dict] = []      # undo history (max 50)

        self._snap_enabled = tk.BooleanVar(value=True)
        self._snap_point: Optional[Tuple[float, float]] = None  # active snap target for overlay
        self._snap_candidates: List[Tuple[float, float]] = []  # pre-built on drag start
        self._snap_threshold2: float = 0.0  # squared snap threshold, cached at drag start

        self._drag_bg_cache = None       # pixel buffer for blit
        self._ov_line = None             # animated overlay artists (init after canvas)
        self._ov_verts = None
        self._ov_sel_vert = None
        self._ov_snap_ring = None

        self._space_drag_active = False
        self._space_drag_start: Optional[Tuple[float, float]] = None
        self._space_drag_orig_polygons: Optional[List] = None  # deep copy of polygons_3d
        self._space_drag_bg_cache = None  # blit buffer for space-move drag

        self._pending_node_pos: Optional[Tuple[float, float]] = None  # deferred node creation
        self._graph_drag_node_ready: bool = False  # blit setup done for current node drag
        self._graph_drag_node_orig_coord: Optional[Tuple[float, float, float]] = None  # node coord at drag start
        self._node_drag_bg_cache = None  # blit buffer for node drag
        self._graph_selected_edge: Optional[str] = None  # edge selected for Delete key
        self._pending_edge_insert: Optional[Tuple] = None  # deferred vertex-on-edge insertion

        self._view_locked = False  # True after first draw; preserves zoom/pan on redraw

        # Floor Z overrides set by "New Floor" dialog.
        # Used when a floor has no cells yet — values are in meters.
        # Key: floor name, value: (floor_z, ceiling_z)
        self._floor_z_overrides: Dict[str, Tuple[float, float]] = {}

        self._bg_image = None        # numpy array loaded from image file
        self._bg_image_path = ""     # original file path for config save/restore
        self._bg_extent = None       # [xmin, xmax, ymin, ymax] in data coordinates
        # File-wide reference (anchor) points — shared across all floors.
        # Loaded from / saved to the bgcfg sidecar. None until set.
        self._reference_points: Optional[Tuple[Tuple[float, float],
                                               Tuple[float, float]]] = None
        # Anchor-capture mode state (B-style click flow)
        self._anchor_mode = False
        self._anchor_step = 0          # 1 → expecting LL, 2 → expecting UR
        self._anchor_canvas_pt1: Optional[Tuple[float, float]] = None
        self._anchor_canvas_pt2: Optional[Tuple[float, float]] = None
        self._anchor_world_pt1:  Optional[Tuple[float, float]] = None
        self._anchor_world_pt2:  Optional[Tuple[float, float]] = None
        self._bg_alpha = tk.DoubleVar(value=0.5)
        self._bg_visible = tk.BooleanVar(value=True)
        self._bg_move_mode = False
        self._bg_move_mode_var = tk.BooleanVar(value=False)
        self._bg_move_start_data = None   # (xdata, ydata) on press
        self._bg_move_start_extent = None # extent snapshot on press

        self._bg_resize_mode = False
        self._bg_resize_mode_var = tk.BooleanVar(value=False)
        self._bg_resize_handle: Optional[str] = None  # 'TL','TR','BL','BR','T','B','L','R'
        self._bg_drag_cache = None   # pixel buffer for bg move/resize blit

        self.figure = None
        self.ax = None
        self.canvas = None

        self._pan_active = False
        self._pan_start_pixel = None
        self._pan_start_xlim = None
        self._pan_start_ylim = None

        self._build_ui()
        self._connect_matplotlib_events()

        if CANVAS_FONT is None:
            # Not fatal — Latin labels are unaffected — but silently drawing
            # every Korean cell name as boxes is worse than saying so.
            self.status_var.set(
                "No CJK font found; non-Latin labels may show as boxes. "
                "Install e.g. Noto Sans KR or Nanum Gothic, then restart.")

    def _build_ui(self) -> None:
        # ── Menubar ──────────────────────────────────────────────────────────
        _mf = ("TkDefaultFont", 10, "bold")  # menu font — match toolbox font size

        # ── Custom frame-based menubar (tk.Menubutton) ──────────────────────
        # Windows native menu bar ignores the font= option on cascade labels,
        # so we build our own bar from Menubutton widgets instead.
        # IMPORTANT: each tk.Menu must be a child of its own Menubutton so that
        # the dropdown opens correctly.
        # "SystemButtonFace" & friends are Windows-only Tk colour names; on
        # X11 / Aqua they raise TclError("unknown color name") and the app
        # dies before the first window appears. Probe once and fall back to
        # neutral literals so the editor starts on Linux and macOS too.
        def _sys_colour(name: str, fallback: str) -> str:
            try:
                self.root.winfo_rgb(name)
                return name
            except tk.TclError:
                return fallback

        _mb_bg  = _sys_colour("SystemButtonFace",      "#f0f0f0")
        _mb_abg = _sys_colour("SystemHighlight",       "#0078d7")
        _mb_afg = _sys_colour("SystemHighlightText",   "#ffffff")
        menubar_frame = tk.Frame(self.root, bg=_mb_bg, bd=0, relief="flat")
        menubar_frame.pack(side="top", fill="x")

        def _make_mb(label: str) -> Tuple[tk.Menubutton, tk.Menu]:
            """Create a Menubutton + its child Menu, pack the button, return both."""
            btn = tk.Menubutton(
                menubar_frame, text=label,
                font=_mf, relief="flat", bd=0,
                padx=6, pady=3,
                bg=_mb_bg, activebackground=_mb_abg, activeforeground=_mb_afg,
                cursor="arrow",
            )
            btn.pack(side="left")
            m = tk.Menu(btn, tearoff=0, font=_mf)
            btn["menu"] = m
            return btn, m

        _, file_menu = _make_mb("File")
        file_menu.add_command(label="New",               command=self.new_file)
        file_menu.add_command(label="Open...",           command=self.open_file)
        file_menu.add_command(label="Close",             command=self.close_file)
        file_menu.add_separator()
        file_menu.add_command(label="Save",              command=self.save_file)
        file_menu.add_command(label="Save As...",        command=self.save_file_as)
        file_menu.add_separator()
        file_menu.add_command(label="Export Floors...", command=self.export_floors)
        file_menu.add_separator()
        file_menu.add_command(label="Quit",              command=self.root.quit)

        _, view_menu = _make_mb("View")
        view_menu.add_command(label="Fit View  [Home]",   command=self.fit_to_extent)
        view_menu.add_separator()
        view_menu.add_checkbutton(label="Show names",     variable=self.label_var,      command=self.refresh_view)
        view_menu.add_checkbutton(label="Show nodes",     variable=self.show_nodes_var, command=self.refresh_view)
        view_menu.add_checkbutton(label="Show floorplan", variable=self._bg_visible,    command=self.refresh_view)
        view_menu.add_checkbutton(label="Show ILC",       variable=self.show_ilc_var,   command=self.refresh_view)
        view_menu.add_checkbutton(label="Show off-floor anchors",
                                  variable=self.show_offfloor_anchors_var,
                                  command=self.refresh_view)

        _, cell_menu = _make_mb("Cell")
        cell_menu.add_checkbutton(label="New Cell (Rectangle)",
                                  variable=self._rect_create_mode_var,
                                  command=self.toggle_rect_create_mode)
        cell_menu.add_checkbutton(label="New Cell (Free-style Polygon)",
                                  variable=self._space_create_mode_var,
                                  command=self.toggle_space_create_mode)
        cell_menu.add_checkbutton(label="Cell Edit",  variable=self._edit_mode_var,         command=self.toggle_edit_mode,
                                  accelerator="C")
        cell_menu.add_checkbutton(label="Cell Split", variable=self._split_mode_var,        command=self.toggle_split_mode)
        cell_menu.add_separator()
        self._multi_class_mode_var = tk.BooleanVar(value=False)
        cell_menu.add_checkbutton(label="Class Bulk Apply",
                                  variable=self._multi_class_mode_var,
                                  command=self.toggle_multi_class_mode)

        _, self._network_menu = _make_mb("Network")
        self._network_menu.add_checkbutton(label="Node Edit", variable=self._graph_edit_mode_var,    command=self.toggle_graph_edit_mode,
                                           accelerator="N")
        self._network_menu.add_checkbutton(label="Edge Edit", variable=self._graph_connect_mode_var, command=self.toggle_graph_connect_mode,
                                           accelerator="E")
        self._network_menu.add_separator()
        self._network_menu.add_checkbutton(label="Vertical Edge",
                                           variable=self._vertical_edge_mode_var,
                                           command=self.toggle_vertical_edge_mode)

        _, door_menu = _make_mb("Door")
        door_menu.add_checkbutton(label="New Door", variable=self._door_mode_var, command=self.toggle_door_mode,
                                  accelerator="D")

        _, self._layer_menu = _make_mb("Layer")
        self._layer_menu.add_command(label="New Layer...",    command=self.new_layer)
        self._layer_menu.add_separator()
        self._layer_menu.add_command(label="Rename Layer...", command=self.rename_layer)
        self._layer_menu.add_command(label="Delete Layer...", command=self.delete_layer)
        _, self._ilc_connect_menu = _make_mb("Connect Layers")
        self._ilc_connect_menu.add_command(label="Setup...", command=self.setup_ilc_mode)
        self._ilc_connect_menu.add_checkbutton(label="Connect Mode",
                                               variable=self._ilc_mode_var,
                                               command=self.toggle_ilc_mode)
        self._ilc_connect_menu.add_separator()
        self._ilc_connect_menu.add_command(label="List Connections...", command=self.list_ilc)
        self._ilc_connect_menu.add_command(label="Delete Connection...", command=self.delete_ilc)

        # Unified "Floor" menu — combines floor management (create / add /
        # delete) with floorplan-image alignment (load / move / resize /
        # extent / reference points / opacity). Previously these were in
        # two separate menus ("Floor" and "Floorplan") which was redundant.
        _, self._fp_menu = _make_mb("Floor")
        self._fp_menu.add_command(label="New Floor...",          command=self.new_floor_and_load_floorplan)
        self._fp_menu.add_command(label="Add Floor from File...", command=self.add_floor_from_file)
        self._fp_menu.add_command(label="Delete Floor...",       command=self.delete_floor)
        self._fp_menu.add_separator()
        self._fp_menu.add_command(label="Load Floorplan...",     command=self.load_background)
        self._fp_menu.add_checkbutton(label="Move Floorplan",    variable=self._bg_move_mode_var,   command=self.toggle_bg_move_mode)
        self._fp_menu.add_checkbutton(label="Resize Floorplan",  variable=self._bg_resize_mode_var, command=self.toggle_bg_resize_mode)
        self._fp_menu.add_command(label="Set Extent...",         command=self.set_background_extent)
        self._fp_menu.add_separator()
        self._fp_menu.add_command(label="Set Reference Points...", command=self.set_reference_points)
        self._fp_menu.add_separator()
        self._fp_menu.add_command(label="Opacity...",            command=self.set_bg_opacity)

        _, self._fn_menu = _make_mb("Function Codes")
        self._fn_menu.add_command(label="Load Function Codes...", command=self.load_function_codes_file)
        self._fn_menu.add_command(label="Apply Function...",      command=self.apply_function_dialog, state="disabled")
        self._multi_apply_mode_var = tk.BooleanVar(value=False)
        self._fn_menu.add_checkbutton(label="Multiple Cells (bulk apply)",
                                      variable=self._multi_apply_mode_var,
                                      command=self.toggle_multi_apply_mode,
                                      state="disabled")

        _, self._misc_menu = _make_mb("Check")
        self._misc_menu.add_command(label="Create Virtual CellSpaces for Standalone Nodes...",
                                    command=self.create_virtual_cellspaces_for_orphan_nodes)
        self._misc_menu.add_separator()
        self._misc_menu.add_command(label="Find Isolated Nodes & CellSpaces...",
                                    command=self.find_isolated_nodes_and_spaces)
        self._misc_menu.add_command(label="Find Nodes without Duality...",
                                    command=self.find_nodes_without_duality)
        self._misc_menu.add_command(label="Find Cells without Node...",
                                    command=self.find_cells_without_node)
        self._misc_menu.add_command(label="Find Broken Edge Connections...",
                                    command=self.find_broken_edge_connections)
        self._misc_menu.add_command(label="Find Duality Conflicts...",
                                    command=self.find_duality_conflicts)
        self._misc_menu.add_command(label="Normalize Floor Z...",
                                    command=self.normalize_floor_z)
        self._misc_menu.add_command(label="Find Duplicate IDs...",
                                    command=self.find_duplicate_ids)
        self._misc_menu.add_command(label="Search by ID / Name...",
                                    command=self.search_by_id_or_name)
        self._misc_menu.add_separator()
        self._measure_mode_var = tk.BooleanVar(value=False)
        self._misc_menu.add_checkbutton(label="Measure Distance",
                                        variable=self._measure_mode_var,
                                        command=self.toggle_measure_mode)
        self._misc_menu.add_separator()
        self._misc_menu.add_command(label="Repair: drop trailing duplicate vertices...",
                                    command=self.repair_trailing_duplicate_vertices)
        self._misc_menu.add_command(label="Repair: remove self-loop edges...",
                                    command=self.repair_self_loop_edges)
        self._misc_menu.add_command(label="Repair: drop self-intersecting tail...",
                                    command=self.repair_self_intersecting_tail)

        # Bottom bar — single slim line
        bottom = ttk.Frame(self.root, padding=(4, 1))
        bottom.pack(side="bottom", fill="x")
        ttk.Label(bottom, textvariable=self.status_var,
                  foreground="gray", font=("TkDefaultFont", 8)).pack(side="right")
        ttk.Label(bottom, textvariable=self._graph_edit_status_var,
                  foreground="darkgreen", font=("TkDefaultFont", 8)).pack(side="right", padx=(0, 8))
        ttk.Label(bottom, textvariable=self.edit_status_var,
                  foreground="darkblue", font=("TkDefaultFont", 8)).pack(side="right", padx=(0, 8))

        # Right panel — compact vertical toolbar
        _tb_s = ttk.Style()
        _tb_s.configure("TB.TFrame", background="#ffffff")
        _tb_s.configure("TB.TLabel", background="#ffffff", font=("TkDefaultFont", 10, "bold"))
        _tb_s.configure("TB.TButton", font=("TkDefaultFont", 10, "bold"))

        right = ttk.Frame(self.root, style="TB.TFrame", padding=(10, 8))
        right.pack(side="right", fill="y")

        ttk.Label(right, textvariable=self.file_name_var, foreground="navy",
                  font=("TkDefaultFont", 10, "bold"), wraplength=170,
                  style="TB.TLabel",
                  justify="left").pack(anchor="w", pady=(0, 2))
        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=2)

        floor_row = ttk.Frame(right, style="TB.TFrame")
        floor_row.pack(anchor="w", fill="x")
        ttk.Label(floor_row, text="Floor:", style="TB.TLabel").pack(side="left")
        self.floor_combo = ttk.Combobox(floor_row, textvariable=self.floor_var,
                                        state="readonly", width=14,
                                        font=("TkDefaultFont", 10, "bold"))
        self.floor_combo.pack(side="left", padx=(2, 0))
        self.floor_combo["values"] = ["All"]
        self.floor_combo.bind("<<ComboboxSelected>>", self.on_floor_changed)

        layer_row = ttk.Frame(right, style="TB.TFrame")
        layer_row.pack(anchor="w", fill="x")
        ttk.Label(layer_row, text="Layer:", style="TB.TLabel").pack(side="left")
        self.layer_combo = ttk.Combobox(layer_row, textvariable=self.layer_var,
                                        state="readonly", width=14,
                                        font=("TkDefaultFont", 10, "bold"))
        self.layer_combo.pack(side="left", padx=(2, 0))
        self.layer_combo["values"] = ["All"]
        self.layer_combo.bind("<<ComboboxSelected>>", self.on_layer_changed)

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=2)

        thick_row = ttk.Frame(right, style="TB.TFrame")
        thick_row.pack(anchor="w", fill="x")
        ttk.Label(thick_row, text="Door thk (cm):", style="TB.TLabel").pack(side="left")
        ttk.Spinbox(thick_row, from_=1.0, to=1000.0, increment=1.0,
                    textvariable=self._door_thickness_var,
                    width=8, font=("TkDefaultFont", 10, "bold")).pack(side="left", padx=(2, 0))

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=2)
        ttk.Button(right, text="Apply Function…", style="TB.TButton",
                   command=self.apply_function_dialog).pack(anchor="w", fill="x", pady=(2, 0))

        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=2)
        ttk.Label(right, text="Selection:", style="TB.TLabel").pack(anchor="w")
        # `_info_outer` is the slot that swaps between the read-only
        # Text view and the editable property form (Cell Edit mode).
        self._info_outer = ttk.Frame(right, style="TB.TFrame")
        self._info_outer.pack(anchor="w", fill="both", expand=True,
                              pady=(2, 0))
        info_frame = ttk.Frame(self._info_outer, style="TB.TFrame")
        info_frame.pack(anchor="w", fill="both", expand=True)
        self._info_text = tk.Text(
            info_frame, width=22, wrap="word",
            font=("Consolas", 9, "bold"), relief="flat",
            bg="#f5f5f5", fg="#1a1a1a",
            insertbackground="#1a1a1a", state="disabled",
            cursor="arrow",
        )
        _info_sb = ttk.Scrollbar(info_frame, orient="vertical", command=self._info_text.yview)
        self._info_text.configure(yscrollcommand=_info_sb.set)
        _info_sb.pack(side="right", fill="y")
        self._info_text.pack(side="left", fill="both", expand=True)
        self._info_text_frame = info_frame
        self._info_edit_frame: Optional[ttk.Frame] = None

        # Canvas fills remaining space
        self.figure = Figure(dpi=100)
        self.figure.patch.set_facecolor("black")
        self.ax = self.figure.add_subplot(111)
        self.figure.subplots_adjust(left=0.05, right=0.99, top=0.97, bottom=0.04)
        self.ax.set_facecolor("black")
        self.ax.tick_params(colors="white")
        self.ax.spines[:].set_edgecolor("white")

        self.canvas = FigureCanvasTkAgg(self.figure, master=self.root)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(side="left", fill="both", expand=True)

        # Animated overlay artists for blit-based drag rendering (vertex edit)
        self._ov_line, = self.ax.plot([], [], color="orange", linewidth=2.0,
                                      animated=True, zorder=3)
        self._ov_verts = self.ax.scatter([], [], color="deepskyblue", s=50,
                                         animated=True, zorder=6)
        self._ov_sel_vert = self.ax.scatter([], [], color="red", s=100,
                                             animated=True, zorder=7, alpha=0.8)
        self._ov_snap_ring = self.ax.scatter([], [], color="green", s=90,
                                              marker="o", facecolors="none",
                                              linewidths=2, animated=True, zorder=8)
        # Animated artists for background move/resize blit
        self._ov_bg_img = self.ax.imshow(
            np.zeros((2, 2, 3), dtype=np.uint8),
            extent=[0, 1, 0, 1], aspect='auto', zorder=0,
            origin='upper', animated=True, alpha=0.5
        )
        self._ov_bg_img.set_visible(False)
        self._ov_bg_border, = self.ax.plot([], [], color="cyan", linewidth=1.0,
                                            linestyle="--", animated=True, zorder=9)
        self._ov_bg_handles = self.ax.scatter([], [], color="cyan", s=80,
                                               marker="s", animated=True, zorder=10)

        # Animated artist for space-move blit
        self._ov_space_move, = self.ax.plot([], [], color="orange", linewidth=2.0,
                                             animated=True, zorder=6)

        # Animated artists for node drag blit
        self._ov_drag_node = self.ax.scatter([], [], color="tomato", s=18,
                                              animated=True, zorder=5)
        self._ov_drag_edges = LineCollection([], colors="deepskyblue", linewidths=1.0,
                                              animated=True, zorder=4)
        self.ax.add_collection(self._ov_drag_edges)

        # Space creation preview
        self._space_create_preview_line, = self.ax.plot([], [], color="green", linewidth=1.5,
                                                         linestyle="--", zorder=6)

        # Door creation preview (rubber-band line from pt1 to mouse)
        self._door_preview_line, = self.ax.plot([], [], color="magenta", linewidth=1.5,
                                                 linestyle="--", zorder=6)

        # Cell Split preview line
        self._split_preview_line, = self.ax.plot([], [], color="yellow", linewidth=1.5,
                                                   linestyle="--", zorder=8)
        self._split_pt1_dot = self.ax.scatter([], [], color="yellow", s=60, zorder=9)

    def _connect_matplotlib_events(self) -> None:
        self.canvas.mpl_connect("scroll_event", self.on_scroll)
        self.canvas.mpl_connect("button_press_event", self.on_mouse_press)
        self.canvas.mpl_connect("button_release_event", self.on_mouse_release)
        self.canvas.mpl_connect("motion_notify_event", self.on_mouse_move)
        self.canvas.mpl_connect("key_press_event", self.on_key_press)
        self.root.bind("<Control-z>", lambda e: self.undo())
        self.root.bind("<Control-Z>", lambda e: self.undo())

        # ------------------------------------------------------------------
        # Edit-mode shortcuts (single-letter, ignored while typing in Entry/
        # Text/Combobox/Listbox so search dialogs etc. work normally).
        #   N  →  Network → Node Edit
        #   E  →  Network → Edge Edit
        #   C  →  Cell    → Cell Edit
        #   D  →  Door    → New Door
        # ------------------------------------------------------------------
        def _typing_focus() -> bool:
            try:
                w = self.root.focus_get()
            except Exception:
                return False
            if w is None:
                return False
            try:
                cls = w.winfo_class()
            except Exception:
                return False
            return cls in ("Entry", "TEntry", "Text",
                           "TCombobox", "Listbox", "Spinbox", "TSpinbox")

        def _shortcut(handler):
            def _h(_evt):
                if _typing_focus():
                    return None
                handler()
            return _h

        self.root.bind("<KeyPress-n>", _shortcut(self.toggle_graph_edit_mode))
        self.root.bind("<KeyPress-N>", _shortcut(self.toggle_graph_edit_mode))
        self.root.bind("<KeyPress-e>", _shortcut(self.toggle_graph_connect_mode))
        self.root.bind("<KeyPress-E>", _shortcut(self.toggle_graph_connect_mode))
        self.root.bind("<KeyPress-c>", _shortcut(self.toggle_edit_mode))
        self.root.bind("<KeyPress-C>", _shortcut(self.toggle_edit_mode))
        self.root.bind("<KeyPress-d>", _shortcut(self.toggle_door_mode))
        self.root.bind("<KeyPress-D>", _shortcut(self.toggle_door_mode))
        # Tkinter-level Enter binding — works regardless of matplotlib focus state
        self.root.bind("<Return>", self._on_tk_enter)
        self.canvas.get_tk_widget().bind("<Return>", self._on_tk_enter)

    # ── Undo ──────────────────────────────────────────────
    def _push_undo(self, action: dict) -> None:
        self._undo_stack.append(action)
        if len(self._undo_stack) > 50:
            self._undo_stack.pop(0)

    def undo(self) -> None:
        if not self._undo_stack:
            self.status_var.set("Nothing to undo.")
            return
        action = self._undo_stack.pop()
        kind = action["kind"]

        if kind == "move_vertex":
            sp = self.model.spaces.get(action["space_id"])
            if sp:
                sp.polygons_3d[action["poly_idx"]] = action["old_poly"]
                sp.centers = [c for c in [polygon_centroid(p) for p in sp.polygons_3d] if c is not None]
            self.status_var.set("Undo: vertex move")

        elif kind == "delete_vertex":
            sp = self.model.spaces.get(action["space_id"])
            if sp:
                sp.polygons_3d = action["old_polygons_3d"]
                sp.centers = [c for c in [polygon_centroid(p) for p in sp.polygons_3d] if c is not None]
            self.status_var.set("Undo: vertex delete")

        elif kind == "insert_vertex":
            sp = self.model.spaces.get(action["space_id"])
            if sp:
                sp.polygons_3d[action["poly_idx"]] = action["old_poly"]
                sp.centers = [c for c in [polygon_centroid(p) for p in sp.polygons_3d] if c is not None]
            self.status_var.set("Undo: vertex insert")

        elif kind == "move_node":
            node = self.model.nodes.get(action["node_id"])
            if node:
                node.coord = action["old_coord"]
            for edge_id, old_coords in action["old_edge_coords"].items():
                edge = self.model.edges.get(edge_id)
                if edge:
                    edge.coords = old_coords
            self.status_var.set("Undo: node move")

        elif kind == "create_node":
            self.model.nodes.pop(action["node_id"], None)
            self._new_node_ids.discard(action["node_id"])
            self.status_var.set("Undo: node create")

        elif kind == "delete_node":
            self.model.nodes[action["node_id"]] = action["node"]
            if action["was_new"]:
                self._new_node_ids.add(action["node_id"])
            for eid, edge in action["deleted_edges"].items():
                self.model.edges[eid] = edge
                if action["was_new"]:
                    self._new_edge_ids.add(eid)
            self.status_var.set("Undo: node delete")

        elif kind == "create_edge":
            self.model.edges.pop(action["edge_id"], None)
            self._new_edge_ids.discard(action["edge_id"])
            self.status_var.set("Undo: edge create")

        elif kind == "delete_edge":
            self.model.edges[action["edge_id"]] = action["edge"]
            if action["was_new"]:
                self._new_edge_ids.add(action["edge_id"])
            self.status_var.set("Undo: edge delete")

        elif kind == "create_space":
            self.model.spaces.pop(action["space_id"], None)
            self._new_space_ids.discard(action["space_id"])
            self.status_var.set("Undo: space create")

        elif kind == "create_space_with_node":
            self.model.spaces.pop(action["space_id"], None)
            self._new_space_ids.discard(action["space_id"])
            if action["node_id"]:
                self.model.nodes.pop(action["node_id"], None)
                self._new_node_ids.discard(action["node_id"])
            self.status_var.set("Undo: space + dual node create")

        elif kind == "delete_space":
            # Restore space
            self.model.spaces[action["space_id"]] = action["space"]
            if action["was_new"]:
                self._new_space_ids.add(action["space_id"])
            # Restore dual node
            if action.get("dual_node_id") and action.get("dual_node"):
                self.model.nodes[action["dual_node_id"]] = action["dual_node"]
                if action.get("dual_node_was_new"):
                    self._new_node_ids.add(action["dual_node_id"])
            # Restore connected edges
            for eid, edge in action.get("deleted_edges", {}).items():
                self.model.edges[eid] = edge
                if eid in action.get("deleted_edges_new", set()):
                    self._new_edge_ids.add(eid)
            self.status_var.set("Undo: space delete (with dual node & edges)")

        elif kind == "move_space":
            sp = self.model.spaces.get(action["space_id"])
            if sp:
                sp.polygons_3d = action["old_polygons_3d"]
                sp.centers = [c for c in [polygon_centroid(p) for p in sp.polygons_3d] if c is not None]
            self.status_var.set("Undo: space move")

        elif kind == "split_space":
            # Remove new sub-B space
            sid2 = action.get("new_space_id")
            if sid2:
                self.model.spaces.pop(sid2, None)
                self._new_space_ids.discard(sid2)
            # Remove new node for sub-B
            nid2 = action.get("new_node_id")
            if nid2:
                self.model.nodes.pop(nid2, None)
                self._new_node_ids.discard(nid2)
            # Remove new edge
            if action.get("new_edge_id"):
                self.model.edges.pop(action["new_edge_id"], None)
                self._new_edge_ids.discard(action["new_edge_id"])
            # Restore original space polygon/name in-place (sp_id kept throughout)
            sp = self.model.spaces.get(action["orig_space_id"])
            if sp is not None:
                sp.polygons_3d = action["orig_polygons_3d"]
                sp.centers = action["orig_centers"]
                sp.name = action["orig_name"]
            # Restore original dual node coordinate
            nid_a = action.get("orig_dual_node_id")
            orig_coord = action.get("orig_dual_node_coord")
            if nid_a and orig_coord and nid_a in self.model.nodes:
                self.model.nodes[nid_a].coord = orig_coord
            self.status_var.set("Undo: cell split")

        self.refresh_view()

    def _edge_status_msg(self, edge_id: str) -> str:
        """Status message for a selected edge: includes connected node IDs."""
        edge = self.model.edges.get(edge_id) if self.model else None
        if edge and edge.connects:
            # connects may be node IDs or space IDs; show as-is
            nodes_str = ", ".join(edge.connects)
            return f"Edge {edge_id} | connects: {nodes_str} — Delete key to delete"
        return f"Edge {edge_id} selected — Delete key to delete"

    def new_file(self) -> None:
        """Create a new empty IndoorGML model."""
        model = IndoorModel()
        # Minimal valid XML skeleton
        ns = {
            "": "http://www.opengis.net/indoorgml/2.0/core",
            "gml": "http://www.opengis.net/gml/3.2",
            "xlink": "http://www.w3.org/1999/xlink",
        }
        root_elem = ET.Element("IndoorFeatures")
        model.source_tree = ET.ElementTree(root_elem)
        model.source_path = ""

        self.model = model
        self.file_var.set("")
        self._view_locked = False
        self._undo_stack.clear()
        self._new_node_ids.clear()
        self._new_edge_ids.clear()
        self._new_space_ids.clear()
        self._space_create_verts.clear()
        self._edit_selected_space_id = None
        self._edit_selected_vertex_idx = None
        self._graph_selected_node = None
        self._graph_selected_edge = None
        self._graph_drag_node = None
        self._door_pt1 = None
        self._door_mode = False
        self._door_mode_var.set(False)
        self._bg_image = None
        self._bg_image_path = ""
        self._bg_extent = None
        self._update_floor_combo()
        self.status_var.set("New file — start drawing with Edit > New Cell.")
        self.edit_status_var.set("")
        self._graph_edit_status_var.set("")
        self.refresh_view()

    def close_file(self) -> None:
        """Close the current file and reset to empty state."""
        self.model = None
        self.file_var.set("")
        self._view_locked = False
        self._undo_stack.clear()
        self._new_node_ids.clear()
        self._new_edge_ids.clear()
        self._new_space_ids.clear()
        self._space_create_verts.clear()
        self._edit_selected_space_id = None
        self._edit_selected_vertex_idx = None
        self._graph_selected_node = None
        self._graph_selected_edge = None
        self._graph_drag_node = None
        self._door_pt1 = None
        self._door_mode = False
        self._door_mode_var.set(False)
        self._update_floor_combo()
        self.status_var.set("Open an IndoorGML file.")
        self.edit_status_var.set("")
        self._graph_edit_status_var.set("")
        self.ax.clear()
        self.ax.set_facecolor("black")
        self.ax.figure.patch.set_facecolor("black")
        self.ax.set_title("IndoorGML Viewer (2D)", color="white")
        self.ax.set_xlabel("X", color="white")
        self.ax.set_ylabel("Y", color="white")
        self.ax.tick_params(colors="white")
        self.ax.spines[:].set_edgecolor("white")
        self.canvas.draw_idle()

    def open_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Open IndoorGML XML",
            filetypes=[("XML files", "*.xml"), ("GML files", "*.gml"), ("All files", "*.*")]
        )
        if not path:
            return

        try:
            model = parse_indoorgml(path)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to parse file:\n{e}")
            return

        self.model = model
        self.file_var.set(path)
        self._view_locked = False
        self._new_node_ids.clear()
        self._new_edge_ids.clear()
        self._new_space_ids.clear()
        self._new_ilc_ids.clear()
        self._space_create_verts.clear()

        # ── Reset all mode flags ──────────────────────────────────────────
        self._edit_mode = False;               self._edit_mode_var.set(False)
        self._edit_selected_space_id = None;   self._edit_selected_vertex_idx = None
        self._graph_edit_mode = False;         self._graph_edit_mode_var.set(False)
        self._graph_connect_mode = False;      self._graph_connect_mode_var.set(False)
        self._graph_selected_node = None;      self._graph_drag_node = None
        self._graph_connect_first = None
        self._vertical_edge_mode = False;      self._vertical_edge_mode_var.set(False)
        self._vertical_edge_first_node = None; self._vertical_edge_first_floor = None
        self._space_create_mode = False;       self._space_create_mode_var.set(False)
        self._rect_create_mode = False;        self._rect_create_mode_var.set(False)
        self._rect_create_pt1 = None
        self._split_mode = False;              self._split_mode_var.set(False)
        self._split_space_id = None;           self._split_vertex_idx = None
        self._split_pt1 = None
        self._door_mode = False;               self._door_mode_var.set(False)
        self._door_pt1 = None
        self._ilc_mode = False;                self._ilc_mode_var.set(False)
        self._ilc_space1 = None
        self._bg_move_mode = False;            self._bg_move_mode_var.set(False)
        self._bg_resize_mode = False;          self._bg_resize_mode_var.set(False)
        self._view_selected_type = None;       self._view_selected_id = None
        self.edit_status_var.set("")
        self._graph_edit_status_var.set("")
        # ─────────────────────────────────────────────────────────────────

        self._update_floor_combo()
        self._update_layer_combo()
        # Show all objects on open
        self.floor_var.set("All")
        self.layer_var.set("All")

        self.status_var.set(
            f"Loaded. Spaces: {len(model.spaces)}, Nodes: {len(model.nodes)}, Edges: {len(model.edges)}"
        )

        # Auto-restore background for the initial floor
        self._bg_image = None
        self._bg_image_path = ""
        self._bg_extent = None
        self._restore_bg_for_floor("All")
        # Restore file-wide reference (anchor) points if previously set
        if self.model and self.model.source_path:
            self._reference_points = load_reference_points(
                self.model.source_path)
        else:
            self._reference_points = None
        self._view_locked = False
        self.refresh_view()
        self._warn_about_load_conflicts()

    def _warn_about_load_conflicts(self) -> None:
        """Tell the user when the source file held two elements with one id.

        Features live in id-keyed dicts, so a repeated id means only the last
        copy survives the load — the earlier one is dropped without a trace.
        Staying quiet about that is the worst option: the user edits and saves
        a file that has quietly lost features. Point them at Check → Find
        Duplicate IDs, which reads the XML tree directly and can clean up.
        """
        if self.model is None or not self.model.load_conflicts:
            return

        by_kind: Dict[str, List[str]] = {}
        schema_note = ""
        for kind, fid, copies in self.model.load_conflicts:
            if kind == "connects→CellSpace":
                schema_note = (
                    f"\n\n{copies} edge endpoint(s) reference a CellSpace "
                    f"instead of a Node. An IndoorGML Edge connects Nodes, "
                    f"so this violates the model — but an XSD validator "
                    f"cannot catch it, because xlink:href is only typed as "
                    f"anyURI. They have been resolved to their dual Nodes "
                    f"for editing, and saving will write the corrected "
                    f"references back."
                )
                continue
            by_kind.setdefault(kind, []).append(fid)

        if not by_kind:
            if schema_note:
                messagebox.showwarning("Non-conforming references",
                                       schema_note.strip())
            return

        lines = []
        for kind in sorted(by_kind):
            ids = by_kind[kind]
            shown = ", ".join(ids[:5])
            if len(ids) > 5:
                shown += f", … (+{len(ids) - 5} more)"
            lines.append(f"  • {kind}: {len(ids)} — {shown}")

        total = sum(len(v) for v in by_kind.values())
        messagebox.showwarning(
            "Duplicate IDs in source file",
            f"{total} element(s) share an id with an earlier element of the "
            f"same kind. Only the last copy of each was loaded, so the file "
            f"currently in the editor has fewer features than the file on "
            f"disk:\n\n" + "\n".join(lines) +
            "\n\nUse Check → Find Duplicate IDs... to inspect every copy and "
            "clean them up before saving." + schema_note,
        )

    def refresh_view(self) -> None:
        if self.model is None:
            self.ax.clear()
            self.ax.set_title("IndoorGML Viewer")
            self.canvas.draw_idle()
            return

        # Preserve the current zoom/pan
        saved_xlim = self.ax.get_xlim() if self._view_locked else None
        saved_ylim = self.ax.get_ylim() if self._view_locked else None

        try:
            excluded = {self._edit_selected_space_id} if self._edit_selected_space_id else None
            ex_nodes = {self._graph_drag_node} if self._graph_drag_node else None
            draw_on_axes(
                self.ax,
                self.model,
                floor=self.floor_var.get(),
                show_labels=self.label_var.get(),
                excluded_space_ids=excluded,
                show_nodes=self.show_nodes_var.get(),
                function_color_map=self._function_color_map or None,
                function_label_map=(dict(self._function_codes)
                                    if self._function_codes else None),
                excluded_node_ids=ex_nodes,
                layer_id=self._layer_id_from_combo(),
                bg_layer_id=self._bg_layer_id_from_combo(),
                bg_floor=self._bg_floor_from_combo(),
                show_offfloor_anchors=self.show_offfloor_anchors_var.get(),
            )
            self._draw_background()
            self._draw_view_selection_overlay()
            self._draw_edit_overlay()
            self._draw_graph_edit_overlay()
            self._draw_space_create_overlay()
            self._draw_door_overlay()
            self._draw_split_overlay()
            self._draw_ilc_overlay()
            self._draw_measure_overlay()
            self._draw_reference_points_overlay()
            self._draw_multi_apply_overlay()
            self._draw_multi_class_overlay()

            # Re-attach preview lines after ax.clear() removed them
            if ((self._space_create_mode or self._rect_create_mode)
                    and self._space_create_preview_line is not None):
                self.ax.add_line(self._space_create_preview_line)
            if self._door_mode and self._door_pt1 is not None:
                self.ax.add_line(self._door_preview_line)

            if saved_xlim is not None:
                # Restore the user's zoom/pan
                self.ax.set_xlim(saved_xlim)
                self.ax.set_ylim(saved_ylim)
            else:
                # First draw for this context: fit view to all visible objects
                self.fit_to_extent(_redraw=False)

            self.canvas.draw_idle()
            self._view_locked = True

            self.status_var.set(
                f"Showing floor: {self.floor_var.get()} | "
                f"Spaces: {len(self.model.spaces)}, Nodes: {len(self.model.nodes)}, Edges: {len(self.model.edges)}"
            )
        except Exception as e:
            messagebox.showerror("Error", f"Failed to render view:\n{e}")

    def fit_to_extent(self, event=None, _redraw: bool = True) -> None:
        """Fit the canvas view to the bounding box of all currently visible objects."""
        if self.model is None:
            return

        xs: List[float] = []
        ys: List[float] = []

        floor    = self.floor_var.get()
        layer_id = self._layer_id_from_combo()

        for sp in self.model.spaces.values():
            if floor and floor != "All" and sp.level != floor:
                continue
            if layer_id and sp.layer_id != layer_id:
                continue
            for poly in sp.polygons_3d:
                for x, y, _ in poly:
                    xs.append(x)
                    ys.append(y)

        for node in self.model.nodes.values():
            if floor and floor != "All" and node.level != floor:
                continue
            if layer_id and node.layer_id != layer_id:
                continue
            if node.coord:
                xs.append(node.coord[0])
                ys.append(node.coord[1])

        if not xs:
            return

        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)

        # Add 5 % padding on each side
        pad_x = max((xmax - xmin) * 0.05, 1.0)
        pad_y = max((ymax - ymin) * 0.05, 1.0)
        xmin -= pad_x;  xmax += pad_x
        ymin -= pad_y;  ymax += pad_y

        # Maintain aspect ratio matching the actual drawable axes box.
        # Prefer the current axes bbox; fall back to the Tk widget size
        # (for the very first draw before matplotlib has laid out the axes).
        ax_ratio = None
        try:
            bbox = self.ax.get_window_extent()
            if bbox.width > 0 and bbox.height > 0:
                ax_ratio = bbox.width / bbox.height
        except Exception:
            ax_ratio = None
        if ax_ratio is None or ax_ratio <= 0:
            canvas_w = self.canvas.get_tk_widget().winfo_width()
            canvas_h = self.canvas.get_tk_widget().winfo_height()
            if canvas_w > 0 and canvas_h > 0:
                ax_ratio = canvas_w / canvas_h

        if ax_ratio is not None and ax_ratio > 0:
            data_w  = xmax - xmin
            data_h  = ymax - ymin
            data_ratio = data_w / data_h if data_h else 1.0
            if data_ratio > ax_ratio:              # data wider → expand height
                extra = data_w / ax_ratio - data_h
                ymin -= extra / 2
                ymax += extra / 2
            else:                                  # data taller → expand width
                extra = data_h * ax_ratio - data_w
                xmin -= extra / 2
                xmax += extra / 2

        # The "Ignoring fixed [xy] limits..." warning is filtered globally
        # at module import (see top of file).
        self.ax.set_xlim(xmin, xmax)
        self.ax.set_ylim(ymin, ymax)
        self._view_locked = True
        if _redraw:
            self.canvas.draw_idle()

    def on_floor_changed(self, event=None) -> None:
        self._restore_bg_for_floor(self.floor_var.get())
        # Keep current zoom/pan when switching floors
        self.refresh_view()

    def on_layer_changed(self, event=None) -> None:
        # Keep current zoom/pan when switching layers
        self.refresh_view()

    def _bg_floor_from_combo(self) -> Optional[str]:
        """Return the BG floor value, or None if 'None' is selected."""
        v = self.bg_floor_var.get()
        return None if v == "None" else v

    def _update_floor_combo(self) -> None:
        """Refresh Active Floor dropdown from model spaces."""
        if self.model is None:
            self.floor_combo["values"] = ["All"]
            self.floor_var.set("All")
            return
        floors = set(get_available_floors(self.model))
        # Include any user-created empty floors (no cells yet)
        floors.update(self._floor_z_overrides.keys())
        floor_list = sorted(floors, key=floor_sort_key)
        active_values = ["All"] + floor_list if floor_list else ["All"]
        self.floor_combo["values"] = active_values
        if self.floor_var.get() not in active_values:
            self.floor_var.set("All")

    def _update_layer_combo(self) -> None:
        """Refresh active-layer dropdown from model.layers."""
        if self.model is None:
            self.layer_combo["values"] = ["All"]
            self.layer_var.set("All")
            return
        layer_entries = [
            f"{lid} ({lf.theme})" for lid, lf in self.model.layers.items()
        ]
        active_values = ["All"] + layer_entries
        self.layer_combo["values"] = active_values
        if self.layer_var.get() not in active_values:
            self.layer_var.set("All")

    def _layer_id_from_combo(self) -> Optional[str]:
        """Parse raw layer_id from active-layer combo value like 'L1 (Navigation)'."""
        v = self.layer_var.get()
        if v == "All":
            return None
        return v.split(" (")[0]

    def _bg_layer_id_from_combo(self) -> Optional[str]:
        """Parse raw layer_id from BG-layer combo value, or None if 'None'."""
        v = self.bg_layer_var.get()
        if v == "None":
            return None
        return v.split(" (")[0]

    def new_layer(self) -> None:
        if self.model is None:
            messagebox.showwarning("No file", "Open or create a file first.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("New Layer")
        dlg.resizable(False, False)
        dlg.grab_set()

        ttk.Label(dlg, text="Layer ID:").grid(row=0, column=0, padx=8, pady=8, sticky="e")
        # Auto-generate default id
        existing_ids = set(self.model.layers.keys())
        i = 1
        while f"L{i}" in existing_ids:
            i += 1
        lid_var = tk.StringVar(value=f"L{i}")
        ttk.Entry(dlg, textvariable=lid_var, width=20).grid(row=0, column=1, padx=8, pady=8)

        ttk.Label(dlg, text="Theme:").grid(row=1, column=0, padx=8, pady=4, sticky="e")
        theme_var = tk.StringVar(value="Navigation")
        theme_combo = ttk.Combobox(dlg, textvariable=theme_var, width=18,
                                   values=["Navigation", "Sensor", "Topographic", "Zone", "Floor", "Other"])
        theme_combo.grid(row=1, column=1, padx=8, pady=4)

        confirmed = [False]
        def on_ok():
            confirmed[0] = True
            dlg.destroy()
        def on_cancel():
            dlg.destroy()

        btn = ttk.Frame(dlg)
        btn.grid(row=2, column=0, columnspan=2, pady=8)
        ttk.Button(btn, text="OK",     command=on_ok).pack(side="left", padx=4)
        ttk.Button(btn, text="Cancel", command=on_cancel).pack(side="left", padx=4)
        dlg.wait_window()

        if not confirmed[0]:
            return

        lid = lid_var.get().strip()
        theme = theme_var.get().strip() or "Navigation"
        if not lid:
            messagebox.showerror("Error", "Layer ID cannot be empty.")
            return
        if lid in self.model.layers:
            messagebox.showerror("Error", f"Layer '{lid}' already exists.")
            return

        # Create XML element for the new ThematicLayer
        root = self.model.source_tree.getroot()
        core_ns, gml_ns, _ = _detect_namespaces(root)
        id_ns = _detect_id_ns(root, gml_ns)
        CORE  = f"{{{core_ns}}}"
        ID_NS = f"{{{id_ns}}}"

        # Find or create the <layers> wrapper
        layers_wrap = None
        for elem in root:
            if local_name(elem.tag) == "layers":
                layers_wrap = elem
                break
        if layers_wrap is None:
            layers_wrap = ET.SubElement(root, f"{CORE}layers")

        tl_elem = ET.SubElement(layers_wrap, f"{CORE}ThematicLayer")
        tl_elem.attrib[f"{ID_NS}id"] = lid
        theme_elem = ET.SubElement(tl_elem, f"{CORE}theme")
        theme_elem.text = theme
        ET.SubElement(tl_elem, f"{CORE}primalSpace").append(
            _make_primal_space_layer_elem(CORE, id_ns, f"PS_{lid}")
        )
        ET.SubElement(tl_elem, f"{CORE}dualSpace").append(
            _make_dual_space_layer_elem(CORE, id_ns, f"DS_{lid}")
        )

        lf = LayerFeature(layer_id=lid, theme=theme, source_element=tl_elem)
        self.model.layers[lid] = lf

        self._update_layer_combo()
        # Auto-select the new layer
        self.layer_var.set(f"{lid} ({theme})")
        self.status_var.set(f"New layer '{lid}' ({theme}) created.")
        self.refresh_view()

    def rename_layer(self) -> None:
        lid = self._layer_id_from_combo()
        if lid is None:
            messagebox.showinfo("Rename Layer", "Select a specific layer first.")
            return
        lf = self.model.layers.get(lid)
        if lf is None:
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Rename Layer")
        dlg.resizable(False, False)
        dlg.grab_set()

        ttk.Label(dlg, text="Theme:").grid(row=0, column=0, padx=8, pady=8, sticky="e")
        theme_var = tk.StringVar(value=lf.theme)
        ttk.Combobox(dlg, textvariable=theme_var, width=18,
                     values=["Navigation", "Sensor", "Topographic", "Zone", "Floor", "Other"]
                     ).grid(row=0, column=1, padx=8, pady=8)

        confirmed = [False]
        def on_ok():
            confirmed[0] = True
            dlg.destroy()
        ttk.Button(dlg, text="OK",     command=on_ok).grid(row=1, column=0, padx=4, pady=8)
        ttk.Button(dlg, text="Cancel", command=dlg.destroy).grid(row=1, column=1, padx=4, pady=8)
        dlg.wait_window()

        if not confirmed[0]:
            return
        lf.theme = theme_var.get().strip() or lf.theme
        # Update XML
        if lf.source_element is not None:
            for child in lf.source_element:
                if local_name(child.tag) == "theme":
                    child.text = lf.theme
                    break
        self._update_layer_combo()
        self.layer_var.set(f"{lid} ({lf.theme})")
        self.status_var.set(f"Layer '{lid}' renamed to theme '{lf.theme}'.")

    def delete_layer(self) -> None:
        lid = self._layer_id_from_combo()
        if lid is None:
            messagebox.showinfo("Delete Layer", "Select a specific layer first.")
            return
        if len(self.model.layers) <= 1:
            messagebox.showerror("Delete Layer", "Cannot delete the only layer.")
            return
        if not messagebox.askyesno("Delete Layer",
                                   f"Delete layer '{lid}' and ALL its spaces, nodes, and edges?"):
            return

        # Remove all spaces/nodes/edges belonging to this layer
        for sid in [s for s, sp in self.model.spaces.items() if sp.layer_id == lid]:
            self.model.spaces.pop(sid, None)
            self._new_space_ids.discard(sid)
        for nid in [n for n, nd in self.model.nodes.items() if nd.layer_id == lid]:
            self.model.nodes.pop(nid, None)
            self._new_node_ids.discard(nid)
        for eid in [e for e, ed in self.model.edges.items() if ed.layer_id == lid]:
            self.model.edges.pop(eid, None)
            self._new_edge_ids.discard(eid)

        # Remove XML element
        lf = self.model.layers.pop(lid, None)
        if lf and lf.source_element is not None:
            root = self.model.source_tree.getroot()
            for layers_wrap in root.iter():
                if local_name(layers_wrap.tag) == "layers":
                    try:
                        layers_wrap.remove(lf.source_element)
                    except ValueError:
                        pass
                    break

        self._update_layer_combo()
        self.layer_var.set("All")
        self.status_var.set(f"Layer '{lid}' deleted.")
        self.refresh_view()

    # ── InterLayerConnection ─────────────────────────────────────────────────

    def setup_ilc_mode(self) -> None:
        """Open dialog to choose background/foreground layers, then enter ILC connect mode."""
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return
        if len(self.model.layers) < 2:
            messagebox.showwarning("Connect Layers", "At least two layers are required.")
            return

        layer_ids = list(self.model.layers.keys())
        layer_labels = [f"{lid} ({lf.theme})" for lid, lf in self.model.layers.items()]

        dlg = tk.Toplevel(self.root)
        dlg.title("Connect Layers — Setup")
        dlg.resizable(False, False)
        dlg.grab_set()

        _mf = ("TkDefaultFont", 9, "bold")
        ttk.Label(dlg, text="Background Layer:", font=_mf).grid(row=0, column=0, padx=10, pady=8, sticky="e")
        bg_var = tk.StringVar(value=layer_labels[0])
        ttk.Combobox(dlg, textvariable=bg_var, values=layer_labels,
                     state="readonly", width=24, font=_mf).grid(row=0, column=1, padx=10, pady=8)

        ttk.Label(dlg, text="Foreground Layer:", font=_mf).grid(row=1, column=0, padx=10, pady=8, sticky="e")
        fg_var = tk.StringVar(value=layer_labels[1] if len(layer_labels) > 1 else layer_labels[0])
        ttk.Combobox(dlg, textvariable=fg_var, values=layer_labels,
                     state="readonly", width=24, font=_mf).grid(row=1, column=1, padx=10, pady=8)

        result = [None]

        def _ok():
            bg_label = bg_var.get()
            fg_label = fg_var.get()
            if bg_label == fg_label:
                messagebox.showerror("Error", "Background and Foreground layers must be different.",
                                     parent=dlg)
                return
            bg_lid = layer_ids[layer_labels.index(bg_label)]
            fg_lid = layer_ids[layer_labels.index(fg_label)]
            result[0] = (bg_lid, fg_lid)
            dlg.destroy()

        def _cancel():
            dlg.destroy()

        btn_frame = ttk.Frame(dlg)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=8)
        ttk.Button(btn_frame, text="OK",     command=_ok).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Cancel", command=_cancel).pack(side="left", padx=6)
        dlg.wait_window()

        if result[0] is None:
            return

        self._ilc_bg_layer, self._ilc_fg_layer = result[0]
        self._ilc_space1 = None
        self._ilc_mode = True
        self._ilc_mode_var.set(True)
        self.edit_status_var.set(
            f"ILC mode: BG={self._ilc_bg_layer}, FG={self._ilc_fg_layer} — "
            f"click a cell in BG layer first")
        self.refresh_view()

    def toggle_ilc_mode(self) -> None:
        """Toggle ILC connect mode on/off."""
        want_on = self._ilc_mode_var.get()
        if want_on:
            # Deactivate every other mode so clicks aren't intercepted.
            self._deactivate_all_edit_modes()
            if self._ilc_bg_layer is None or self._ilc_fg_layer is None:
                self.setup_ilc_mode()
                return
            self._ilc_mode = True
            self._ilc_mode_var.set(True)
            self.edit_status_var.set(
                f"ILC mode: BG={self._ilc_bg_layer}, FG={self._ilc_fg_layer} — "
                f"click a cell in BG layer first")
        else:
            self._ilc_mode = False
            self._ilc_space1 = None
            self.edit_status_var.set("")
        self.refresh_view()

    def _handle_ilc_click(self, xdata: float, ydata: float) -> bool:
        """Handle a canvas click in ILC mode. Returns True if event was consumed."""
        if not self._ilc_mode:
            return False
        sid = self._find_space_at(xdata, ydata)
        if sid is None:
            return True  # consumed but no target

        sp = self.model.spaces.get(sid)
        if sp is None:
            return True

        if self._ilc_space1 is None:
            # Expect a cell from the BG layer
            if sp.layer_id != self._ilc_bg_layer:
                self.edit_status_var.set(
                    f"ILC: select a cell from BG layer '{self._ilc_bg_layer}' first")
                return True
            self._ilc_space1 = sid
            self.edit_status_var.set(
                f"ILC: '{sid}' (BG) selected — now click a cell in FG layer '{self._ilc_fg_layer}'")
            self.refresh_view()
            return True
        else:
            # Expect a cell from the FG layer
            if sp.layer_id != self._ilc_fg_layer:
                self.edit_status_var.set(
                    f"ILC: select a cell from FG layer '{self._ilc_fg_layer}'")
                return True
            sid2 = sid
            sid1 = self._ilc_space1

            # Generate unique ILC id
            existing = set(self.model.inter_layer_connections.keys())
            i = len(existing) + 1
            while f"ILC_{i}" in existing:
                i += 1
            ilc_id = f"ILC_{i}"

            ilc = InterLayerConnection(
                ilc_id=ilc_id,
                space1_id=sid1,
                space2_id=sid2,
                layer1_id=self._ilc_bg_layer,
                layer2_id=self._ilc_fg_layer,
                type_of_topo_expr="dummy",
            )
            self.model.inter_layer_connections[ilc_id] = ilc
            self._new_ilc_ids.add(ilc_id)

            self._ilc_space1 = None
            self.edit_status_var.set(
                f"ILC created: {ilc_id} ({sid1} ↔ {sid2}) — click next BG cell or disable mode")
            self.refresh_view()
            return True

    def list_ilc(self) -> None:
        """Show a dialog listing all InterLayerConnections."""
        if self.model is None or not self.model.inter_layer_connections:
            messagebox.showinfo("Connections", "No InterLayerConnections defined.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("InterLayerConnections")
        dlg.resizable(True, True)

        cols = ("ID", "Space 1", "Layer 1", "Space 2", "Layer 2", "TypeOfTopoExpr")
        tv = ttk.Treeview(dlg, columns=cols, show="headings", height=15)
        for c in cols:
            tv.heading(c, text=c)
            tv.column(c, width=130, anchor="w")
        for ilc in self.model.inter_layer_connections.values():
            tv.insert("", "end", values=(
                ilc.ilc_id, ilc.space1_id, ilc.layer1_id,
                ilc.space2_id, ilc.layer2_id, ilc.type_of_topo_expr))

        sb = ttk.Scrollbar(dlg, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def delete_ilc(self) -> None:
        """Open a dialog to select and delete an InterLayerConnection."""
        if self.model is None or not self.model.inter_layer_connections:
            messagebox.showinfo("Delete Connection", "No InterLayerConnections defined.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Delete InterLayerConnection")
        dlg.resizable(True, True)
        dlg.grab_set()

        ttk.Label(dlg, text="Select a connection to delete:").pack(anchor="w", padx=8, pady=(8, 2))

        cols = ("ID", "Space 1", "Layer 1", "Space 2", "Layer 2", "TypeOfTopoExpr")
        tv = ttk.Treeview(dlg, columns=cols, show="headings", height=12, selectmode="browse")
        for c in cols:
            tv.heading(c, text=c)
            tv.column(c, width=120, anchor="w")
        for ilc in self.model.inter_layer_connections.values():
            tv.insert("", "end", iid=ilc.ilc_id, values=(
                ilc.ilc_id, ilc.space1_id, ilc.layer1_id,
                ilc.space2_id, ilc.layer2_id, ilc.type_of_topo_expr))

        sb = ttk.Scrollbar(dlg, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=4)
        sb.pack(side="left", fill="y", pady=4)

        def _do_delete():
            sel = tv.selection()
            if not sel:
                messagebox.showwarning("Delete Connection", "Select a connection first.", parent=dlg)
                return
            ilc_id = sel[0]
            if not messagebox.askyesno("Delete Connection",
                                       f"Delete connection '{ilc_id}'?", parent=dlg):
                return
            ilc = self.model.inter_layer_connections.pop(ilc_id, None)
            self._new_ilc_ids.discard(ilc_id)
            # Remove from XML if it was already written
            if ilc and ilc.source_element is not None:
                root = self.model.source_tree.getroot()
                for parent in root.iter():
                    try:
                        parent.remove(ilc.source_element)
                        break
                    except ValueError:
                        pass
                # Also remove the wrapper element if it is now empty
                for parent in root.iter():
                    if local_name(parent.tag) == "interLayerConnection":
                        if len(parent) == 0:
                            for gp in root.iter():
                                try:
                                    gp.remove(parent)
                                    break
                                except ValueError:
                                    pass
                            break
            tv.delete(ilc_id)
            self.edit_status_var.set(f"Deleted ILC '{ilc_id}'.")
            self.refresh_view()

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill="x", padx=8, pady=6)
        ttk.Button(btn_frame, text="Delete", command=_do_delete).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Close",  command=dlg.destroy).pack(side="left", padx=4)

    def _draw_ilc_overlay(self) -> None:
        """Draw lines between connected cell centers for all ILCs."""
        if not self.show_ilc_var.get():
            return
        if self.model is None or not self.model.inter_layer_connections:
            return
        for ilc in self.model.inter_layer_connections.values():
            sp1 = self.model.spaces.get(ilc.space1_id)
            sp2 = self.model.spaces.get(ilc.space2_id)
            if sp1 is None or sp2 is None:
                continue
            c1 = sp1.centers[0] if sp1.centers else None
            c2 = sp2.centers[0] if sp2.centers else None
            if c1 is None or c2 is None:
                continue
            # Highlight selected space1 in ILC mode
            if self._ilc_mode and self._ilc_space1 == ilc.space1_id:
                color = "cyan"
            else:
                color = "lime"
            self.ax.plot([c1[0], c2[0]], [c1[1], c2[1]],
                         color=color, linewidth=1.5, linestyle="--",
                         zorder=8, alpha=0.85)
            mx, my = (c1[0] + c2[0]) / 2, (c1[1] + c2[1]) / 2
            self.ax.text(mx, my, ilc.ilc_id, fontsize=7, color=color,
                         ha="center", va="bottom", zorder=9)
        # Highlight first-selected space in current click sequence
        if self._ilc_mode and self._ilc_space1:
            sp = self.model.spaces.get(self._ilc_space1)
            if sp and sp.polygons_3d:
                ring = sp.polygons_3d[0]
                xs = [p[0] for p in ring] + [ring[0][0]]
                ys = [p[1] for p in ring] + [ring[0][1]]
                self.ax.plot(xs, ys, color="cyan", linewidth=2.5, zorder=9)

    def _on_tk_enter(self, event=None) -> None:
        """Tkinter <Return> binding — finish space creation."""
        if self._space_create_mode and len(self._space_create_verts) >= 3:
            self._finish_space_creation()

    def on_key_press(self, event) -> None:
        # Home key: fit view to all visible objects
        if event.key == "home":
            self.fit_to_extent()
            return

        # Enter key via matplotlib (canvas has focus)
        if event.key in ("enter", "return") and self._space_create_mode:
            if len(self._space_create_verts) >= 3:
                self._finish_space_creation()
            return

        if event.key not in ("delete", "Delete"):
            return

        # Edit geometry mode: Delete key deletes selected vertex, then selected space
        if self._edit_mode:
            if self._edit_selected_vertex_idx is not None:
                self._delete_selected_vertex()
                return
            if self._edit_selected_space_id and self._edit_selected_space_id in self.model.spaces:
                sp_id = self._edit_selected_space_id
                sp = self.model.spaces[sp_id]

                # Collect dual node and its connected edges for cascade delete
                dual_node_id = sp.duality or None
                dual_node = self.model.nodes.get(dual_node_id) if dual_node_id else None
                deleted_edges: Dict[str, "EdgeFeature"] = {}
                if dual_node_id and dual_node_id in self.model.nodes:
                    deleted_edges = {
                        eid: self.model.edges[eid]
                        for eid, e in self.model.edges.items()
                        if dual_node_id in e.connects
                    }

                self._push_undo({
                    "kind": "delete_space",
                    "space_id": sp_id,
                    "space": sp,
                    "was_new": sp_id in self._new_space_ids,
                    # cascade
                    "dual_node_id": dual_node_id,
                    "dual_node": dual_node,
                    "dual_node_was_new": dual_node_id in self._new_node_ids if dual_node_id else False,
                    "deleted_edges": deleted_edges,
                    "deleted_edges_new": {eid for eid in deleted_edges if eid in self._new_edge_ids},
                })

                # Cascade delete: edges → node → space
                for eid in deleted_edges:
                    self.model.edges.pop(eid, None)
                    self._new_edge_ids.discard(eid)
                if dual_node_id:
                    self.model.nodes.pop(dual_node_id, None)
                    self._new_node_ids.discard(dual_node_id)
                self.model.spaces.pop(sp_id)
                self._new_space_ids.discard(sp_id)

                self._edit_selected_space_id = None
                n_edges = len(deleted_edges)
                msg = f"Deleted space {sp_id}"
                if dual_node_id:
                    msg += f", node {dual_node_id}"
                if n_edges:
                    msg += f", {n_edges} edge(s)"
                self.edit_status_var.set(msg + ".")
                self.refresh_view()
            return

        # Edge Edit mode: Delete key deletes selected edge
        if self._graph_connect_mode:
            if self._graph_selected_edge and self._graph_selected_edge in self.model.edges:
                edge_id = self._graph_selected_edge
                self._push_undo({
                    "kind": "delete_edge",
                    "edge_id": edge_id,
                    "edge": self.model.edges[edge_id],
                    "was_new": edge_id in self._new_edge_ids,
                })
                self._delete_edge(edge_id)
                self._graph_selected_edge = None
                self._graph_edit_status_var.set(f"Deleted edge {edge_id}.")
                self.refresh_view()
            return

        # Graph edit mode: Delete key deletes selected node or selected edge
        if self._graph_edit_mode:
            if self._graph_selected_node and self._graph_selected_node in self.model.nodes:
                node_id = self._graph_selected_node
                deleted_edges = {
                    eid: self.model.edges[eid]
                    for eid, e in self.model.edges.items() if node_id in e.connects
                }
                self._push_undo({
                    "kind": "delete_node",
                    "node_id": node_id,
                    "node": self.model.nodes[node_id],
                    "was_new": node_id in self._new_node_ids,
                    "deleted_edges": deleted_edges,
                })
                n_edges = len(deleted_edges)
                self._delete_node(node_id)
                self._graph_selected_node = None
                self._graph_selected_edge = None
                self._graph_edit_status_var.set(
                    f"Deleted node {node_id} and {n_edges} connected edge(s).")
                self.refresh_view()
                return
            if self._graph_selected_edge and self._graph_selected_edge in self.model.edges:
                edge_id = self._graph_selected_edge
                self._push_undo({
                    "kind": "delete_edge",
                    "edge_id": edge_id,
                    "edge": self.model.edges[edge_id],
                    "was_new": edge_id in self._new_edge_ids,
                })
                self._delete_edge(edge_id)
                self._graph_selected_edge = None
                self._graph_edit_status_var.set(f"Deleted edge {edge_id}.")
                self.refresh_view()

    def on_scroll(self, event) -> None:
        if event.inaxes != self.ax:
            return
        if event.xdata is None or event.ydata is None:
            return

        base_scale = 1.2

        if event.button == "up":
            scale_factor = 1 / base_scale
        elif event.button == "down":
            scale_factor = base_scale
        else:
            return

        cur_xlim = self.ax.get_xlim()
        cur_ylim = self.ax.get_ylim()

        xdata = event.xdata
        ydata = event.ydata

        new_width = (cur_xlim[1] - cur_xlim[0]) * scale_factor
        new_height = (cur_ylim[1] - cur_ylim[0]) * scale_factor

        relx = (cur_xlim[1] - xdata) / (cur_xlim[1] - cur_xlim[0]) if cur_xlim[1] != cur_xlim[0] else 0.5
        rely = (cur_ylim[1] - ydata) / (cur_ylim[1] - cur_ylim[0]) if cur_ylim[1] != cur_ylim[0] else 0.5

        self.ax.set_xlim([xdata - new_width * (1 - relx), xdata + new_width * relx])
        self.ax.set_ylim([ydata - new_height * (1 - rely), ydata + new_height * rely])

        self.canvas.draw_idle()

    def on_mouse_press(self, event) -> None:
        if event.inaxes != self.ax:
            return
        if event.x is None or event.y is None:
            return

        # Anchor-capture (Reference Point) mode runs above any other
        # click handler so it never collides with edit modes.
        if self._anchor_mode:
            if event.button == 3:
                # Right-click cancels the in-progress capture
                self._end_anchor_capture()
                return
            if event.button == 1 and event.xdata is not None and event.ydata is not None:
                self._on_anchor_click(event.xdata, event.ydata)
            return

        # Multi-cell function-code / class bulk-apply modes: only the
        # LEFT click toggles a cell. Right-click must fall through to
        # the standard pan handler at the bottom of this function so
        # the user can pan the canvas while picking many cells.
        if self._multi_apply_mode and event.button == 1:
            if event.xdata is not None and event.ydata is not None:
                self._on_multi_apply_click(event.xdata, event.ydata)
            return
        if self._multi_class_mode and event.button == 1:
            if event.xdata is not None and event.ydata is not None:
                self._on_multi_class_click(event.xdata, event.ydata)
            return

        if event.button == 1 and self._bg_resize_mode:
            if event.xdata is not None and event.ydata is not None and self._bg_extent is not None:
                self._bg_resize_handle = self._find_resize_handle(event.xdata, event.ydata)
                if self._bg_resize_handle:
                    self._cache_bg_drag_background()
            return

        if event.button == 1 and self._bg_move_mode:
            if event.xdata is not None and event.ydata is not None and self._bg_extent is not None:
                self._bg_move_start_data = (event.xdata, event.ydata)
                self._bg_move_start_extent = list(self._bg_extent)
                self._cache_bg_drag_background()
            return

        # --- InterLayerConnection mode ---
        if self._ilc_mode:
            if event.button == 1 and event.xdata is not None and event.ydata is not None:
                self._handle_ilc_click(event.xdata, event.ydata)
            elif event.button == 3:
                # Right-click: cancel current selection
                self._ilc_space1 = None
                self.edit_status_var.set(
                    f"ILC mode: BG={self._ilc_bg_layer}, FG={self._ilc_fg_layer} — "
                    f"click a cell in BG layer first")
                self.refresh_view()
            return

        # --- Cell Split mode ---
        if self._split_mode:
            if event.xdata is None or event.ydata is None:
                return
            if event.button == 3:
                self._split_pt1 = None
                self._split_space_id = None
                self._split_vertex_idx = None
                self._split_preview_line.set_data([], [])
                self._split_pt1_dot.set_offsets(np.empty((0, 2)))
                self.edit_status_var.set("Split cancelled.")
                self.refresh_view()
                return
            if event.button != 1:
                return
            if self._split_space_id is None:
                # Phase 1: pick a cell
                sp_id = self._find_space_at(event.xdata, event.ydata)
                if sp_id is None:
                    self.edit_status_var.set("Cell Split: click inside a cell.")
                    return
                sp = self.model.spaces[sp_id]
                if not sp.polygons_3d:
                    return
                self._split_space_id = sp_id
                # Immediately try to pick the nearest vertex as first vertex
                hit = self._find_vertex_near_split_space(event.xdata, event.ydata)
                if hit is not None:
                    vi, vpt = hit
                    self._split_vertex_idx = vi
                    self._split_pt1 = vpt
                    self._split_pt1_dot.set_offsets([[vpt[0], vpt[1]]])
                    self.edit_status_var.set(
                        f"'{sp.name or sp_id}' vertex {vi} selected — click another vertex.")
                else:
                    self.edit_status_var.set(
                        f"Cell '{sp.name or sp_id}' selected — click a vertex to split.")
                self.refresh_view()
            elif self._split_vertex_idx is None:
                # Phase 2: pick first vertex
                hit = self._find_vertex_near_split_space(event.xdata, event.ydata)
                if hit is None:
                    self.edit_status_var.set("Click closer to a vertex of the selected cell.")
                    return
                vi, vpt = hit
                self._split_vertex_idx = vi
                self._split_pt1 = vpt
                self._split_pt1_dot.set_offsets([[vpt[0], vpt[1]]])
                sp = self.model.spaces[self._split_space_id]
                self.edit_status_var.set(
                    f"Vertex {vi} selected — click another vertex to split.")
                self.canvas.draw_idle()
            else:
                # Phase 3: pick second vertex and split
                hit = self._find_vertex_near_split_space(event.xdata, event.ydata)
                if hit is None:
                    self.edit_status_var.set("Click closer to a vertex of the selected cell.")
                    return
                vj, _ = hit
                if vj == self._split_vertex_idx:
                    self.edit_status_var.set("Same vertex — click a different vertex.")
                    return
                self._split_preview_line.set_data([], [])
                self._split_pt1_dot.set_offsets(np.empty((0, 2)))
                sp_id = self._split_space_id
                vi = self._split_vertex_idx
                self._split_pt1 = None
                self._split_space_id = None
                self._split_vertex_idx = None
                self._do_cell_split(sp_id, vi, vj)
            return

        # --- Measure Distance mode ---
        if self._measure_mode:
            if event.xdata is None or event.ydata is None:
                return
            if event.button == 3:
                # Right-click: cancel first point or stay idle
                self._measure_pt1 = None
                self.status_var.set(
                    "Measure: click first point (right-click to cancel).")
                self.refresh_view()
                return
            if event.button != 1:
                return
            if self._measure_pt1 is None:
                self._measure_pt1 = (event.xdata, event.ydata)
                self.status_var.set(
                    f"Measure: pt1 = ({event.xdata:.2f}, {event.ydata:.2f}) "
                    f"— click second point.")
                self.refresh_view()
            else:
                x1, y1 = self._measure_pt1
                x2, y2 = event.xdata, event.ydata
                dx = x2 - x1
                dy = y2 - y1
                dist = math.hypot(dx, dy)
                self._measure_pt1 = None
                # Coordinates are in cm; show cm and the m equivalent.
                msg = (
                    f"Distance: {dist:,.2f} cm  ({dist/100.0:,.4f} m)\n"
                    f"  Δx = {dx:,.2f} cm    Δy = {dy:,.2f} cm\n"
                    f"  pt1 = ({x1:,.2f}, {y1:,.2f})\n"
                    f"  pt2 = ({x2:,.2f}, {y2:,.2f})"
                )
                self.status_var.set(
                    f"Distance = {dist:,.2f} cm  ({dist/100.0:,.4f} m)  "
                    f"Δx={dx:,.1f} Δy={dy:,.1f}")
                self.refresh_view()
                messagebox.showinfo("Measure Distance", msg)
            return

        # --- Door create mode ---
        if self._door_mode and event.button == 1:
            if event.inaxes != self.ax or event.xdata is None:
                return
            if self._door_pt1 is None:
                self._door_pt1 = (event.xdata, event.ydata)
                self.status_var.set(f"Door pt1: ({event.xdata:.1f}, {event.ydata:.1f}) — click second endpoint.")
                self.refresh_view()
            else:
                pt2 = (event.xdata, event.ydata)
                self._door_preview_line.set_data([], [])
                self._finish_door_creation(self._door_pt1, pt2)
                self._door_pt1 = None
            return

        # --- Rectangle create mode (two opposite corners) ---
        if self._rect_create_mode:
            if event.xdata is None or event.ydata is None:
                return
            if event.button == 3:
                self._rect_create_pt1 = None
                if self._space_create_preview_line is not None:
                    self._space_create_preview_line.set_data([], [])
                self._graph_edit_status_var.set(
                    "Rectangle: click first corner. (Right-click cancels.)")
                self.refresh_view()
                return
            if event.button != 1:
                return
            if self._rect_create_pt1 is None:
                self._rect_create_pt1 = (event.xdata, event.ydata)
                self._graph_edit_status_var.set(
                    f"Rectangle: pt1=({event.xdata:.1f}, "
                    f"{event.ydata:.1f}) — click opposite corner.")
                self.refresh_view()
            else:
                x1, y1 = self._rect_create_pt1
                x2, y2 = event.xdata, event.ydata
                if abs(x2 - x1) < 1e-6 or abs(y2 - y1) < 1e-6:
                    self._graph_edit_status_var.set(
                        "Rectangle: corners must differ in both x and y.")
                    return
                xmin, xmax = min(x1, x2), max(x1, x2)
                ymin, ymax = min(y1, y2), max(y1, y2)
                # Build a 4-vertex CCW polygon (LL, LR, UR, UL) and reuse
                # the existing finishing path so all bookkeeping is shared
                # with the polygon mode (name dialog, level, undo, etc.).
                self._space_create_verts = [
                    (xmin, ymin), (xmax, ymin),
                    (xmax, ymax), (xmin, ymax),
                ]
                self._rect_create_pt1 = None
                if self._space_create_preview_line is not None:
                    self._space_create_preview_line.set_data([], [])
                self._finish_space_creation()
            return

        # --- Space create mode ---
        if self._space_create_mode:
            if event.xdata is None or event.ydata is None:
                return
            if event.button == 3:
                # Right-click: cancel
                self._space_create_verts.clear()
                if self._space_create_preview_line is not None:
                    self._space_create_preview_line.set_data([], [])
                self._graph_edit_status_var.set(
                    "Left-click: add vertex | Double-click: finish | Right-click: cancel")
                self.refresh_view()
                return
            if event.button == 1:
                # Double-click: finish without adding a duplicate vertex
                if getattr(event, 'dblclick', False) and len(self._space_create_verts) >= 3:
                    self._finish_space_creation()
                    return
                self._space_create_verts.append((event.xdata, event.ydata))
                n = len(self._space_create_verts)
                hint = " — double-click or press Enter to finish" if n >= 3 else ""
                self._graph_edit_status_var.set(f"{n} vertices{hint}")
                self.refresh_view()
            return

        # --- Vertical Edge mode (two nodes on different floors) ---
        if self._vertical_edge_mode:
            if event.xdata is None or event.ydata is None:
                return
            if event.button == 3:
                self._pan_active = True
                self._pan_start_pixel = (event.x, event.y)
                self._pan_start_xlim = self.ax.get_xlim()
                self._pan_start_ylim = self.ax.get_ylim()
                return
            if event.button != 1:
                return
            node_id = self._find_node_near_graph(event.xdata, event.ydata)
            if not node_id:
                self._graph_edit_status_var.set(
                    "No node near click — try again. (change Floor to switch levels)")
                return
            node = self.model.nodes.get(node_id)
            node_floor = (node.level or "").strip() if node else ""
            current_floor = (self.floor_var.get() or "").strip()

            if self._vertical_edge_first_node is None:
                # First selection
                self._vertical_edge_first_node  = node_id
                self._vertical_edge_first_floor = node_floor or current_floor
                self._graph_edit_status_var.set(
                    f"First node: {node_id} (floor '{self._vertical_edge_first_floor}') — "
                    f"change Floor and click the second node.")
                self.refresh_view()
                return

            # Second selection
            first_id    = self._vertical_edge_first_node
            first_floor = self._vertical_edge_first_floor or ""
            if node_id == first_id:
                self._graph_edit_status_var.set(
                    "Same node as first — pick a different node.")
                return
            second_floor = node_floor or current_floor
            if first_floor and second_floor and first_floor == second_floor:
                self._graph_edit_status_var.set(
                    f"Both nodes are on floor '{first_floor}' — change Floor first.")
                return

            # Vertical Edge legitimately needs cross-floor → opt in.
            eid = self._create_edge(first_id, node_id,
                                    allow_cross_floor=True)
            if eid:
                self._push_undo({"kind": "create_edge", "edge_id": eid})
                self._graph_edit_status_var.set(
                    f"Vertical edge '{eid}' created: {first_id} ({first_floor}) ↔ "
                    f"{node_id} ({second_floor}).")
                # Reset state BEFORE showing the modal dialog so the overlay
                # redraws cleanly behind it.
                self._vertical_edge_first_node  = None
                self._vertical_edge_first_floor = None
                self.refresh_view()
                messagebox.showinfo(
                    "Vertical Edge Created",
                    f"Vertical edge '{eid}' has been added.\n\n"
                    f"  • {first_id}  (floor: {first_floor})\n"
                    f"  • {node_id}  (floor: {second_floor})")
            else:
                self._graph_edit_status_var.set(
                    "Edge already exists between those nodes.")
                self._vertical_edge_first_node  = None
                self._vertical_edge_first_floor = None
                self.refresh_view()
                messagebox.showwarning(
                    "Vertical Edge",
                    f"An edge already exists between\n"
                    f"  {first_id}  (floor: {first_floor})\n"
                    f"  {node_id}  (floor: {second_floor}).")
            return

        # --- Edge Edit mode (independent of Node Edit) ---
        if self._graph_connect_mode:
            if event.xdata is None or event.ydata is None:
                return
            if event.button == 3:
                self._pan_active = True
                self._pan_start_pixel = (event.x, event.y)
                self._pan_start_xlim = self.ax.get_xlim()
                self._pan_start_ylim = self.ax.get_ylim()
                return
            if event.button != 1:
                return
            node_id = self._find_node_near_graph(event.xdata, event.ydata)
            if node_id:
                self._graph_selected_edge = None
                if self._graph_connect_first is None:
                    self._graph_connect_first = node_id
                    self._graph_edit_status_var.set(
                        f"First node: {node_id} — click second node to create edge.")
                    self.refresh_view()
                elif node_id != self._graph_connect_first:
                    eid = self._create_edge(self._graph_connect_first, node_id)
                    if eid:
                        self._push_undo({"kind": "create_edge", "edge_id": eid})
                        self._graph_edit_status_var.set(
                            f"Edge created: {self._graph_connect_first} ↔ {node_id}")
                    else:
                        self._graph_edit_status_var.set("Edge already exists between these nodes.")
                    self._graph_connect_first = None
                    self.refresh_view()
            else:
                edge_id = self._find_edge_near_graph(event.xdata, event.ydata)
                if edge_id:
                    self._graph_connect_first = None
                    self._graph_selected_edge = edge_id
                    self._graph_edit_status_var.set(
                        f"Edge {edge_id} selected — Delete key to delete.")
                    self.refresh_view()
                else:
                    self._graph_connect_first = None
                    self._graph_selected_edge = None
                    self._graph_edit_status_var.set(
                        "Click first node, then second node to create edge.")
                    self.refresh_view()
            return

        # --- Graph edit (Node Edit) mode ---
        if self._graph_edit_mode:
            if event.xdata is None or event.ydata is None:
                return
            if event.button == 3:
                # Right-click → pan
                self._pan_active = True
                self._pan_start_pixel = (event.x, event.y)
                self._pan_start_xlim = self.ax.get_xlim()
                self._pan_start_ylim = self.ax.get_ylim()
                return
            if event.button != 1:
                return
            node_id = self._find_node_near_graph(event.xdata, event.ydata)

            # Also check the selected node explicitly (in case floor filter missed it)
            if node_id is None and self._graph_selected_node:
                sel = self.model.nodes.get(self._graph_selected_node)
                if sel and sel.coord:
                    cp = self.ax.transData.transform((event.xdata, event.ydata))
                    np_ = self.ax.transData.transform((sel.coord[0], sel.coord[1]))
                    if math.sqrt((np_[0]-cp[0])**2 + (np_[1]-cp[1])**2) < 30.0:
                        node_id = self._graph_selected_node

            if node_id:
                if node_id == self._graph_selected_node:
                    # Re-click on already-selected node → start drag
                    self._graph_drag_node = node_id
                    self._graph_drag_node_ready = False
                    self._pending_node_pos = None
                    self._graph_edit_status_var.set(f"Dragging node {node_id}...")
                else:
                    # Click on different node → just select it
                    self._graph_selected_node = node_id
                    self._graph_selected_edge = None
                    self._pending_node_pos = None
                    self._graph_drag_node = None
                    # Mirror the selection into the Selection info panel
                    # so the user sees the node's properties (incl. the
                    # dual cell's Name) while in Node Edit mode.
                    self._view_selected_type = "node"
                    self._view_selected_id = node_id
                    self._show_view_selection()
                    self._graph_edit_status_var.set(
                        f"Node {node_id} selected — click again to drag, Delete to delete.")
                    self.refresh_view()
            elif self._graph_selected_node:
                # Selected node exists but click missed it → check edge, else just deselect
                edge_id = self._find_edge_near_graph(event.xdata, event.ydata)
                if edge_id:
                    self._graph_selected_edge = edge_id
                    self._graph_selected_node = None
                    self._graph_drag_node = None
                    self._pending_node_pos = None
                    self._graph_edit_status_var.set(
                        self._edge_status_msg(edge_id))
                    self.refresh_view()
                else:
                    # Deselect only — do NOT create a node
                    self._graph_selected_node = None
                    self._graph_selected_edge = None
                    self._graph_drag_node = None
                    self._pending_node_pos = None
                    self._graph_edit_status_var.set(
                        "Left-click: select/create/move node or edge | Delete key: delete selected")
                    self.refresh_view()
            else:
                # Nothing selected, nothing near → check edge, else defer node creation
                edge_id = self._find_edge_near_graph(event.xdata, event.ydata)
                if edge_id:
                    self._graph_selected_edge = edge_id
                    self._graph_drag_node = None
                    self._pending_node_pos = None
                    self._graph_edit_status_var.set(
                        self._edge_status_msg(edge_id))
                    self.refresh_view()
                else:
                    self._graph_drag_node = None
                    self._pending_node_pos = (event.xdata, event.ydata)
            return

        if event.button == 3:
            # Right-click → pan
            self._pan_active = True
            self._pan_start_pixel = (event.x, event.y)
            self._pan_start_xlim = self.ax.get_xlim()
            self._pan_start_ylim = self.ax.get_ylim()
            return

        if event.button != 1:
            return

        if self._edit_mode:
            if event.xdata is None or event.ydata is None:
                return

            # Step 1: if a vertex is already selected, start dragging it
            if self._edit_selected_vertex_idx is not None:
                hit_same = self._find_vertex_near(event.xdata, event.ydata)
                if hit_same is not None and hit_same == (self._edit_selected_poly_idx, self._edit_selected_vertex_idx):
                    # Clicked the selected vertex → start drag
                    self._edit_drag_active = True
                    sp = self.model.spaces.get(self._edit_selected_space_id)
                    if sp:
                        self._push_undo({
                            "kind": "move_vertex",
                            "space_id": self._edit_selected_space_id,
                            "poly_idx": self._edit_selected_poly_idx,
                            "old_poly": list(sp.polygons_3d[self._edit_selected_poly_idx]),
                        })
                    self._cache_drag_background()
                    vi = self._edit_selected_vertex_idx
                    floor = self.floor_var.get()
                    floor_space_ids = get_space_ids_on_floor(self.model, floor) if (floor and floor != "All") else set(self.model.spaces.keys())
                    self._snap_candidates = [
                        (p[0], p[1])
                        for sp_id, sp in self.model.spaces.items()
                        if sp_id in floor_space_ids and sp.polygons_3d
                        for idx, p in enumerate(sp.polygons_3d[0])
                        if not (sp_id == self._edit_selected_space_id and idx == vi)
                    ]
                    xlim = self.ax.get_xlim()
                    ylim = self.ax.get_ylim()
                    thr = min(abs(xlim[1] - xlim[0]), abs(ylim[1] - ylim[0])) * 0.010
                    self._snap_threshold2 = thr * thr
                    return

            # Step 2: try to select a vertex (any space)
            hit = self._find_vertex_near(event.xdata, event.ydata, search_all=True)
            if hit is not None:
                self._edit_selected_poly_idx, self._edit_selected_vertex_idx = hit
                sp = self.model.spaces.get(self._edit_selected_space_id)
                self.edit_status_var.set(
                    f"Vertex {hit[1]} selected — click it again to drag, Delete key to delete"
                )
                self.refresh_view()
                return

            # Step 3: no vertex hit — deselect vertex, handle space selection/drag/edge insert
            self._edit_selected_vertex_idx = None

            if self._edit_selected_space_id is not None:
                edge_hit = self._find_edge_near(event.xdata, event.ydata)
                if edge_hit is not None:
                    self._pending_edge_insert = edge_hit
                else:
                    sp_id = self._find_space_at(event.xdata, event.ydata)
                    if sp_id == self._edit_selected_space_id:
                        sp = self.model.spaces[sp_id]
                        self._push_undo({
                            "kind": "move_space",
                            "space_id": sp_id,
                            "old_polygons_3d": copy.deepcopy(sp.polygons_3d),
                        })
                        self._space_drag_active = True
                        self._space_drag_start = (event.xdata, event.ydata)
                        self._space_drag_orig_polygons = copy.deepcopy(sp.polygons_3d)
                        self.edit_status_var.set(f"Moving space {sp_id}...")
                        self._cache_space_move_background()
                    else:
                        self._edit_selected_space_id = sp_id
                        self._edit_selected_poly_idx = 0
                        self._edit_drag_active = False
                        if sp_id:
                            sp = self.model.spaces[sp_id]
                            self.edit_status_var.set(
                                f"Selected: {sp.name or sp_id} — click a vertex to select it"
                            )
                        else:
                            self.edit_status_var.set("Click to select space | Drag to move | Delete key to delete selected")
                        self._update_edit_space_info(sp_id)
                        self.refresh_view()
            else:
                sp_id = self._find_space_at(event.xdata, event.ydata)
                self._edit_selected_space_id = sp_id
                self._edit_selected_poly_idx = 0
                self._edit_drag_active = False
                if sp_id:
                    sp = self.model.spaces[sp_id]
                    self.edit_status_var.set(
                        f"Selected: {sp.name or sp_id} — click a vertex to select it"
                    )
                else:
                    self.edit_status_var.set("Click to select space | Drag to move | Delete key to delete selected")
                self._update_edit_space_info(sp_id)
                self.refresh_view()
            return

        # --- View-only selection (no edit mode active, left click) ---
        if event.button == 1 and event.xdata is not None and event.ydata is not None:
            self._do_view_select(event.xdata, event.ydata)
            return

        self._pan_active = True
        self._pan_start_pixel = (event.x, event.y)
        self._pan_start_xlim = self.ax.get_xlim()
        self._pan_start_ylim = self.ax.get_ylim()

    def on_mouse_release(self, event) -> None:
        if self._bg_resize_mode and event.button == 1:
            self._bg_resize_handle = None
            self._bg_drag_cache = None
            self._ov_bg_img.set_visible(False)
            self._save_bg_config()
            self.refresh_view()
            return

        if self._bg_move_mode and event.button == 1:
            self._bg_move_start_data = None
            self._bg_move_start_extent = None
            self._bg_drag_cache = None
            self._ov_bg_img.set_visible(False)
            self._save_bg_config()
            self.refresh_view()
            return

        if self._graph_edit_mode and event.button == 1:
            if self._graph_drag_node:
                was_dragged = self._graph_drag_node_ready
                node_id = self._graph_drag_node
                self._graph_drag_node = None
                self._graph_drag_node_ready = False
                self._graph_drag_node_orig_coord = None
                self._node_drag_bg_cache = None
                self._ov_drag_node.set_offsets(np.empty((0, 2)))
                self._ov_drag_edges.set_segments([])
                self._pending_node_pos = None
                if was_dragged:
                    # Keep node selected after drag so user can delete or drag again
                    self._graph_selected_node = node_id
                    self._view_selected_type = "node"
                    self._view_selected_id = node_id
                    self._show_view_selection()
                    self._graph_edit_status_var.set(
                        f"Node {node_id} moved — click again to drag, Delete to delete.")
                self.refresh_view()
                return
            if self._pending_node_pos is not None:
                px, py = self._pending_node_pos
                self._pending_node_pos = None
                new_id = self._create_node(px, py)
                self._push_undo({"kind": "create_node", "node_id": new_id})
                self._graph_selected_node = new_id
                self._graph_edit_status_var.set(f"Created node {new_id}.")
                self.refresh_view()
                return

        if self._edit_mode and self._space_drag_active and event.button == 1:
            # Commit the final position to the model
            if (self._edit_selected_space_id and self._space_drag_start
                    and self._space_drag_orig_polygons and event.xdata is not None):
                sp = self.model.spaces.get(self._edit_selected_space_id)
                if sp:
                    dx = event.xdata - self._space_drag_start[0]
                    dy = (event.ydata or self._space_drag_start[1]) - self._space_drag_start[1]
                    new_polygons = [
                        [(x + dx, y + dy, z) for x, y, z in poly]
                        for poly in self._space_drag_orig_polygons
                    ]
                    sp.polygons_3d = new_polygons
                    sp.centers = [c for c in [polygon_centroid(p) for p in sp.polygons_3d] if c is not None]
            self._space_drag_active = False
            self._space_drag_start = None
            self._space_drag_orig_polygons = None
            self._space_drag_bg_cache = None
            self._ov_space_move.set_data([], [])
            sp = self.model.spaces.get(self._edit_selected_space_id)
            if sp:
                self.edit_status_var.set(
                    f"Selected: {self._edit_selected_space_id} ({sp.name or 'no name'}) — drag to move, click vertex to edit"
                )
            self.refresh_view()
            return

        if self._edit_mode and event.button == 1:
            if self._edit_drag_active:
                self._edit_drag_active = False
                self._snap_point = None
                self._drag_bg_cache = None
                self._pending_edge_insert = None
                self.refresh_view()
                return
            if self._pending_edge_insert is not None:
                poly_idx, insert_idx, nx, ny = self._pending_edge_insert
                self._pending_edge_insert = None
                self._insert_vertex_on_edge(poly_idx, insert_idx, nx, ny)
                return

        self._pan_active = False
        self._pan_start_pixel = None
        self._pan_start_xlim = None
        self._pan_start_ylim = None

    def on_mouse_move(self, event) -> None:
        if self._bg_resize_mode and self._bg_resize_handle is not None:
            if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
                return
            h = self._bg_resize_handle
            e = self._bg_extent
            x, y = event.xdata, event.ydata
            if "L" in h:
                e[0] = min(x, e[1] - 1)
            if "R" in h:
                e[1] = max(x, e[0] + 1)
            if "B" in h:
                e[2] = min(y, e[3] - 1)
            if "T" in h:
                e[3] = max(y, e[2] + 1)
            self._fast_bg_blit()
            return

        if self._bg_move_mode and self._bg_move_start_data is not None:
            if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
                return
            dx = event.xdata - self._bg_move_start_data[0]
            dy = event.ydata - self._bg_move_start_data[1]
            e = self._bg_move_start_extent
            self._bg_extent = [e[0] + dx, e[1] + dx, e[2] + dy, e[3] + dy]
            self._fast_bg_blit()
            return

        if self._split_mode and self._split_pt1 is not None and self._split_vertex_idx is not None:
            if event.inaxes == self.ax and event.xdata is not None and event.ydata is not None:
                x1, y1 = self._split_pt1
                self._split_preview_line.set_data([x1, event.xdata], [y1, event.ydata])
            else:
                self._split_preview_line.set_data([], [])
            self.canvas.draw_idle()
            return

        if self._door_mode and self._door_pt1 is not None:
            if event.inaxes == self.ax and event.xdata is not None and event.ydata is not None:
                x1, y1 = self._door_pt1
                self._door_preview_line.set_data([x1, event.xdata], [y1, event.ydata])
            else:
                self._door_preview_line.set_data([], [])
            self.canvas.draw_idle()
            return

        if self._space_create_mode and self._space_create_verts:
            if event.inaxes == self.ax and event.xdata is not None and event.ydata is not None:
                self._update_space_create_preview((event.xdata, event.ydata))
            return

        if (self._rect_create_mode
                and self._rect_create_pt1 is not None
                and self._space_create_preview_line is not None):
            if (event.inaxes == self.ax
                    and event.xdata is not None and event.ydata is not None):
                x1, y1 = self._rect_create_pt1
                x2, y2 = event.xdata, event.ydata
                xs = [x1, x2, x2, x1, x1]
                ys = [y1, y1, y2, y2, y1]
                self._space_create_preview_line.set_data(xs, ys)
            else:
                self._space_create_preview_line.set_data([], [])
            self.canvas.draw_idle()
            return

        if self._graph_edit_mode and self._graph_drag_node is not None:
            if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
                return
            self._pending_node_pos = None
            # First actual movement: push undo + set up blit cache
            if not self._graph_drag_node_ready:
                self._graph_drag_node_ready = True
                node = self.model.nodes.get(self._graph_drag_node)
                if node is None:
                    return
                self._graph_drag_node_orig_coord = node.coord
                old_edge_coords = {
                    eid: list(e.coords)
                    for eid, e in self.model.edges.items()
                    if self._graph_drag_node in e.connects
                }
                self._push_undo({
                    "kind": "move_node",
                    "node_id": self._graph_drag_node,
                    "old_coord": node.coord,
                    "old_edge_coords": old_edge_coords,
                })
                self._graph_edit_status_var.set(f"Dragging node {self._graph_drag_node}...")
                # Render background without dragged node, capture blit buffer
                self._setup_node_drag_blit(self._graph_drag_node)
            # Update node position and connected edge coords
            node = self.model.nodes.get(self._graph_drag_node)
            if node is not None:
                old_z = node.coord[2] if node.coord else 0.0
                new_x, new_y = event.xdata, event.ydata
                node.coord = (new_x, new_y, old_z)
                orig = self._graph_drag_node_orig_coord
                if orig is not None:
                    ox, oy = orig[0], orig[1]
                    for edge in self.model.edges.values():
                        if self._graph_drag_node not in edge.connects:
                            continue
                        coords = list(edge.coords)
                        if len(coords) < 2:
                            continue
                        d_first = (coords[0][0] - ox) ** 2 + (coords[0][1] - oy) ** 2
                        d_last  = (coords[-1][0] - ox) ** 2 + (coords[-1][1] - oy) ** 2
                        if d_first <= d_last:
                            coords[0] = (new_x, new_y, coords[0][2])
                        else:
                            coords[-1] = (new_x, new_y, coords[-1][2])
                        edge.coords = coords
                self._graph_drag_node_orig_coord = (new_x, new_y, old_z)
                self._fast_node_drag_redraw(self._graph_drag_node, new_x, new_y)
            return

        if self._edit_mode and self._space_drag_active:
            if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
                return
            if self._edit_selected_space_id is None or self._space_drag_start is None:
                return
            sp = self.model.spaces.get(self._edit_selected_space_id)
            if sp is None or self._space_drag_orig_polygons is None:
                return
            dx = event.xdata - self._space_drag_start[0]
            dy = event.ydata - self._space_drag_start[1]
            # Update only floor polygon for fast blit preview
            floor_orig = self._space_drag_orig_polygons[0]
            xs = [p[0] + dx for p in floor_orig]
            ys = [p[1] + dy for p in floor_orig]
            if xs[0] != xs[-1] or ys[0] != ys[-1]:
                xs.append(xs[0])
                ys.append(ys[0])
            self._ov_space_move.set_data(xs, ys)
            if self._space_drag_bg_cache is not None:
                self.canvas.restore_region(self._space_drag_bg_cache)
                self.ax.draw_artist(self._ov_space_move)
                self.canvas.blit(self.figure.bbox)
            return

        if self._edit_mode and self._edit_drag_active:
            if event.inaxes != self.ax or event.xdata is None or event.ydata is None:
                return
            self._pending_edge_insert = None  # moved → cancel pending insert
            if self._edit_selected_space_id is None or self._edit_selected_vertex_idx is None:
                return
            sp = self.model.spaces.get(self._edit_selected_space_id)
            if sp is None or self._edit_selected_poly_idx >= len(sp.polygons_3d):
                return
            poly = sp.polygons_3d[self._edit_selected_poly_idx]
            vi = self._edit_selected_vertex_idx
            if vi >= len(poly):
                return
            old_x, old_y, old_z = poly[vi]
            new_x, new_y = event.xdata, event.ydata
            if self._snap_enabled.get():
                snap = self._find_snap_target(new_x, new_y)
                if snap:
                    new_x, new_y = snap
                    self._snap_point = snap
                else:
                    self._snap_point = None
            else:
                self._snap_point = None
            # A ring parsed from GML repeats its first vertex at the end.
            # Dragging one twin has to move the other, or the ring silently
            # opens: the loop below propagates to *sibling* polygons but
            # skips this one, so nothing else would catch it.
            was_closed = len(poly) > 2 and poly[0][:2] == poly[-1][:2]
            poly[vi] = (new_x, new_y, old_z)
            if was_closed:
                if vi == 0:
                    poly[-1] = (new_x, new_y, poly[-1][2])
                elif vi == len(poly) - 1:
                    poly[0] = (new_x, new_y, poly[0][2])
            # Propagate the same X,Y shift to all sibling polygons (ceiling, walls)
            for other_poly in sp.polygons_3d:
                if other_poly is poly:
                    continue
                for j, (ox, oy, oz) in enumerate(other_poly):
                    if abs(ox - old_x) < 1e-3 and abs(oy - old_y) < 1e-3:
                        other_poly[j] = (new_x, new_y, oz)
            sp.centers = [c for c in [polygon_centroid(p) for p in sp.polygons_3d] if c is not None]
            self._fast_drag_redraw()
            return

        if not self._pan_active:
            return
        if event.inaxes != self.ax:
            return
        if event.x is None or event.y is None:
            return
        if self._pan_start_pixel is None:
            return
        if self._pan_start_xlim is None or self._pan_start_ylim is None:
            return

        start_x_pix, start_y_pix = self._pan_start_pixel
        dx_pix = event.x - start_x_pix
        dy_pix = event.y - start_y_pix

        cur_xlim = self._pan_start_xlim
        cur_ylim = self._pan_start_ylim

        bbox = self.ax.get_window_extent()
        width_pix = bbox.width
        height_pix = bbox.height

        if width_pix <= 0 or height_pix <= 0:
            return

        x_per_pix = (cur_xlim[1] - cur_xlim[0]) / width_pix
        y_per_pix = (cur_ylim[1] - cur_ylim[0]) / height_pix

        dx_data = dx_pix * x_per_pix
        dy_data = dy_pix * y_per_pix

        self.ax.set_xlim(cur_xlim[0] - dx_data, cur_xlim[1] - dx_data)
        self.ax.set_ylim(cur_ylim[0] - dy_data, cur_ylim[1] - dy_data)

        self.canvas.draw_idle()


    def load_function_codes_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Load Function Code List (CSV)",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            codes = load_function_codes(path)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load CSV:\n{e}")
            return
        if not codes:
            messagebox.showwarning("Empty", "No codes found in CSV file.")
            return
        self._function_codes = codes
        self._function_csv_path = path
        self._function_color_map = _build_function_color_map(codes)
        # Count spaces that already have a matching function code
        n_colored = 0
        if self.model:
            n_colored = sum(1 for sp in self.model.spaces.values()
                            if sp.function and sp.function in self._function_color_map)
        self.status_var.set(
            f"Function codes loaded: {os.path.basename(path)} ({len(codes)} codes)"
            + (f" — {n_colored} spaces colored" if self.model else "")
        )
        self._fn_menu.entryconfig("Apply Function...", state="normal")
        self._fn_menu.entryconfig("Multiple Cells (bulk apply)",
                                   state="normal")
        self.refresh_view()

    # ------------------------------------------------------------------
    # Function Codes → "Multiple Cells (bulk apply)"
    # ------------------------------------------------------------------
    def toggle_multi_apply_mode(self) -> None:
        """Enter / exit multi-cell function-code bulk-apply mode.

        Workflow:
          1. Toggle the menu item ON. A small modal asks for the
             function code to apply (one of the loaded codes).
          2. The user clicks cells on the canvas to add them to the
             selection set; clicking a cell again removes it.
             Selected cells are highlighted in cyan on top of their
             usual fill.
          3. The user opens **Function Codes → Multiple Cells…** menu
             item again (or right-click) to pop the Apply / Cancel
             dialog. Apply assigns the chosen code to every selected
             cell in one shot; Cancel exits without changes.
        """
        want_on = self._multi_apply_mode_var.get()
        if not want_on:
            # User toggled OFF → present Apply / Cancel dialog (this is
            # how the workflow ends — the menu toggle doubles as the
            # finish button).
            if self._multi_apply_mode:
                self._multi_apply_finish()
            else:
                self.status_var.set("")
                self.refresh_view()
            return

        if not self._function_codes:
            messagebox.showwarning("No codes",
                                   "Load a function code CSV first.")
            self._multi_apply_mode_var.set(False)
            return

        # Ask for the code first, then enter selection mode.
        code = self._prompt_function_code(initial="")
        if not code:
            self._multi_apply_mode_var.set(False)
            return
        # Mutually exclusive with other edit modes
        self._deactivate_all_edit_modes()
        self._multi_apply_mode = True
        self._multi_apply_mode_var.set(True)
        self._multi_apply_code = code
        self._multi_apply_selected.clear()
        self.status_var.set(
            f"Multi-apply '{code}': click cells to toggle. "
            "Re-toggle the menu item to Apply or Cancel.")
        self.refresh_view()

    def _prompt_function_code(self,
                              initial: str = "") -> Optional[str]:
        """Modal pull-down dialog returning a chosen function code,
        or None on cancel."""
        if not self._function_codes:
            return None
        dlg = tk.Toplevel(self.root)
        dlg.title("Pick Function Code")
        dlg.resizable(False, False)
        dlg.grab_set()
        ttk.Label(dlg, text="Function code:").grid(
            row=0, column=0, padx=12, pady=(12, 4), sticky="e")
        labels = [f"{c} — {d}" if c != d else c
                  for c, d in self._function_codes]
        var = tk.StringVar()
        combo = ttk.Combobox(dlg, textvariable=var, values=labels,
                             state="readonly", width=36)
        combo.grid(row=0, column=1, padx=(4, 12), pady=(12, 4))
        if initial:
            for i, (code, _) in enumerate(self._function_codes):
                if code == initial:
                    combo.current(i); break
        elif labels:
            combo.current(0)
        out: List[Optional[str]] = [None]
        def _ok():
            idx = combo.current()
            if 0 <= idx < len(self._function_codes):
                out[0] = self._function_codes[idx][0]
            dlg.destroy()
        btn = ttk.Frame(dlg)
        btn.grid(row=1, column=0, columnspan=2, pady=(4, 12))
        ttk.Button(btn, text="OK", command=_ok).pack(side="left", padx=6)
        ttk.Button(btn, text="Cancel", command=dlg.destroy
                   ).pack(side="left", padx=6)
        dlg.bind("<Return>", lambda e: _ok())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.wait_window()
        return out[0]

    def _on_multi_apply_click(self, xdata: float, ydata: float) -> None:
        """Toggle a cell in the multi-apply selection set."""
        sp_id = self._find_space_at(xdata, ydata)
        if sp_id is None:
            return
        if sp_id in self._multi_apply_selected:
            self._multi_apply_selected.discard(sp_id)
        else:
            self._multi_apply_selected.add(sp_id)
        self.status_var.set(
            f"Multi-apply '{self._multi_apply_code}': "
            f"{len(self._multi_apply_selected)} cell(s) selected. "
            "Re-toggle the menu item to Apply or Cancel.")
        self.refresh_view()

    def _draw_multi_apply_overlay(self) -> None:
        """Highlight every cell currently in the multi-apply set."""
        if not self._multi_apply_mode or self.model is None:
            return
        if not self._multi_apply_selected:
            return
        from matplotlib.collections import PolyCollection as _PC
        verts: List = []
        for sid in self._multi_apply_selected:
            sp = self.model.spaces.get(sid)
            if not sp or not sp.polygons_3d:
                continue
            verts.append([(p[0], p[1]) for p in sp.polygons_3d[0]])
        if verts:
            self.ax.add_collection(_PC(
                verts, facecolors=[(0, 1, 1, 0.30)],
                edgecolors="cyan", linewidths=1.5, zorder=15))

    def _multi_apply_finish(self) -> None:
        """Pop a confirm dialog and commit the bulk function-code change."""
        n = len(self._multi_apply_selected)
        if n == 0:
            if not messagebox.askyesno(
                "Multiple Cells",
                "No cells selected. Exit multi-apply mode?"):
                # Re-tick the var so the menu stays on
                self._multi_apply_mode_var.set(True)
                return
            self._multi_apply_mode = False
            self._multi_apply_code = ""
            self.status_var.set("")
            self.refresh_view()
            return
        if not messagebox.askyesno(
            "Multiple Cells",
            f"Apply function code '{self._multi_apply_code}' "
            f"to {n} selected cell(s)?"):
            # Restore mode (user wants to keep editing the selection)
            self._multi_apply_mode_var.set(True)
            return
        code = self._multi_apply_code
        for sid in list(self._multi_apply_selected):
            sp = self.model.spaces.get(sid)
            if sp is None:
                continue
            sp.function = code
            if sp.source_element is not None:
                self._update_function_in_xml(sp.source_element, code)
        # Exit mode
        self._multi_apply_mode = False
        self._multi_apply_selected.clear()
        self._multi_apply_code = ""
        self.status_var.set(
            f"Function code '{code}' applied to {n} cell(s). "
            "Save the file to persist.")
        self.refresh_view()

    # ------------------------------------------------------------------
    # Cell → "Class Bulk Apply"
    # ------------------------------------------------------------------
    _CLASS_OPTIONS = ("CellSpace", "TransferSpace",
                      "ConnectionSpace", "AnchorSpace")

    def toggle_multi_class_mode(self) -> None:
        """Enter / exit the bulk Class-rewrite mode for cells.

        Workflow mirrors `toggle_multi_apply_mode`:
          1. Toggle ON → small dialog asks for the target Class.
          2. Click cells on the canvas to toggle them in/out of the
             selection set (highlighted in cyan).
          3. Toggle OFF → confirm → every selected cell's CellSpace
             element is retagged in place to the target Class.
        """
        want_on = self._multi_class_mode_var.get()
        if not want_on:
            if self._multi_class_mode:
                self._multi_class_finish()
            else:
                self.status_var.set("")
                self.refresh_view()
            return

        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            self._multi_class_mode_var.set(False)
            return

        cls = self._prompt_class_value()
        if not cls:
            self._multi_class_mode_var.set(False)
            return
        self._deactivate_all_edit_modes()
        self._multi_class_mode = True
        self._multi_class_mode_var.set(True)
        self._multi_class_value = cls
        self._multi_class_selected.clear()
        self.status_var.set(
            f"Class bulk-apply '{cls}': click cells to toggle. "
            "Re-toggle the menu item to Apply or Cancel.")
        self.refresh_view()

    def _prompt_class_value(self) -> Optional[str]:
        """Modal pull-down dialog returning a CellSpace subclass name,
        or None on cancel."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Pick Class")
        dlg.resizable(False, False)
        dlg.grab_set()
        ttk.Label(dlg, text="Class:").grid(
            row=0, column=0, padx=12, pady=(12, 4), sticky="e")
        var = tk.StringVar(value=self._CLASS_OPTIONS[0])
        combo = ttk.Combobox(dlg, textvariable=var,
                             values=list(self._CLASS_OPTIONS),
                             state="readonly", width=24)
        combo.grid(row=0, column=1, padx=(4, 12), pady=(12, 4))
        combo.current(0)
        out: List[Optional[str]] = [None]
        def _ok():
            v = var.get().strip()
            if v in self._CLASS_OPTIONS:
                out[0] = v
            dlg.destroy()
        btn = ttk.Frame(dlg)
        btn.grid(row=1, column=0, columnspan=2, pady=(4, 12))
        ttk.Button(btn, text="OK", command=_ok).pack(side="left", padx=6)
        ttk.Button(btn, text="Cancel", command=dlg.destroy
                   ).pack(side="left", padx=6)
        dlg.bind("<Return>", lambda e: _ok())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.wait_window()
        return out[0]

    def _on_multi_class_click(self, xdata: float, ydata: float) -> None:
        sp_id = self._find_space_at(xdata, ydata)
        if sp_id is None:
            return
        if sp_id in self._multi_class_selected:
            self._multi_class_selected.discard(sp_id)
        else:
            self._multi_class_selected.add(sp_id)
        self.status_var.set(
            f"Class bulk-apply '{self._multi_class_value}': "
            f"{len(self._multi_class_selected)} cell(s) selected. "
            "Re-toggle the menu item to Apply or Cancel.")
        self.refresh_view()

    def _draw_multi_class_overlay(self) -> None:
        if not self._multi_class_mode or self.model is None:
            return
        if not self._multi_class_selected:
            return
        from matplotlib.collections import PolyCollection as _PC
        verts: List = []
        for sid in self._multi_class_selected:
            sp = self.model.spaces.get(sid)
            if not sp or not sp.polygons_3d:
                continue
            verts.append([(p[0], p[1]) for p in sp.polygons_3d[0]])
        if verts:
            self.ax.add_collection(_PC(
                verts, facecolors=[(0, 1, 1, 0.30)],
                edgecolors="cyan", linewidths=1.5, zorder=15))

    def _multi_class_finish(self) -> None:
        n = len(self._multi_class_selected)
        cls = self._multi_class_value
        if n == 0:
            if not messagebox.askyesno(
                "Class Bulk Apply",
                "No cells selected. Exit bulk-apply mode?"):
                self._multi_class_mode_var.set(True)
                return
            self._multi_class_mode = False
            self._multi_class_value = ""
            self.status_var.set("")
            self.refresh_view()
            return
        if not messagebox.askyesno(
            "Class Bulk Apply",
            f"Set Class to '{cls}' for {n} selected cell(s)?"):
            self._multi_class_mode_var.set(True)
            return
        for sid in list(self._multi_class_selected):
            sp = self.model.spaces.get(sid)
            if sp is None:
                continue
            sp.cls = cls
            # Reuse the in-place property patcher; it rewrites the XML
            # element's tag (CellSpace → TransferSpace / …).
            self._patch_cellspace_xml_props(sp)
        self._multi_class_mode = False
        self._multi_class_selected.clear()
        self._multi_class_value = ""
        self.status_var.set(
            f"Class '{cls}' applied to {n} cell(s). "
            "Save the file to persist.")
        self.refresh_view()

    def apply_function_dialog(self) -> None:
        """Open a dialog to select and apply a function code to the selected space."""
        if not self._function_codes:
            messagebox.showwarning("No codes", "Load a function code CSV first.")
            return
        if self.model is None or self._edit_selected_space_id is None:
            messagebox.showinfo("No selection", "Select a CellSpace in Cell Edit mode first.")
            return
        sp = self.model.spaces.get(self._edit_selected_space_id)
        if sp is None:
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Apply Function Code")
        dlg.resizable(False, False)
        dlg.grab_set()

        ttk.Label(dlg, text=f"Space: {self._edit_selected_space_id}").grid(
            row=0, column=0, columnspan=2, padx=12, pady=(12, 4), sticky="w")
        ttk.Label(dlg, text="Function:").grid(row=1, column=0, padx=12, pady=4, sticky="e")
        func_var = tk.StringVar(value=sp.function or "")
        labels = [f"{c} — {d}" if c != d else c for c, d in self._function_codes]
        combo = ttk.Combobox(dlg, textvariable=func_var, values=labels, state="readonly", width=36)
        # Pre-select current value
        for i, (code, _) in enumerate(self._function_codes):
            if code == sp.function:
                combo.current(i)
                break
        combo.grid(row=1, column=1, padx=(4, 12), pady=4)

        def apply() -> None:
            idx = combo.current()
            if 0 <= idx < len(self._function_codes):
                code, _ = self._function_codes[idx]
            else:
                code = func_var.get().split(" — ")[0].strip()
            if not code:
                return
            sp.function = code
            if sp.source_element is not None:
                self._update_function_in_xml(sp.source_element, code)
            self.edit_status_var.set(f"Function set to '{code}' for {self._edit_selected_space_id}.")
            dlg.destroy()
            self._update_edit_space_info(self._edit_selected_space_id)
            self.refresh_view()

        btn_frame = ttk.Frame(dlg)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=(4, 12))
        ttk.Button(btn_frame, text="Apply", command=apply).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Cancel", command=dlg.destroy).pack(side="left", padx=6)

    def _update_function_in_xml(self, elem: ET.Element, value: str) -> None:
        """Update or create the <function> child element inside a space element."""
        # Find existing function element (any namespace)
        for child in elem:
            if local_name(child.tag) == "function":
                child.text = value
                return
        # Not found — create it using the same namespace as the parent
        ns = elem.tag.split("}")[0][1:] if "}" in elem.tag else ""
        tag = f"{{{ns}}}function" if ns else "function"
        func_elem = ET.SubElement(elem, tag)
        func_elem.text = value

    def _save_bg_config(self) -> None:
        if self.model is None or not self.model.source_path:
            return
        if self._bg_image is None or self._bg_extent is None:
            return
        if not self._bg_image_path:
            return
        floor = self.floor_var.get() or "All"
        try:
            save_bg_config(self.model.source_path, floor, self._bg_image_path, self._bg_extent)
        except Exception:
            pass

    def _restore_bg_for_floor(self, floor: str) -> None:
        """Load background image and extent for the given floor from sidecar JSON."""
        if self.model is None or not self.model.source_path:
            return
        entry = load_bg_config_for_floor(self.model.source_path, floor)
        if entry is None:
            # Clear background when no config exists for this floor
            self._bg_image = None
            self._bg_image_path = ""
            self._bg_extent = None
            return
        img_path = entry.get("image_path", "")
        extent = entry.get("extent")
        if img_path and os.path.exists(img_path) and extent and len(extent) == 4:
            try:
                self._bg_image = mpimg.imread(img_path)
                self._bg_image_path = img_path
                self._bg_extent = extent
                # Don't force `_bg_visible` ON here — View menu toggles are
                # global (apply to all floors), so respect whatever state the
                # user last chose. Forcing it would re-enable Show floorplan
                # every time the user switches to a floor that has a sidecar.
            except Exception:
                self._bg_image = None
                self._bg_image_path = ""
                self._bg_extent = None
        else:
            self._bg_image = None
            self._bg_image_path = ""
            self._bg_extent = None

    def load_background(self) -> None:
        """Load a floorplan image and align it via two reference anchors.

        Workflow (B-style click capture):

          1. User picks an image file.
          2. The image is shown at a default 1:1 pixel-extent so the
             user can SEE it on the canvas.
          3. Anchor-capture mode begins. Status bar prompts the user to
             click the LL anchor on the canvas. If no file-wide
             reference points are set yet (first floorplan), a small
             dialog asks for the world (x, y) of that click. If they
             ARE set, the saved world coords are reused without asking.
          4. Same for the UR anchor.
          5. From the two (canvas, world) pairs the editor solves a 2-
             axis affine transform and re-extents the image so that the
             two clicked pixels land exactly at their world targets.
        """
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return
        path = filedialog.askopenfilename(
            title="Open Background Image",
            filetypes=[("Image files", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            self._bg_image = mpimg.imread(path)
            self._bg_image_path = path
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load image:\n{e}")
            return

        # Default 1:1 pixel-extent so the image is visible while the user
        # picks anchors. This will be overwritten in step 5.
        h_, w_ = self._bg_image.shape[:2]
        self._bg_extent = [0.0, float(w_), 0.0, float(h_)]

        self._bg_visible.set(True)
        self._save_bg_config()
        self.refresh_view()
        self.fit_to_extent()         # zoom to image so user can click on it

        # Pre-load saved reference world coords (if any)
        saved = None
        if self.model.source_path:
            saved = load_reference_points(self.model.source_path)
        self._reference_points = saved

        # Begin click-capture
        self._begin_anchor_capture(saved)

    # ----------------------------------------------------------------
    # Anchor-capture mode (B-style)
    # ----------------------------------------------------------------
    def _begin_anchor_capture(
        self,
        saved: Optional[Tuple[Tuple[float, float], Tuple[float, float]]]
    ) -> None:
        """Enter the two-click anchor-capture mode."""
        self._deactivate_all_edit_modes()
        self._anchor_mode = True
        self._anchor_step = 1                       # 1 → wait for LL, 2 → UR
        self._anchor_canvas_pt1 = None
        self._anchor_canvas_pt2 = None
        self._anchor_world_pt1: Optional[Tuple[float, float]] = (
            saved[0] if saved else None)
        self._anchor_world_pt2: Optional[Tuple[float, float]] = (
            saved[1] if saved else None)
        msg = ("Reference Points: click LL on the floorplan."
               + (f"  (saved world coord {saved[0]} will be reused)"
                  if saved else "  (you will be asked for its world coord)"))
        self.status_var.set(msg)
        self.refresh_view()

    def _end_anchor_capture(self) -> None:
        self._anchor_mode = False
        self._anchor_step = 0
        self._anchor_canvas_pt1 = None
        self._anchor_canvas_pt2 = None
        self._anchor_world_pt1 = None
        self._anchor_world_pt2 = None
        self.status_var.set("")
        self.refresh_view()

    def _ask_world_xy(self,
                      title: str,
                      default: Optional[Tuple[float, float]] = None
                      ) -> Optional[Tuple[float, float]]:
        """Pop a tiny modal asking for a world (x, y); return None on cancel."""
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.resizable(False, False)
        dlg.grab_set()
        try:
            dlg.transient(self.root)
        except Exception:
            pass
        ttk.Label(dlg, text="World coordinate:").grid(
            row=0, column=0, columnspan=4, padx=12, pady=(10, 4), sticky="w")
        ttk.Label(dlg, text="x =").grid(row=1, column=0, padx=(12, 2), sticky="e")
        x_var = tk.StringVar(value=f"{default[0]:g}" if default else "")
        e_x = ttk.Entry(dlg, textvariable=x_var, width=14)
        e_x.grid(row=1, column=1, padx=(2, 8), pady=4)
        ttk.Label(dlg, text="y =").grid(row=1, column=2, padx=(0, 2), sticky="e")
        y_var = tk.StringVar(value=f"{default[1]:g}" if default else "")
        ttk.Entry(dlg, textvariable=y_var, width=14).grid(
            row=1, column=3, padx=(2, 12), pady=4)
        out: List[Optional[Tuple[float, float]]] = [None]
        def _ok():
            try:
                out[0] = (float(x_var.get().strip()),
                          float(y_var.get().strip()))
            except ValueError:
                messagebox.showwarning(title,
                                       "Both values must be numbers.",
                                       parent=dlg)
                return
            dlg.destroy()
        btn = ttk.Frame(dlg)
        btn.grid(row=2, column=0, columnspan=4, pady=(6, 12))
        ttk.Button(btn, text="OK",     command=_ok).pack(side="left", padx=4)
        ttk.Button(btn, text="Cancel", command=dlg.destroy
                   ).pack(side="left", padx=4)
        e_x.focus_set()
        dlg.bind("<Return>", lambda e: _ok())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.wait_window()
        return out[0]

    def _on_anchor_click(self, xdata: float, ydata: float) -> None:
        """Handle a left-click while in anchor-capture mode."""
        if self._anchor_step == 1:
            self._anchor_canvas_pt1 = (xdata, ydata)
            if self._anchor_world_pt1 is None:
                w = self._ask_world_xy("LL Reference — World Coord")
                if w is None:
                    self._end_anchor_capture()
                    return
                self._anchor_world_pt1 = w
            self._anchor_step = 2
            self.status_var.set(
                "Reference Points: click UR on the floorplan."
                + (f"  (saved world coord {self._anchor_world_pt2} "
                   "will be reused)"
                   if self._anchor_world_pt2 else
                   "  (you will be asked for its world coord)"))
            self.refresh_view()
            return

        if self._anchor_step == 2:
            self._anchor_canvas_pt2 = (xdata, ydata)
            if self._anchor_world_pt2 is None:
                w = self._ask_world_xy("UR Reference — World Coord")
                if w is None:
                    self._end_anchor_capture()
                    return
                self._anchor_world_pt2 = w
            # Ensure LL is actually lower-left of UR in world coords
            ll_w = self._anchor_world_pt1
            ur_w = self._anchor_world_pt2
            if ll_w[0] >= ur_w[0] or ll_w[1] >= ur_w[1]:
                messagebox.showwarning(
                    "Reference Points",
                    "Lower-Left must be strictly below and to the left "
                    "of Upper-Right in world coordinates.\n"
                    "Re-doing the capture.")
                self._begin_anchor_capture(self._reference_points)
                return

            # Solve 2-axis affine: new_world = a * old_world + b
            (cx1, cy1) = self._anchor_canvas_pt1
            (cx2, cy2) = self._anchor_canvas_pt2
            if abs(cx2 - cx1) < 1e-9 or abs(cy2 - cy1) < 1e-9:
                messagebox.showwarning(
                    "Reference Points",
                    "The two anchor clicks are too close in x or y. "
                    "Pick clearly distinct points.")
                self._begin_anchor_capture(self._reference_points)
                return
            ax = (ur_w[0] - ll_w[0]) / (cx2 - cx1)
            bx = ll_w[0] - ax * cx1
            ay = (ur_w[1] - ll_w[1]) / (cy2 - cy1)
            by = ll_w[1] - ay * cy1

            old_L, old_R, old_B, old_T = self._bg_extent
            new_L = ax * old_L + bx
            new_R = ax * old_R + bx
            new_B = ay * old_B + by
            new_T = ay * old_T + by
            # Maintain canonical ordering xmin<xmax, ymin<ymax
            if new_L > new_R:
                new_L, new_R = new_R, new_L
            if new_B > new_T:
                new_B, new_T = new_T, new_B
            self._bg_extent = [new_L, new_R, new_B, new_T]
            self._reference_points = (ll_w, ur_w)

            if self.model and self.model.source_path:
                save_reference_points(self.model.source_path,
                                      ll_w, ur_w)
                self._save_bg_config()

            self.status_var.set(
                f"Floorplan aligned. LL=({ll_w[0]:g}, {ll_w[1]:g})  "
                f"UR=({ur_w[0]:g}, {ur_w[1]:g})")
            self._end_anchor_capture()
            self.fit_to_extent()

    def set_reference_points(self) -> None:
        """Floor menu command: (re)capture the two reference anchors.

        Reuses the same click-capture flow as the first-time floorplan
        load. If a floorplan is already shown the user can simply click
        the LL and UR anchors on it; otherwise the command warns.
        """
        if self.model is None or not self.model.source_path:
            messagebox.showwarning("Reference Points",
                                   "Open or save a file first.")
            return
        if self._bg_image is None:
            messagebox.showwarning(
                "Reference Points",
                "Load a floorplan first (Floorplan → Load Floorplan...).")
            return
        # Force a re-capture of BOTH world coords by clearing any saved
        # reference; the click handler will then ask for both.
        self._reference_points = None
        if self.model.source_path:
            clear_reference_points(self.model.source_path)
        self._begin_anchor_capture(None)

    def toggle_bg_resize_mode(self) -> None:
        if self._bg_image is None:
            messagebox.showwarning("No image", "Load a background image first.")
            self._bg_resize_mode_var.set(False)
            return
        was_active = self._bg_resize_mode
        # Turning ON: deactivate every other mode so clicks aren't fought over.
        if not was_active:
            self._deactivate_all_edit_modes()
        self._bg_resize_mode = not was_active
        self._bg_resize_mode_var.set(self._bg_resize_mode)
        self._bg_resize_handle = None
        self.refresh_view()

    def _get_resize_handles(self) -> Dict[str, Tuple[float, float]]:
        """Return handle name → (x, y) for the 8 handles around the bg extent."""
        xmin, xmax, ymin, ymax = self._bg_extent
        mx, my = (xmin + xmax) / 2, (ymin + ymax) / 2
        return {
            "TL": (xmin, ymax), "T": (mx, ymax), "TR": (xmax, ymax),
            "L":  (xmin, my),                     "R":  (xmax, my),
            "BL": (xmin, ymin), "B": (mx, ymin),  "BR": (xmax, ymin),
        }

    def _find_resize_handle(self, xdata: float, ydata: float) -> Optional[str]:
        xlim = self.ax.get_xlim()
        ylim = self.ax.get_ylim()
        threshold = min(abs(xlim[1] - xlim[0]), abs(ylim[1] - ylim[0])) * 0.02
        best, best_dist = None, threshold
        for name, (hx, hy) in self._get_resize_handles().items():
            dist = ((hx - xdata) ** 2 + (hy - ydata) ** 2) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best = name
        return best

    def toggle_bg_move_mode(self) -> None:
        if self._bg_image is None:
            messagebox.showwarning("No image", "Load a background image first.")
            self._bg_move_mode_var.set(False)
            return
        was_active = self._bg_move_mode
        if not was_active:
            self._deactivate_all_edit_modes()
        self._bg_move_mode = not was_active
        self._bg_move_mode_var.set(self._bg_move_mode)
        self._bg_move_start_data = None
        self._bg_move_start_extent = None
        self.refresh_view()

    def set_bg_opacity(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Floorplan Opacity")
        dlg.resizable(False, False)
        dlg.grab_set()
        ttk.Label(dlg, text="Opacity (0.0 – 1.0):").grid(row=0, column=0, padx=12, pady=(12, 4), sticky="e")
        var = tk.DoubleVar(value=self._bg_alpha.get())
        sp = ttk.Spinbox(dlg, from_=0.0, to=1.0, increment=0.05, textvariable=var, width=7)
        sp.grid(row=0, column=1, padx=(4, 12), pady=(12, 4))
        scale = ttk.Scale(dlg, from_=0.0, to=1.0, variable=var, orient="horizontal", length=200)
        scale.grid(row=1, column=0, columnspan=2, padx=12, pady=4)

        def apply() -> None:
            try:
                v = float(var.get())
                self._bg_alpha.set(max(0.0, min(1.0, v)))
                self.refresh_view()
            except ValueError:
                pass

        scale.config(command=lambda _: apply())
        btn_frame = ttk.Frame(dlg)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=(4, 12))
        ttk.Button(btn_frame, text="OK", command=dlg.destroy).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Apply", command=apply).pack(side="left", padx=6)

    def set_background_extent(self) -> None:
        if self._bg_image is None:
            messagebox.showwarning("No image", "Load a background image first.")
            return

        current = self._bg_extent or [0, 1, 0, 1]

        dlg = tk.Toplevel(self.root)
        dlg.title("Set Background Extent")
        dlg.resizable(False, False)
        dlg.grab_set()

        labels = ["X min", "X max", "Y min", "Y max"]
        entries = []
        for i, (lbl, val) in enumerate(zip(labels, current)):
            ttk.Label(dlg, text=lbl).grid(row=i, column=0, padx=(12, 4), pady=4, sticky="e")
            var = tk.StringVar(value=str(val))
            e = ttk.Entry(dlg, textvariable=var, width=20)
            e.grid(row=i, column=1, padx=(0, 12), pady=4)
            entries.append(var)

        def apply() -> None:
            try:
                vals = [float(v.get()) for v in entries]
            except ValueError:
                messagebox.showerror("Invalid input", "All values must be numbers.", parent=dlg)
                return
            if vals[0] >= vals[1]:
                messagebox.showerror("Invalid input", "X min must be less than X max.", parent=dlg)
                return
            if vals[2] >= vals[3]:
                messagebox.showerror("Invalid input", "Y min must be less than Y max.", parent=dlg)
                return
            self._bg_extent = vals
            self._save_bg_config()
            self.refresh_view()

        def ok() -> None:
            apply()
            dlg.destroy()

        btn_frame = ttk.Frame(dlg)
        btn_frame.grid(row=4, column=0, columnspan=2, pady=(4, 12))
        ttk.Button(btn_frame, text="Apply", command=apply).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Cancel", command=dlg.destroy).pack(side="left", padx=4)

        entries[0].widget = dlg  # keep reference
        dlg.wait_window()

    def _draw_background(self) -> None:
        if self._bg_image is None or not self._bg_visible.get():
            return
        xmin, xmax, ymin, ymax = self._bg_extent
        try:
            alpha = float(self._bg_alpha.get())
        except (tk.TclError, ValueError):
            alpha = 0.5
        # aspect='auto' prevents imshow from calling ax.set_aspect('equal',
        # adjustable='datalim') internally, which would conflict with our
        # explicit set_xlim/set_ylim and produce the warning.
        self.ax.imshow(
            self._bg_image,
            extent=[xmin, xmax, ymin, ymax],
            alpha=alpha,
            zorder=0,
            origin='upper',
            aspect='auto',
        )
        if self._bg_resize_mode:
            handles = self._get_resize_handles()
            hxs = [p[0] for p in handles.values()]
            hys = [p[1] for p in handles.values()]
            self.ax.scatter(hxs, hys, color="cyan", s=80, zorder=10, marker="s")
            # Border rect
            self.ax.plot([xmin, xmax, xmax, xmin, xmin],
                         [ymin, ymin, ymax, ymax, ymin],
                         color="cyan", linewidth=1.0, linestyle="--", zorder=9)

    def export_floors(self) -> None:
        """Export selected floors (multi-select) into a single IndoorGML
        file, preserving the original Z values of every vertex."""
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return
        floors = get_available_floors(self.model)
        if not floors:
            messagebox.showwarning(
                "No floors",
                "No floor (level) information found in the model.")
            return

        # Multi-select dialog
        dlg = tk.Toplevel(self.root)
        dlg.title("Export Floors")
        dlg.resizable(False, True)
        dlg.grab_set()
        ttk.Label(dlg, text="Select one or more floors to export:") \
            .grid(row=0, column=0, columnspan=2, padx=10, pady=(10, 4), sticky="w")
        listbox = tk.Listbox(
            dlg, selectmode="extended", height=min(15, max(5, len(floors))),
            exportselection=False, width=24)
        for f in floors:
            listbox.insert("end", f)
        listbox.grid(row=1, column=0, columnspan=2, padx=10, pady=4, sticky="nsew")
        dlg.rowconfigure(1, weight=1)

        # Pre-select the active floor if any
        active = self.floor_var.get()
        if active and active in floors:
            listbox.selection_set(floors.index(active))

        confirmed = [False]
        # Capture the selection BEFORE the dialog (and its listbox) are
        # destroyed — querying a destroyed widget raises TclError
        # "invalid command name .!toplevel.!listbox".
        chosen_indices: List[int] = []
        def on_ok():
            sel = list(listbox.curselection())
            if not sel:
                messagebox.showwarning(
                    "Selection required",
                    "Select at least one floor to export.", parent=dlg)
                return
            chosen_indices.extend(sel)
            confirmed[0] = True
            dlg.destroy()
        def on_cancel():
            dlg.destroy()

        btn = ttk.Frame(dlg)
        btn.grid(row=2, column=0, columnspan=2, pady=8)
        ttk.Button(btn, text="OK",     command=on_ok).pack(side="left", padx=4)
        ttk.Button(btn, text="Cancel", command=on_cancel).pack(side="left", padx=4)
        dlg.wait_window()

        if not confirmed[0]:
            return
        selected = [floors[i] for i in chosen_indices]

        # Single combined output file — user picks the path
        base = "export"
        if self.model.source_path:
            base = os.path.splitext(os.path.basename(self.model.source_path))[0]
        default_dir = (os.path.dirname(self.model.source_path)
                       if self.model.source_path else os.getcwd())
        suffix = "_" + "_".join(re.sub(r'[\\/:*?"<>|]', '_', f)
                                for f in selected[:3])
        if len(selected) > 3:
            suffix += f"_and_{len(selected) - 3}_more"
        out_path = filedialog.asksaveasfilename(
            title="Export Floors as IndoorGML — output file",
            defaultextension=".xml",
            initialdir=default_dir,
            initialfile=f"{base}{suffix}.xml",
            filetypes=[("XML files", "*.xml"),
                       ("GML files", "*.gml"),
                       ("All files", "*.*")])
        if not out_path:
            return

        try:
            export_indoorgml_floors(self.model, selected, out_path)
        except Exception as e:
            messagebox.showerror("Export Error", str(e))
            return

        floor_list = ", ".join(selected)
        messagebox.showinfo(
            "Export complete",
            f"Exported floor(s) [{floor_list}] to:\n{out_path}\n\n"
            f"Z values preserved.")
        self.status_var.set(
            f"Exported {len(selected)} floor(s) → {os.path.basename(out_path)}")

    def add_floor_from_file(self) -> None:
        """Import all data from an XML file as a new floor.

        Every space and node gets the user-specified floor level assigned.
        If an ID from the file already exists in the model a floor-name suffix
        is appended so the original data is never overwritten.
        duality and edge.connects references are remapped to the new IDs.
        """
        if self.model is None:
            messagebox.showwarning("No file", "Open the base model file first.")
            return

        # ── 1. Select source file ─────────────────────────────────────────────
        path = filedialog.askopenfilename(
            title="Select floor XML file to add",
            filetypes=[("XML files", "*.xml"), ("GML files", "*.gml"), ("All files", "*.*")]
        )
        if not path:
            return

        # ── 2. Ask for the floor name to assign ───────────────────────────────
        existing_floors = get_available_floors(self.model)

        dlg = tk.Toplevel(self.root)
        dlg.title("New Floor Name")
        dlg.resizable(False, False)
        dlg.grab_set()

        ttk.Label(dlg, text="Floor name / level to assign:").grid(
            row=0, column=0, columnspan=2, padx=14, pady=(14, 4), sticky="w")

        name_var = tk.StringVar(value="")
        entry = ttk.Entry(dlg, textvariable=name_var, width=22)
        entry.grid(row=1, column=0, padx=(14, 4), pady=4, sticky="w")
        entry.focus_set()

        if existing_floors:
            hint = "Existing: " + ", ".join(existing_floors)
            ttk.Label(dlg, text=hint, foreground="gray",
                      font=("TkDefaultFont", 8)).grid(
                row=1, column=1, padx=(0, 14), pady=4, sticky="w")

        result: List[Optional[str]] = [None]

        def on_ok():
            v = name_var.get().strip()
            if not v:
                messagebox.showwarning("Empty name", "Please enter a floor name.", parent=dlg)
                return
            if v in existing_floors:
                if not messagebox.askyesno(
                    "Floor exists",
                    f"Floor '{v}' already exists in the model.\n"
                    "Items from the file will be added alongside existing items.\n\n"
                    "Continue?",
                    parent=dlg
                ):
                    return
            result[0] = v
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        btn_frame = ttk.Frame(dlg)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=(6, 14))
        ttk.Button(btn_frame, text="OK",     command=on_ok).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side="left", padx=6)
        dlg.bind("<Return>", lambda e: on_ok())
        dlg.bind("<Escape>", lambda e: on_cancel())
        dlg.wait_window()

        floor_name = result[0]
        if floor_name is None:
            return

        # ── 3. Parse the floor file ───────────────────────────────────────────
        try:
            floor_model = parse_indoorgml(path)
        except Exception as e:
            messagebox.showerror("Parse Error", str(e))
            return

        if not floor_model.spaces and not floor_model.nodes:
            messagebox.showwarning(
                "Empty file",
                f"No spaces or nodes found in:\n{os.path.basename(path)}"
            )
            return

        # ── 4. Build ID-remap tables then insert — never overwrite existing ───
        import copy as _copy

        suffix = f"_{floor_name}"

        # Pass 1: decide final IDs (add suffix only on collision)
        space_id_map: Dict[str, str] = {}
        for fid in floor_model.spaces:
            space_id_map[fid] = fid if fid not in self.model.spaces else f"{fid}{suffix}"

        node_id_map: Dict[str, str] = {}
        for nid in floor_model.nodes:
            node_id_map[nid] = nid if nid not in self.model.nodes else f"{nid}{suffix}"

        # Pass 2: insert spaces
        #   source_element is set to None so save_indoorgml treats them as
        #   brand-new items and writes proper XML entries via _add_new_spaces_to_xml.
        added_sp = 0
        for fid, fsp in floor_model.spaces.items():
            new_id = space_id_map[fid]
            new_sp = _copy.deepcopy(fsp)
            new_sp.level = floor_name
            new_sp.feature_id = new_id
            new_sp.source_element = None          # must be None → treated as new on save
            if new_sp.duality and new_sp.duality in node_id_map:
                new_sp.duality = node_id_map[new_sp.duality]
            self.model.spaces[new_id] = new_sp
            self._new_space_ids.add(new_id)       # register so save writes it to XML
            added_sp += 1

        # Pass 3: insert nodes
        added_nd = 0
        for nid, fnode in floor_model.nodes.items():
            new_nid = node_id_map[nid]
            new_node = _copy.deepcopy(fnode)
            new_node.level = floor_name
            new_node.node_id = new_nid
            new_node.source_element = None        # must be None → treated as new on save
            if new_node.duality and new_node.duality in space_id_map:
                new_node.duality = space_id_map[new_node.duality]
            self.model.nodes[new_nid] = new_node
            self._new_node_ids.add(new_nid)       # register so save writes it to XML
            added_nd += 1

        # Pass 4: insert edges
        added_ed = 0
        for eid, fedge in floor_model.edges.items():
            new_eid = eid if eid not in self.model.edges else f"{eid}{suffix}"
            new_edge = _copy.deepcopy(fedge)
            new_edge.edge_id = new_eid
            new_edge.source_element = None        # must be None → treated as new on save
            new_edge.connects = [
                node_id_map.get(c, space_id_map.get(c, c))
                for c in fedge.connects
            ]
            self.model.edges[new_eid] = new_edge
            self._new_edge_ids.add(new_eid)       # register so save writes it to XML
            added_ed += 1

        # ── 5. Refresh UI ─────────────────────────────────────────────────────
        self._update_floor_combo()
        self.floor_var.set(floor_name)   # switch view to the new floor
        self._view_locked = False

        self.status_var.set(
            f"Floor '{floor_name}' added — "
            f"{added_sp} space(s), {added_nd} node(s), {added_ed} edge(s)"
        )
        messagebox.showinfo(
            "Add Floor",
            f"Floor '{floor_name}' added from:\n{os.path.basename(path)}\n\n"
            f"  Spaces : {added_sp}\n"
            f"  Nodes  : {added_nd}\n"
            f"  Edges  : {added_ed}"
        )
        self.refresh_view()

    def _floor_ceiling_z(self, level: str) -> Tuple[float, float]:
        """Resolve (floor_z, ceiling_z) for a level.

        Priority (FIXED — was reversed before):
          1. User override registered by the New Floor dialog. This is
             the most authoritative — the user explicitly told us what
             the floor heights are.
          2. Existing cells on that floor (via `_get_floor_ceiling_z`).
             Only consulted when there is no override and at least one
             cell on the level has a sensible Z.
          3. Hard default (0.0, 3.0) meters.

        Why the priority change:
        Older builds checked existing cells FIRST and the user override
        SECOND. That meant if even ONE cell on the floor had a
        corrupted z=0 (e.g. imported from a 2D-only IndoorGML, or left
        over from a prior session that didn't have an override), every
        subsequent New Cell / New Node on that floor inherited z=0 —
        propagating the corruption forward. Honouring the user's
        override first makes "I told you 2F is at 500" stick.
        """
        # 1) explicit user override
        if level in self._floor_z_overrides:
            return self._floor_z_overrides[level]
        # 2) existing cells on that floor
        if self.model is not None:
            for sp in self.model.spaces.values():
                if sp.level == level and sp.polygons_3d:
                    return _get_floor_ceiling_z(self.model, level)
        # 3) hard default (cm)
        return 0.0, 300.0

    def new_floor_and_load_floorplan(self) -> None:
        """Floorplan menu entry — create a brand-new floor and immediately
        start the Load Floorplan flow on it.

        Combines the existing New Floor dialog (floor name + Z heights)
        with the click-anchor floorplan loader so the user can lay out a
        fresh floor in one go: pick a name, pick the image, click two
        reference anchors. The two reference points stay shared with all
        the other floors so the new floor lands in the same coordinate
        frame.
        """
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return
        floors_before = set(get_available_floors(self.model))
        # Capture the user's floor-Z overrides too — `new_floor` may add
        # an entry there even before a CellSpace exists on the floor.
        zovr_before = set(self._floor_z_overrides.keys())

        self.new_floor()

        floors_after = set(get_available_floors(self.model))
        zovr_after = set(self._floor_z_overrides.keys())
        added = list((floors_after | zovr_after) -
                     (floors_before | zovr_before))
        if not added:
            # User cancelled the New Floor dialog — nothing to do.
            return

        # Switch to the new floor (new_floor already does this, but be
        # defensive in case it failed) and chain into Load Floorplan.
        new_name = added[0]
        if new_name in self.floor_combo["values"]:
            self.floor_var.set(new_name)
        self.refresh_view()
        # Invoke Load Floorplan automatically so the user goes straight
        # into picking the image + clicking the two anchors.
        self.load_background()

    def new_floor(self) -> None:
        """Create a new empty floor level and switch to it."""
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return

        existing_floors = get_available_floors(self.model)

        dlg = tk.Toplevel(self.root)
        dlg.title("New Floor")
        dlg.resizable(False, False)
        dlg.grab_set()

        ttk.Label(dlg, text="Floor name:").grid(
            row=0, column=0, padx=14, pady=(14, 4), sticky="w")

        name_var = tk.StringVar()
        name_entry = ttk.Entry(dlg, textvariable=name_var, width=22)
        name_entry.grid(row=0, column=1, padx=(0, 14), pady=(14, 4), sticky="w")
        name_entry.focus_set()

        # Floor Z (cm)
        ttk.Label(dlg, text="Floor Z (cm):").grid(
            row=1, column=0, padx=14, pady=4, sticky="w")
        floor_z_var = tk.StringVar(value="0")
        ttk.Entry(dlg, textvariable=floor_z_var, width=22).grid(
            row=1, column=1, padx=(0, 14), pady=4, sticky="w")

        # Ceiling Z (cm)
        ttk.Label(dlg, text="Ceiling Z (cm):").grid(
            row=2, column=0, padx=14, pady=4, sticky="w")
        ceiling_z_var = tk.StringVar(value="300")
        ttk.Entry(dlg, textvariable=ceiling_z_var, width=22).grid(
            row=2, column=1, padx=(0, 14), pady=4, sticky="w")

        if existing_floors:
            ttk.Label(dlg, text=f"Existing: {', '.join(existing_floors)}",
                      font=("TkDefaultFont", 8), foreground="gray").grid(
                row=3, column=0, columnspan=2, padx=14, pady=(0, 4), sticky="w")

        warn_var = tk.StringVar()
        warn_lbl = ttk.Label(dlg, textvariable=warn_var, foreground="red",
                             font=("TkDefaultFont", 8))
        warn_lbl.grid(row=4, column=0, columnspan=2, padx=14, sticky="w")

        result: List[Optional[Tuple[str, float, float]]] = [None]

        def on_ok():
            name = name_var.get().strip()
            if not name:
                warn_var.set("Floor name cannot be empty.")
                return
            if name in existing_floors:
                warn_var.set(f"Floor '{name}' already exists.")
                return
            try:
                fz = float(floor_z_var.get().strip())
                cz = float(ceiling_z_var.get().strip())
            except ValueError:
                warn_var.set("Floor Z / Ceiling Z must be numeric (cm).")
                return
            if cz <= fz:
                warn_var.set("Ceiling Z must be greater than Floor Z.")
                return
            result[0] = (name, fz, cz)
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        btn_frame = ttk.Frame(dlg)
        btn_frame.grid(row=5, column=0, columnspan=2, pady=(6, 14))
        ttk.Button(btn_frame, text="Create", command=on_ok).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side="left", padx=6)
        dlg.bind("<Return>", lambda e: on_ok())
        dlg.bind("<Escape>", lambda e: on_cancel())
        dlg.wait_window()

        if result[0] is None:
            return
        floor_name, floor_z, ceiling_z = result[0]

        # Register the floor-Z override so the next New Cell on this floor
        # uses the user-specified height instead of the hardcoded fallback.
        self._floor_z_overrides[floor_name] = (floor_z, ceiling_z)

        # Add the new floor to the combo and switch to it
        self._update_floor_combo()
        self.floor_var.set(floor_name)
        self._view_locked = False

        self.status_var.set(
            f"Floor '{floor_name}' created (z={floor_z}m..{ceiling_z}m) "
            f"— draw new cells with Cell → New Cell.")
        self.refresh_view()

    def delete_floor(self) -> None:
        """Delete all spaces, nodes and edges that belong to a chosen floor."""
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return

        floors = get_available_floors(self.model)
        if not floors:
            messagebox.showwarning("No floors", "No floor (level) information found in the model.")
            return

        # ── Floor selection dialog ────────────────────────────────────────────
        dlg = tk.Toplevel(self.root)
        dlg.title("Delete Floor")
        dlg.resizable(False, False)
        dlg.grab_set()

        ttk.Label(dlg, text="Select floor to delete:").grid(
            row=0, column=0, columnspan=2, padx=14, pady=(14, 4), sticky="w")

        sel_var = tk.StringVar(value=floors[0])
        combo = ttk.Combobox(dlg, textvariable=sel_var,
                             values=floors, state="readonly", width=20)
        combo.grid(row=1, column=0, columnspan=2, padx=14, pady=4, sticky="w")

        result: List[Optional[str]] = [None]

        def on_ok():
            result[0] = sel_var.get().strip()
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        btn_frame = ttk.Frame(dlg)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=(6, 14))
        ttk.Button(btn_frame, text="Delete", command=on_ok).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side="left", padx=6)
        dlg.bind("<Return>", lambda e: on_ok())
        dlg.bind("<Escape>", lambda e: on_cancel())
        dlg.wait_window()

        target = result[0]
        if not target:
            return

        # ── Confirm ───────────────────────────────────────────────────────────
        if not messagebox.askyesno(
            "Confirm Delete",
            f"Delete ALL data for floor '{target}'?\n"
            "(Spaces, dual nodes and connected edges will be removed.)\n\n"
            "This cannot be undone."
        ):
            return

        # ── Collect IDs to remove ─────────────────────────────────────────────
        # 1. spaces on target floor
        space_ids_to_del = get_space_ids_on_floor(self.model, target)

        # 2. dual nodes whose duality points to one of those spaces,
        #    OR nodes explicitly tagged with this floor level
        node_ids_to_del: Set[str] = set()
        for nid, node in self.model.nodes.items():
            if node.duality and node.duality in space_ids_to_del:
                node_ids_to_del.add(nid)
            elif node.level and node.level.strip() == target.strip():
                node_ids_to_del.add(nid)

        # 3. edges that connect exclusively to nodes/spaces being deleted
        #    (remove an edge if ALL its connect-refs are in the deleted sets)
        edge_ids_to_del: Set[str] = set()
        deleted_refs = space_ids_to_del | node_ids_to_del
        for eid, edge in self.model.edges.items():
            if edge.connects and all(c in deleted_refs for c in edge.connects):
                edge_ids_to_del.add(eid)

        # ── Delete ────────────────────────────────────────────────────────────
        for sid in space_ids_to_del:
            self.model.spaces.pop(sid, None)
            self._new_space_ids.discard(sid)
        for nid in node_ids_to_del:
            self.model.nodes.pop(nid, None)
            self._new_node_ids.discard(nid)
        for eid in edge_ids_to_del:
            self.model.edges.pop(eid, None)
            self._new_edge_ids.discard(eid)

        # ── Update floor combo ────────────────────────────────────────────────
        deleted = self.floor_var.get() == target
        self._update_floor_combo()
        if deleted:
            self.floor_var.set(self.floor_combo["values"][0])
        self._view_locked = False

        n_sp = len(space_ids_to_del)
        n_nd = len(node_ids_to_del)
        n_ed = len(edge_ids_to_del)
        self.status_var.set(
            f"Floor '{target}' deleted — "
            f"{n_sp} space(s), {n_nd} node(s), {n_ed} edge(s) removed."
        )
        messagebox.showinfo(
            "Delete Floor",
            f"Floor '{target}' deleted.\n"
            f"  Spaces removed : {n_sp}\n"
            f"  Nodes removed  : {n_nd}\n"
            f"  Edges removed  : {n_ed}"
        )
        self.refresh_view()

    def save_file(self) -> None:
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return
        try:
            n_nodes = len(self._new_node_ids)
            n_edges = len(self._new_edge_ids)
            n_spaces = len(self._new_space_ids)
            saved_node_ids = set(self._new_node_ids)
            saved_edge_ids = set(self._new_edge_ids)
            saved_space_ids = set(self._new_space_ids)
            save_indoorgml(self.model, self.model.source_path,
                           new_node_ids=saved_node_ids,
                           new_edge_ids=saved_edge_ids,
                           new_space_ids=saved_space_ids)
            self._new_node_ids.clear()
            self._new_edge_ids.clear()
            self._new_space_ids.clear()
            self._new_ilc_ids.clear()
            msg = f"Saved: {self.model.source_path}"
            parts = []
            if n_spaces:
                parts.append(f"+{n_spaces} space(s)")
            if n_nodes:
                parts.append(f"+{n_nodes} node(s)")
            if n_edges:
                parts.append(f"+{n_edges} edge(s)")
            if parts:
                msg += "  (" + ", ".join(parts) + ")"
            self.status_var.set(msg)
        except Exception as e:
            messagebox.showerror("Save Error", str(e))

    def save_file_as(self) -> None:
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save IndoorGML As",
            defaultextension=".xml",
            filetypes=[("XML files", "*.xml"), ("GML files", "*.gml"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            old_path = self.model.source_path
            save_indoorgml(self.model, path,
                           new_node_ids=set(self._new_node_ids),
                           new_edge_ids=set(self._new_edge_ids),
                           new_space_ids=set(self._new_space_ids))
            self._new_node_ids.clear()
            self._new_edge_ids.clear()
            self._new_space_ids.clear()
            # Carry the floorplan-alignment sidecar (image path + extent per
            # floor) over to the new file. The sidecar lives next to the GML
            # as "<gml_path>.bgcfg.json"; without copying it the user would
            # lose every Floorplan position/scale they had aligned.
            self._copy_bg_config_to(old_path, path)
            self.file_var.set(path)
            self.status_var.set(f"Saved as: {path}")
        except Exception as e:
            messagebox.showerror("Save Error", str(e))

    def _copy_bg_config_to(self, old_gml_path: Optional[str], new_gml_path: str) -> None:
        """Copy the floorplan sidecar from old_gml_path next to new_gml_path.

        Falls back gracefully when:
          • the old file had no sidecar (nothing to copy)
          • old and new resolve to the same path (Save As over the same file)
        """
        if not new_gml_path:
            return
        new_sidecar = _bgcfg_path(new_gml_path)
        if old_gml_path:
            old_sidecar = _bgcfg_path(old_gml_path)
            try:
                same = (os.path.abspath(old_sidecar) ==
                        os.path.abspath(new_sidecar))
            except Exception:
                same = False
            if not same and os.path.exists(old_sidecar):
                try:
                    import shutil as _shutil
                    _shutil.copy2(old_sidecar, new_sidecar)
                    return
                except Exception:
                    pass
        # No old sidecar (or copy failed) — write the current in-memory BG
        # state so the alignment isn't lost. Only writes when something is
        # actually loaded for the active floor.
        if self._bg_image is not None and self._bg_extent is not None and \
                self._bg_image_path:
            floor = self.floor_var.get() or ""
            if floor and floor != "All":
                try:
                    save_bg_config(new_gml_path, floor,
                                   self._bg_image_path, self._bg_extent)
                except Exception:
                    pass

    def _clear_view_selection(self) -> None:
        """Deselect any view-only selection and clear the info panel."""
        self._view_selected_type = None
        self._view_selected_id = None
        self._set_info_text("")

    def _update_edit_space_info(self, sp_id: Optional[str]) -> None:
        """Show the selected space's info in the right-panel Selection widget (Cell Edit mode)."""
        if sp_id and self.model and sp_id in self.model.spaces:
            self._view_selected_type = "space"
            self._view_selected_id = sp_id
            self._show_view_selection()
        else:
            self._view_selected_type = None
            self._view_selected_id = None
            self._set_info_text("")

    # -------------------------------------------------------
    # Mutual exclusion helper — deactivate ALL edit modes
    # -------------------------------------------------------
    def _deactivate_all_edit_modes(self) -> None:
        """Turn off every Cell/Network/Door edit mode and reset related state."""
        # Cell Edit
        self._edit_mode = False
        self._edit_mode_var.set(False)
        self._edit_selected_space_id = None
        self._edit_selected_vertex_idx = None
        self._edit_drag_active = False
        self._space_drag_active = False
        self.edit_status_var.set("")
        # New Cell
        self._space_create_mode = False
        self._space_create_mode_var.set(False)
        self._space_create_verts.clear()
        if self._space_create_preview_line is not None:
            self._space_create_preview_line.set_data([], [])
        # New Cell (Rectangle)
        self._rect_create_mode = False
        if hasattr(self, "_rect_create_mode_var"):
            self._rect_create_mode_var.set(False)
        self._rect_create_pt1 = None
        # Cell Split
        self._split_mode = False
        self._split_mode_var.set(False)
        self._split_pt1 = None
        self._split_space_id = None
        self._split_vertex_idx = None
        self._split_preview_line.set_data([], [])
        self._split_pt1_dot.set_offsets(np.empty((0, 2)))
        # Node Edit
        self._graph_edit_mode = False
        self._graph_edit_mode_var.set(False)
        self._graph_selected_node = None
        self._graph_drag_node = None
        self._graph_selected_edge = None
        # Edge Edit (Connect)
        self._graph_connect_mode = False
        self._graph_connect_mode_var.set(False)
        self._graph_connect_first = None
        # Vertical Edge
        self._vertical_edge_mode = False
        self._vertical_edge_mode_var.set(False)
        self._vertical_edge_first_node = None
        self._vertical_edge_first_floor = None
        self._graph_edit_status_var.set("")
        # Door
        self._door_mode = False
        self._door_mode_var.set(False)
        self._door_pt1 = None
        self._door_preview_line.set_data([], [])
        # Measure Distance
        self._measure_mode = False
        if hasattr(self, "_measure_mode_var"):
            self._measure_mode_var.set(False)
        self._measure_pt1 = None
        # Floorplan (BG) move / resize — these eat left-clicks at the very
        # top of on_mouse_press, so they MUST be disabled when switching
        # to any cell/network/door edit mode, otherwise clicks silently
        # drop into the BG-handle hit-test path and never reach the active
        # edit mode's branch.
        self._bg_move_mode = False
        if hasattr(self, "_bg_move_mode_var"):
            self._bg_move_mode_var.set(False)
        self._bg_move_start_data = None
        self._bg_move_start_extent = None
        self._bg_resize_mode = False
        if hasattr(self, "_bg_resize_mode_var"):
            self._bg_resize_mode_var.set(False)
        self._bg_resize_handle = None
        # InterLayerConnection — also returns unconditionally on click,
        # so reset alongside the others.
        self._ilc_mode = False
        if hasattr(self, "_ilc_mode_var"):
            self._ilc_mode_var.set(False)
        self._ilc_space1 = None
        # Multi-cell bulk-apply modes (function code / class) — these
        # also intercept every left click, so make them mutually
        # exclusive with everything else.
        if hasattr(self, "_multi_apply_mode"):
            self._multi_apply_mode = False
            self._multi_apply_selected.clear()
            self._multi_apply_code = ""
            if hasattr(self, "_multi_apply_mode_var"):
                self._multi_apply_mode_var.set(False)
        if hasattr(self, "_multi_class_mode"):
            self._multi_class_mode = False
            self._multi_class_selected.clear()
            self._multi_class_value = ""
            if hasattr(self, "_multi_class_mode_var"):
                self._multi_class_mode_var.set(False)

    def toggle_edit_mode(self) -> None:
        self._clear_view_selection()
        was_active = self._edit_mode
        self._deactivate_all_edit_modes()
        if not was_active:
            self._edit_mode = True
            self._edit_mode_var.set(True)
            self.edit_status_var.set("Click to select space | Drag to move | Delete key to delete selected")
        self.refresh_view()

    def _delete_selected_vertex(self) -> None:
        if self._edit_selected_space_id is None or self._edit_selected_vertex_idx is None:
            return
        sp = self.model.spaces.get(self._edit_selected_space_id)
        if sp is None or not sp.polygons_3d:
            return

        poly = sp.polygons_3d[self._edit_selected_poly_idx]
        vi = self._edit_selected_vertex_idx

        # Treat closing duplicate (last == first) as index 0
        is_closed = len(poly) > 1 and poly[0][:2] == poly[-1][:2]
        if is_closed and vi == len(poly) - 1:
            vi = 0

        unique_count = len(poly) - (1 if is_closed else 0)

        # Push undo snapshot before deletion
        self._push_undo({
            "kind": "delete_vertex",
            "space_id": self._edit_selected_space_id,
            "old_polygons_3d": [list(p) for p in sp.polygons_3d],
        })
        if unique_count <= 3:
            self.edit_status_var.set("Cannot remove: polygon must keep at least 3 vertices.")
            return

        del_x, del_y = poly[vi][0], poly[vi][1]
        del poly[vi]

        # Re-close if first vertex was deleted
        if is_closed and vi == 0:
            poly[-1] = poly[0]

        # Propagate deletion to sibling polygons (ceiling, walls)
        surviving = []
        for other_poly in sp.polygons_3d[1:]:
            filtered = [v for v in other_poly
                        if not (abs(v[0] - del_x) < 1e-3 and abs(v[1] - del_y) < 1e-3)]
            unique_xy = {(round(v[0], 3), round(v[1], 3)) for v in filtered}
            if len(unique_xy) >= 3:
                surviving.append(filtered)
        sp.polygons_3d = [poly] + surviving

        self._edit_selected_vertex_idx = None
        sp.centers = [c for c in [polygon_centroid(p) for p in sp.polygons_3d] if c is not None]
        self.edit_status_var.set(f"Vertex deleted from {self._edit_selected_space_id}.")
        self.refresh_view()


    def _cache_bg_drag_background(self) -> None:
        """Render scene WITHOUT background image, capture buffer for bg blit."""
        saved_xlim = self.ax.get_xlim()
        saved_ylim = self.ax.get_ylim()

        # draw_on_axes calls ax.clear(), which detaches all artists.
        draw_on_axes(self.ax, self.model, floor=self.floor_var.get(),
                     show_labels=self.label_var.get(),
                     show_nodes=self.show_nodes_var.get(),
                     bg_floor=self._bg_floor_from_combo())
        self.ax.set_xlim(saved_xlim)
        self.ax.set_ylim(saved_ylim)

        # Re-attach animated bg artists (ax.clear() removed them).
        self.ax.add_artist(self._ov_bg_img)
        self.ax.add_line(self._ov_bg_border)
        self.ax.add_artist(self._ov_bg_handles)

        # Set up animated bg image artist
        try:
            alpha = float(self._bg_alpha.get())
        except (tk.TclError, ValueError):
            alpha = 0.5
        self._ov_bg_img.set_data(self._bg_image)
        self._ov_bg_img.set_alpha(alpha)
        self._ov_bg_img.set_visible(True)

        # canvas.draw() skips animated artists → buffer has only GML geometry
        self.canvas.draw()
        self._bg_drag_cache = self.canvas.copy_from_bbox(self.figure.bbox)
        # immediately blit image on top so screen never shows image-less state
        self._fast_bg_blit()

    def _fast_bg_blit(self) -> None:
        """Restore GML-only background, redraw image + handles via blit."""
        if self._bg_drag_cache is None or self._bg_extent is None:
            return

        xmin, xmax, ymin, ymax = self._bg_extent
        try:
            alpha = float(self._bg_alpha.get())
        except (tk.TclError, ValueError):
            alpha = 0.5

        # Update animated image extent and alpha
        self._ov_bg_img.set_extent([xmin, xmax, ymin, ymax])
        self._ov_bg_img.set_alpha(alpha)

        # Update border and handles if in resize mode
        if self._bg_resize_mode:
            self._ov_bg_border.set_data(
                [xmin, xmax, xmax, xmin, xmin],
                [ymin, ymin, ymax, ymax, ymin]
            )
            handles = self._get_resize_handles()
            self._ov_bg_handles.set_offsets([[h[0], h[1]] for h in handles.values()])
        else:
            self._ov_bg_border.set_data([], [])
            self._ov_bg_handles.set_offsets(np.empty((0, 2)))

        self.canvas.restore_region(self._bg_drag_cache)
        self.ax.draw_artist(self._ov_bg_img)
        self.ax.draw_artist(self._ov_bg_border)
        self.ax.draw_artist(self._ov_bg_handles)
        self.canvas.blit(self.figure.bbox)

    def _prepare_overlay_artists(self) -> None:
        """Populate animated overlay artists from current selection state (no screen update)."""
        sp = self.model.spaces.get(self._edit_selected_space_id)
        if sp is None or not sp.polygons_3d:
            self._ov_line.set_data([], [])
            self._ov_verts.set_offsets(np.empty((0, 2)))
            self._ov_sel_vert.set_offsets(np.empty((0, 2)))
            self._ov_snap_ring.set_offsets(np.empty((0, 2)))
            return
        poly = sp.polygons_3d[0]

        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        if xs[0] != xs[-1] or ys[0] != ys[-1]:
            xs.append(xs[0])
            ys.append(ys[0])
        self._ov_line.set_data(xs, ys)
        self._ov_verts.set_offsets([[p[0], p[1]] for p in poly])

        vi = self._edit_selected_vertex_idx
        if vi is not None and vi < len(poly):
            self._ov_sel_vert.set_offsets([[poly[vi][0], poly[vi][1]]])
        else:
            self._ov_sel_vert.set_offsets(np.empty((0, 2)))

        if self._snap_point:
            self._ov_snap_ring.set_offsets([list(self._snap_point)])
        else:
            self._ov_snap_ring.set_offsets(np.empty((0, 2)))

    def _cache_space_move_background(self) -> None:
        """Render scene without the selected space, capture for space-move blit."""
        saved_xlim = self.ax.get_xlim()
        saved_ylim = self.ax.get_ylim()

        excluded = {self._edit_selected_space_id} if self._edit_selected_space_id else None
        draw_on_axes(self.ax, self.model, floor=self.floor_var.get(),
                     show_labels=False, excluded_space_ids=excluded,
                     show_nodes=self.show_nodes_var.get(),
                     bg_floor=self._bg_floor_from_combo())
        self._draw_background()
        self.ax.set_xlim(saved_xlim)
        self.ax.set_ylim(saved_ylim)

        self.ax.add_line(self._ov_space_move)
        self._ov_space_move.set_data([], [])

        self.canvas.draw()
        self._space_drag_bg_cache = self.canvas.copy_from_bbox(self.figure.bbox)

    def _cache_drag_background(self) -> None:
        """Render background (no selected space), capture pixel buffer, then
        immediately blit overlay so the screen never shows the intermediate state."""
        saved_xlim = self.ax.get_xlim()
        saved_ylim = self.ax.get_ylim()

        excluded = {self._edit_selected_space_id} if self._edit_selected_space_id else None
        draw_on_axes(self.ax, self.model, floor=self.floor_var.get(),
                     show_labels=False, excluded_space_ids=excluded,
                     show_nodes=self.show_nodes_var.get(),
                     bg_floor=self._bg_floor_from_combo())
        self._draw_background()
        self.ax.set_xlim(saved_xlim)
        self.ax.set_ylim(saved_ylim)

        # Re-attach animated vertex overlay artists (ax.clear() removed them).
        self.ax.add_line(self._ov_line)
        self.ax.add_artist(self._ov_verts)
        self.ax.add_artist(self._ov_sel_vert)
        self.ax.add_artist(self._ov_snap_ring)

        # Pre-populate artists before canvas.draw() so they are excluded from
        # the renderer (animated=True) but ready for the blit step below.
        self._prepare_overlay_artists()

        self.canvas.draw()  # renders to buffer; animated artists are skipped
        self._drag_bg_cache = self.canvas.copy_from_bbox(self.figure.bbox)

        # Immediately restore + blit overlay — one atomic screen update,
        # so user never sees the intermediate state (polygon missing).
        self.canvas.restore_region(self._drag_bg_cache)
        self.ax.draw_artist(self._ov_line)
        self.ax.draw_artist(self._ov_verts)
        self.ax.draw_artist(self._ov_sel_vert)
        self.ax.draw_artist(self._ov_snap_ring)
        self.canvas.blit(self.figure.bbox)

    def _fast_drag_redraw(self) -> None:
        """Blit-based drag redraw: restore cached background + draw overlay only."""
        if self._drag_bg_cache is None:
            return
        self._prepare_overlay_artists()
        self.canvas.restore_region(self._drag_bg_cache)
        self.ax.draw_artist(self._ov_line)
        self.ax.draw_artist(self._ov_verts)
        self.ax.draw_artist(self._ov_sel_vert)
        self.ax.draw_artist(self._ov_snap_ring)
        self.canvas.blit(self.figure.bbox)

    # ── Node drag blit ─────────────────────────────────────

    def _setup_node_drag_blit(self, node_id: str) -> None:
        """Render scene without the dragged node, capture buffer for blit."""
        saved_xlim = self.ax.get_xlim()
        saved_ylim = self.ax.get_ylim()
        excluded = {self._edit_selected_space_id} if self._edit_selected_space_id else None
        draw_on_axes(
            self.ax, self.model,
            floor=self.floor_var.get(),
            show_labels=self.label_var.get(),
            excluded_space_ids=excluded,
            show_nodes=self.show_nodes_var.get(),
            function_color_map=self._function_color_map or None,
            function_label_map=(dict(self._function_codes)
                                if self._function_codes else None),
            excluded_node_ids={node_id},
            bg_floor=self._bg_floor_from_combo(),
        )
        self._draw_background()
        self._draw_view_selection_overlay()
        self._draw_edit_overlay()
        # graph overlay: skip selected-node highlight so the old position isn't cached
        self._draw_graph_edit_overlay()
        self._draw_space_create_overlay()
        self._draw_door_overlay()
        if self._space_create_mode and self._space_create_preview_line is not None:
            self.ax.add_line(self._space_create_preview_line)
        self.ax.set_xlim(saved_xlim)
        self.ax.set_ylim(saved_ylim)
        # ax.clear() inside draw_on_axes detaches animated overlay artists from the figure.
        # Re-attach them so draw_artist() can find the figure during blit.
        self.ax.add_collection(self._ov_drag_edges)
        self.ax.add_artist(self._ov_drag_node)
        self.canvas.draw()
        self._node_drag_bg_cache = self.canvas.copy_from_bbox(self.figure.bbox)

    def _fast_node_drag_redraw(self, node_id: str, x: float, y: float) -> None:
        """Blit-based node drag: restore cached bg + draw node dot + connected edges."""
        if self._node_drag_bg_cache is None:
            self.refresh_view()
            return
        self._ov_drag_node.set_offsets([[x, y]])
        segs = []
        for edge in self.model.edges.values():
            if node_id in edge.connects and len(edge.coords) >= 2:
                segs.append([(p[0], p[1]) for p in edge.coords])
        self._ov_drag_edges.set_segments(segs)
        self.canvas.restore_region(self._node_drag_bg_cache)
        self.ax.draw_artist(self._ov_drag_edges)
        self.ax.draw_artist(self._ov_drag_node)
        self.canvas.blit(self.figure.bbox)

    def _find_snap_target(self, x: float, y: float) -> Optional[Tuple[float, float]]:
        """Return (sx, sy) from pre-built candidate list within snap threshold, or None."""
        thr2 = self._snap_threshold2 if self._snap_threshold2 > 0 else 1e-6
        best_dist2 = thr2
        best: Optional[Tuple[float, float]] = None
        for (vx, vy) in self._snap_candidates:
            d2 = (vx - x) * (vx - x) + (vy - y) * (vy - y)
            if d2 < best_dist2:
                best_dist2 = d2
                best = (vx, vy)
        return best

    def _find_space_at(self, xdata: float, ydata: float) -> Optional[str]:
        if self.model is None:
            return None
        floor = self.floor_var.get()
        if floor is None or floor == "All":
            visible_ids = set(self.model.spaces.keys())
        else:
            floor_ids = get_space_ids_on_floor(self.model, floor)
            visible_ids, _, _ = expand_floor_graph(self.model, floor_ids, floor=floor)
        # Layer filter
        active_lid = self._layer_id_from_combo()
        if active_lid:
            visible_ids = {s for s in visible_ids
                           if self.model.spaces[s].layer_id == active_lid}

        # Compute a small click-tolerance in data units (1% of view range)
        xlim = self.ax.get_xlim()
        ylim = self.ax.get_ylim()
        radius = min(abs(xlim[1] - xlim[0]), abs(ylim[1] - ylim[0])) * 0.01

        for sp_id in visible_ids:
            sp = self.model.spaces[sp_id]
            if not sp.polygons_3d:
                continue
            poly = sp.polygons_3d[0]
            # Deduplicate closing vertex
            pts = [(p[0], p[1]) for p in poly]
            if len(pts) > 1 and pts[0] == pts[-1]:
                pts = pts[:-1]
            if len(pts) < 3:
                continue
            # Build a properly closed MplPath with CLOSEPOLY
            codes = ([MplPath.MOVETO]
                     + [MplPath.LINETO] * (len(pts) - 1)
                     + [MplPath.CLOSEPOLY])
            path = MplPath(pts + [pts[0]], codes)
            if path.contains_point((xdata, ydata), radius=radius):
                return sp_id
        return None

    # ---- View-only selection -----------------------------------------------

    def _do_view_select(self, xdata: float, ydata: float) -> None:
        """Hit-test at (xdata, ydata) with no edit mode active. Update _view_selected_*."""
        # Any normal click clears the duplicate-id locate override so we
        # use model-dict coords from now on.
        self._view_selected_override_xy = None
        self._view_selected_override_poly = None
        # Priority: node > edge > space
        node_id = self._find_node_near_graph(xdata, ydata)
        if node_id:
            self._view_selected_type = "node"
            self._view_selected_id = node_id
            self._show_view_selection()
            self.refresh_view()
            return

        edge_id = self._find_edge_near_graph(xdata, ydata)
        if edge_id:
            self._view_selected_type = "edge"
            self._view_selected_id = edge_id
            self._show_view_selection()
            self.refresh_view()
            return

        sp_id = self._find_space_at(xdata, ydata)
        if sp_id:
            self._view_selected_type = "space"
            self._view_selected_id = sp_id
            self._show_view_selection()
            self.refresh_view()
            return

        # Clicked on empty area → deselect
        self._view_selected_type = None
        self._view_selected_id = None
        self._set_info_text("")
        self.refresh_view()

    def _show_view_selection(self) -> None:
        """Populate the Selection info panel for the currently selected item.

        While Cell Edit mode is active and a CellSpace is selected, the
        panel switches to an editable form (Name / Class / Function) so
        the user can change properties directly. Otherwise it shows the
        read-only Text summary.
        """
        if self._view_selected_type is None or self._view_selected_id is None or self.model is None:
            self._set_info_text("")
            self._teardown_edit_form()
            return

        # ── Editable form path: Cell Edit + CellSpace selected ──────
        if (self._view_selected_type == "space"
                and getattr(self, "_edit_mode", False)):
            sp = self.model.spaces.get(self._view_selected_id)
            if sp is not None:
                self._build_space_edit_form(sp)
                return
        # If we previously had the form up, take it down before showing
        # the read-only Text again.
        self._teardown_edit_form()

        lines: List[str] = []

        if self._view_selected_type == "space":
            sp = self.model.spaces.get(self._view_selected_id)
            if sp is None:
                self._set_info_text("")
                return
            lines.append(f"[CellSpace]")
            lines.append(f"ID:    {sp.feature_id}")
            lines.append(f"Name:  {sp.name or '—'}")
            lines.append(f"Class: {sp.cls or '—'}")
            lines.append(f"Level: {sp.level or '—'}")
            lines.append(f"Func:  {sp.function or '—'}")
            if sp.layer_id:
                lf = self.model.layers.get(sp.layer_id)
                layer_str = f"{sp.layer_id} ({lf.theme})" if lf else sp.layer_id
            else:
                layer_str = "—"
            lines.append(f"Layer: {layer_str}")
            if sp.duality:
                lines.append(f"Node:  {sp.duality}")
            if sp.centers:
                cx, cy, cz = sp.centers[0]
                lines.append(f"Center:")
                lines.append(f"  x={cx:.2f}")
                lines.append(f"  y={cy:.2f}")
                lines.append(f"  z={cz:.2f}")
            if sp.polygons_3d:
                poly = sp.polygons_3d[0]
                lines.append(f"Verts: {len(poly)}")

        elif self._view_selected_type == "node":
            node = self.model.nodes.get(self._view_selected_id)
            if node is None:
                self._set_info_text("")
                return
            lines.append(f"[Node]")
            lines.append(f"ID:   {node.node_id}")
            # Nodes themselves carry no `<name>` in IndoorGML; the
            # human-readable label is the dual CellSpace's name. Show it
            # so the Selection panel parity with CellSpace is complete.
            dual_name = ""
            if node.duality:
                dual_sp = self.model.spaces.get(node.duality)
                if dual_sp is not None:
                    dual_name = dual_sp.name or ""
            lines.append(f"Name: {dual_name or '—'}")
            if node.coord:
                lines.append(f"x={node.coord[0]:.2f}")
                lines.append(f"y={node.coord[1]:.2f}")
                lines.append(f"z={node.coord[2]:.2f}")
            lines.append(f"Level:{node.level or '—'}")
            if node.layer_id:
                lf = self.model.layers.get(node.layer_id)
                layer_str = f"{node.layer_id} ({lf.theme})" if lf else node.layer_id
            else:
                layer_str = "—"
            lines.append(f"Layer:{layer_str}")
            if node.duality:
                lines.append(f"Space:{node.duality}")
            if node.connects:
                lines.append(f"Connects ({len(node.connects)}):")
                for c in node.connects:
                    lines.append(f"  {c}")

        elif self._view_selected_type == "edge":
            edge = self.model.edges.get(self._view_selected_id)
            if edge is None:
                self._set_info_text("")
                return
            lines.append(f"[Edge]")
            lines.append(f"ID: {edge.edge_id}")
            if edge.layer_id:
                lf = self.model.layers.get(edge.layer_id)
                layer_str = f"{edge.layer_id} ({lf.theme})" if lf else edge.layer_id
            else:
                layer_str = "—"
            lines.append(f"Layer:{layer_str}")
            if edge.connects:
                lines.append(f"Connects:")
                for c in edge.connects:
                    lines.append(f"  {c}")
            if edge.coords and len(edge.coords) >= 2:
                x1, y1 = edge.coords[0][0], edge.coords[0][1]
                x2, y2 = edge.coords[-1][0], edge.coords[-1][1]
                length = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
                lines.append(f"Pt1:")
                lines.append(f"  x={x1:.2f}")
                lines.append(f"  y={y1:.2f}")
                lines.append(f"Pt2:")
                lines.append(f"  x={x2:.2f}")
                lines.append(f"  y={y2:.2f}")
                lines.append(f"Len: {length:.2f}")

        self._set_info_text("\n".join(lines))

    def _set_info_text(self, text: str) -> None:
        """Update the read-only info Text widget."""
        # Make sure the read-only Text is the visible widget.
        if (self._info_edit_frame is not None
                and self._info_edit_frame.winfo_exists()):
            self._info_edit_frame.pack_forget()
        if (hasattr(self, "_info_text_frame")
                and self._info_text_frame is not None):
            try:
                self._info_text_frame.pack_forget()
                self._info_text_frame.pack(anchor="w", fill="both",
                                           expand=True)
            except Exception:
                pass
        self._info_text.configure(state="normal")
        self._info_text.delete("1.0", "end")
        if text:
            self._info_text.insert("end", text)
        self._info_text.configure(state="disabled")

    def _teardown_edit_form(self) -> None:
        """Hide the editable form and restore the read-only Text view."""
        if (self._info_edit_frame is not None
                and self._info_edit_frame.winfo_exists()):
            try:
                self._info_edit_frame.destroy()
            except Exception:
                pass
        self._info_edit_frame = None
        # Re-show the Text widget container
        if (hasattr(self, "_info_text_frame")
                and self._info_text_frame is not None):
            try:
                self._info_text_frame.pack(anchor="w", fill="both",
                                           expand=True)
            except Exception:
                pass

    def _build_space_edit_form(self, sp: SpaceFeature) -> None:
        """Replace the read-only Text with editable widgets for `sp`.

        Shows: ID (read-only), Name, Class, Function (Combobox sourced
        from the loaded function-codes CSV), Level (read-only),
        Layer (read-only), POI (Checkbutton), Duality (read-only).

        Changes are committed by the "Apply" button (or Enter in Name /
        Class). The current cell's source_element is patched in place
        so a subsequent Save preserves the edits, AND the in-memory
        SpaceFeature is updated so the UI immediately reflects them.
        """
        # Hide read-only Text and remove any prior form
        if (hasattr(self, "_info_text_frame")
                and self._info_text_frame is not None):
            try:
                self._info_text_frame.pack_forget()
            except Exception:
                pass
        if (self._info_edit_frame is not None
                and self._info_edit_frame.winfo_exists()):
            try:
                self._info_edit_frame.destroy()
            except Exception:
                pass

        f = ttk.Frame(self._info_outer, style="TB.TFrame")
        f.pack(anchor="w", fill="both", expand=True)
        self._info_edit_frame = f

        small = ("TkDefaultFont", 9, "bold")
        ttk.Label(f, text="[CellSpace]", style="TB.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        # ID — read-only
        ttk.Label(f, text="ID:", style="TB.TLabel").grid(
            row=1, column=0, sticky="e", padx=(0, 4))
        ttk.Label(f, text=sp.feature_id, style="TB.TLabel").grid(
            row=1, column=1, sticky="w")
        # Name
        ttk.Label(f, text="Name:", style="TB.TLabel").grid(
            row=2, column=0, sticky="e", padx=(0, 4), pady=(4, 0))
        name_var = tk.StringVar(value=sp.name or "")
        name_entry = ttk.Entry(f, textvariable=name_var, width=22,
                               font=small)
        name_entry.grid(row=2, column=1, sticky="we", pady=(4, 0))
        # Class
        ttk.Label(f, text="Class:", style="TB.TLabel").grid(
            row=3, column=0, sticky="e", padx=(0, 4))
        cls_var = tk.StringVar(value=sp.cls or "")
        ttk.Combobox(f, textvariable=cls_var,
                     values=["CellSpace", "TransferSpace",
                             "ConnectionSpace", "AnchorSpace"],
                     width=20, font=small).grid(
            row=3, column=1, sticky="we")
        # Function — pull-down from loaded codes
        ttk.Label(f, text="Func:", style="TB.TLabel").grid(
            row=4, column=0, sticky="e", padx=(0, 4))
        func_var = tk.StringVar(value=sp.function or "")
        codes = [code for code, _ in self._function_codes] if \
            getattr(self, "_function_codes", None) else []
        # Make the Combobox readonly when codes are loaded so the user
        # can only pick one of the official function codes; otherwise
        # leave it free-form so they can still type a custom value.
        func_state = "readonly" if codes else "normal"
        # Show "code — label" for each entry while keeping the bare
        # code as the underlying value the user picks.
        if codes:
            label_for = {code: label
                         for code, label in self._function_codes}
            display_values = [(f"{c} — {label_for[c]}"
                               if label_for.get(c) and label_for[c] != c
                               else c)
                              for c in codes]
            # Determine the visible string corresponding to current value
            cur_disp = (
                f"{sp.function} — {label_for[sp.function]}"
                if (sp.function in label_for
                    and label_for[sp.function] != sp.function)
                else (sp.function or ""))
            disp_var = tk.StringVar(value=cur_disp)
            func_combo = ttk.Combobox(
                f, textvariable=disp_var, values=display_values,
                state=func_state, width=20, font=small)
            func_combo.grid(row=4, column=1, sticky="we")

            def _disp_to_code() -> str:
                v = disp_var.get().strip()
                if " — " in v:
                    return v.split(" — ", 1)[0]
                return v
            self._sel_func_get_code = _disp_to_code
        else:
            ttk.Combobox(f, textvariable=func_var, values=codes,
                         state=func_state, width=20, font=small).grid(
                row=4, column=1, sticky="we")
            self._sel_func_get_code = lambda: func_var.get().strip()

        # Level (read-only — the floor combo controls level)
        ttk.Label(f, text="Level:", style="TB.TLabel").grid(
            row=5, column=0, sticky="e", padx=(0, 4))
        ttk.Label(f, text=sp.level or "—", style="TB.TLabel").grid(
            row=5, column=1, sticky="w")
        # Layer (read-only)
        ttk.Label(f, text="Layer:", style="TB.TLabel").grid(
            row=6, column=0, sticky="e", padx=(0, 4))
        if sp.layer_id:
            lf = self.model.layers.get(sp.layer_id)
            layer_str = (f"{sp.layer_id} ({lf.theme})" if lf
                         else sp.layer_id)
        else:
            layer_str = "—"
        ttk.Label(f, text=layer_str, style="TB.TLabel").grid(
            row=6, column=1, sticky="w")
        # POI checkbox
        poi_var = tk.BooleanVar(value=getattr(sp, "is_poi", False))
        ttk.Checkbutton(f, text="POI", variable=poi_var,
                        style="TB.TCheckbutton").grid(
            row=7, column=1, sticky="w", pady=(4, 0))
        # Duality
        ttk.Label(f, text="Node:", style="TB.TLabel").grid(
            row=8, column=0, sticky="e", padx=(0, 4))
        ttk.Label(f, text=sp.duality or "—", style="TB.TLabel").grid(
            row=8, column=1, sticky="w")

        f.columnconfigure(1, weight=1)

        # Apply button — commits everything to the model + XML.
        def _apply():
            sp.name = name_var.get().strip()
            sp.cls = cls_var.get().strip() or "CellSpace"
            sp.function = self._sel_func_get_code()
            sp.is_poi = bool(poi_var.get())
            self._patch_cellspace_xml_props(sp)
            self.refresh_view()
            self.status_var.set(f"Updated CellSpace '{sp.feature_id}'.")

        ttk.Button(f, text="Apply", style="TB.TButton",
                   command=_apply).grid(
            row=9, column=0, columnspan=2, pady=(6, 2), sticky="we")
        # Enter on Name / Class also commits
        name_entry.bind("<Return>", lambda _e: _apply())

    def _patch_cellspace_xml_props(self, sp: SpaceFeature) -> None:
        """Update the existing CellSpace element to match `sp`.

        - `sp.name`     → `<cellSpaceName>` text
        - `sp.function` → `<function>` text (element removed when blank)
        - `sp.is_poi`   → `<poi>` text ("true" / "false")
        - `sp.cls`      → ELEMENT TAG itself. IndoorGML 2.0 carries the
                          subclass via the element local name
                          (CellSpace / TransferSpace / ConnectionSpace /
                          AnchorSpace), so a class change is a TAG
                          rename, not a child-element update.

        Newly-created cells (still in `_new_space_ids`) get the
        up-to-date values emitted via `_add_new_spaces_to_xml` on Save,
        so we can return early for those.
        """
        elem = sp.source_element
        if elem is None or self.model is None or self.model.source_tree is None:
            return                                # nothing to patch yet
        ns = elem.tag.split("}")[0][1:] if "}" in elem.tag else ""
        CORE = f"{{{ns}}}" if ns else ""

        # 1) Class change → rewrite the element's tag in place.
        cur_local = local_name(elem.tag)
        new_local = (sp.cls or "CellSpace").strip() or "CellSpace"
        if new_local != cur_local:
            elem.tag = f"{CORE}{new_local}" if ns else new_local
            # IndoorGML 2.0 wraps each space in either
            # `<cellSpaceMember>` (for CellSpace) or
            # `<transferSpaceMember>` etc.; don't try to retag the
            # wrapper here — the parser/SPACE_TAG_HINTS already accept
            # generic CellSpace-style elements regardless of wrapper.
            sp.cls = new_local

        def _get_or_make(tag_local: str) -> ET.Element:
            for ch in elem:
                if local_name(ch.tag) == tag_local:
                    return ch
            new = ET.SubElement(elem, f"{CORE}{tag_local}")
            return new

        # 2) Name → <cellSpaceName>
        nm = _get_or_make("cellSpaceName")
        nm.text = sp.name or ""
        # 3) Function → <function>
        if sp.function:
            fn = _get_or_make("function")
            fn.text = sp.function
        else:
            for ch in list(elem):
                if local_name(ch.tag) == "function":
                    elem.remove(ch)
        # 4) POI → <poi>
        poi = _get_or_make("poi")
        poi.text = "true" if getattr(sp, "is_poi", False) else "false"

    def _draw_view_selection_overlay(self) -> None:
        """Highlight the view-selected item on the canvas.

        The highlight uses a colour family (bright magenta + white) that
        does NOT clash with any other rendering colour in the editor —
        tomato (current-floor nodes), orange (downward vertical-edge
        endpoints), dodgerblue (down-link node), deepskyblue (regular
        edges), orchid / limegreen (vertical edges), yellow (measure /
        first-vertex markers), cyan (vertical-edge first node) — so the
        picked feature is always unambiguous, even on a busy floor.
        """
        if self._split_mode:
            return
        if (self._view_selected_type is None
                or self._view_selected_id is None
                or self.model is None):
            return

        # Pick a center point so we can draw a crosshair across the
        # current viewport, helping the user spot the highlight even on a
        # very zoomed-out view.
        cx = cy = None
        ovr_xy   = self._view_selected_override_xy
        ovr_poly = self._view_selected_override_poly

        if self._view_selected_type == "space":
            poly_xy: Optional[List[Tuple[float, float]]] = None
            if ovr_poly:
                poly_xy = list(ovr_poly)
            else:
                sp = self.model.spaces.get(self._view_selected_id)
                if sp and sp.polygons_3d:
                    poly_xy = [(p[0], p[1]) for p in sp.polygons_3d[0]]
            if poly_xy:
                xs = [p[0] for p in poly_xy]
                ys = [p[1] for p in poly_xy]
                if poly_xy and poly_xy[0] != poly_xy[-1]:
                    xs.append(poly_xy[0][0])
                    ys.append(poly_xy[0][1])
                self.ax.fill(xs, ys, color="magenta", alpha=0.35, zorder=20)
                self.ax.plot(xs, ys, color="white",   linewidth=4.5, zorder=21)
                self.ax.plot(xs, ys, color="magenta", linewidth=2.5, zorder=22)
                cx = sum(xs) / len(xs)
                cy = sum(ys) / len(ys)

        elif self._view_selected_type == "node":
            xy: Optional[Tuple[float, float]] = ovr_xy
            if xy is None:
                node = self.model.nodes.get(self._view_selected_id)
                if node and node.coord:
                    xy = (node.coord[0], node.coord[1])
            if xy is not None:
                cx, cy = xy
                self.ax.scatter([cx], [cy], s=380, color="white",
                                zorder=20, alpha=0.95)
                self.ax.scatter([cx], [cy], s=320,
                                facecolors="none", edgecolors="magenta",
                                linewidths=3.0, zorder=21)
                self.ax.scatter([cx], [cy], s=120, color="magenta",
                                edgecolors="white", linewidths=1.5,
                                zorder=22)

        elif self._view_selected_type == "edge":
            line_xy: Optional[List[Tuple[float, float]]] = None
            if ovr_poly:
                line_xy = list(ovr_poly)
            else:
                edge = self.model.edges.get(self._view_selected_id)
                if edge and len(edge.coords) >= 2:
                    line_xy = [(p[0], p[1]) for p in edge.coords]
            if line_xy and len(line_xy) >= 2:
                xs = [p[0] for p in line_xy]
                ys = [p[1] for p in line_xy]
                self.ax.plot(xs, ys, color="white",   linewidth=6.0,
                             zorder=20, alpha=0.95)
                self.ax.plot(xs, ys, color="magenta", linewidth=3.0,
                             zorder=21)
                cx = sum(xs) / len(xs)
                cy = sum(ys) / len(ys)

        # Crosshair lines spanning the current viewport — make the
        # selected feature trivially findable at any zoom level.
        if cx is not None and cy is not None:
            xlim = self.ax.get_xlim()
            ylim = self.ax.get_ylim()
            self.ax.plot([xlim[0], xlim[1]], [cy, cy],
                         color="magenta", linewidth=0.6, alpha=0.45,
                         linestyle=":", zorder=19)
            self.ax.plot([cx, cx], [ylim[0], ylim[1]],
                         color="magenta", linewidth=0.6, alpha=0.45,
                         linestyle=":", zorder=19)

    # ---- End view-only selection -------------------------------------------

    def _find_edge_near(self, xdata: float, ydata: float) -> Optional[Tuple[int, int, float, float]]:
        """Return (poly_idx, insert_idx, proj_x, proj_y) if click is near a polygon edge, else None.
        insert_idx is the position at which the new vertex should be inserted in the polygon list.
        Does NOT match if the click is within VERTEX_PIXEL_THRESHOLD of an existing vertex.
        """
        if self.model is None:
            return None
        if self._edit_selected_space_id is None:
            return None
        sp = self.model.spaces.get(self._edit_selected_space_id)
        if sp is None or not sp.polygons_3d:
            return None

        EDGE_PIXEL_THRESHOLD = 10.0
        VERTEX_PIXEL_THRESHOLD = 30.0  # if click is this close to a vertex, don't insert
        click_px = self.ax.transData.transform((xdata, ydata))

        poly_idx = self._edit_selected_poly_idx
        if poly_idx >= len(sp.polygons_3d):
            return None
        poly = sp.polygons_3d[poly_idx]

        # If click is close to an existing vertex, don't offer edge insertion
        for (x, y, z) in poly:
            vert_px = self.ax.transData.transform((x, y))
            d = ((vert_px[0] - click_px[0]) ** 2 + (vert_px[1] - click_px[1]) ** 2) ** 0.5
            if d < VERTEX_PIXEL_THRESHOLD:
                return None

        # Work with unique vertices (strip closing duplicate)
        is_closed = len(poly) > 1 and poly[0][:2] == poly[-1][:2]
        n = len(poly) - 1 if is_closed else len(poly)
        if n < 2:
            return None

        best_dist_px = EDGE_PIXEL_THRESHOLD
        best = None

        for i in range(n):
            x1, y1 = poly[i][0], poly[i][1]
            x2, y2 = poly[(i + 1) % n][0], poly[(i + 1) % n][1]

            dx, dy = x2 - x1, y2 - y1
            seg_len2 = dx * dx + dy * dy
            if seg_len2 < 1e-12:
                continue

            t = ((xdata - x1) * dx + (ydata - y1) * dy) / seg_len2
            t = max(0.05, min(0.95, t))
            px = x1 + t * dx
            py = y1 + t * dy

            proj_px = self.ax.transData.transform((px, py))
            dist_px = ((proj_px[0] - click_px[0]) ** 2 + (proj_px[1] - click_px[1]) ** 2) ** 0.5
            if dist_px < best_dist_px:
                best_dist_px = dist_px
                if is_closed and i == n - 1:
                    insert_idx = len(poly) - 1
                else:
                    insert_idx = i + 1
                best = (poly_idx, insert_idx, px, py)

        return best

    def _insert_vertex_on_edge(self, poly_idx: int, insert_idx: int, nx: float, ny: float) -> None:
        """Insert a new vertex at (nx, ny) into the polygon at insert_idx."""
        sp = self.model.spaces.get(self._edit_selected_space_id)
        if sp is None or poly_idx >= len(sp.polygons_3d):
            return
        poly = sp.polygons_3d[poly_idx]

        # Push undo snapshot before insertion
        self._push_undo({
            "kind": "insert_vertex",
            "space_id": self._edit_selected_space_id,
            "poly_idx": poly_idx,
            "old_poly": list(poly),
        })

        # Interpolate z from adjacent vertices
        prev_idx = insert_idx - 1
        z = poly[prev_idx][2] if 0 <= prev_idx < len(poly) else 0.0

        poly.insert(insert_idx, (nx, ny, z))

        sp.centers = [c for c in [polygon_centroid(p) for p in sp.polygons_3d] if c is not None]
        self._edit_selected_vertex_idx = insert_idx
        self.edit_status_var.set(
            f"Vertex inserted at ({nx:.3f}, {ny:.3f}) — drag to reposition."
        )
        self.refresh_view()

    def _find_vertex_near(self, xdata: float, ydata: float,
                          search_all: bool = False) -> Optional[Tuple[int, int]]:
        """Return (poly_idx, vertex_idx) of nearest vertex within pixel threshold.

        If search_all=True, searches all visible spaces and sets _edit_selected_space_id
        to the space that owns the found vertex.
        """
        if self.model is None:
            return None
        PIXEL_THRESHOLD = 30.0
        click_px = self.ax.transData.transform((xdata, ydata))

        if search_all:
            floor = self.floor_var.get()
            if floor is None or floor == "All":
                candidate_ids = list(self.model.spaces.keys())
            else:
                candidate_ids = list(get_space_ids_on_floor(self.model, floor))
            # Layer filter
            active_lid = self._layer_id_from_combo()
            if active_lid:
                candidate_ids = [s for s in candidate_ids
                                 if self.model.spaces[s].layer_id == active_lid]
        else:
            if self._edit_selected_space_id is None:
                return None
            candidate_ids = [self._edit_selected_space_id]

        best_dist_px = PIXEL_THRESHOLD
        best: Optional[Tuple[int, int]] = None
        best_sp_id: Optional[str] = None

        for sp_id in candidate_ids:
            sp = self.model.spaces.get(sp_id)
            if sp is None or not sp.polygons_3d:
                continue
            poly_idx = 0
            poly = sp.polygons_3d[poly_idx]
            for vi, (x, y, z) in enumerate(poly):
                vert_px = self.ax.transData.transform((x, y))
                dist_px = ((vert_px[0] - click_px[0]) ** 2 + (vert_px[1] - click_px[1]) ** 2) ** 0.5
                if dist_px < best_dist_px:
                    best_dist_px = dist_px
                    best = (poly_idx, vi)
                    best_sp_id = sp_id

        if best is not None and best_sp_id is not None and search_all:
            self._edit_selected_space_id = best_sp_id
            self._edit_selected_poly_idx = best[0]
            self._update_edit_space_info(best_sp_id)

        return best

    def _draw_edit_overlay(self) -> None:
        if not self._edit_mode:
            return

        # Show vertex dots for ALL visible spaces so user can see clickable points
        floor = self.floor_var.get()
        if floor is None or floor == "All":
            visible_space_ids = set(self.model.spaces.keys())
        else:
            visible_space_ids = get_space_ids_on_floor(self.model, floor)

        all_vx: List[float] = []
        all_vy: List[float] = []
        for sp_id, sp in self.model.spaces.items():
            if sp_id not in visible_space_ids:
                continue
            if sp_id == self._edit_selected_space_id:
                continue  # selected space drawn separately below
            if sp.polygons_3d:
                poly = sp.polygons_3d[0]
                all_vx.extend(p[0] for p in poly)
                all_vy.extend(p[1] for p in poly)
        if all_vx:
            self.ax.scatter(all_vx, all_vy, color="deepskyblue", s=30, zorder=5, alpha=0.7)

        # Selected space highlight
        if self._edit_selected_space_id is None:
            return
        sp = self.model.spaces.get(self._edit_selected_space_id)
        if sp is None or not sp.polygons_3d:
            return

        poly_idx = self._edit_selected_poly_idx
        if poly_idx >= len(sp.polygons_3d):
            return
        poly = sp.polygons_3d[poly_idx]
        if not poly:
            return

        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        if xs[0] != xs[-1] or ys[0] != ys[-1]:
            xs.append(xs[0])
            ys.append(ys[0])
        self.ax.fill(xs, ys, color="yellow", alpha=0.3, zorder=2)
        self.ax.plot(xs, ys, color="orange", linewidth=2.0, zorder=3)

        vxs = [p[0] for p in poly]
        vys = [p[1] for p in poly]
        self.ax.scatter(vxs, vys, color="deepskyblue", s=50, zorder=6)

        if self._edit_selected_vertex_idx is not None and self._edit_selected_vertex_idx < len(poly):
            vx, vy, vz = poly[self._edit_selected_vertex_idx]
            # Large semi-transparent red circle: signals "selected, click again to drag"
            self.ax.scatter([vx], [vy], color="red", s=150, zorder=7, alpha=0.45)
            # Small opaque center dot so position is clearly visible
            self.ax.scatter([vx], [vy], color="red", s=20, zorder=8, alpha=1.0)
            if self._snap_point:
                sx, sy = self._snap_point
                self.ax.scatter([sx], [sy], color="green", s=90, zorder=9,
                                marker="o", facecolors="none", linewidths=2)
                self.edit_status_var.set(
                    f"SNAP → ({sx:.3f}, {sy:.3f})"
                )
            else:
                self.edit_status_var.set(
                    f"Vertex {self._edit_selected_vertex_idx} selected ({vx:.3f}, {vy:.3f}) "
                    f"— click again to drag, Delete key to delete"
                )


    # -------------------------------------------------------
    # Graph edit helpers
    # -------------------------------------------------------
    def toggle_graph_edit_mode(self) -> None:
        self._clear_view_selection()
        was_active = self._graph_edit_mode
        self._deactivate_all_edit_modes()
        if not was_active:
            self._graph_edit_mode = True
            self._graph_edit_mode_var.set(True)
            self._graph_edit_status_var.set(
                "Left-click: select/create/move node or edge | Delete key: delete selected")
        self.refresh_view()

    def toggle_graph_connect_mode(self) -> None:
        self._clear_view_selection()
        was_active = self._graph_connect_mode
        self._deactivate_all_edit_modes()
        if not was_active:
            self._graph_connect_mode = True
            self._graph_connect_mode_var.set(True)
            self._graph_edit_status_var.set("Click first node, then second node to create edge.")
        self.refresh_view()

    def toggle_door_mode(self) -> None:
        self._clear_view_selection()
        was_active = self._door_mode
        self._deactivate_all_edit_modes()
        if not was_active:
            self._door_mode = True
            self._door_mode_var.set(True)
            self.status_var.set("Door mode: click first endpoint of door.")
        self.refresh_view()

    def toggle_space_create_mode(self) -> None:
        self._clear_view_selection()
        was_active = self._space_create_mode
        self._deactivate_all_edit_modes()
        if not was_active:
            self._space_create_mode = True
            self._space_create_mode_var.set(True)
            self._graph_edit_status_var.set(
                "Left-click: add vertex | Double-click: finish | Right-click: cancel")
        self.refresh_view()

    def toggle_rect_create_mode(self) -> None:
        """Toggle Rectangle-shortcut variant of New Cell.

        Workflow: click TWO opposite corners — the editor builds a
        4-vertex axis-aligned polygon between them and runs the same
        save / dialog flow as the free-style polygon mode (so the cell
        is stored exactly as a 4-point polygon in the IndoorGML XML).
        """
        self._clear_view_selection()
        was_active = self._rect_create_mode
        self._deactivate_all_edit_modes()
        if not was_active:
            self._rect_create_mode = True
            self._rect_create_mode_var.set(True)
            self._rect_create_pt1 = None
            self._graph_edit_status_var.set(
                "Rectangle: click first corner. (Right-click cancels.)")
        self.refresh_view()

    def _find_vertex_near_split_space(self, xdata: float, ydata: float):
        """Return (vertex_index, (vx, vy)) of nearest polygon vertex in the split space, or None."""
        if self.model is None:
            return None
        if not self._split_space_id:
            return None
        sp = self.model.spaces.get(self._split_space_id)
        if not sp or not sp.polygons_3d:
            return None
        pts = _poly_open([(p[0], p[1]) for p in sp.polygons_3d[0]])
        click_px = self.ax.transData.transform((xdata, ydata))
        best_dist = float("inf")
        best_idx = None
        for i, (vx, vy) in enumerate(pts):
            vpx = self.ax.transData.transform((vx, vy))
            d = math.sqrt((vpx[0]-click_px[0])**2 + (vpx[1]-click_px[1])**2)
            if d < best_dist:
                best_dist = d
                best_idx = i
        if best_idx is None:
            return None
        return best_idx, pts[best_idx]

    def toggle_split_mode(self) -> None:
        self._clear_view_selection()
        was_active = self._split_mode
        self._deactivate_all_edit_modes()
        if not was_active:
            self._split_mode = True
            self._split_mode_var.set(True)
            self.edit_status_var.set("Cell Split: click inside a cell, then click a second point to split.")
        self.canvas.draw_idle()

    def _do_cell_split(self, sp_id: str, vi: int, vj: int) -> None:
        """Split a CellSpace along the diagonal from vertex vi to vertex vj."""
        sp = self.model.spaces.get(sp_id)
        if sp is None or not sp.polygons_3d:
            messagebox.showerror("Split Error", "Cell not found.")
            return

        poly_3d = sp.polygons_3d[0]
        poly_2d = [(p[0], p[1]) for p in poly_3d]
        floor_z = poly_3d[0][2] if poly_3d else 0.0
        _, ceiling_z = _get_floor_ceiling_z(self.model, sp.level or "")

        result = _split_polygon_by_vertices(poly_2d, vi, vj)
        if result is None:
            messagebox.showerror("Split Error",
                                 "Cannot split this cell with the selected vertices.\n"
                                 "The two vertices must produce polygons with at least 3 vertices each.")
            return
        sub_a, sub_b = result

        base_name = sp.name or sp_id
        level = sp.level or (self.floor_var.get() if self.floor_var.get() != "All" else "")

        # Save original state for undo BEFORE modifying
        orig_polygons_3d = [list(p) for p in sp.polygons_3d]
        orig_centers = list(sp.centers)
        orig_name = sp.name
        orig_dual_node_id = sp.duality or None
        orig_dual_node_coord = (
            self.model.nodes[orig_dual_node_id].coord
            if orig_dual_node_id and orig_dual_node_id in self.model.nodes else None
        )
        orig_was_new = sp_id in self._new_space_ids

        # Sub-A: update original space IN PLACE (keep sp_id and original dual node unchanged)
        sub_a_3d = [(x, y, floor_z) for x, y in sub_a]
        sub_a_ceil = [(x, y, ceiling_z) for x, y in sub_a]
        center_a = polygon_centroid(sub_a_3d)
        sp.polygons_3d = [sub_a_3d, sub_a_ceil]
        sp.centers = [center_a] if center_a else []
        sp.name = base_name
        # Move original dual node to centroid of sub-A so it stays inside
        if orig_dual_node_id and orig_dual_node_id in self.model.nodes and center_a:
            node_a = self.model.nodes[orig_dual_node_id]
            node_a.coord = (center_a[0], center_a[1], node_a.coord[2] if node_a.coord else 0.0)

        # Sub-B: new space with ID = sp_id + "_1" (avoid collisions)
        sid2 = sp_id + "_1"
        while sid2 in self.model.spaces:
            sid2 = sid2 + "_1"
        sub_b_3d = [(x, y, floor_z) for x, y in sub_b]
        sub_b_ceil = [(x, y, ceiling_z) for x, y in sub_b]
        center_b = polygon_centroid(sub_b_3d)
        sp2 = SpaceFeature(
            feature_id=sid2, name=f"{base_name}_1",
            cls=sp.cls or "CellSpace", level=level, function=sp.function,
            polygons_3d=[sub_b_3d, sub_b_ceil],
            centers=[center_b] if center_b else [],
            layer_id=sp.layer_id,          # inherit layer from original space
            source_element=None,
        )
        self.model.spaces[sid2] = sp2
        self._new_space_ids.add(sid2)

        # New duality node for sub-B: ID = orig_dual_node_id + "_1" or sp_id + "_1"
        base_nid = orig_dual_node_id if orig_dual_node_id else sp_id
        nid2 = base_nid + "_1"
        while nid2 in self.model.nodes:
            nid2 = nid2 + "_1"
        if center_b:
            node_z = 0.0
            if orig_dual_node_id and orig_dual_node_id in self.model.nodes:
                node_z = self.model.nodes[orig_dual_node_id].coord[2] if self.model.nodes[orig_dual_node_id].coord else 0.0
            new_node = NodeFeature(
                node_id=nid2,
                coord=(center_b[0], center_b[1], node_z),
                level=level,
                duality=sid2,
                layer_id=sp.layer_id,      # inherit layer from original space
                source_element=None,
            )
            self.model.nodes[nid2] = new_node
            self._new_node_ids.add(nid2)
            sp2.duality = nid2
        else:
            nid2 = None

        # Edge between original dual node and new node (only if both exist)
        new_edge_id = self._create_edge(orig_dual_node_id, nid2) if (orig_dual_node_id and nid2) else None

        self._push_undo({
            "kind": "split_space",
            "orig_space_id": sp_id,
            "orig_polygons_3d": orig_polygons_3d,
            "orig_centers": orig_centers,
            "orig_name": orig_name,
            "orig_was_new": orig_was_new,
            "orig_dual_node_id": orig_dual_node_id,
            "orig_dual_node_coord": orig_dual_node_coord,
            "new_space_id": sid2,
            "new_node_id": nid2,
            "new_edge_id": new_edge_id,
        })

        self.edit_status_var.set(
            f"Split '{orig_name}' → '{sp.name}' (kept) + '{sp2.name}' (new {sid2})")
        # After the split, clear all selection state completely — no cell should appear
        # selected/highlighted, since both new cells are separate entities now.
        self._clear_view_selection()
        self._edit_selected_space_id = None
        self._edit_selected_vertex_idx = None
        self._edit_mode = False
        self._edit_mode_var.set(False)
        self.refresh_view()

    def _update_space_create_preview(self, cursor_pos: Optional[Tuple[float, float]] = None) -> None:
        """Redraw the in-progress polygon preview line."""
        if self._space_create_preview_line is None:
            return
        verts = self._space_create_verts
        if not verts:
            self._space_create_preview_line.set_data([], [])
            self.canvas.draw_idle()
            return
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        if cursor_pos:
            xs.append(cursor_pos[0])
            ys.append(cursor_pos[1])
        # Close preview back to start
        xs.append(verts[0][0])
        ys.append(verts[0][1])
        self._space_create_preview_line.set_data(xs, ys)
        self.canvas.draw_idle()

    def _finish_space_creation(self) -> None:
        """Complete polygon, prompt for metadata, create the space."""
        verts = self._space_create_verts
        if len(verts) < 3:
            self.status_var.set("Need at least 3 vertices to create a space.")
            self._space_create_verts.clear()
            if self._space_create_preview_line is not None:
                self._space_create_preview_line.set_data([], [])
            self.canvas.draw_idle()
            return

        # Level is taken directly from the floor selector (no dialog needed)
        floor = self.floor_var.get()
        if floor == "All":
            floor = ""

        dlg = tk.Toplevel(self.root)
        dlg.title("New CellSpace")
        dlg.resizable(False, False)
        dlg.grab_set()

        ttk.Label(dlg, text="Name:").grid(row=0, column=0, padx=8, pady=8, sticky="e")
        name_var = tk.StringVar(value="")
        ttk.Entry(dlg, textvariable=name_var, width=30).grid(row=0, column=1, padx=8, pady=8)

        confirmed = [False]
        def on_ok():
            confirmed[0] = True
            dlg.destroy()
        def on_cancel():
            dlg.destroy()

        btn_frame = ttk.Frame(dlg)
        btn_frame.grid(row=1, column=0, columnspan=2, pady=8)
        ttk.Button(btn_frame, text="OK", command=on_ok).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Cancel", command=on_cancel).pack(side="left", padx=4)
        dlg.wait_window()

        snapshot = list(verts)
        # Remove closing duplicate vertex if first == last
        if len(snapshot) >= 2:
            x0, y0 = snapshot[0]
            xl, yl = snapshot[-1]
            px0 = self.ax.transData.transform((x0, y0))
            pxl = self.ax.transData.transform((xl, yl))
            dist_px = ((px0[0]-pxl[0])**2 + (px0[1]-pxl[1])**2) ** 0.5
            if dist_px < 10:
                snapshot = snapshot[:-1]
        self._space_create_verts.clear()
        if self._space_create_preview_line is not None:
            self._space_create_preview_line.set_data([], [])

        if not confirmed[0]:
            self.canvas.draw_idle()
            return

        name = name_var.get().strip()
        level = floor  # use floor selector value directly
        sid = self._create_space(snapshot, name, level)

        # Auto-create dual node at centroid — use the same numeric suffix as the space
        sp = self.model.spaces[sid]
        nid = None
        if sp.polygons_3d:
            centroid = polygon_centroid(sp.polygons_3d[0])
            if centroid:
                cx, cy, cz = centroid
                # Derive node number from the space id (e.g. "space_new_15" → 15)
                existing_nodes = set(self.model.nodes.keys())
                suffix = sid.split("_")[-1] if "_" in sid else "1"
                candidate = f"node_new_{suffix}"
                if candidate in existing_nodes:
                    # Fallback: find next free number
                    i = 1
                    while f"node_new_{i}" in existing_nodes:
                        i += 1
                    candidate = f"node_new_{i}"
                nid = candidate
                self.model.nodes[nid] = NodeFeature(
                    node_id=nid, coord=(cx, cy, cz), level=level or None,
                    duality=sid,
                    layer_id=self._layer_id_from_combo() or "",
                )
                self._new_node_ids.add(nid)
                sp.duality = nid

        # Single undo action covering both space and its dual node
        self._push_undo({"kind": "create_space_with_node",
                         "space_id": sid, "node_id": nid})

        msg = f"Created space {sid}"
        msg += f" with dual node {nid}." if nid else "."
        self.status_var.set(msg)
        self.refresh_view()

    def _create_space(self, verts_2d: List[Tuple[float, float]], name: str, level: str) -> str:
        """Create a new SpaceFeature from a 2D polygon and add to model."""
        existing = set(self.model.spaces.keys())
        i = 1
        while f"space_new_{i}" in existing:
            i += 1
        sid = f"space_new_{i}"

        floor_z, ceiling_z = self._floor_ceiling_z(level)
        floor_pts = [(x, y, floor_z) for x, y in verts_2d]
        ceil_pts  = [(x, y, ceiling_z) for x, y in verts_2d]

        center = polygon_centroid(floor_pts)
        sp = SpaceFeature(
            feature_id=sid,
            name=name,
            cls="CellSpace",
            level=level,
            polygons_3d=[floor_pts, ceil_pts],
            centers=[center] if center else [],
            layer_id=self._layer_id_from_combo() or "",
        )
        self.model.spaces[sid] = sp
        self._new_space_ids.add(sid)
        return sid

    def _find_node_near_graph(self, xdata: float, ydata: float) -> Optional[str]:
        if self.model is None:
            return None
        floor = self.floor_var.get()
        if floor is None or floor == "All":
            visible_ids = set(self.model.nodes.keys())
        else:
            floor_space_ids = get_space_ids_on_floor(self.model, floor)
            _, visible_ids, _ = expand_floor_graph(self.model, floor_space_ids, floor=floor)
            # Include session-new nodes too — but ONLY those that belong
            # to the active floor. Force-including every new node would
            # let the user's click on the canvas pick up an invisible
            # node from a different floor and accidentally create a
            # vertical edge spanning two floors (e.g. edge_new_188:
            # node_new_76(4F) ↔ node_new_77(3F)).
            for nid in self._new_node_ids:
                n = self.model.nodes.get(nid)
                if n is None:
                    continue
                if n.level == floor:
                    visible_ids.add(nid)
        # Layer filter
        active_lid = self._layer_id_from_combo()
        if active_lid:
            visible_ids = {n for n in visible_ids
                           if self.model.nodes.get(n) and
                           self.model.nodes[n].layer_id == active_lid}

        # Suppress nodes on floors strictly above the active floor — they
        # are hidden in draw_on_axes (vertical-edge filter), so they must
        # not be hit-testable either.
        if floor and floor != "All":
            cur_floor_z: Optional[float] = None
            for s_id in floor_space_ids:
                sp = self.model.spaces.get(s_id)
                if sp is not None and sp.polygons_3d:
                    cur_floor_z = sp.polygons_3d[0][0][2]
                    break
            if cur_floor_z is not None:
                kept: Set[str] = set()
                for nid in visible_ids:
                    n = self.model.nodes.get(nid)
                    if n is None or n.coord is None:
                        kept.add(nid)
                        continue
                    if (n.level == floor) or n.coord[2] <= cur_floor_z:
                        kept.add(nid)
                visible_ids = kept

        # Use pixel-space threshold (20px) — independent of zoom level
        PIXEL_THRESHOLD = 30.0
        click_px = self.ax.transData.transform((xdata, ydata))

        best_dist_px = PIXEL_THRESHOLD
        best: Optional[str] = None
        for node_id in visible_ids:
            node = self.model.nodes.get(node_id)
            if node is None or node.coord is None:
                continue
            node_px = self.ax.transData.transform((node.coord[0], node.coord[1]))
            dist_px = ((node_px[0] - click_px[0]) ** 2 + (node_px[1] - click_px[1]) ** 2) ** 0.5
            if dist_px < best_dist_px:
                best_dist_px = dist_px
                best = node_id
        return best

    def _find_edge_near_graph(self, xdata: float, ydata: float) -> Optional[str]:
        if self.model is None:
            return None
        floor = self.floor_var.get()
        if floor is None or floor == "All":
            visible_ids = set(self.model.edges.keys())
        else:
            floor_space_ids = get_space_ids_on_floor(self.model, floor)
            _, _, visible_ids = expand_floor_graph(self.model, floor_space_ids, floor=floor)
        # Layer filter
        active_lid = self._layer_id_from_combo()
        if active_lid:
            visible_ids = {e for e in visible_ids
                           if self.model.edges.get(e) and
                           self.model.edges[e].layer_id == active_lid}

        xlim = self.ax.get_xlim()
        ylim = self.ax.get_ylim()
        threshold = min(abs(xlim[1] - xlim[0]), abs(ylim[1] - ylim[0])) * 0.02

        best_dist = threshold
        best: Optional[str] = None
        for edge_id in visible_ids:
            edge = self.model.edges.get(edge_id)
            if edge is None or len(edge.coords) < 2:
                continue
            for i in range(len(edge.coords) - 1):
                x1, y1 = edge.coords[i][0], edge.coords[i][1]
                x2, y2 = edge.coords[i + 1][0], edge.coords[i + 1][1]
                dx, dy = x2 - x1, y2 - y1
                seg_len2 = dx * dx + dy * dy
                if seg_len2 < 1e-12:
                    continue
                t = ((xdata - x1) * dx + (ydata - y1) * dy) / seg_len2
                t = max(0.0, min(1.0, t))
                px = x1 + t * dx
                py = y1 + t * dy
                dist = ((xdata - px) ** 2 + (ydata - py) ** 2) ** 0.5
                if dist < best_dist:
                    best_dist = dist
                    best = edge_id
        return best

    def _create_node(self, x: float, y: float) -> str:
        # Collect ids in BOTH the in-memory model AND the source XML tree
        # so we don't reuse an id that was previously saved + then undone
        # (the model dropped it but the XML element still exists, and
        # picking the same id would yield two Node elements with the same
        # id on next save).
        existing = set(self.model.nodes.keys())
        if self.model.source_tree is not None:
            for elem in self.model.source_tree.getroot().iter():
                if local_name(elem.tag) == "Node":
                    for k, v in elem.attrib.items():
                        if local_name(k) == "id":
                            existing.add(v)
                            break
        i = 1
        while f"node_new_{i}" in existing:
            i += 1
        nid = f"node_new_{i}"
        current_floor = self.floor_var.get()
        level = None if current_floor == "All" else current_floor
        # Derive Z from the *CellSpaces* on this floor, NOT from any node.
        # Reason: expand_floor_graph() pulls in off-floor vertical-edge
        # endpoints; picking z_vals[0] from that set could grab the Z of a
        # node on a different floor and silently leave the new node with
        # `level=current_floor` but `z=other-floor-z`. CellSpace polygons
        # on a floor always share the same Z, so they are the authority.
        z = 0.0
        if level:
            for sp in self.model.spaces.values():
                if sp.level == level and sp.polygons_3d:
                    z = sp.polygons_3d[0][0][2]
                    break
            else:
                # No space exists on this floor yet (newly created floor).
                fz = self._floor_z_overrides.get(level)
                if fz is not None:
                    z = fz[0]
                else:
                    # Last-resort fallback: any node's Z (legacy behaviour).
                    all_z = [n.coord[2] for n in self.model.nodes.values()
                             if n.coord is not None]
                    z = all_z[0] if all_z else 0.0
        else:
            z_vals = [n.coord[2] for n in self.model.nodes.values()
                      if n.coord is not None]
            z = z_vals[0] if z_vals else 0.0
        self.model.nodes[nid] = NodeFeature(
            node_id=nid, coord=(x, y, z), level=level,
            layer_id=self._layer_id_from_combo() or "",
        )
        self._new_node_ids.add(nid)

        # IndoorGML 2.0 requires every Node to have a dual CellSpace. Newly
        # created standalone nodes have no geometry-backed CellSpace, so spin
        # up a virtual one and link it via duality.
        self._create_virtual_space_for_node(nid)
        return nid

    def _create_virtual_space_for_node(self, nid: str) -> Optional[str]:
        """Create a virtual (geometry-less) CellSpace dual to the given node.

        Returns the new space id, or None if the node already has a duality
        or is missing from the model.
        """
        node = self.model.nodes.get(nid) if self.model else None
        if node is None:
            return None
        if node.duality and node.duality in self.model.spaces:
            return None  # already has a dual CellSpace
        vs_id = f"VS_{nid}"
        if vs_id in self.model.spaces:
            node.duality = vs_id
            return vs_id
        vs = SpaceFeature(
            feature_id=vs_id,
            name=f"Virtual_{nid}",
            cls="",
            level=node.level,
            polygons_3d=[],                              # no geometry
            centers=[node.coord] if node.coord else [],
            duality=nid,
            layer_id=node.layer_id or "",
            is_poi=False,
            is_virtual=True,
        )
        self.model.spaces[vs_id] = vs
        self._new_space_ids.add(vs_id)
        node.duality = vs_id
        return vs_id

    def _delete_node(self, node_id: str) -> None:
        edges_to_del = [eid for eid, e in self.model.edges.items() if node_id in e.connects]
        for eid in edges_to_del:
            self.model.edges.pop(eid, None)
            self._new_edge_ids.discard(eid)
        self.model.nodes.pop(node_id, None)
        self._new_node_ids.discard(node_id)
        if self._graph_selected_node == node_id:
            self._graph_selected_node = None
        if self._graph_connect_first == node_id:
            self._graph_connect_first = None
        if self._graph_drag_node == node_id:
            self._graph_drag_node = None

    def _create_edge(self, node1_id: str, node2_id: str,
                     allow_cross_floor: bool = False) -> Optional[str]:
        """Create an Edge between two existing nodes.

        Returns the new edge id, or None if the edge already exists.

        Cross-floor guard: by default this function REFUSES to connect
        two nodes whose levels differ (or whose Z values differ by more
        than 50 mm) — even if the caller computed otherwise — and pops
        an error dialog instead. This stops a bad hit-test from
        silently creating a vertical edge that the user never intended.
        Vertical Edge mode legitimately needs cross-floor edges and
        passes ``allow_cross_floor=True`` to opt out of the guard.
        """
        for edge in self.model.edges.values():
            conns = set(edge.connects)
            if node1_id in conns and node2_id in conns:
                return None

        n1 = self.model.nodes.get(node1_id)
        n2 = self.model.nodes.get(node2_id)

        # ---- Cross-floor safety guard --------------------------------
        if not allow_cross_floor and n1 is not None and n2 is not None:
            same_level = (n1.level == n2.level
                          and n1.level is not None)
            same_z = False
            if n1.coord is not None and n2.coord is not None:
                same_z = abs(n1.coord[2] - n2.coord[2]) < 50.0
            if not (same_level or same_z):
                lv1 = n1.level or "?"
                lv2 = n2.level or "?"
                z1 = n1.coord[2] if n1.coord else None
                z2 = n2.coord[2] if n2.coord else None
                messagebox.showwarning(
                    "Edge refused",
                    f"Refusing to create an edge between nodes on "
                    f"different floors:\n"
                    f"  {node1_id}  (level {lv1}, z={z1})\n"
                    f"  {node2_id}  (level {lv2}, z={z2})\n\n"
                    "If you intended a vertical edge, switch to "
                    "Network → Vertical Edge mode.")
                return None
        # --------------------------------------------------------------

        existing = set(self.model.edges.keys())
        i = 1
        while f"edge_new_{i}" in existing:
            i += 1
        eid = f"edge_new_{i}"
        coords = [c for c in [n1.coord if n1 else None,
                              n2.coord if n2 else None] if c is not None]
        self.model.edges[eid] = EdgeFeature(
            edge_id=eid, coords=coords, connects=[node1_id, node2_id],
            layer_id=self._layer_id_from_combo() or "",
            weight=1.0,
        )
        self._new_edge_ids.add(eid)
        return eid

    def _delete_edge(self, edge_id: str) -> None:
        self.model.edges.pop(edge_id, None)
        self._new_edge_ids.discard(edge_id)

    # -------------------------------------------------------
    # Vertical Edge — click a node on floor A, switch floor, click a node on floor B
    # -------------------------------------------------------
    def toggle_vertical_edge_mode(self) -> None:
        """Toggle the interactive vertical-edge mode.

        Workflow:
          1. Activate the mode → first-click captures a node on the current floor
             and remembers which floor it belongs to.
          2. User changes Floor combo to a different floor.
          3. Second click picks a node on the new floor and an edge is created
             linking both nodes (with their 3D coordinates, so the line is vertical).
          4. Mode stays on so the user can make more vertical edges; toggle off
             from the menu when done.
        """
        self._clear_view_selection()
        was_active = self._vertical_edge_mode
        self._deactivate_all_edit_modes()
        if not was_active:
            if self.model is None:
                messagebox.showwarning("No file", "Open a file first.")
                self._vertical_edge_mode_var.set(False)
                return
            self._vertical_edge_mode = True
            self._vertical_edge_mode_var.set(True)
            self._graph_edit_status_var.set(
                "Vertical Edge: click first node → change Floor → click second node.")
        self.refresh_view()

    # -------------------------------------------------------
    # Misc → Measure Distance
    # -------------------------------------------------------
    def toggle_measure_mode(self) -> None:
        """Toggle interactive distance measurement.

        Workflow: click two points on the canvas — the editor reports the
        Euclidean distance, dx, dy in a status line and a popup. Right-click
        cancels the first point. The mode stays active until toggled off
        (multiple measurements in a row).
        """
        self._clear_view_selection()
        want_on = self._measure_mode_var.get()
        if want_on:
            self._deactivate_all_edit_modes()
            self._measure_mode = True
            self._measure_mode_var.set(True)
            self._measure_pt1 = None
            self.status_var.set(
                "Measure: click first point (right-click to cancel).")
        else:
            self._measure_mode = False
            self._measure_pt1 = None
            self.status_var.set("")
        self.refresh_view()

    # -------------------------------------------------------
    # Misc → Create Virtual CellSpaces for standalone (orphan) nodes
    # -------------------------------------------------------
    def repair_trailing_duplicate_vertices(self) -> None:
        """Drop trailing vertices that exactly repeat the start of the polygon.

        Pattern caused by the old `_update_ring_coords` bug: when a vertex was
        deleted, the source XML kept stale `<pos>` siblings at the tail with
        old coordinates. On reload these appear as “zombie” vertices that
        replicate the first portion of the polygon — e.g.
            … v14, v15(=v0)            ← polygon properly closes here
                  , v16(=v14), v17(=v15)  ← stale leftovers
        This repair scans every CellSpace's first ring, finds the longest
        valid prefix that already forms a closed polygon, and truncates the
        ring to that prefix.
        """
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return

        EPS = 0.5  # mm tolerance — coordinates from XML are typically integers
        def same(p, q):
            return abs(p[0]-q[0]) < EPS and abs(p[1]-q[1]) < EPS

        repaired: List[str] = []
        details: List[str] = []
        for sid, sp in self.model.spaces.items():
            if not sp.polygons_3d:
                continue
            for k, ring in enumerate(sp.polygons_3d):
                if len(ring) < 4:
                    continue
                # Find the smallest n >= 3 such that ring[n] == ring[0] and
                # the first n+1 vertices form a non-degenerate closed polygon.
                # Trailing vertices after that index are zombies.
                n = len(ring)
                cut = None
                # Look for a closure index strictly before the end
                for i in range(3, n - 1):
                    if same(ring[i], ring[0]):
                        cut = i + 1   # keep ring[:cut] (closes properly)
                        break
                if cut is not None and cut < n:
                    sp.polygons_3d[k] = list(ring[:cut])
                    if k == 0:        # only report the floor ring
                        repaired.append(sid)
                        details.append(f"  • {sid} ({sp.level}): {n} → {cut} vertices")

        if not repaired:
            messagebox.showinfo("Repair",
                                "No CellSpace had trailing duplicate vertices.")
            return

        # The fixed _update_ring_coords() will sync the XML <pos> count to
        # match the trimmed polygons_3d on Save — no source_element reset
        # needed (and resetting would duplicate the CellSpace in XML).

        head = "\n".join(details[:25])
        more = "" if len(details) <= 25 else f"\n  … and {len(details)-25} more."
        messagebox.showinfo(
            "Repair",
            f"Trimmed trailing duplicate vertices from {len(repaired)} CellSpace(s):\n\n"
            f"{head}{more}\n\nSave the file to make the fix permanent.")
        self.refresh_view()

    def repair_self_loop_edges(self) -> None:
        """Delete Edges whose two endpoints resolve to the same Node.

        A self-loop expresses no transition: it cannot change reachability,
        so it contributes nothing to routing, while still being walked by
        every graph traversal (and tripping algorithms that assume an edge
        has two distinct ends).

        These are typically produced upstream, not in this editor — a
        converter that maps each endpoint to `owner_cell(boundary)` without
        checking whether both boundaries belong to the *same* cell. A big
        room with two or more openings then yields an edge from that room's
        dual node back to itself. The original intent ("cross this room from
        opening A to opening B") needs a node per opening to be expressed
        properly, and the boundary geometry that would allow rebuilding that
        is generally not present in the file — so removal is the only repair
        available here. Regenerate from the source data to keep the detail.
        """
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return

        loops: List[Tuple[str, str, str]] = []   # (edge_id, floor, cell_id)
        for eid, e in self.model.edges.items():
            refs = e.connects
            if len(refs) != 2 or refs[0] != refs[1]:
                continue
            node = self.model.nodes.get(refs[0])
            cell = self.model.spaces.get(node.duality) if node else None
            floor = ""
            if node is not None and node.level:
                floor = node.level
            elif cell is not None and cell.level:
                floor = cell.level
            loops.append((eid, floor or "?",
                          cell.feature_id if cell else (refs[0] or "?")))

        if not loops:
            messagebox.showinfo("Repair", "No self-loop edges found.")
            return

        # Preview grouped by the cell that carries the loops — far more
        # readable than 1500 raw edge ids, and it makes the "a few big rooms
        # cause all of this" shape obvious at a glance.
        per_cell: Dict[Tuple[str, str], int] = {}
        for _eid, floor, cell_id in loops:
            per_cell[(floor, cell_id)] = per_cell.get((floor, cell_id), 0) + 1
        ranked = sorted(per_cell.items(), key=lambda kv: (-kv[1], kv[0]))

        lines = []
        for (floor, cell_id), n in ranked[:20]:
            sp = self.model.spaces.get(cell_id)
            nm = f" ({sp.name})" if sp is not None and sp.name else ""
            lines.append(f"  • {floor}  {cell_id}{nm} — {n} edge(s)")
        more = ("" if len(ranked) <= 20
                else f"\n  … and {len(ranked) - 20} more cell(s).")

        if not messagebox.askyesno(
                "Repair: remove self-loop edges",
                f"{len(loops)} self-loop edge(s) on {len(per_cell)} cell(s) "
                f"will be removed:\n\n" + "\n".join(lines) + more +
                "\n\nRouting is unaffected — a self-loop cannot change "
                "reachability.\n\nSave the file to make the fix permanent."):
            return

        # Build the child→parent map ONCE. `_delete_one` rebuilds it per
        # feature, which is fine for a handful of rows but quadratic here:
        # this file has ~600k elements and 1500 loops to drop.
        parent_of: Dict[ET.Element, ET.Element] = {}
        root = (self.model.source_tree.getroot()
                if self.model.source_tree is not None else None)
        if root is not None:
            for parent in root.iter():
                for child in parent:
                    parent_of[child] = parent

        removed = 0
        for eid, _floor, _cell_id in loops:
            edge = self.model.edges.pop(eid, None)
            self._new_edge_ids.discard(eid)
            removed += 1
            if edge is None or edge.source_element is None or root is None:
                continue
            target = edge.source_element
            p = parent_of.get(target)
            if p is None:
                continue
            gp = parent_of.get(p)
            if gp is not None and local_name(p.tag) == "edgeMember":
                try: gp.remove(p)
                except ValueError: pass
            else:
                try: p.remove(target)
                except ValueError: pass

        if self._graph_selected_edge in {l[0] for l in loops}:
            self._graph_selected_edge = None
        if (self._view_selected_type == "edge"
                and self._view_selected_id in {l[0] for l in loops}):
            self._view_selected_type = None
            self._view_selected_id = None

        messagebox.showinfo(
            "Repair",
            f"Removed {removed} self-loop edge(s).\n\n"
            "Save the file to make the fix permanent.")
        self.refresh_view()

    def repair_self_intersecting_tail(self) -> None:
        """Heuristic repair of self-intersecting polygons by truncating their
        tail until the polygon becomes simple (non-self-intersecting).

        Iteratively drops the last vertex while the polygon self-intersects,
        stopping at 4 vertices. Spaces that cannot be repaired this way are
        listed at the end so the user can clean them up manually.
        """
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return

        def segs_intersect(p1, p2, p3, p4):
            def ccw(a, b, c):
                return (c[1]-a[1])*(b[0]-a[0]) > (b[1]-a[1])*(c[0]-a[0])
            return (ccw(p1, p3, p4) != ccw(p2, p3, p4)
                    and ccw(p1, p2, p3) != ccw(p1, p2, p4))

        def is_self_intersect(poly):
            pts = [(p[0], p[1]) for p in poly]
            if len(pts) >= 2 and pts[0] == pts[-1]:
                pts = pts[:-1]
            n = len(pts)
            if n < 4:
                return False
            for i in range(n):
                a, b = pts[i], pts[(i+1) % n]
                for j in range(i + 2, n):
                    if i == 0 and j == n - 1:
                        continue
                    c, d = pts[j], pts[(j+1) % n]
                    if segs_intersect(a, b, c, d):
                        return True
            return False

        fixed: List[str] = []
        unfixed: List[str] = []
        details: List[str] = []
        for sid, sp in self.model.spaces.items():
            if not sp.polygons_3d:
                continue
            ring = list(sp.polygons_3d[0])
            if not is_self_intersect(ring):
                continue
            original_n = len(ring)
            while len(ring) > 4 and is_self_intersect(ring):
                ring.pop()
            if not is_self_intersect(ring) and len(ring) >= 4:
                # Ensure closure
                if ring[0] != ring[-1]:
                    ring.append(ring[0])
                sp.polygons_3d[0] = ring
                fixed.append(sid)
                details.append(f"  • {sid} ({sp.level}): {original_n} → {len(ring)} vertices")
            else:
                unfixed.append(sid)

        if not fixed and not unfixed:
            messagebox.showinfo("Repair", "No self-intersecting CellSpace found.")
            return

        # Keep source_element intact — the fixed _update_ring_coords() will
        # sync the XML <pos> list to the new polygon length on Save.

        head = "\n".join(details[:25])
        more = "" if len(details) <= 25 else f"\n  … and {len(details)-25} more."
        msg = (f"Repaired {len(fixed)} self-intersecting CellSpace(s):\n\n"
               f"{head}{more}")
        if unfixed:
            msg += (f"\n\nCould not repair {len(unfixed)} (require manual fix):\n"
                    + "\n".join(f"  • {s}" for s in unfixed[:15]))
            if len(unfixed) > 15:
                msg += f"\n  … and {len(unfixed)-15} more."
        msg += "\n\nSave the file to make the fix permanent."
        messagebox.showinfo("Repair", msg)
        self.refresh_view()

    def search_by_id_or_name(self) -> None:
        """Open a non-modal search dialog. Type any substring; matches across
        CellSpace IDs/names, Node IDs, and Edge IDs are listed below.
        Double-click a row to locate it on the canvas (auto-switches floor).
        """
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return

        # Reuse the existing dialog if still open
        existing = getattr(self, "_search_dlg", None)
        if existing is not None and existing.winfo_exists():
            try:
                existing.destroy()
            except Exception:
                pass

        dlg = tk.Toplevel(self.root)
        dlg.title("Search by ID / Name")
        dlg.geometry("700x520")
        try:
            dlg.transient(self.root)
        except Exception:
            pass
        self._search_dlg = dlg

        # --- Top row: query + filter checkboxes ---
        top = ttk.Frame(dlg)
        top.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(top, text="Query:").pack(side="left")
        query_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=query_var, width=40)
        entry.pack(side="left", padx=4)
        case_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Match case",
                        variable=case_var).pack(side="left", padx=4)
        regex_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Regex",
                        variable=regex_var).pack(side="left", padx=4)

        # Type filters
        type_frame = ttk.Frame(dlg)
        type_frame.pack(fill="x", padx=8, pady=(0, 4))
        ttk.Label(type_frame, text="Search in:").pack(side="left")
        f_space = tk.BooleanVar(value=True)
        f_node  = tk.BooleanVar(value=True)
        f_edge  = tk.BooleanVar(value=True)
        ttk.Checkbutton(type_frame, text="CellSpaces",
                        variable=f_space).pack(side="left", padx=4)
        ttk.Checkbutton(type_frame, text="Nodes",
                        variable=f_node).pack(side="left", padx=4)
        ttk.Checkbutton(type_frame, text="Edges",
                        variable=f_edge).pack(side="left", padx=4)

        # --- Results table ---
        body = ttk.Frame(dlg)
        body.pack(fill="both", expand=True, padx=8, pady=4)
        tree = ttk.Treeview(
            body, columns=("type", "level", "id", "name"),
            show="headings", selectmode="browse", height=18)
        tree.heading("type",  text="Type")
        tree.heading("level", text="Floor")
        tree.heading("id",    text="ID")
        tree.heading("name",  text="Name")
        tree.column("type",  width=80,  anchor="w")
        tree.column("level", width=60,  anchor="w")
        tree.column("id",    width=320, anchor="w")
        tree.column("name",  width=220, anchor="w")
        tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        sb.pack(side="right", fill="y")
        tree.configure(yscrollcommand=sb.set)

        # Footer
        footer = ttk.Frame(dlg)
        footer.pack(fill="x", padx=8, pady=8)
        summary = tk.StringVar(value="Type a query above and press Enter / Search.")
        ttk.Label(footer, textvariable=summary).pack(side="left")
        ttk.Button(footer, text="Close",
                   command=dlg.destroy).pack(side="right")

        # --- Search logic ---
        def _do_search(_evt=None):
            q = query_var.get().strip()
            tree.delete(*tree.get_children())
            if not q:
                summary.set("Enter a query.")
                return

            # Build a matcher
            if regex_var.get():
                try:
                    flags = 0 if case_var.get() else re.IGNORECASE
                    pat = re.compile(q, flags)
                    matches = lambda s: bool(s) and pat.search(s) is not None
                except re.error as e:
                    summary.set(f"Invalid regex: {e}")
                    return
            else:
                if case_var.get():
                    matches = lambda s: bool(s) and q in s
                else:
                    qq = q.casefold()
                    matches = lambda s: bool(s) and qq in s.casefold()

            count = 0
            if f_space.get():
                for sid, sp in self.model.spaces.items():
                    if matches(sid) or matches(sp.name or ""):
                        kind = "Space*" if sp.is_virtual else "Space"
                        tree.insert("", "end",
                                    values=(kind, sp.level or "?",
                                            sid, sp.name or ""))
                        count += 1
            if f_node.get():
                for nid, n in self.model.nodes.items():
                    if matches(nid):
                        tree.insert("", "end",
                                    values=("Node", n.level or "?",
                                            nid, ""))
                        count += 1
            if f_edge.get():
                for eid, e in self.model.edges.items():
                    if matches(eid):
                        # Edge level via Z (best effort)
                        lv = "?"
                        if e.coords:
                            try:
                                z = e.coords[0][2]
                                # find nearest floor by sample space Z
                                fz = {sp.level: sp.polygons_3d[0][0][2]
                                      for sp in self.model.spaces.values()
                                      if sp.level and sp.polygons_3d}
                                if fz:
                                    lv = min(fz.keys(),
                                             key=lambda k: abs(fz[k]-z))
                            except Exception:
                                pass
                        tree.insert("", "end",
                                    values=("Edge", lv, eid, ""))
                        count += 1

            summary.set(f"{count} match(es) for '{q}'."
                        + (" (* = virtual)" if any(
                            sp.is_virtual for sp in self.model.spaces.values()) else ""))

        # Bind Enter to trigger search
        entry.bind("<Return>", _do_search)
        ttk.Button(top, text="Search",
                   command=_do_search).pack(side="left", padx=4)
        entry.focus_set()

        # --- Locate-on-canvas (double-click) ---
        def _go(_evt=None):
            sel = tree.selection()
            if not sel:
                return
            kind, lv, fid, _ = tree.item(sel[0], "values")
            if lv and lv != "?" and lv in self.floor_combo["values"]:
                self.floor_var.set(lv)

            cx = cy = None
            if kind.startswith("Space"):
                sp = self.model.spaces.get(fid)
                if sp and sp.polygons_3d:
                    xs = [p[0] for p in sp.polygons_3d[0]]
                    ys = [p[1] for p in sp.polygons_3d[0]]
                    cx = sum(xs)/len(xs); cy = sum(ys)/len(ys)
                self._view_selected_type = "space"
                self._view_selected_id   = fid
            elif kind == "Node":
                n = self.model.nodes.get(fid)
                if n and n.coord:
                    cx, cy = n.coord[0], n.coord[1]
                self._view_selected_type = "node"
                self._view_selected_id   = fid
            elif kind == "Edge":
                e = self.model.edges.get(fid)
                if e and e.coords:
                    xs = [p[0] for p in e.coords]
                    ys = [p[1] for p in e.coords]
                    cx = sum(xs)/len(xs); cy = sum(ys)/len(ys)
                self._view_selected_type = "edge"
                self._view_selected_id   = fid

            if cx is not None and cy is not None:
                xlim = self.ax.get_xlim()
                w = abs(xlim[1]-xlim[0]) * 0.5 or 5.0
                self.ax.set_xlim(cx - w/2, cx + w/2)
                ylim = self.ax.get_ylim()
                h = abs(ylim[1]-ylim[0]) * 0.5 or 5.0
                self.ax.set_ylim(cy - h/2, cy + h/2)
                self._view_locked = True
            self.refresh_view()

        tree.bind("<Double-1>", _go)

    def find_duplicate_ids(self) -> None:
        """Scan the source XML tree for any feature element (Node, Edge,
        CellSpace, …) that appears more than once with the same id.

        Reports each duplicate group, the number of copies and a brief
        signature for each copy (Z value for nodes, vertex count for cells).
        Includes a "Clean up" button that removes the surplus copies,
        keeping the first occurrence and binding the model's
        `source_element` to it. Run Save afterwards to persist the cleanup.
        """
        if self.model is None or self.model.source_tree is None:
            messagebox.showwarning("No file", "Open a file first.")
            return

        TARGET_LOCAL_NAMES = {
            "Node", "Edge", "CellSpace", "ThematicLayer",
            "PrimalSpaceLayer", "DualSpaceLayer",
            "InterLayerConnection",
        }

        # Collect (kind, id) -> list of element refs
        from collections import defaultdict as _dd
        by_id: Dict[Tuple[str, str], List[ET.Element]] = _dd(list)
        root = self.model.source_tree.getroot()
        for elem in root.iter():
            ln = local_name(elem.tag)
            if ln not in TARGET_LOCAL_NAMES:
                continue
            for k, v in elem.attrib.items():
                if local_name(k) == "id":
                    by_id[(ln, v)].append(elem)
                    break

        # Sort priority: Node first (rarest, most concerning), then Edge,
        # then CellSpace, then layer-level metadata. This way the user
        # never has to scroll past hundreds of Edge entries to discover a
        # single Node collision.
        _KIND_ORDER = {
            "Node": 0, "Edge": 1, "CellSpace": 2,
            "InterLayerConnection": 3,
            "ThematicLayer": 4,
            "PrimalSpaceLayer": 5, "DualSpaceLayer": 6,
        }
        dup_groups = [(kind, fid, elems)
                      for (kind, fid), elems in by_id.items()
                      if len(elems) >= 2]
        dup_groups.sort(key=lambda t: (_KIND_ORDER.get(t[0], 99), t[1]))

        # Reuse-or-recreate a single non-modal dialog
        existing = getattr(self, "_dup_id_dlg", None)
        if existing is not None and existing.winfo_exists():
            try:
                existing.destroy()
            except Exception:
                pass

        dlg = tk.Toplevel(self.root)
        dlg.title("Duplicate IDs")
        dlg.geometry("720x520")
        try:
            dlg.transient(self.root)
        except Exception:
            pass
        self._dup_id_dlg = dlg

        # Header / summary
        summary_var = tk.StringVar()
        ttk.Label(dlg, textvariable=summary_var,
                  font=("TkDefaultFont", 9, "bold"))\
            .pack(anchor="w", padx=8, pady=(8, 4))

        body = ttk.Frame(dlg)
        body.pack(fill="both", expand=True, padx=8, pady=4)
        tree = ttk.Treeview(
            body,
            columns=("kind", "id", "copy", "signature"),
            show="tree headings", selectmode="extended", height=18)
        tree.heading("#0",        text="")          # tree column for grouping
        tree.heading("kind",      text="Kind")
        tree.heading("id",        text="ID")
        tree.heading("copy",      text="Copy")
        tree.heading("signature", text="Signature")
        tree.column("#0",        width=24,  stretch=False)
        tree.column("kind",      width=110, anchor="w")
        tree.column("id",        width=220, anchor="w")
        tree.column("copy",      width=60,  anchor="center")
        tree.column("signature", width=280, anchor="w")
        tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        sb.pack(side="right", fill="y")
        tree.configure(yscrollcommand=sb.set)

        # Per-row → element map so we can act on the picked copy
        row_to_elem: Dict[str, ET.Element] = {}
        row_to_kind_id: Dict[str, Tuple[str, str]] = {}

        def _signature(elem: ET.Element) -> str:
            """Short per-copy descriptor so you can tell duplicates apart."""
            ln = local_name(elem.tag)
            if ln == "Node":
                for sub in elem.iter():
                    if local_name(sub.tag) == "pos" and sub.text:
                        parts = sub.text.strip().split()
                        if len(parts) >= 3:
                            return f"z={parts[2]}"
                        if len(parts) == 2:
                            return f"xy=({parts[0]},{parts[1]})"
                return "(no pos)"
            if ln == "Edge":
                for sub in elem.iter():
                    if local_name(sub.tag) == "pos" and sub.text:
                        parts = sub.text.strip().split()
                        if len(parts) >= 3:
                            return f"first z={parts[2]}"
                        return f"first pos={sub.text.strip()}"
                return "(no pos)"
            if ln == "CellSpace":
                vc = sum(1 for s in elem.iter()
                         if local_name(s.tag) == "pos")
                lvl = ""
                for sub in elem:
                    if local_name(sub.tag) == "level" and sub.text:
                        lvl = sub.text.strip()
                        break
                return f"{vc} pos | level={lvl or '?'}"
            return ""

        def _populate():
            tree.delete(*tree.get_children())
            row_to_elem.clear()
            row_to_kind_id.clear()
            for kind, fid, elems in dup_groups:
                # Group header row (no element bound)
                gid = tree.insert(
                    "", "end",
                    values=(kind, fid, f"{len(elems)} copies", ""),
                    open=True)
                # One child row per copy — each is individually selectable
                for i, e in enumerate(elems, start=1):
                    rid = tree.insert(
                        gid, "end",
                        values=("", "", f"#{i}", _signature(e)))
                    row_to_elem[rid] = e
                    row_to_kind_id[rid] = (kind, fid)
            total_extra = sum(len(es) - 1 for _, _, es in dup_groups)
            # Per-kind breakdown so the user immediately sees how many of
            # each type are affected.
            from collections import Counter as _Counter
            kind_counts = _Counter(k for k, _, _ in dup_groups)
            breakdown = ", ".join(
                f"{kind_counts[k]} {k}" for k in
                ("Node", "Edge", "CellSpace",
                 "InterLayerConnection", "ThematicLayer",
                 "PrimalSpaceLayer", "DualSpaceLayer")
                if kind_counts.get(k))
            head = f"{len(dup_groups)} duplicate group(s)"
            if breakdown:
                head += f"  [{breakdown}]"
            summary_var.set(
                f"{head} — {total_extra} surplus element(s). "
                "Pick a row, then 'Locate' to verify or "
                "'Delete this copy' to remove just that element.")

        _populate()

        # Footer with action buttons
        footer = ttk.Frame(dlg)
        footer.pack(fill="x", padx=8, pady=8)

        def _on_close():
            self._dup_id_dlg = None
            dlg.destroy()
        ttk.Button(footer, text="Close",
                   command=_on_close).pack(side="right")

        def _rescan():
            dup_groups.clear()
            by_id2: Dict[Tuple[str, str], List[ET.Element]] = _dd(list)
            for elem in root.iter():
                ln = local_name(elem.tag)
                if ln not in TARGET_LOCAL_NAMES:
                    continue
                for k, v in elem.attrib.items():
                    if local_name(k) == "id":
                        by_id2[(ln, v)].append(elem)
                        break
            dup_groups.extend(
                (kind, fid, elems)
                for (kind, fid), elems in by_id2.items()
                if len(elems) >= 2
            )
            dup_groups.sort(key=lambda t: (t[0], t[1]))
            _populate()

        # ---- Locate the picked copy on the canvas (preview only) ----
        def _locate_picked():
            sel = tree.selection()
            if not sel or sel[0] not in row_to_elem:
                return
            elem = row_to_elem[sel[0]]
            kind, fid = row_to_kind_id[sel[0]]

            # Pull every <pos> in this element so we can both (a) place
            # the highlight on the picked copy's own coordinates and (b)
            # decide which floor to switch to. The model dict only holds
            # one of the duplicates' coords — using the picked copy's
            # `<pos>` directly is what makes the two duplicates show in
            # different places when the user clicks them in turn.
            poly_xy: List[Tuple[float, float]] = []
            cz: Optional[float] = None
            level_text: Optional[str] = None
            for sub in elem.iter():
                ln = local_name(sub.tag)
                if ln == "level" and sub.text:
                    level_text = sub.text.strip()
                elif ln == "pos" and sub.text:
                    parts = sub.text.strip().split()
                    if len(parts) >= 2:
                        try:
                            poly_xy.append((float(parts[0]), float(parts[1])))
                            if len(parts) >= 3 and cz is None:
                                cz = float(parts[2])
                        except ValueError:
                            pass

            # Resolve target floor — explicit <level> > Z-coord match.
            target_floor = level_text
            if (not target_floor
                    or target_floor not in self.floor_combo["values"]):
                if cz is not None:
                    floor_z = {sp.level: sp.polygons_3d[0][0][2]
                               for sp in self.model.spaces.values()
                               if sp.level and sp.polygons_3d}
                    if floor_z:
                        target_floor = min(floor_z.keys(),
                                           key=lambda k: abs(floor_z[k]-cz))
            if target_floor and target_floor in self.floor_combo["values"]:
                self.floor_var.set(target_floor)

            if poly_xy:
                cx = sum(p[0] for p in poly_xy) / len(poly_xy)
                cy = sum(p[1] for p in poly_xy) / len(poly_xy)
                xlim = self.ax.get_xlim(); ylim = self.ax.get_ylim()
                w = abs(xlim[1]-xlim[0]) * 0.5 or 5.0
                h = abs(ylim[1]-ylim[0]) * 0.5 or 5.0
                self.ax.set_xlim(cx-w/2, cx+w/2)
                self.ax.set_ylim(cy-h/2, cy+h/2)
                self._view_locked = True

            # Drive the magenta-crosshair highlight at the picked copy's
            # exact coords by setting the *override* fields. This is what
            # makes duplicate #1 and duplicate #2 light up at *different*
            # spots even though the model dict points at only one of them.
            self._view_selected_override_xy   = None
            self._view_selected_override_poly = None
            if kind == "Node":
                self._view_selected_type = "node"
                self._view_selected_id   = fid
                if poly_xy:
                    self._view_selected_override_xy = poly_xy[0]
                if fid in self.model.nodes:
                    self.model.nodes[fid].source_element = elem
            elif kind == "CellSpace":
                self._view_selected_type = "space"
                self._view_selected_id   = fid
                if poly_xy:
                    self._view_selected_override_poly = poly_xy
                if fid in self.model.spaces:
                    self.model.spaces[fid].source_element = elem
            elif kind == "Edge":
                self._view_selected_type = "edge"
                self._view_selected_id   = fid
                if poly_xy:
                    self._view_selected_override_poly = poly_xy
                if fid in self.model.edges:
                    self.model.edges[fid].source_element = elem
            self.refresh_view()

        # ---- Delete *only* the picked copy ----
        def _delete_one(row_id, parent_of):
            """Delete a single picked copy. `parent_of` is a precomputed
            element→parent map (so we don't rebuild it per call when
            deleting many rows in bulk). Returns the number of
            cascade-deleted edges, or None on failure."""
            if row_id not in row_to_elem:
                return None
            elem = row_to_elem[row_id]
            kind, fid = row_to_kind_id[row_id]
            removed = False
            p = parent_of.get(elem)
            if p is not None:
                gp = parent_of.get(p)
                wrapper_local = local_name(p.tag)
                if (gp is not None and wrapper_local in (
                        "nodeMember", "edgeMember", "cellSpaceMember")):
                    try:
                        gp.remove(p); removed = True
                    except ValueError:
                        pass
                if not removed:
                    try:
                        p.remove(elem); removed = True
                    except ValueError:
                        pass

            # Helper: read the z value of the *first* <pos> in an element
            def _first_z(e: ET.Element) -> Optional[float]:
                for sub in e.iter():
                    if local_name(sub.tag) == "pos" and sub.text:
                        parts = sub.text.strip().split()
                        if len(parts) >= 3:
                            try:
                                return float(parts[2])
                            except ValueError:
                                return None
                return None

            Z_EPS = 50.0  # mm — same tolerance used elsewhere

            cascaded_edges = 0
            survivors: List[ET.Element] = []
            survivor_zs: List[float] = []
            deleted_z: Optional[float] = None
            if removed:
                deleted_z = _first_z(elem)
                for k_, fid_, elems_ in dup_groups:
                    if k_ == kind and fid_ == fid:
                        survivors = [e for e in elems_ if e is not elem]
                        survivor_zs = [_first_z(e) for e in survivors
                                       if _first_z(e) is not None]
                        break

                if survivors:
                    # Re-bind model to one of the survivors so the in-memory
                    # representation stays valid.
                    keep = survivors[0]
                    if kind == "Node" and fid in self.model.nodes:
                        self.model.nodes[fid].source_element = keep
                    elif kind == "Edge" and fid in self.model.edges:
                        self.model.edges[fid].source_element = keep
                    elif (kind == "CellSpace"
                          and fid in self.model.spaces):
                        self.model.spaces[fid].source_element = keep

                # ----- Cascade-delete decisions per kind -----
                if kind == "Node":
                    # An edge "physically belongs to" the deleted copy
                    # when one of its endpoints matches the deleted copy's
                    # FULL (x, y, z). Earlier we matched z only, but that
                    # wrongly cascaded any same-floor edge that referenced
                    # `fid` — even ones that physically connected to a
                    # different node sharing the same z. Tighten to exact
                    # XYZ to keep unrelated edges intact.
                    deleted_xy: Optional[Tuple[float, float]] = None
                    for sub_ in elem.iter():
                        if (local_name(sub_.tag) == "pos"
                                and sub_.text):
                            parts = sub_.text.strip().split()
                            if len(parts) >= 2:
                                try:
                                    deleted_xy = (float(parts[0]),
                                                  float(parts[1]))
                                except ValueError:
                                    pass
                            break
                    XY_EPS_DEL = 0.5  # mm

                    no_survivor_at_z = (
                        deleted_z is None
                        or all(abs(z - deleted_z) >= Z_EPS
                               for z in survivor_zs)
                    )
                    if no_survivor_at_z:
                        edges_to_del: List[str] = []
                        for eid, e in self.model.edges.items():
                            if fid not in e.connects:
                                continue
                            if not e.coords:
                                continue
                            if deleted_z is None or deleted_xy is None:
                                edges_to_del.append(eid)
                                continue
                            # Edge must have an endpoint whose full
                            # (x, y, z) matches the deleted copy.
                            def _ep_match(pt):
                                return (abs(pt[0] - deleted_xy[0]) < XY_EPS_DEL
                                        and abs(pt[1] - deleted_xy[1]) < XY_EPS_DEL
                                        and abs(pt[2] - deleted_z) < Z_EPS)
                            if _ep_match(e.coords[0]) or _ep_match(e.coords[-1]):
                                edges_to_del.append(eid)
                        for eid in edges_to_del:
                            edge = self.model.edges.pop(eid, None)
                            self._new_edge_ids.discard(eid)
                            cascaded_edges += 1
                            if edge is not None and edge.source_element is not None:
                                ep = parent_of.get(edge.source_element)
                                if ep is not None:
                                    epp = parent_of.get(ep)
                                    if (epp is not None
                                            and local_name(ep.tag)
                                            == "edgeMember"):
                                        try:
                                            epp.remove(ep)
                                        except ValueError:
                                            pass
                                    else:
                                        try:
                                            ep.remove(edge.source_element)
                                        except ValueError:
                                            pass
                        if not survivors:
                            self.model.nodes.pop(fid, None)
                            self._new_node_ids.discard(fid)
                elif kind == "Edge" and not survivors:
                    self.model.edges.pop(fid, None)
                    self._new_edge_ids.discard(fid)
                elif kind == "CellSpace" and not survivors:
                    self.model.spaces.pop(fid, None)
                    self._new_space_ids.discard(fid)

            if not removed:
                return None
            return cascaded_edges

        def _delete_picked():
            """Delete every selected duplicate-copy row. Multi-select
            (Ctrl/Shift click in the tree, or marquee) is supported —
            each row is removed in turn under one confirmation dialog."""
            sels = list(tree.selection())
            sels = [s for s in sels if s in row_to_elem]
            if not sels:
                messagebox.showinfo(
                    "Delete this copy",
                    "Pick one or more specific copy row(s) "
                    "(under a duplicate group) first.",
                    parent=dlg)
                return
            if len(sels) == 1:
                elem = row_to_elem[sels[0]]
                kind, fid = row_to_kind_id[sels[0]]
                sig = _signature(elem)
                msg = (f"Remove this copy of {kind} '{fid}'?\n"
                       f"  signature: {sig}\n\n"
                       "The other duplicate(s) will remain. "
                       "Save the file afterwards to persist.")
            else:
                preview = "\n".join(
                    f"  • {row_to_kind_id[s][0]} '{row_to_kind_id[s][1]}'"
                    f"  ({_signature(row_to_elem[s])})"
                    for s in sels[:10])
                more = "" if len(sels) <= 10 else f"\n  … +{len(sels)-10} more"
                msg = (f"Remove {len(sels)} selected copies?\n\n"
                       f"{preview}{more}\n\n"
                       "Save the file afterwards to persist.")
            if not messagebox.askyesno("Delete this copy", msg, parent=dlg):
                return

            parent_of: Dict[ET.Element, ET.Element] = {}
            for parent in root.iter():
                for child in parent:
                    parent_of[child] = parent

            total_removed = 0
            total_cascaded = 0
            for row in sels:
                r = _delete_one(row, parent_of)
                if r is not None:
                    total_removed += 1
                    total_cascaded += r
            if total_removed == 0:
                messagebox.showwarning(
                    "Delete this copy",
                    "Could not remove the element(s) (already detached?).",
                    parent=dlg)
            else:
                if total_cascaded:
                    messagebox.showinfo(
                        "Delete this copy",
                        f"Removed {total_removed} copy(ies). "
                        f"{total_cascaded} dangling edge(s) were "
                        f"cascade-deleted.\nSave the file to persist.",
                        parent=dlg)
                _rescan()
                self.refresh_view()

        # ---- Rename the picked copy to a fresh, unused id ----
        def _rename_picked():
            sel = tree.selection()
            if not sel or sel[0] not in row_to_elem:
                messagebox.showinfo(
                    "Rename ID",
                    "Pick a specific copy row (under a duplicate group) "
                    "first.", parent=dlg)
                return
            elem = row_to_elem[sel[0]]
            kind, fid = row_to_kind_id[sel[0]]

            # Build the set of EVERY existing id of this kind in the
            # source tree so we can suggest / validate a unique new id.
            existing_ids: Set[str] = set()
            target_ln = kind   # "Node" / "Edge" / "CellSpace" / ...
            for e_ in root.iter():
                if local_name(e_.tag) == target_ln:
                    for k_, v_ in e_.attrib.items():
                        if local_name(k_) == "id":
                            existing_ids.add(v_)
                            break

            # Default suggestion: append "_2", "_3", … until unique.
            base = fid
            i = 2
            while f"{base}_{i}" in existing_ids:
                i += 1
            default_new_id = f"{base}_{i}"

            # Modal sub-dialog asking for the new id.
            sub = tk.Toplevel(dlg)
            sub.title("Rename ID")
            sub.resizable(False, False)
            sub.grab_set()
            ttk.Label(sub, text=f"Renaming this {kind} copy of "
                              f"'{fid}'.").grid(
                row=0, column=0, columnspan=2, padx=10, pady=(10, 4),
                sticky="w")
            ttk.Label(sub, text="New ID:").grid(
                row=1, column=0, padx=10, pady=4, sticky="e")
            id_var = tk.StringVar(value=default_new_id)
            entry = ttk.Entry(sub, textvariable=id_var, width=36)
            entry.grid(row=1, column=1, padx=(0, 10), pady=4, sticky="w")
            entry.focus_set()
            entry.select_range(0, "end")

            confirmed_new_id: List[str] = []
            def _ok():
                v = id_var.get().strip()
                if not v:
                    messagebox.showwarning(
                        "Rename ID", "Enter a non-empty ID.", parent=sub)
                    return
                if v == fid:
                    messagebox.showwarning(
                        "Rename ID",
                        "New ID equals the current ID — nothing to do.",
                        parent=sub)
                    return
                if v in existing_ids:
                    messagebox.showwarning(
                        "Rename ID",
                        f"'{v}' already exists in this file.", parent=sub)
                    return
                confirmed_new_id.append(v)
                sub.destroy()
            ttk.Button(sub, text="OK", command=_ok).grid(
                row=2, column=0, pady=10)
            ttk.Button(sub, text="Cancel",
                       command=sub.destroy).grid(row=2, column=1, pady=10)
            sub.bind("<Return>", lambda e: _ok())
            sub.bind("<Escape>", lambda e: sub.destroy())
            sub.wait_window()

            if not confirmed_new_id:
                return
            new_id = confirmed_new_id[0]

            # Apply the rename — update the id attribute of the picked
            # element AND update connects-references that physically
            # belong to this copy (matched by z), so the renamed copy
            # keeps its incident edges.
            id_changed = False
            for k_ in list(elem.attrib.keys()):
                if local_name(k_) == "id":
                    elem.attrib[k_] = new_id
                    id_changed = True
                    break
            if not id_changed:
                messagebox.showwarning(
                    "Rename ID",
                    "Could not locate the id attribute on this element.",
                    parent=dlg)
                return

            # For Nodes, also reattach the edges that physically belong
            # to this copy (endpoint z matches this copy's z and at most
            # one survivor sits at the same z).
            reattached_edges = 0
            if kind == "Node":
                this_z: Optional[float] = None
                for sub_ in elem.iter():
                    if local_name(sub_.tag) == "pos" and sub_.text:
                        parts = sub_.text.strip().split()
                        if len(parts) >= 3:
                            try:
                                this_z = float(parts[2])
                            except ValueError:
                                pass
                        break

                # Find sibling copies still at the same z — if any, we
                # cannot uniquely reassign edges, so leave them alone.
                same_z_sibling = False
                if this_z is not None:
                    for k_, fid_, elems_ in dup_groups:
                        if k_ == "Node" and fid_ == fid:
                            for e_ in elems_:
                                if e_ is elem:
                                    continue
                                for sub_ in e_.iter():
                                    if (local_name(sub_.tag) == "pos"
                                            and sub_.text):
                                        parts = sub_.text.strip().split()
                                        if len(parts) >= 3:
                                            try:
                                                z_ = float(parts[2])
                                            except ValueError:
                                                continue
                                            if abs(z_ - this_z) < 50.0:
                                                same_z_sibling = True
                                        break
                            break

                # Pull the renamed copy's exact (x, y) so we can match
                # edges that GEOMETRICALLY end at this node — the previous
                # version retargeted any edge whose endpoint shared the
                # same z, which wrongly modified every same-floor edge
                # whose connects happened to reference `fid`. That bug
                # produced cross-floor edges like
                #   edge_new_732 connects=[node_new_262, node_new_266]
                # whose <gml:pos> values were both at z=3695.
                this_xy: Optional[Tuple[float, float]] = None
                if this_z is not None:
                    for sub_ in elem.iter():
                        if (local_name(sub_.tag) == "pos"
                                and sub_.text):
                            parts = sub_.text.strip().split()
                            if len(parts) >= 2:
                                try:
                                    this_xy = (float(parts[0]),
                                               float(parts[1]))
                                except ValueError:
                                    pass
                            break

                XY_EPS = 0.5  # mm — coordinates are typically integers
                if (this_z is not None and this_xy is not None
                        and not same_z_sibling):
                    # Walk every Edge in the tree; only retarget when an
                    # edge has an ENDPOINT (first or last <pos>) whose
                    # full (x, y, z) matches the renamed copy's pos —
                    # i.e. the edge is geometrically incident on this
                    # specific node, not merely on any node at the same
                    # floor.
                    for e_elem in root.iter():
                        if local_name(e_elem.tag) != "Edge":
                            continue
                        edge_pts: List[Tuple[float, float, float]] = []
                        for sub_ in e_elem.iter():
                            if (local_name(sub_.tag) == "pos"
                                    and sub_.text):
                                parts = sub_.text.strip().split()
                                if len(parts) >= 3:
                                    try:
                                        edge_pts.append((
                                            float(parts[0]),
                                            float(parts[1]),
                                            float(parts[2])))
                                    except ValueError:
                                        pass
                        if len(edge_pts) < 2:
                            continue

                        def _match(pt: Tuple[float, float, float]) -> bool:
                            return (abs(pt[0] - this_xy[0]) < XY_EPS
                                    and abs(pt[1] - this_xy[1]) < XY_EPS
                                    and abs(pt[2] - this_z) < 50.0)

                        if not (_match(edge_pts[0])
                                or _match(edge_pts[-1])):
                            continue
                        # Geometrically incident on this exact node →
                        # retarget the connects href that names `fid`.
                        for ce in e_elem:
                            if local_name(ce.tag) != "connects":
                                continue
                            for ak, av in list(ce.attrib.items()):
                                if (local_name(ak) == "href"
                                        and av.lstrip("#") == fid):
                                    ce.attrib[ak] = f"#{new_id}"
                                    reattached_edges += 1
                        # Sync the in-memory edge.connects so editor
                        # logic stays consistent with the XML.
                        eid_attr: Optional[str] = None
                        for ak, av in e_elem.attrib.items():
                            if local_name(ak) == "id":
                                eid_attr = av
                                break
                        if eid_attr and eid_attr in self.model.edges:
                            edge = self.model.edges[eid_attr]
                            edge.connects = [
                                new_id if c == fid else c
                                for c in edge.connects
                            ]

            # Update the model's per-id dicts: insert renamed copy as a
            # fresh entry; the original id keeps its surviving copy.
            if kind == "Node":
                # Detach a NodeFeature for the new id from the renamed
                # element (parse coord & duality from the element).
                nx = ny = nz = 0.0
                for sub_ in elem.iter():
                    if local_name(sub_.tag) == "pos" and sub_.text:
                        parts = sub_.text.strip().split()
                        if len(parts) >= 2:
                            try:
                                nx = float(parts[0]); ny = float(parts[1])
                                if len(parts) >= 3:
                                    nz = float(parts[2])
                            except ValueError:
                                pass
                        break
                duality = ""
                for sub_ in elem.iter():
                    if local_name(sub_.tag) == "duality":
                        for ak, av in sub_.attrib.items():
                            if local_name(ak) == "href" and av:
                                duality = av.lstrip("#")
                                break
                        break
                new_node = NodeFeature(
                    node_id=new_id, coord=(nx, ny, nz),
                    source_element=elem, duality=duality,
                    layer_id=(self.model.nodes[fid].layer_id
                              if fid in self.model.nodes else ""),
                )
                self.model.nodes[new_id] = new_node
            elif kind == "Edge" and fid in self.model.edges:
                # Move the EdgeFeature reference to new id (edge.connects
                # endpoints don't change here).
                edge = self.model.edges.pop(fid, None)
                if edge is not None:
                    edge.edge_id = new_id
                    edge.source_element = elem
                    self.model.edges[new_id] = edge
            elif kind == "CellSpace" and fid in self.model.spaces:
                sp = self.model.spaces.pop(fid, None)
                if sp is not None:
                    sp.feature_id = new_id
                    sp.source_element = elem
                    self.model.spaces[new_id] = sp
            self.model.invalidate_floor_index()

            messagebox.showinfo(
                "Rename ID",
                f"'{fid}' → '{new_id}' applied to this copy.\n"
                + (f"{reattached_edges} edge connects-reference(s) "
                   f"updated to point at the renamed copy.\n"
                   if reattached_edges else "")
                + "Save the file to persist.",
                parent=dlg)
            _rescan()
            self.refresh_view()

        ttk.Button(footer, text="Delete this copy",
                   command=_delete_picked).pack(side="right", padx=(0, 6))
        ttk.Button(footer, text="Rename ID...",
                   command=_rename_picked).pack(side="right", padx=(0, 6))
        ttk.Button(footer, text="Locate selected",
                   command=_locate_picked).pack(side="right", padx=(0, 6))
        # Press Delete (or Backspace) on the tree to remove the selected
        # copy without reaching for the button.
        tree.bind("<Delete>",    lambda _e: _delete_picked())
        tree.bind("<BackSpace>", lambda _e: _delete_picked())
        dlg.bind("<Delete>",     lambda _e: _delete_picked())
        dlg.bind("<BackSpace>",  lambda _e: _delete_picked())

        # Double-click on a copy row → locate (preview only, no destructive
        # action). Locate uses the picked element's own coords, not model's,
        # so each duplicate can be inspected independently.
        tree.bind("<Double-1>", lambda _e: _locate_picked())

    def _open_check_list_dialog(self,
                                title: str,
                                rows: List[Tuple[str, str, str, str]],
                                kind: str,
                                rescan_fn) -> None:
        """Generic non-modal list dialog used by `find_nodes_without_duality`
        and `find_cells_without_node`. Each row tuple is
        (id, level, name_or_blank, signature).
        `kind` is "node", "space" or "edge" — drives Locate / Delete
        behaviour.
        `rescan_fn` returns a fresh list of rows.
        """
        existing = getattr(self, "_check_list_dlg", None)
        if existing is not None and existing.winfo_exists():
            try:
                existing.destroy()
            except Exception:
                pass

        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.geometry("680x520")
        try:
            dlg.transient(self.root)
        except Exception:
            pass
        self._check_list_dlg = dlg

        summary_var = tk.StringVar()
        ttk.Label(dlg, textvariable=summary_var,
                  font=("TkDefaultFont", 9, "bold"))\
            .pack(anchor="w", padx=8, pady=(8, 4))

        body = ttk.Frame(dlg)
        body.pack(fill="both", expand=True, padx=8, pady=4)
        tree = ttk.Treeview(
            body,
            columns=("level", "id", "name", "signature"),
            show="headings", selectmode="extended", height=18)
        tree.heading("level",     text="Floor")
        tree.heading("id",        text="ID")
        tree.heading("name",      text="Name")
        tree.heading("signature", text="Note")
        tree.column("level",     width=60,  anchor="w")
        tree.column("id",        width=240, anchor="w")
        tree.column("name",      width=180, anchor="w")
        tree.column("signature", width=180, anchor="w")
        tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        sb.pack(side="right", fill="y")
        tree.configure(yscrollcommand=sb.set)

        def _populate(data):
            tree.delete(*tree.get_children())
            for fid, lv, nm, sig in data:
                tree.insert("", "end", values=(lv, fid, nm, sig))
            summary_var.set(f"{len(data)} entry(ies). "
                            "Double-click to locate; "
                            "Delete button removes the selected one.")
        _populate(rows)

        def _go(_evt=None):
            sel = tree.selection()
            if not sel:
                return
            lv, fid, _, _ = tree.item(sel[0], "values")
            if lv and lv != "?" and lv in self.floor_combo["values"]:
                self.floor_var.set(lv)
            cx = cy = None
            if kind == "node":
                n = self.model.nodes.get(fid)
                if n and n.coord:
                    cx, cy = n.coord[0], n.coord[1]
                self._view_selected_type = "node"
                self._view_selected_id   = fid
            elif kind == "edge":
                e = self.model.edges.get(fid)
                if e and len(e.coords) >= 2:
                    # Centre on the segment's midpoint so both endpoints
                    # stay in frame.
                    cx = (e.coords[0][0] + e.coords[-1][0]) / 2.0
                    cy = (e.coords[0][1] + e.coords[-1][1]) / 2.0
                elif e and e.connects:
                    # Degenerate geometry — fall back to a live endpoint.
                    for ref in e.connects:
                        nd = self.model.nodes.get(ref)
                        if nd and nd.coord:
                            cx, cy = nd.coord[0], nd.coord[1]
                            break
                self._view_selected_type = "edge"
                self._view_selected_id   = fid
            else:
                sp = self.model.spaces.get(fid)
                if sp and sp.polygons_3d:
                    xs = [p[0] for p in sp.polygons_3d[0]]
                    ys = [p[1] for p in sp.polygons_3d[0]]
                    if xs and ys:
                        cx = sum(xs)/len(xs); cy = sum(ys)/len(ys)
                self._view_selected_type = "space"
                self._view_selected_id   = fid
            self._view_selected_override_xy = None
            self._view_selected_override_poly = None
            if cx is not None and cy is not None:
                xlim = self.ax.get_xlim(); ylim = self.ax.get_ylim()
                w = abs(xlim[1]-xlim[0]) * 0.5 or 5.0
                h = abs(ylim[1]-ylim[0]) * 0.5 or 5.0
                self.ax.set_xlim(cx-w/2, cx+w/2)
                self.ax.set_ylim(cy-h/2, cy+h/2)
                self._view_locked = True
            self.refresh_view()
        tree.bind("<Double-1>", _go)

        def _delete_one(fid):
            """Delete a single node/space without re-prompting."""
            if kind == "node":
                node = self.model.nodes.pop(fid, None)
                self._new_node_ids.discard(fid)
                # Cascade-delete edges referencing this node id
                edges_to_del = [eid for eid, e in self.model.edges.items()
                                if fid in e.connects]
                for eid in edges_to_del:
                    edge = self.model.edges.pop(eid, None)
                    self._new_edge_ids.discard(eid)
                    if (edge is not None
                            and edge.source_element is not None
                            and self.model.source_tree is not None):
                        root_ = self.model.source_tree.getroot()
                        parent_of: Dict[ET.Element, ET.Element] = {}
                        for parent in root_.iter():
                            for child in parent:
                                parent_of[child] = parent
                        ep = parent_of.get(edge.source_element)
                        if ep is not None:
                            epp = parent_of.get(ep)
                            if (epp is not None
                                    and local_name(ep.tag) == "edgeMember"):
                                try: epp.remove(ep)
                                except ValueError: pass
                            else:
                                try: ep.remove(edge.source_element)
                                except ValueError: pass
                # Remove the node element from XML
                if (node is not None
                        and node.source_element is not None
                        and self.model.source_tree is not None):
                    root_ = self.model.source_tree.getroot()
                    parent_of: Dict[ET.Element, ET.Element] = {}
                    for parent in root_.iter():
                        for child in parent:
                            parent_of[child] = parent
                    target = node.source_element
                    p = parent_of.get(target)
                    if p is not None:
                        gp = parent_of.get(p)
                        if (gp is not None
                                and local_name(p.tag) == "nodeMember"):
                            try: gp.remove(p)
                            except ValueError: pass
                        else:
                            try: p.remove(target)
                            except ValueError: pass
                # Drop the auto-generated VS dual cell, if any
                vs_id = f"VS_{fid}"
                if vs_id in self.model.spaces:
                    self.model.spaces.pop(vs_id, None)
                    self._new_space_ids.discard(vs_id)
            elif kind == "edge":
                edge = self.model.edges.pop(fid, None)
                self._new_edge_ids.discard(fid)
                if (edge is not None
                        and edge.source_element is not None
                        and self.model.source_tree is not None):
                    root_ = self.model.source_tree.getroot()
                    parent_of: Dict[ET.Element, ET.Element] = {}
                    for parent in root_.iter():
                        for child in parent:
                            parent_of[child] = parent
                    target = edge.source_element
                    p = parent_of.get(target)
                    if p is not None:
                        gp = parent_of.get(p)
                        if (gp is not None
                                and local_name(p.tag) == "edgeMember"):
                            try: gp.remove(p)
                            except ValueError: pass
                        else:
                            try: p.remove(target)
                            except ValueError: pass
            else:                                   # space
                sp = self.model.spaces.pop(fid, None)
                self._new_space_ids.discard(fid)
                if (sp is not None
                        and sp.source_element is not None
                        and self.model.source_tree is not None):
                    root_ = self.model.source_tree.getroot()
                    parent_of: Dict[ET.Element, ET.Element] = {}
                    for parent in root_.iter():
                        for child in parent:
                            parent_of[child] = parent
                    target = sp.source_element
                    p = parent_of.get(target)
                    if p is not None:
                        gp = parent_of.get(p)
                        if (gp is not None
                                and local_name(p.tag) == "cellSpaceMember"):
                            try: gp.remove(p)
                            except ValueError: pass
                        else:
                            try: p.remove(target)
                            except ValueError: pass

        def _delete():
            """Delete every selected row in one go (multi-select via
            Ctrl/Shift click in the tree, or marquee)."""
            sels = list(tree.selection())
            if not sels:
                messagebox.showinfo("Delete", "Pick one or more rows first.",
                                    parent=dlg)
                return
            ids = [tree.item(s, "values")[1] for s in sels]
            if len(ids) == 1:
                msg = (f"Remove this {kind} '{ids[0]}'?\n"
                       "Save the file to persist.")
            else:
                preview = "\n".join(f"  • {x}" for x in ids[:10])
                more = "" if len(ids) <= 10 else f"\n  … +{len(ids)-10} more"
                msg = (f"Remove {len(ids)} {kind}(s)?\n\n"
                       f"{preview}{more}\n\nSave the file to persist.")
            if not messagebox.askyesno("Delete", msg, parent=dlg):
                return
            for fid in ids:
                _delete_one(fid)
            _populate(rescan_fn())
            self.refresh_view()

        footer = ttk.Frame(dlg)
        footer.pack(fill="x", padx=8, pady=8)
        ttk.Button(footer, text="Delete",
                   command=_delete).pack(side="right", padx=(0, 6))
        ttk.Button(footer, text="Refresh",
                   command=lambda: _populate(rescan_fn())
                   ).pack(side="right", padx=(0, 6))
        ttk.Button(footer, text="Close",
                   command=dlg.destroy).pack(side="right")
        # Delete key shortcut on the tree / dialog
        tree.bind("<Delete>",    lambda _e: _delete())
        tree.bind("<BackSpace>", lambda _e: _delete())
        dlg.bind("<Delete>",     lambda _e: _delete())
        dlg.bind("<BackSpace>",  lambda _e: _delete())

    def find_nodes_without_duality(self) -> None:
        """List Nodes whose `<duality>` href is empty or points at a
        CellSpace that does not exist in the model."""
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return

        def _scan():
            rows: List[Tuple[str, str, str, str]] = []
            for nid, n in self.model.nodes.items():
                d = (n.duality or "").strip()
                if not d:
                    note = "duality empty"
                elif d not in self.model.spaces:
                    note = f"dangling → '{d}'"
                else:
                    continue                   # ok
                rows.append((nid, n.level or "?", "", note))
            rows.sort(key=lambda r: (r[1], r[0]))
            return rows

        self._open_check_list_dialog(
            "Nodes without Duality",
            _scan(),
            kind="node",
            rescan_fn=_scan,
        )

    def find_broken_edge_connections(self) -> None:
        """List Edges whose endpoints do not describe a valid transition.

        An IndoorGML Edge joins exactly two distinct Nodes. Four ways a
        source file breaks that, each reported with its own note:

        * ``endpoints: N``   — not exactly two ``<connects>``.
        * ``dangling → 'X'`` — an endpoint naming a Node that is nowhere in
          the file. Such an edge cannot be traversed and is invisible to
          node-based cascade deletes.
        * ``self-loop``      — both endpoints on the same Node, so the edge
          expresses no transition at all. Usually a converter bug that wrote
          one cell's href twice.
        * ``duality → 'X'``  — the edge's dual CellBoundary is missing. The
          edge still works for routing, so this is listed last and on its own.
        """
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return

        def _node_floor(ref: str) -> str:
            n = self.model.nodes.get(ref)
            if n is None:
                return ""
            if n.level:
                return n.level
            sp = self.model.spaces.get(n.duality or "")
            return (sp.level if sp and sp.level else "") or ""

        def _scan():
            rows: List[Tuple[str, str, str, str]] = []
            known_ids = (set(self.model.spaces)
                         | set(self.model.nodes)
                         | set(self.model.edges))
            for eid, e in self.model.edges.items():
                refs = list(e.connects)
                notes: List[str] = []

                if len(refs) != 2:
                    notes.append(f"endpoints: {len(refs)}")

                missing = [r for r in refs if r not in self.model.nodes]
                for r in dict.fromkeys(missing):
                    notes.append(f"dangling → '{r}'")

                if len(refs) == 2 and refs[0] == refs[1]:
                    notes.append("self-loop")

                dual = (e.duality or "").strip()
                if dual and dual not in known_ids:
                    notes.append(f"duality → '{dual}'")

                if not notes:
                    continue

                floor = next((f for f in (_node_floor(r) for r in refs) if f), "?")
                rows.append((eid, floor, " ↔ ".join(refs) or "—",
                             "; ".join(notes)))

            # Traversal-breaking problems first, duality-only rows last.
            def _rank(row):
                note = row[3]
                if "dangling" in note or "endpoints" in note:
                    return 0
                if "self-loop" in note:
                    return 1
                return 2
            rows.sort(key=lambda r: (_rank(r), r[1], r[0]))
            return rows

        self._open_check_list_dialog(
            "Broken Edge Connections",
            _scan(),
            kind="edge",
            rescan_fn=_scan,
        )

    def find_cells_without_node(self) -> None:
        """List CellSpaces whose `<duality>` href is empty or points at a
        Node that does not exist in the model.

        Virtual cells (auto-created `VS_*` for standalone nodes) are
        included ONLY if they're orphans — i.e. their dual node was
        deleted leaving the cell behind. A virtual cell whose dual
        relationship is intact is excluded since "missing duality" is
        the expected state for a freshly-created VS that the editor
        will fill in on save.
        """
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return

        def _scan():
            rows: List[Tuple[str, str, str, str]] = []
            # Index of which space ids are pointed at by some node's
            # duality — used to skip "healthy" virtual cells.
            spaces_pointed_at: Set[str] = {
                n.duality for n in self.model.nodes.values() if n.duality
            }
            for sid, sp in self.model.spaces.items():
                d = (sp.duality or "").strip()
                if not d:
                    note = "duality empty"
                elif d not in self.model.nodes:
                    note = f"dangling → '{d}'"
                else:
                    continue                          # healthy
                # If this is a virtual cell whose dual node DOES point
                # back at it, it's healthy from the node side — skip it
                # (the "missing duality" on the cell side is harmless).
                is_vs = (sp.feature_id.startswith("VS_")
                         and not sp.polygons_3d) or getattr(
                             sp, "is_virtual", False)
                if is_vs and sid in spaces_pointed_at:
                    continue
                # Tag orphan virtual cells in the note for clarity.
                if is_vs:
                    note = f"orphan VS — {note}"
                rows.append((sid, sp.level or "?", sp.name or "", note))
            rows.sort(key=lambda r: (r[1], r[0]))
            return rows

        self._open_check_list_dialog(
            "Cells without Dual Node",
            _scan(),
            kind="space",
            rescan_fn=_scan,
        )

    def normalize_floor_z(self) -> None:
        """Force every cell + node on each floor onto its canonical
        floor / ceiling Z. Useful for files in which some elements lost
        their Z (e.g. saved as z=0 by an older converter).

        The dialog asks for a `Floor Z` AND a `Ceiling Z` per level so
        the cells get a real 3D extrusion (floor ring at floor_z,
        ceiling ring at ceiling_z, walls regenerated on save). Nodes
        are placed at floor_z and edge endpoint Z values follow the
        node Z.
        """
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return

        from collections import Counter as _Counter
        floors = sorted({sp.level for sp in self.model.spaces.values()
                         if sp.level})
        if not floors:
            messagebox.showinfo("Normalize Floor Z",
                                "No floor information found.")
            return

        # ── Build proposals (floor_z, ceiling_z) per level ──────────
        proposals: Dict[str, Tuple[float, float]] = {}
        for fl in floors:
            ovr = self._floor_z_overrides.get(fl)
            if ovr is not None and len(ovr) >= 2:
                proposals[fl] = (float(ovr[0]), float(ovr[1]))
                continue
            # Most common non-zero floor and ceiling Z among existing
            # cells; if everything is zero, default the ceiling to
            # floor + 3000 mm.
            f_zs, c_zs = [], []
            for sp in self.model.spaces.values():
                if sp.level != fl or not sp.polygons_3d:
                    continue
                f_zs.append(sp.polygons_3d[0][0][2])
                if len(sp.polygons_3d) >= 2:
                    c_zs.append(sp.polygons_3d[1][0][2])
            def _pick(zs: List[float], default: float) -> float:
                if not zs:
                    return default
                non_zero = [z for z in zs if z != 0]
                ctr = _Counter(non_zero or zs)
                return float(ctr.most_common(1)[0][0])
            f_pick = _pick(f_zs, 0.0)
            c_pick = _pick(c_zs, f_pick + 300.0)
            if c_pick <= f_pick:
                c_pick = f_pick + 300.0
            proposals[fl] = (f_pick, c_pick)

        # ── Dialog ──────────────────────────────────────────────────
        dlg = tk.Toplevel(self.root)
        dlg.title("Normalize Floor Z")
        dlg.resizable(False, False)
        dlg.grab_set()
        ttk.Label(dlg,
                  text=("Set canonical Floor Z and Ceiling Z (cm) for "
                        "each level.\nEvery cell on that level will be "
                        "rebuilt with floor ring @ Floor Z and\n"
                        "ceiling ring @ Ceiling Z; nodes move to Floor Z."),
                  justify="left").grid(
            row=0, column=0, columnspan=4, padx=12, pady=(12, 8),
            sticky="w")
        ttk.Label(dlg, text="Floor",
                  font=("TkDefaultFont", 9, "bold")
                  ).grid(row=1, column=0, padx=(12, 4), pady=2, sticky="e")
        ttk.Label(dlg, text="Floor Z (cm)",
                  font=("TkDefaultFont", 9, "bold")
                  ).grid(row=1, column=1, padx=4, pady=2, sticky="w")
        ttk.Label(dlg, text="Ceiling Z (cm)",
                  font=("TkDefaultFont", 9, "bold")
                  ).grid(row=1, column=2, padx=4, pady=2, sticky="w")
        ttk.Label(dlg, text="Current",
                  font=("TkDefaultFont", 9, "bold")
                  ).grid(row=1, column=3, padx=(4, 12), pady=2, sticky="w")

        f_vars: Dict[str, tk.StringVar] = {}
        c_vars: Dict[str, tk.StringVar] = {}
        for i, fl in enumerate(floors):
            ttk.Label(dlg, text=fl).grid(
                row=2 + i, column=0, padx=(12, 4), pady=2, sticky="e")
            fv = tk.StringVar(value=f"{proposals[fl][0]:g}")
            cv = tk.StringVar(value=f"{proposals[fl][1]:g}")
            f_vars[fl] = fv; c_vars[fl] = cv
            ttk.Entry(dlg, textvariable=fv, width=11,
                      font=("TkDefaultFont", 9, "bold")).grid(
                row=2 + i, column=1, padx=4, pady=2, sticky="w")
            ttk.Entry(dlg, textvariable=cv, width=11,
                      font=("TkDefaultFont", 9, "bold")).grid(
                row=2 + i, column=2, padx=4, pady=2, sticky="w")
            f_zs = []; c_zs = []
            for sp in self.model.spaces.values():
                if sp.level != fl or not sp.polygons_3d:
                    continue
                f_zs.append(sp.polygons_3d[0][0][2])
                if len(sp.polygons_3d) >= 2:
                    c_zs.append(sp.polygons_3d[1][0][2])
            cur_f = ", ".join(f"{z:g}×{c}"
                              for z, c in sorted(_Counter(f_zs).items()))
            cur_c = ", ".join(f"{z:g}×{c}"
                              for z, c in sorted(_Counter(c_zs).items()))
            cur_summary = f"floor[{cur_f or '—'}]  ceil[{cur_c or '—'}]"
            ttk.Label(dlg, text=cur_summary, foreground="#666",
                      font=("TkDefaultFont", 8)).grid(
                row=2 + i, column=3, padx=(4, 12), pady=2, sticky="w")

        confirmed = [False]
        def _ok():
            for fl in floors:
                try:
                    fz = float(f_vars[fl].get().strip())
                    cz = float(c_vars[fl].get().strip())
                except ValueError:
                    messagebox.showwarning(
                        "Normalize Floor Z",
                        f"Floor '{fl}': both Z values must be numbers.",
                        parent=dlg)
                    return
                if cz <= fz:
                    messagebox.showwarning(
                        "Normalize Floor Z",
                        f"Floor '{fl}': Ceiling Z ({cz}) must be greater "
                        f"than Floor Z ({fz}).", parent=dlg)
                    return
            confirmed[0] = True
            dlg.destroy()
        btn = ttk.Frame(dlg)
        btn.grid(row=2 + len(floors), column=0, columnspan=4, pady=10)
        ttk.Button(btn, text="OK", command=_ok).pack(side="left", padx=4)
        ttk.Button(btn, text="Cancel", command=dlg.destroy
                   ).pack(side="left", padx=4)
        dlg.wait_window()
        if not confirmed[0]:
            return

        targets: Dict[str, Tuple[float, float]] = {
            fl: (float(f_vars[fl].get().strip()),
                 float(c_vars[fl].get().strip()))
            for fl in floors
        }

        # ── Apply ───────────────────────────────────────────────────
        n_cells = 0
        n_nodes = 0
        for sp in self.model.spaces.values():
            if not sp.level or sp.level not in targets:
                continue
            fz, cz = targets[sp.level]
            if not sp.polygons_3d:
                continue
            # Reduce to (floor_ring, ceiling_ring) — walls regenerate
            # on save from `_add_new_spaces_to_xml`. Pull (x, y) from
            # the EXISTING floor ring so the footprint is preserved.
            base_ring = sp.polygons_3d[0]
            new_floor_ring = [(x, y, fz) for x, y, _ in base_ring]
            new_ceil_ring  = [(x, y, cz) for x, y, _ in base_ring]
            if (sp.polygons_3d[0] != new_floor_ring
                    or (len(sp.polygons_3d) >= 2
                        and sp.polygons_3d[1] != new_ceil_ring)
                    or len(sp.polygons_3d) > 2):
                sp.polygons_3d = [new_floor_ring, new_ceil_ring]
                if sp.centers:
                    sp.centers = [(c[0], c[1], fz) for c in sp.centers]
                # Force the writer to regenerate the geometry block
                sp.source_element = None
                self._new_space_ids.add(sp.feature_id)
                # Persist the override so future New Cell on this floor
                # uses the right Z without needing a per-cell touch-up.
                self._floor_z_overrides[sp.level] = (fz, cz)
                n_cells += 1
        for n in self.model.nodes.values():
            if not n.level or n.level not in targets or n.coord is None:
                continue
            fz, _ = targets[n.level]
            if n.coord[2] != fz:
                n.coord = (n.coord[0], n.coord[1], fz)
                n.source_element = None
                self._new_node_ids.add(n.node_id)
                n_nodes += 1
        # Re-sync edge endpoint Z to follow node Z
        for e in self.model.edges.values():
            if not e.coords or len(e.coords) < 2:
                continue
            new_coords = list(e.coords)
            for i, ep_id in enumerate(e.connects[:2]):
                node = self.model.nodes.get(ep_id)
                if node is None or node.coord is None:
                    continue
                idx = 0 if i == 0 else -1
                old = new_coords[idx]
                if old[2] != node.coord[2]:
                    new_coords[idx] = (old[0], old[1], node.coord[2])
            if new_coords != list(e.coords):
                e.coords = new_coords
                e.source_element = None
                self._new_edge_ids.add(e.edge_id)

        # Save the new overrides so the next session uses them too
        for fl, (fz, cz) in targets.items():
            self._floor_z_overrides[fl] = (fz, cz)

        messagebox.showinfo(
            "Normalize Floor Z",
            f"Updated {n_cells} CellSpace(s) and {n_nodes} Node(s).\n"
            "Save the file to persist.")
        self.refresh_view()

    def find_duality_conflicts(self) -> None:
        """List places where the IndoorGML 2.0 1:1 cell ↔ node duality
        is broken:

        1. Multiple cells claim the SAME node as their dual
           (e.g. ``VS_node_new_210`` + ``door_new_13`` both have
           ``<duality href="#node_new_210">``).
        2. Multiple nodes claim the SAME cell as their dual.
        3. Asymmetric duality — cell → node but node → some other cell
           (or vice versa).

        Typical cause for case 1: a standalone node was given an auto
        ``VS_*`` virtual cell, and later a real cell (door / space)
        was created that took over the duality. The leftover
        ``VS_*`` becomes a duality conflict.
        """
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return

        # Reuse-or-recreate dialog handle
        existing = getattr(self, "_dual_conflict_dlg", None)
        if existing is not None and existing.winfo_exists():
            try:
                existing.destroy()
            except Exception:
                pass

        dlg = tk.Toplevel(self.root)
        dlg.title("Duality Conflicts")
        dlg.geometry("780x520")
        try:
            dlg.transient(self.root)
        except Exception:
            pass
        self._dual_conflict_dlg = dlg

        summary_var = tk.StringVar()
        ttk.Label(dlg, textvariable=summary_var,
                  font=("TkDefaultFont", 9, "bold"))\
            .pack(anchor="w", padx=8, pady=(8, 4))

        body = ttk.Frame(dlg)
        body.pack(fill="both", expand=True, padx=8, pady=4)
        tree = ttk.Treeview(
            body,
            columns=("kind", "id", "level", "info"),
            show="tree headings", selectmode="extended", height=18)
        tree.heading("#0",    text="")
        tree.heading("kind",  text="Conflict")
        tree.heading("id",    text="ID")
        tree.heading("level", text="Floor")
        tree.heading("info",  text="Detail")
        tree.column("#0",    width=24,  stretch=False)
        tree.column("kind",  width=180, anchor="w")
        tree.column("id",    width=240, anchor="w")
        tree.column("level", width=60,  anchor="w")
        tree.column("info",  width=240, anchor="w")
        tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        sb.pack(side="right", fill="y")
        tree.configure(yscrollcommand=sb.set)

        row_to_target: Dict[str, Tuple[str, str]] = {}   # row → (kind, id)

        def _scan_and_populate():
            tree.delete(*tree.get_children())
            row_to_target.clear()

            from collections import defaultdict as _dd

            # Case 1: nodes referenced by ≥ 2 cells
            cells_per_node: Dict[str, List[str]] = _dd(list)
            for sid, sp in self.model.spaces.items():
                if sp.duality:
                    cells_per_node[sp.duality].append(sid)
            shared_nodes = [(nid, sids) for nid, sids in cells_per_node.items()
                            if len(sids) >= 2 and nid in self.model.nodes]
            shared_nodes.sort(key=lambda t: (-len(t[1]), t[0]))

            # Case 2: cells referenced by ≥ 2 nodes
            nodes_per_cell: Dict[str, List[str]] = _dd(list)
            for nid, n in self.model.nodes.items():
                if n.duality:
                    nodes_per_cell[n.duality].append(nid)
            shared_cells = [(sid, nids) for sid, nids in nodes_per_cell.items()
                            if len(nids) >= 2 and sid in self.model.spaces]
            shared_cells.sort(key=lambda t: (-len(t[1]), t[0]))

            # Case 3: asymmetric — cell.duality = N but N.duality != cell
            asym_cell_to_node: List[Tuple[str, str, str]] = []
            for sid, sp in self.model.spaces.items():
                if not sp.duality or sp.duality not in self.model.nodes:
                    continue
                n = self.model.nodes[sp.duality]
                if n.duality != sid:
                    asym_cell_to_node.append(
                        (sid, sp.duality, n.duality or ""))
            asym_cell_to_node.sort(key=lambda t: t[0])

            # Populate tree
            if shared_nodes:
                hdr = tree.insert(
                    "", "end",
                    values=(f"Multi-cell → 1 node ({len(shared_nodes)})",
                            "", "", ""), open=True)
                for nid, sids in shared_nodes:
                    n = self.model.nodes.get(nid)
                    sub = tree.insert(
                        hdr, "end",
                        values=("", nid, n.level if n else "?",
                                f"{len(sids)} cells: {', '.join(sids)}"))
                    row_to_target[sub] = ("node", nid)
                    for sid in sids:
                        sp = self.model.spaces.get(sid)
                        c = tree.insert(
                            sub, "end",
                            values=("",
                                    f"   ↳ cell {sid}",
                                    sp.level if sp else "?",
                                    "duality → " + nid))
                        row_to_target[c] = ("space", sid)

            if shared_cells:
                hdr = tree.insert(
                    "", "end",
                    values=(f"Multi-node → 1 cell ({len(shared_cells)})",
                            "", "", ""), open=True)
                for sid, nids in shared_cells:
                    sp = self.model.spaces.get(sid)
                    sub = tree.insert(
                        hdr, "end",
                        values=("", sid, sp.level if sp else "?",
                                f"{len(nids)} nodes: {', '.join(nids)}"))
                    row_to_target[sub] = ("space", sid)
                    for nid in nids:
                        n = self.model.nodes.get(nid)
                        c = tree.insert(
                            sub, "end",
                            values=("",
                                    f"   ↳ node {nid}",
                                    n.level if n else "?",
                                    "duality → " + sid))
                        row_to_target[c] = ("node", nid)

            if asym_cell_to_node:
                hdr = tree.insert(
                    "", "end",
                    values=(f"Asymmetric duality "
                            f"({len(asym_cell_to_node)})",
                            "", "", ""), open=True)
                for sid, nid, n_dual in asym_cell_to_node:
                    sp = self.model.spaces.get(sid)
                    detail = (f"cell→{nid}, but node→"
                              + (n_dual if n_dual else "(empty)"))
                    sub = tree.insert(
                        hdr, "end",
                        values=("", sid, sp.level if sp else "?", detail))
                    row_to_target[sub] = ("space", sid)

            total = (len(shared_nodes) + len(shared_cells)
                     + len(asym_cell_to_node))
            summary_var.set(
                f"{total} duality conflict group(s) — "
                f"{len(shared_nodes)} multi-cell→1node, "
                f"{len(shared_cells)} multi-node→1cell, "
                f"{len(asym_cell_to_node)} asymmetric.")

        _scan_and_populate()

        # Footer
        footer = ttk.Frame(dlg)
        footer.pack(fill="x", padx=8, pady=8)

        def _go():
            sel = tree.selection()
            if not sel or sel[0] not in row_to_target:
                return
            kind, fid = row_to_target[sel[0]]
            if kind == "node" and fid in self.model.nodes:
                n = self.model.nodes[fid]
                if n.coord:
                    if n.level and n.level in self.floor_combo["values"]:
                        self.floor_var.set(n.level)
                    cx, cy = n.coord[0], n.coord[1]
                    xlim = self.ax.get_xlim(); ylim = self.ax.get_ylim()
                    w = abs(xlim[1]-xlim[0]) * 0.5 or 5.0
                    h = abs(ylim[1]-ylim[0]) * 0.5 or 5.0
                    self.ax.set_xlim(cx-w/2, cx+w/2)
                    self.ax.set_ylim(cy-h/2, cy+h/2)
                    self._view_locked = True
                self._view_selected_type = "node"
                self._view_selected_id = fid
            elif kind == "space" and fid in self.model.spaces:
                sp = self.model.spaces[fid]
                if sp.polygons_3d:
                    xs = [p[0] for p in sp.polygons_3d[0]]
                    ys = [p[1] for p in sp.polygons_3d[0]]
                    if xs:
                        cx = sum(xs)/len(xs); cy = sum(ys)/len(ys)
                        if sp.level and sp.level in self.floor_combo["values"]:
                            self.floor_var.set(sp.level)
                        xlim = self.ax.get_xlim(); ylim = self.ax.get_ylim()
                        w = abs(xlim[1]-xlim[0]) * 0.5 or 5.0
                        h = abs(ylim[1]-ylim[0]) * 0.5 or 5.0
                        self.ax.set_xlim(cx-w/2, cx+w/2)
                        self.ax.set_ylim(cy-h/2, cy+h/2)
                        self._view_locked = True
                self._view_selected_type = "space"
                self._view_selected_id = fid
            self._view_selected_override_xy = None
            self._view_selected_override_poly = None
            self.refresh_view()
        tree.bind("<Double-1>", lambda _e: _go())

        # ---- Delete one or many selected feature rows --------------------
        def _delete_one_node(nid: str) -> int:
            """Remove a Node + cascade-delete edges referring to it.
            Returns the number of edges cascade-deleted."""
            cascaded = 0
            if nid not in self.model.nodes:
                return 0
            node = self.model.nodes.pop(nid, None)
            self._new_node_ids.discard(nid)
            # Cascade related edges
            edges_to_del = [eid for eid, e in self.model.edges.items()
                            if nid in e.connects]
            if edges_to_del and self.model.source_tree is not None:
                root_ = self.model.source_tree.getroot()
                parent_of: Dict[ET.Element, ET.Element] = {}
                for parent in root_.iter():
                    for child in parent:
                        parent_of[child] = parent
                for eid in edges_to_del:
                    edge = self.model.edges.pop(eid, None)
                    self._new_edge_ids.discard(eid)
                    cascaded += 1
                    if edge is not None and edge.source_element is not None:
                        ep = parent_of.get(edge.source_element)
                        if ep is not None:
                            epp = parent_of.get(ep)
                            if (epp is not None
                                    and local_name(ep.tag) == "edgeMember"):
                                try: epp.remove(ep)
                                except ValueError: pass
                            else:
                                try: ep.remove(edge.source_element)
                                except ValueError: pass
            # Remove the node element from XML
            if (node is not None
                    and node.source_element is not None
                    and self.model.source_tree is not None):
                root_ = self.model.source_tree.getroot()
                parent_of: Dict[ET.Element, ET.Element] = {}
                for parent in root_.iter():
                    for child in parent:
                        parent_of[child] = parent
                target = node.source_element
                p = parent_of.get(target)
                if p is not None:
                    gp = parent_of.get(p)
                    if (gp is not None
                            and local_name(p.tag) == "nodeMember"):
                        try: gp.remove(p)
                        except ValueError: pass
                    else:
                        try: p.remove(target)
                        except ValueError: pass
            # Also drop the auto-generated VS dual cell, if any
            vs_id = f"VS_{nid}"
            if vs_id in self.model.spaces:
                self.model.spaces.pop(vs_id, None)
                self._new_space_ids.discard(vs_id)
            return cascaded

        def _delete_one_space(sid: str) -> bool:
            if sid not in self.model.spaces:
                return False
            sp = self.model.spaces.pop(sid, None)
            self._new_space_ids.discard(sid)
            if (sp is not None
                    and sp.source_element is not None
                    and self.model.source_tree is not None):
                root_ = self.model.source_tree.getroot()
                parent_of: Dict[ET.Element, ET.Element] = {}
                for parent in root_.iter():
                    for child in parent:
                        parent_of[child] = parent
                target = sp.source_element
                p = parent_of.get(target)
                if p is not None:
                    gp = parent_of.get(p)
                    if (gp is not None
                            and local_name(p.tag) == "cellSpaceMember"):
                        try: gp.remove(p)
                        except ValueError: pass
                    else:
                        try: p.remove(target)
                        except ValueError: pass
            return True

        def _delete_picked():
            """Delete every selected feature row. Header rows (no model
            target) are silently skipped. Multi-select supported."""
            sels = list(tree.selection())
            picks: List[Tuple[str, str]] = [
                row_to_target[s] for s in sels if s in row_to_target
            ]
            # De-duplicate (a feature may appear under multiple sub-rows
            # — e.g. a node listed inside a "Multi-cell→1 node" group).
            seen: Set[Tuple[str, str]] = set()
            uniq: List[Tuple[str, str]] = []
            for kf in picks:
                if kf not in seen:
                    seen.add(kf); uniq.append(kf)
            if not uniq:
                messagebox.showinfo(
                    "Delete",
                    "Pick one or more conflict feature rows first "
                    "(group header rows are not deletable).",
                    parent=dlg)
                return

            if len(uniq) == 1:
                kind, fid = uniq[0]
                msg = (f"Remove {kind} '{fid}'?\n\n"
                       + ("(Cascade: also removes edges referring to it"
                          " and the auto-generated VS cell, if any.)\n"
                          if kind == "node" else "")
                       + "Save the file afterwards to persist.")
            else:
                preview = "\n".join(
                    f"  • {k} '{f}'" for k, f in uniq[:10])
                more = "" if len(uniq) <= 10 else f"\n  … +{len(uniq)-10} more"
                msg = (f"Remove {len(uniq)} selected items?\n\n"
                       f"{preview}{more}\n\n"
                       "Save the file afterwards to persist.")
            if not messagebox.askyesno("Delete", msg, parent=dlg):
                return

            removed = 0
            cascaded_total = 0
            for kind, fid in uniq:
                if kind == "node":
                    casc = _delete_one_node(fid)
                    if casc >= 0 and fid not in self.model.nodes:
                        removed += 1
                        cascaded_total += casc
                elif kind == "space":
                    if _delete_one_space(fid):
                        removed += 1

            if cascaded_total:
                messagebox.showinfo(
                    "Delete",
                    f"Removed {removed} item(s). "
                    f"{cascaded_total} dangling edge(s) cascade-deleted.\n"
                    "Save the file to persist.", parent=dlg)
            _scan_and_populate()
            self.refresh_view()

        ttk.Button(footer, text="Delete",
                   command=_delete_picked).pack(side="right", padx=(0, 6))
        ttk.Button(footer, text="Refresh",
                   command=_scan_and_populate).pack(side="right", padx=(0, 6))
        ttk.Button(footer, text="Locate selected",
                   command=_go).pack(side="right", padx=(0, 6))
        ttk.Button(footer, text="Close",
                   command=dlg.destroy).pack(side="right")
        # Delete-key shortcut
        tree.bind("<Delete>",    lambda _e: _delete_picked())
        tree.bind("<BackSpace>", lambda _e: _delete_picked())
        dlg.bind("<Delete>",     lambda _e: _delete_picked())
        dlg.bind("<BackSpace>",  lambda _e: _delete_picked())

    def find_isolated_nodes_and_spaces(self) -> None:
        """Show a dialog listing isolated Nodes and CellSpaces.

        An *isolated Node* has no edge connected to it.
        An *isolated CellSpace* has no Node whose `duality` points to it
        (i.e. no dual node anchors it in the navigation graph). Virtual
        CellSpaces (auto-created for orphan nodes) are excluded — they are
        intentionally connected only via duality, never via geometry.

        Double-click an entry to select it on the canvas (and switch the
        active floor if needed).
        """
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return

        # Build edge-incidence index over node IDs.
        # Robustness: edge.connects can sometimes carry a non-normalized
        # space-id reference (the parser left a CellSpace href because no
        # node had `duality` pointing at it). Treat the duality space-id
        # of a node as a proxy for the node id when scanning, so a node
        # whose dual space is referenced by an edge is NOT flagged
        # isolated. We also accept edges whose endpoint coords match a
        # node's coords (geometric incidence).
        nodes_with_edges: Set[str] = set()
        # Map: space id → node id (via duality)
        space_to_node: Dict[str, str] = {
            n.duality: nid for nid, n in self.model.nodes.items()
            if n.duality
        }
        XY_EPS_ISO = 0.5
        Z_EPS_ISO  = 50.0
        for e in self.model.edges.values():
            for c in e.connects:
                if c in self.model.nodes:
                    nodes_with_edges.add(c)
                elif c in space_to_node:
                    nodes_with_edges.add(space_to_node[c])
            # Geometric incidence as a final safety net
            if e.coords and len(e.coords) >= 2:
                for ep in (e.coords[0], e.coords[-1]):
                    for nid, n in self.model.nodes.items():
                        if n.coord is None:
                            continue
                        if (abs(ep[0] - n.coord[0]) < XY_EPS_ISO
                                and abs(ep[1] - n.coord[1]) < XY_EPS_ISO
                                and abs(ep[2] - n.coord[2]) < Z_EPS_ISO):
                            nodes_with_edges.add(nid)
                            break

        isolated_nodes: List[Tuple[str, str]] = []  # (node_id, level)
        for nid, n in self.model.nodes.items():
            if nid not in nodes_with_edges:
                isolated_nodes.append((nid, n.level or "?"))
        isolated_nodes.sort(key=lambda x: (x[1], x[0]))

        # Build duality-target index over space IDs
        # Virtual-cell detection robust to parse round-trip: the
        # in-memory `is_virtual` flag is dropped on reload, so we also
        # treat any cell with id "VS_…" and no geometry as virtual.
        def _is_virtual(sp) -> bool:
            if getattr(sp, "is_virtual", False):
                return True
            if sp.feature_id.startswith("VS_") and not sp.polygons_3d:
                return True
            return False

        spaces_with_dual: Set[str] = {
            n.duality for n in self.model.nodes.values() if n.duality
        }
        isolated_spaces: List[Tuple[str, str, str]] = []  # (space_id, level, name)
        for sid, sp in self.model.spaces.items():
            if _is_virtual(sp):
                continue
            if sid not in spaces_with_dual:
                isolated_spaces.append((sid, sp.level or "?", sp.name or ""))
        isolated_spaces.sort(key=lambda x: (x[1], x[0]))

        # Reuse the existing dialog if it's still open — avoids stacking
        # multiple copies as the user re-runs the command.
        existing = getattr(self, "_isolated_dlg", None)
        if existing is not None and existing.winfo_exists():
            try:
                existing.destroy()
            except Exception:
                pass

        # Non-modal dialog: the user can still interact with the canvas
        # (Cell Edit / Network Edit / etc.) while this list is open.
        # `transient` keeps it floating above the main window without
        # blocking input. `grab_set()` is intentionally NOT called.
        dlg = tk.Toplevel(self.root)
        dlg.title("Isolated Nodes & CellSpaces")
        dlg.geometry("640x520")
        try:
            dlg.transient(self.root)
        except Exception:
            pass
        self._isolated_dlg = dlg

        nb = ttk.Notebook(dlg)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        # --- Nodes tab ---
        n_frame = ttk.Frame(nb)
        nb.add(n_frame, text=f"Isolated Nodes ({len(isolated_nodes)})")
        n_tree = ttk.Treeview(
            n_frame, columns=("level", "id"), show="headings",
            selectmode="extended", height=18)
        n_tree.heading("level", text="Floor")
        n_tree.heading("id",    text="Node ID")
        n_tree.column("level", width=80, anchor="w")
        n_tree.column("id",    width=520, anchor="w")
        for nid, lv in isolated_nodes:
            n_tree.insert("", "end", values=(lv, nid))
        n_tree.pack(side="left", fill="both", expand=True)
        n_sb = ttk.Scrollbar(n_frame, orient="vertical", command=n_tree.yview)
        n_sb.pack(side="right", fill="y")
        n_tree.configure(yscrollcommand=n_sb.set)

        # --- Spaces tab ---
        s_frame = ttk.Frame(nb)
        nb.add(s_frame, text=f"Isolated CellSpaces ({len(isolated_spaces)})")
        s_tree = ttk.Treeview(
            s_frame, columns=("level", "id", "name"), show="headings",
            selectmode="extended", height=18)
        s_tree.heading("level", text="Floor")
        s_tree.heading("id",    text="CellSpace ID")
        s_tree.heading("name",  text="Name")
        s_tree.column("level", width=70,  anchor="w")
        s_tree.column("id",    width=300, anchor="w")
        s_tree.column("name",  width=220, anchor="w")
        for sid, lv, name in isolated_spaces:
            s_tree.insert("", "end", values=(lv, sid, name))
        s_tree.pack(side="left", fill="both", expand=True)
        s_sb = ttk.Scrollbar(s_frame, orient="vertical", command=s_tree.yview)
        s_sb.pack(side="right", fill="y")
        s_tree.configure(yscrollcommand=s_sb.set)

        # Select-on-canvas: switch floor + view-select the picked feature
        def _go_to_node(_evt=None):
            sel = n_tree.selection()
            if not sel:
                return
            lv, nid = n_tree.item(sel[0], "values")
            if lv and lv != "?" and lv in self.floor_combo["values"]:
                self.floor_var.set(lv)
            self._view_selected_type = "node"
            self._view_selected_id   = nid
            node = self.model.nodes.get(nid)
            if node and node.coord:
                xlim = self.ax.get_xlim()
                w = abs(xlim[1] - xlim[0]) * 0.5 or 5.0
                self.ax.set_xlim(node.coord[0]-w/2, node.coord[0]+w/2)
                ylim = self.ax.get_ylim()
                h = abs(ylim[1] - ylim[0]) * 0.5 or 5.0
                self.ax.set_ylim(node.coord[1]-h/2, node.coord[1]+h/2)
                self._view_locked = True
            self.refresh_view()

        def _go_to_space(_evt=None):
            sel = s_tree.selection()
            if not sel:
                return
            lv, sid, _ = s_tree.item(sel[0], "values")
            if lv and lv != "?" and lv in self.floor_combo["values"]:
                self.floor_var.set(lv)
            self._view_selected_type = "space"
            self._view_selected_id   = sid
            sp = self.model.spaces.get(sid)
            if sp and sp.polygons_3d:
                xs = [p[0] for p in sp.polygons_3d[0]]
                ys = [p[1] for p in sp.polygons_3d[0]]
                if xs and ys:
                    cx = sum(xs)/len(xs); cy = sum(ys)/len(ys)
                    xlim = self.ax.get_xlim()
                    w = abs(xlim[1]-xlim[0]) * 0.5 or 5.0
                    self.ax.set_xlim(cx-w/2, cx+w/2)
                    ylim = self.ax.get_ylim()
                    h = abs(ylim[1]-ylim[0]) * 0.5 or 5.0
                    self.ax.set_ylim(cy-h/2, cy+h/2)
                    self._view_locked = True
            self.refresh_view()

        n_tree.bind("<Double-1>", _go_to_node)
        s_tree.bind("<Double-1>", _go_to_space)

        # Footer
        footer = ttk.Frame(dlg)
        footer.pack(fill="x", padx=8, pady=8)
        summary_var = tk.StringVar(value=(
            f"Total: {len(isolated_nodes)} isolated nodes, "
            f"{len(isolated_spaces)} isolated cell spaces. "
            "Double-click a row to locate it."))
        ttk.Label(footer, textvariable=summary_var).pack(side="left")

        def _rescan():
            """Recompute lists in place — handy after editing the model
            (creating edges, deleting nodes, etc.) without closing the
            dialog."""
            if self.model is None:
                return
            n_with_e: Set[str] = set()
            # Same conservative incidence check as the initial scan.
            s2n: Dict[str, str] = {
                nn.duality: nid_ for nid_, nn in self.model.nodes.items()
                if nn.duality
            }
            for ed in self.model.edges.values():
                for cc in ed.connects:
                    if cc in self.model.nodes:
                        n_with_e.add(cc)
                    elif cc in s2n:
                        n_with_e.add(s2n[cc])
                if ed.coords and len(ed.coords) >= 2:
                    for ep in (ed.coords[0], ed.coords[-1]):
                        for nid_, nn in self.model.nodes.items():
                            if nn.coord is None:
                                continue
                            if (abs(ep[0] - nn.coord[0]) < 0.5
                                    and abs(ep[1] - nn.coord[1]) < 0.5
                                    and abs(ep[2] - nn.coord[2]) < 50.0):
                                n_with_e.add(nid_)
                                break
            new_iso_n = sorted(
                ((nid, n.level or "?") for nid, n in self.model.nodes.items()
                 if nid not in n_with_e),
                key=lambda x: (x[1], x[0]))
            sp_with_d: Set[str] = {
                n.duality for n in self.model.nodes.values() if n.duality}
            def _is_virt(sp) -> bool:
                if getattr(sp, "is_virtual", False):
                    return True
                if sp.feature_id.startswith("VS_") and not sp.polygons_3d:
                    return True
                return False
            new_iso_s = sorted(
                ((sid, sp.level or "?", sp.name or "")
                 for sid, sp in self.model.spaces.items()
                 if not _is_virt(sp) and sid not in sp_with_d),
                key=lambda x: (x[1], x[0]))

            n_tree.delete(*n_tree.get_children())
            for nid, lv in new_iso_n:
                n_tree.insert("", "end", values=(lv, nid))
            s_tree.delete(*s_tree.get_children())
            for sid, lv, name in new_iso_s:
                s_tree.insert("", "end", values=(lv, sid, name))
            nb.tab(0, text=f"Isolated Nodes ({len(new_iso_n)})")
            nb.tab(1, text=f"Isolated CellSpaces ({len(new_iso_s)})")
            summary_var.set(
                f"Total: {len(new_iso_n)} isolated nodes, "
                f"{len(new_iso_s)} isolated cell spaces. "
                "Double-click a row to locate it.")

        def _delete_one_node(nid):
            if nid not in self.model.nodes:
                return False
            node = self.model.nodes.pop(nid, None)
            self._new_node_ids.discard(nid)
            if (node is not None
                    and node.source_element is not None
                    and self.model.source_tree is not None):
                root_ = self.model.source_tree.getroot()
                parent_of: Dict[ET.Element, ET.Element] = {}
                for parent in root_.iter():
                    for child in parent:
                        parent_of[child] = parent
                target = node.source_element
                p = parent_of.get(target)
                if p is not None:
                    gp = parent_of.get(p)
                    if (gp is not None
                            and local_name(p.tag) == "nodeMember"):
                        try: gp.remove(p)
                        except ValueError: pass
                    else:
                        try: p.remove(target)
                        except ValueError: pass
            vs_id = f"VS_{nid}"
            if vs_id in self.model.spaces:
                self.model.spaces.pop(vs_id, None)
                self._new_space_ids.discard(vs_id)
            return True

        def _delete_one_space(sid):
            if sid not in self.model.spaces:
                return False
            sp = self.model.spaces.pop(sid, None)
            self._new_space_ids.discard(sid)
            if (sp is not None
                    and sp.source_element is not None
                    and self.model.source_tree is not None):
                root_ = self.model.source_tree.getroot()
                parent_of: Dict[ET.Element, ET.Element] = {}
                for parent in root_.iter():
                    for child in parent:
                        parent_of[child] = parent
                target = sp.source_element
                p = parent_of.get(target)
                if p is not None:
                    gp = parent_of.get(p)
                    if (gp is not None
                            and local_name(p.tag) == "cellSpaceMember"):
                        try: gp.remove(p)
                        except ValueError: pass
                    else:
                        try: p.remove(target)
                        except ValueError: pass
            return True

        def _delete_selected():
            """Delete every selected row in the currently visible tab.
            Multi-select via Ctrl/Shift click is supported.
            """
            if self.model is None:
                return
            try:
                tab_idx = nb.index(nb.select())
            except Exception:
                tab_idx = 0
            tree = n_tree if tab_idx == 0 else s_tree
            sels = list(tree.selection())
            if not sels:
                messagebox.showinfo(
                    "Delete", "Pick one or more rows first.", parent=dlg)
                return
            ids = [tree.item(s, "values")[1] for s in sels]
            kind = "node" if tab_idx == 0 else "CellSpace"
            if len(ids) == 1:
                msg = (f"Remove isolated {kind} '{ids[0]}'?\n"
                       "Save the file afterwards to persist.")
            else:
                preview = "\n".join(f"  • {x}" for x in ids[:10])
                more = "" if len(ids) <= 10 else f"\n  … +{len(ids)-10} more"
                msg = (f"Remove {len(ids)} isolated {kind}(s)?\n\n"
                       f"{preview}{more}\n\n"
                       "Save the file afterwards to persist.")
            if not messagebox.askyesno("Delete", msg, parent=dlg):
                return

            removed = 0
            for fid in ids:
                if tab_idx == 0:
                    if _delete_one_node(fid):
                        removed += 1
                else:
                    if _delete_one_space(fid):
                        removed += 1

            _rescan()
            self.refresh_view()

        ttk.Button(footer, text="Delete",
                   command=_delete_selected).pack(side="right", padx=(0, 6))
        ttk.Button(footer, text="Refresh",
                   command=_rescan).pack(side="right", padx=(0, 6))
        ttk.Button(footer, text="Close",
                   command=dlg.destroy).pack(side="right")
        # Delete key shortcut — works whether focus is on either tab tree
        # or on the dialog itself.
        n_tree.bind("<Delete>",    lambda _e: _delete_selected())
        n_tree.bind("<BackSpace>", lambda _e: _delete_selected())
        s_tree.bind("<Delete>",    lambda _e: _delete_selected())
        s_tree.bind("<BackSpace>", lambda _e: _delete_selected())
        dlg.bind("<Delete>",       lambda _e: _delete_selected())
        dlg.bind("<BackSpace>",    lambda _e: _delete_selected())

    def create_virtual_cellspaces_for_orphan_nodes(self) -> None:
        """IndoorGML 2.0 requires each Node to have a dual CellSpace.
        Scan the model for nodes whose duality is empty or dangling, and
        create a virtual CellSpace for each:

          • id       = "VS_" + node_id
          • name     = "Virtual_" + (node name or node_id)
          • geometry = none
          • external reference = none
          • poi      = false
          • duality  ↔ the source node

        The created virtual CellSpaces are registered as new spaces so they
        are written to XML on next Save.
        """
        if self.model is None:
            messagebox.showwarning("No file", "Open a file first.")
            return

        existing_space_ids = set(self.model.spaces.keys())
        orphans: List[str] = []
        for nid, node in self.model.nodes.items():
            dual = (node.duality or "").strip()
            if not dual or dual not in existing_space_ids:
                orphans.append(nid)

        if not orphans:
            messagebox.showinfo(
                "Virtual CellSpaces",
                "No standalone nodes found — every node already has a dual CellSpace.")
            return

        if not messagebox.askyesno(
            "Create Virtual CellSpaces",
            f"{len(orphans)} standalone node(s) found.\n\n"
            f"Create a virtual CellSpace (VS_<node>) for each?"
        ):
            return

        # Detect XLINK namespace so we can patch existing Node elements' duality.
        xlink_ns = ""
        if self.model.source_tree is not None:
            try:
                _, _, xlink_ns = _detect_namespaces(self.model.source_tree.getroot())
            except Exception:
                xlink_ns = "http://www.w3.org/1999/xlink"

        created: List[str] = []
        skipped: List[str] = []
        for nid in orphans:
            node = self.model.nodes[nid]
            vs_id = f"VS_{nid}"
            if vs_id in self.model.spaces:
                # Someone created one already — just re-link it.
                node.duality = vs_id
                skipped.append(nid)
                continue

            display_name = (node.node_id if not hasattr(node, "name") else nid)
            # NodeFeature has no "name" field — use node_id as human name source
            virtual_name = f"Virtual_{nid}"

            vs = SpaceFeature(
                feature_id=vs_id,
                name=virtual_name,
                cls="",
                level=node.level,
                polygons_3d=[],              # no geometry
                centers=[node.coord] if node.coord else [],
                duality=nid,
                layer_id=node.layer_id or "",
                is_poi=False,
                is_virtual=True,
            )
            self.model.spaces[vs_id] = vs
            self._new_space_ids.add(vs_id)

            # Link node → virtual space.
            node.duality = vs_id
            # If the node came from the source XML, patch its element so the
            # new duality reference shows up in the saved file.
            if node.source_element is not None:
                # Replace any existing duality element(s), then append a fresh one.
                for child in list(node.source_element):
                    if local_name(child.tag) == "duality":
                        node.source_element.remove(child)
                ns = node.source_element.tag.split("}")[0][1:] if "}" in node.source_element.tag else ""
                core_prefix = f"{{{ns}}}" if ns else ""
                dual_elem = ET.SubElement(node.source_element, f"{core_prefix}duality")
                if xlink_ns:
                    dual_elem.attrib[f"{{{xlink_ns}}}href"] = f"#{vs_id}"
                else:
                    dual_elem.attrib["href"] = f"#{vs_id}"

            created.append(vs_id)

        self.refresh_view()

        summary = [f"Created {len(created)} virtual CellSpace(s)."]
        if skipped:
            summary.append(f"Re-linked {len(skipped)} node(s) to existing VS_*.")
        if created:
            preview = ", ".join(created[:8])
            if len(created) > 8:
                preview += ", ..."
            summary.append("\nNew: " + preview)
        messagebox.showinfo("Virtual CellSpaces", "\n".join(summary))

        self.status_var.set(
            f"Virtual CellSpaces: +{len(created)} created, {len(skipped)} re-linked.")

    def _draw_graph_edit_overlay(self) -> None:
        if (not self._graph_edit_mode
                and not self._graph_connect_mode
                and not self._vertical_edge_mode):
            return

        # The previous version force-drew every node / edge created in the
        # current session here so they wouldn't be lost behind the floor
        # filter. That fallback is no longer needed: node.level is always
        # assigned at parse / create time, draw_on_axes uses an explicit Z
        # fallback when the level is missing, and `expand_floor_graph` pulls
        # in dually-linked nodes. Drawing them again caused off-floor new
        # objects to bleed into every floor view in edit modes.

        # Highlight first node selected for connection (star marker)
        if self._graph_connect_first:
            node = self.model.nodes.get(self._graph_connect_first)
            if node and node.coord:
                self.ax.scatter([node.coord[0]], [node.coord[1]],
                                color="lime", s=200, zorder=12, marker="*")

        # Highlight first node captured in Vertical Edge mode.
        # It is drawn even when it lives on a different floor than the current
        # view, so the user keeps visual feedback after switching Floor combo.
        # Color differs by whether the node's floor matches the current view:
        #   • same floor    → cyan
        #   • other floor   → yellow (visual cue: "this node is not on the
        #                              current floor — switch back to see it")
        if self._vertical_edge_first_node:
            node = self.model.nodes.get(self._vertical_edge_first_node)
            if node and node.coord:
                current_floor = self.floor_var.get()
                same_floor = (node.level == current_floor) or (current_floor == "All")
                marker_color = "cyan" if same_floor else "yellow"
                self.ax.scatter([node.coord[0]], [node.coord[1]],
                                color=marker_color, s=220, zorder=12, marker="*",
                                edgecolors="black", linewidths=0.8)

        # Highlight selected node (semi-transparent large circle + small solid center)
        if self._graph_selected_node and self._graph_selected_node != self._graph_connect_first:
            node = self.model.nodes.get(self._graph_selected_node)
            if node and node.coord:
                self.ax.scatter([node.coord[0]], [node.coord[1]],
                                color="orange", s=300, zorder=11, alpha=0.45)
                self.ax.scatter([node.coord[0]], [node.coord[1]],
                                color="orange", s=40, zorder=12, alpha=1.0)

        # Highlight selected edge
        if self._graph_selected_edge:
            edge = self.model.edges.get(self._graph_selected_edge)
            if edge and len(edge.coords) >= 2:
                xs = [p[0] for p in edge.coords]
                ys = [p[1] for p in edge.coords]
                self.ax.plot(xs, ys, color="orange", linewidth=3.0, zorder=10, alpha=0.8)

    def _draw_space_create_overlay(self) -> None:
        """Draw the in-progress polygon and confirmed vertices during space creation."""
        # Rectangle mode: highlight the first corner dot if set.
        if (self._rect_create_mode
                and self._rect_create_pt1 is not None):
            x1, y1 = self._rect_create_pt1
            self.ax.scatter([x1], [y1], color="green", s=60, zorder=10,
                            edgecolors="black", linewidths=1.0)
            self.ax.annotate("pt1", (x1, y1), color="green", fontsize=8,
                             xytext=(6, 6), textcoords="offset points")

        verts = self._space_create_verts
        if not verts:
            return
        # Vertices as dots
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        self.ax.scatter(xs, ys, color="green", s=50, zorder=10)
        # Closed polygon outline (without cursor pos; cursor adds to preview line)
        if len(verts) >= 2:
            cx = xs + [xs[0]]
            cy = ys + [ys[0]]
            self.ax.plot(cx, cy, color="green", linewidth=1.5, linestyle="--", zorder=9)


    def _draw_door_overlay(self) -> None:
        """Draw door mode preview: first endpoint dot if set."""
        if not self._door_mode:
            return
        if self._door_pt1 is not None:
            self.ax.scatter([self._door_pt1[0]], [self._door_pt1[1]],
                            color="magenta", s=60, zorder=10)
            self.ax.annotate("door pt1", self._door_pt1, color="magenta", fontsize=8)

    def _draw_measure_overlay(self) -> None:
        """Draw measure-mode first endpoint dot if set."""
        if not self._measure_mode or self._measure_pt1 is None:
            return
        x1, y1 = self._measure_pt1
        self.ax.scatter([x1], [y1], color="yellow",
                        s=80, edgecolors="black", linewidths=1.0, zorder=10)
        self.ax.annotate("pt1", (x1, y1), color="yellow", fontsize=8,
                         xytext=(6, 6), textcoords="offset points")

    def _draw_reference_points_overlay(self) -> None:
        """Draw the file-wide LL/UR reference anchors and any pending
        anchor-capture clicks. These are visible on every floor so the
        user can align floorplans consistently."""
        # Saved (file-wide) markers: red for LL, green for UR
        if self._reference_points is not None:
            ll, ur = self._reference_points
            self.ax.scatter([ll[0]], [ll[1]], color="red",
                            s=160, marker="X", zorder=15,
                            edgecolors="white", linewidths=1.2)
            self.ax.annotate(f"LL ({ll[0]:g}, {ll[1]:g})",
                             (ll[0], ll[1]), color="red", fontsize=8,
                             xytext=(8, -10), textcoords="offset points")
            self.ax.scatter([ur[0]], [ur[1]], color="lime",
                            s=160, marker="X", zorder=15,
                            edgecolors="white", linewidths=1.2)
            self.ax.annotate(f"UR ({ur[0]:g}, {ur[1]:g})",
                             (ur[0], ur[1]), color="lime", fontsize=8,
                             xytext=(8, 8), textcoords="offset points")

        # In-progress anchor capture: highlight already-clicked points
        # at their canvas coords so the user gets feedback before the
        # extent is solved.
        if self._anchor_mode:
            if self._anchor_canvas_pt1 is not None:
                x, y = self._anchor_canvas_pt1
                self.ax.scatter([x], [y], color="orange", s=140,
                                marker="o", zorder=16,
                                edgecolors="black", linewidths=1.2,
                                facecolors="none")
                self.ax.annotate("LL?", (x, y), color="orange",
                                 fontsize=9,
                                 xytext=(8, -10),
                                 textcoords="offset points")
            if self._anchor_canvas_pt2 is not None:
                x, y = self._anchor_canvas_pt2
                self.ax.scatter([x], [y], color="orange", s=140,
                                marker="o", zorder=16,
                                edgecolors="black", linewidths=1.2,
                                facecolors="none")
                self.ax.annotate("UR?", (x, y), color="orange",
                                 fontsize=9,
                                 xytext=(8, 8),
                                 textcoords="offset points")

    def _draw_split_overlay(self) -> None:
        """Draw polygon vertices of the split-target cell; highlight selected vertex."""
        if not self._split_mode or not self._split_space_id:
            return
        sp = self.model.spaces.get(self._split_space_id)
        if not sp or not sp.polygons_3d:
            return
        pts = _poly_open([(p[0], p[1]) for p in sp.polygons_3d[0]])
        if not pts:
            return
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        # All vertices as small yellow circles
        self.ax.scatter(xs, ys, color="yellow", s=40, zorder=11, alpha=0.8)
        # Index labels
        for i, (vx, vy) in enumerate(pts):
            self.ax.text(vx, vy, str(i), fontsize=7, color="yellow",
                         ha="center", va="bottom", zorder=12)
        # Highlight first selected vertex as a star
        if self._split_vertex_idx is not None and self._split_vertex_idx < len(pts):
            vx, vy = pts[self._split_vertex_idx]
            self.ax.scatter([vx], [vy], color="yellow", s=200, zorder=13, marker="*")

    def _finish_door_creation(self, pt1: Tuple[float, float], pt2: Tuple[float, float]) -> None:
        """두 끝점과 thickness로 TransferSpace(문) 직사각형을 생성한다."""
        x1, y1 = pt1
        x2, y2 = pt2
        dx, dy = x2 - x1, y2 - y1
        length = math.sqrt(dx * dx + dy * dy)
        if length < 1e-6:
            self.status_var.set("Door endpoints are too close.")
            return

        floor = self.floor_var.get()
        if floor == "All":
            floor = ""

        # Dialog: name, level, thickness
        dlg = tk.Toplevel(self.root)
        dlg.title("New Door (TransferSpace)")
        dlg.resizable(False, False)
        dlg.grab_set()

        ttk.Label(dlg, text="Name:").grid(row=0, column=0, padx=8, pady=6, sticky="e")
        name_var = tk.StringVar(value="")
        ttk.Entry(dlg, textvariable=name_var, width=24).grid(row=0, column=1, padx=8, pady=6)

        ttk.Label(dlg, text="Level:").grid(row=1, column=0, padx=8, pady=4, sticky="e")
        level_var = tk.StringVar(value=floor)
        ttk.Entry(dlg, textvariable=level_var, width=24).grid(row=1, column=1, padx=8, pady=4)

        ttk.Label(dlg, text="Thickness (cm):").grid(row=2, column=0, padx=8, pady=4, sticky="e")
        thick_var = tk.DoubleVar(value=self._door_thickness_var.get())
        ttk.Spinbox(dlg, from_=1.0, to=1000.0, increment=1.0,
                    textvariable=thick_var, width=10).grid(row=2, column=1, padx=8, pady=4, sticky="w")

        confirmed = [False]

        def on_ok():
            confirmed[0] = True
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        bf = ttk.Frame(dlg)
        bf.grid(row=3, column=0, columnspan=2, pady=8)
        ttk.Button(bf, text="OK",     command=on_ok).pack(side="left", padx=4)
        ttk.Button(bf, text="Cancel", command=on_cancel).pack(side="left", padx=4)
        dlg.wait_window()

        if not confirmed[0]:
            self.refresh_view()
            return

        name      = name_var.get().strip()
        level     = level_var.get().strip()
        thickness = thick_var.get()
        self._door_thickness_var.set(thickness)  # persist last-used value

        # Build rectangle: perpendicular direction scaled by thickness/2
        t = thickness / 2.0
        px, py = -dy / length * t, dx / length * t  # perpendicular offset

        verts_2d = [
            (x1 + px, y1 + py),
            (x1 - px, y1 - py),
            (x2 - px, y2 - py),
            (x2 + px, y2 + py),
        ]

        # Create TransferSpace
        existing_s = set(self.model.spaces.keys())
        i = 1
        while f"door_new_{i}" in existing_s:
            i += 1
        sid = f"door_new_{i}"

        floor_z, ceiling_z = self._floor_ceiling_z(level)
        floor_pts = [(x, y, floor_z) for x, y in verts_2d]
        ceil_pts  = [(x, y, ceiling_z) for x, y in verts_2d]
        center    = polygon_centroid(floor_pts)

        sp = SpaceFeature(
            feature_id=sid,
            name=name,
            # Doors carry the IndoorGML 2.0 ConnectionSpace subclass
            # (this gets emitted as the element's local name on save:
            # `<core:ConnectionSpace ns1:id="door_new_…">`).
            cls="ConnectionSpace",
            level=level,
            polygons_3d=[floor_pts, ceil_pts],
            centers=[center] if center else [],
        )
        self.model.spaces[sid] = sp
        self._new_space_ids.add(sid)

        # Auto-create dual node at centroid
        nid = None
        if center:
            cx, cy, cz = center
            existing_n = set(self.model.nodes.keys())
            j = 1
            while f"node_new_{j}" in existing_n:
                j += 1
            nid = f"node_new_{j}"
            self.model.nodes[nid] = NodeFeature(
                node_id=nid, coord=(cx, cy, cz),
                level=level or None, duality=sid,
            )
            self._new_node_ids.add(nid)
            sp.duality = nid

        self._push_undo({"kind": "create_space_with_node", "space_id": sid, "node_id": nid})
        self.status_var.set(f"Door {sid} created" + (f" with node {nid}." if nid else "."))
        self.refresh_view()


def main() -> None:
    root = tk.Tk()
    app = IndoorGMLViewerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

# last updated on May 14, 2026 by Ki-Joune Li with the support of Claude
