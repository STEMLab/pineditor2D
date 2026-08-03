[README.md](https://github.com/user-attachments/files/27756751/README.md)
# PyInEditor — IndoorGML 2.0 Editor

A Tkinter + Matplotlib desktop editor for **IndoorGML 2.0** files.
Inspect, edit, and refactor multi-floor indoor maps: cell spaces,
navigation graphs (nodes / edges), thematic layers, inter-layer
connections, and floorplan underlays — all backed by round-trippable
GML XML.

> Tested on Windows / Python 3.10+. Should work on macOS / Linux with
> the stock Tk that ships with Python.

> **Coordinate units throughout the editor are centimetres (cm).**
> All dialogs (Floor Z, Ceiling Z, door thickness, distance read-out)
> are labelled in cm; raw `<gml:pos>` values in saved files are in cm
> as well. A typical floor height is 300 cm.

---

## Table of contents

1. [Features](#features)
2. [Installation](#installation)
3. [Running the editor](#running-the-editor)
4. [File format and units](#file-format-and-units)
5. [User interface tour](#user-interface-tour)
6. [Editing workflows](#editing-workflows)
7. [Bulk apply modes](#bulk-apply-modes)
8. [Floor menu — floors and floorplan underlay](#floor-menu)
9. [Reference points (anchor system)](#reference-points)
10. [Layers (ThematicLayer)](#layers-thematiclayer)
11. [Inter-layer connections](#inter-layer-connections)
12. [Function codes & legend](#function-codes--legend)
13. [Keyboard shortcuts](#keyboard-shortcuts)
14. [Check menu — diagnostics & repairs](#check-menu)
15. [Selection panel and live property edits](#selection-panel)
16. [Saving and exporting](#saving-and-exporting)
17. [Troubleshooting](#troubleshooting)
18. [License](#license)

---

## Features

- **Open / save IndoorGML 2.0** XML files (`*.xml`, `*.gml`).
- Robust parser that tolerates **multi-byte encodings** declared in
  the XML prolog (cp949, EUC-KR, Shift-JIS, GB2312, …).
- **Per-floor 2D rendering** with a black canvas and white cell
  borders. Switch floors instantly with a combo box.
- **Cell editing**: create rectangles or free-style polygons, drag,
  split, and delete `CellSpace` polygons; insert / delete / move
  individual vertices with snap-to-vertex.
- **Cell subclass support**: every cell can be a
  `CellSpace` / `TransferSpace` / `ConnectionSpace` / `AnchorSpace`.
  Changing the Class in the Selection panel actually rewrites the
  XML element's tag so the file round-trips correctly.
- **Bulk apply modes**: set one function code or one Class on many
  cells at once with click-toggling and a single Apply confirmation.
- **Network editing**: create and move `Node`s and `Edge`s; connect
  any two nodes; new edges default to weight = 1.0; cross-floor
  guard refuses accidental vertical edges.
- **Vertical edges**: click a node on one floor, switch the active
  floor, click a node on another floor — the edge is created
  vertically. Downstair vertical edges are drawn in orchid, upstair
  in lime green.
- **Doors**: stamp a `ConnectionSpace` rectangle of configurable
  thickness (cm) between two clicked endpoints; the door's dual
  node is created automatically.
- **Thematic layers**: create / rename / delete layers, switch
  active / background layers.
- **Inter-layer connections** (ILC) between cells in different
  layers.
- **Floorplan underlay**: per-floor PNG/JPG/TIFF image with a
  click-capture **two-anchor (LL / UR)** alignment workflow that
  keeps every floor in the same world frame; alignment persists in
  a sidecar JSON next to the GML.
- **Function-code colouring + legend** with human-readable labels
  loaded from a simple CSV.
- **Live property edit** in the right-side Selection panel when a
  cell is selected in Cell Edit mode (Name, Class, Function via
  combobox, POI checkbox, Apply).
- **Check menu** with a full diagnostic suite: isolated /
  duality-conflict / duplicate-id detection, search, measure-
  distance, geometry repair, and per-floor Z normalisation.
- **Mutually exclusive edit modes** with a clear status line so you
  always know what a click will do.
- **Multi-select + Delete key** support in every Check dialog.

---

## Installation

```bash
git clone https://github.com/<your-account>/pyineditor.git
cd pyineditor
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
```

Dependencies:

- `numpy >= 1.21`
- `matplotlib >= 3.5`
- `tkinter` (ships with CPython on Windows / macOS; on Linux install
  `python3-tk`).

---

## Running the editor

```bash
python pineditor2D.py
```

A sample IndoorGML file is provided for a quick test.

---

## File format and units

The editor consumes and produces standard **IndoorGML 2.0**
(`http://www.opengis.net/indoorgml/2.0/core`) with the GML 3.2
geometry namespace.

**Units**: all coordinates are interpreted as **centimetres (cm)**.
A 3-metre floor height is `300`. The default ceiling distance for
new cells is 300 cm. Door thickness defaults to 20 cm.

The parser preserves:

- The original namespace prefixes used in the source file (so a file
  using `ns1:id` keeps `ns1:id`, files using `gml:id` keep
  `gml:id`).
- The original ordering of feature members.
- All non-edited elements verbatim (the editor does in-place updates
  of text content; it does not rewrite the entire tree).
- The cell subclass — `CellSpace` / `TransferSpace` /
  `ConnectionSpace` / `AnchorSpace` is carried as the XML element's
  local name and is rewritten in place when the Class is changed.

**Sidecar files** stored next to your GML:

- `<your_file>.gml.bgcfg.json` — per-floor floorplan image path /
  extent + the file-wide LL/UR reference points.

---

## User interface tour

```
┌──────────────────────────────────────────────────────────────────┐
│ File  View  Cell  Network  Door  Floor  Layer  Connect Layers  │
│ Function Codes  Check                                            │
├──────────────────────────────────────────────────────────────────┤
│ Floor: [1F  ▼]   Layer: [L1 (Navigation) ▼]   Door thk (cm) [20]│
├──────────────────────────────────────┬───────────────────────────┤
│                                      │  Selection                │
│                                      │  ─────────                │
│ ┌─Function legend─┐                  │  [CellSpace]              │
│ │ ▢ Office        │                  │  ID:    cell_AB12         │
│ │ ▢ Laboratory    │                  │  Name:  Lobby             │
│ │ ▢ Corridor      │                  │  Class: ▼ CellSpace       │
│ └─────────────────┘                  │  Func:  ▼ ROOM            │
│                                      │  Level: 1F                │
│      (Matplotlib canvas)             │  POI ☐                    │
│                                      │  [ Apply ]                │
├──────────────────────────────────────┴───────────────────────────┤
│ Status: <current edit mode hint or measurement / save status>    │
└──────────────────────────────────────────────────────────────────┘
```

- **Left mouse**: select / drag / add a vertex (depends on mode).
- **Right mouse**: pan / cancel a pending point — works during
  bulk-apply modes too.
- **Mouse wheel**: zoom under cursor.
- **Home key**: fit view to all visible objects.
- **Delete / Backspace key**: delete the selected feature (in Edit
  modes and every Check dialog).

---

## Editing workflows

Every edit mode is a checkbutton in its menu. **Only one** edit mode
is active at a time — toggling another deactivates the previous.

### Cell

| Menu item                          | What it does                                                                                                                |
|------------------------------------|-----------------------------------------------------------------------------------------------------------------------------|
| **New Cell (Rectangle)**           | Click two opposite corners → 4-vertex axis-aligned polygon. Live rectangle preview while moving the mouse. Right-click cancels first corner. Stored as a regular polygon. |
| **New Cell (Free-style Polygon)**  | Click vertices in turn; double-click or press `Enter` (with ≥ 3 vertices) to finish; right-click cancels.                   |
| **Cell Edit** (`C`)                | Click cell → click vertex → click again to drag, with snap-to-vertex. Click a cell-edge to insert a vertex. Drag inside a selected cell to translate it. `Delete` removes the selected vertex / cell. While in Cell Edit mode the Selection panel switches to an editable form (see [Selection panel](#selection-panel)). |
| **Cell Split**                     | Click a cell, then two of its vertices to split it along that diagonal. Two new cells are created with new dual nodes and an edge between them. |
| **Class Bulk Apply**               | See [Bulk apply modes](#bulk-apply-modes).                                                                                   |

### Network

| Menu item              | What it does                                                                                                              |
|------------------------|---------------------------------------------------------------------------------------------------------------------------|
| **Node Edit** (`N`)    | Click empty space to create a new (standalone) node. Click an existing node to select; click again to drag. `Delete` removes the node and all its incident edges. Standalone nodes automatically get a virtual `CellSpace` (`VS_<node_id>`) for IndoorGML duality compliance. |
| **Edge Edit** (`E`)    | Click first node, then second node → an edge is created. Click an edge to select; `Delete` removes it. New edges default to weight = 1.0. **Cross-floor guard**: refuses to create an edge between nodes on different floors unless you are in Vertical Edge mode. |
| **Vertical Edge**      | Click a node on the current floor → switch the Floor combo to a different floor → click a node on that floor. An edge is created connecting the two nodes vertically (preserves both Z values). Downstair vertical edges render in **orchid**, upstair in **lime green**. |

### Door

**New Door** (`D`) — stamp a doorway as a thin **ConnectionSpace**
rectangle. Click two endpoints; a dialog asks for the thickness in
cm (default reuses the last value). The door's centroid spawns a
dual node, and the door is connected via duality. The newly created
door cell's Class is automatically set to `ConnectionSpace` and is
written that way to the XML element tag on save.

---

## Bulk apply modes

Two click-based bulk editors share the same workflow — one for the
**function code**, one for the **Class** field. They are mutually
exclusive with each other and with every other edit mode.

### Function Codes → "Multiple Cells (bulk apply)"

1. Toggle the menu item ON.
2. A small dialog asks you to pick **one function code** from the
   loaded codes (sourced from your function-codes CSV — see
   [Function codes & legend](#function-codes--legend)).
3. Left-click cells on the canvas to toggle them in / out of the
   selection set. Selected cells are filled in cyan.
4. Right-click drag still works for panning. Wheel still zooms.
5. Toggle the menu item OFF — a confirmation dialog asks
   "Apply function code 'XXX' to N selected cell(s)?".
   - **Yes** → assigns the code to every selected cell's
     `<function>` element in one shot.
   - **No** → keeps the selection so you can adjust and try again.

### Cell → "Class Bulk Apply"

Identical workflow, but the pick-one dialog asks for a Class
(`CellSpace` / `TransferSpace` / `ConnectionSpace` / `AnchorSpace`).
On confirm the chosen Class is applied to every selected cell —
the editor rewrites each XML element's tag in place, so the change
is durable across save / reload.

---

## Floor menu

A unified **Floor** menu groups floor management AND floorplan-image
alignment:

| Item                      | What it does                                                                                                                  |
|---------------------------|------------------------------------------------------------------------------------------------------------------------------|
| **New Floor…**            | Create a new floor with name + Floor Z (cm) + Ceiling Z (cm). Immediately chains into Load Floorplan so the user can drop in the new floor's image and align it via the LL/UR anchors. |
| **Add Floor from File…**  | Import all spaces / nodes / edges from another XML file as a new floor. IDs that collide with existing ones get a `_<floor>` suffix. |
| **Delete Floor…**         | Pick a floor; remove every space, node and edge that belongs to it.                                                           |
| **Load Floorplan…**       | Pick an image for the active floor. Triggers the [reference-point click capture](#reference-points) flow.                     |
| **Move Floorplan**        | Toggle: drag the loaded image around with the mouse.                                                                          |
| **Resize Floorplan**      | Toggle: drag the 8 handles around the image to change its extent.                                                             |
| **Set Extent…**           | Type exact `[xmin xmax ymin ymax]` extent in cm.                                                                              |
| **Set Reference Points…** | Re-capture the file-wide LL / UR anchors via the click flow.                                                                  |
| **Opacity…**              | Set the floorplan image's transparency.                                                                                       |

The View → "Show floorplan" toggle is **global** — it persists
across floor changes so a single OFF stays OFF.

---

## Reference points

Two file-wide anchor points (Lower-Left and Upper-Right in world
coordinates) keep all floors in the same coordinate frame regardless
of how each per-floor PNG was scanned.

**First Load Floorplan…** for a fresh GML:

1. Pick the image; it appears at a 1:1 pixel-extent default.
2. Status: *"click LL on the floorplan."* — click the location that
   should be at LL world coords.
3. A small dialog asks for that point's world (x, y) in cm.
4. *"click UR on the floorplan."* — click and enter UR's world
   (x, y) in cm.
5. The editor solves a 2-axis affine transform; the image's extent
   is re-computed so those two pixels land exactly at the entered
   world coords. Both points are saved to the sidecar.

**Subsequent Load Floorplan…** (any other floor):

1. The two LL / UR markers are already drawn on the canvas (red X
   for LL, lime X for UR, with coordinate labels).
2. Pick the new floor's image; status: *"click LL on the floorplan
   (saved world coord (X, Y) will be reused)."*
3. Click the two anchors on the new image. World coords are not
   asked again — they are taken from the sidecar.
4. The new image is re-extented automatically to match.

Right-click cancels an in-progress capture.

---

## Layers (ThematicLayer)

IndoorGML 2.0 supports multiple **ThematicLayer**s (one
PrimalSpaceLayer + one DualSpaceLayer each).

- **Layer combo (active)**: cells / nodes / edges of this layer are
  drawn normally; everything else is dimmed.
- **Layer combo (background)**: a second layer can be displayed
  dimly on top of the active one.
- **Layer menu**: New Layer, Rename Layer, Delete Layer.

---

## Inter-layer connections

**Connect Layers** menu:

- **Setup…** — pick a Background layer and a Foreground layer.
- **Connect Mode** — click a cell in BG, then a cell in FG → an
  `InterLayerConnection` is created.
- **List Connections…** — view all ILCs.
- **Delete Connection…** — remove an ILC.

---

## Function codes & legend

Cells carry a free-form `<function>` text. Loading a function-codes
CSV (Function Codes → **Load Function Codes…**) turns those texts
into colour fills and a legend.

**CSV format** (header optional):

```
code,label
ROOM,Office
LAB,Laboratory
HALL,Corridor
```

- One column ⇒ code is also the label.
- Two columns ⇒ second column is the human-readable label.

**Legend** (top-left of canvas, shown only when codes are loaded):

- One swatch per code that is actually in use on the current view.
- The swatch label is the **human-readable name** (e.g.
  `Office`), not the bare code. When a code has no separate label
  in the CSV, the code itself is shown as fallback.
- Cells inherit a unique colour per code from the `tab20` palette.

**Apply Function…** (menu) opens a per-cell dialog to assign a code
to the currently-selected cell. To assign one code to many cells in
one go, use Function Codes → **Multiple Cells (bulk apply)** (see
[Bulk apply modes](#bulk-apply-modes)).

---

## Keyboard shortcuts

| Key                      | Action                          |
|--------------------------|---------------------------------|
| `Home`                   | Fit view to all visible objects |
| `Ctrl + Z`               | Undo                            |
| `N`                      | Toggle **Node Edit** mode       |
| `E`                      | Toggle **Edge Edit** mode       |
| `C`                      | Toggle **Cell Edit** mode       |
| `D`                      | Toggle **New Door** mode        |
| `Delete` / `Backspace`   | Delete selected feature, or selected rows in any Check dialog |
| `Enter`                  | Finish polygon (in New Cell)    |
| `Right-click drag`       | Pan the canvas (works during bulk-apply modes too) |
| `Mouse wheel`            | Zoom in / out under cursor      |

Single-letter shortcuts are **disabled** while an Entry / Combobox /
Text widget has focus, so dialog text fields receive characters
normally.

---

## Check menu

Diagnostics and bulk operations. Every Check dialog supports
**multi-select** (Ctrl/Shift click in the row tree) and **Delete /
Backspace key** as a shortcut for the Delete button.

| Item                                       | What it does                                                                                                                           |
|--------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------------|
| **Create Virtual CellSpaces for Standalone Nodes…** | IndoorGML 2.0 requires every Node to have a dual CellSpace. Scans for nodes with empty / dangling duality and creates a minimal virtual cell (`VS_<node_id>`, no geometry, no external reference, `poi = false`) for each. The auto-cell is `is_virtual=True` so it never renders as a marker. |
| **Find Isolated Nodes & CellSpaces…**      | Two-tab non-modal dialog: nodes with no incident edge / non-virtual cells with no dual node. Locate (auto-floor + magenta crosshair) + Delete (with cascade for nodes). |
| **Find Nodes without Duality…**            | Nodes whose own `<duality>` href is empty or dangles to a missing CellSpace.                                                          |
| **Find Cells without Node…**               | Cells whose own `<duality>` href is empty / dangling. Also surfaces *orphan virtual cells* (left over from a node that was later deleted) under a clear "orphan VS" tag. |
| **Find Duality Conflicts…**                | Three categories: multi-cell → 1 node, multi-node → 1 cell, asymmetric (cell→N but N→other). Tree view with sub-rows per conflicting feature. Delete with cascade. |
| **Find Broken Edge Connections…**          | Edges that do not describe a valid transition, each row tagged with why: `endpoints: N` (not exactly two `<connects>`), `dangling → 'X'` (endpoint names a Node that is not in the file), `self-loop` (both endpoints on one Node), `duality → 'X'` (the dual CellBoundary is missing). Traversal-breaking rows sort first; duality-only rows last, since those still route fine. Locate centres on the segment midpoint. |
| **Find Duplicate IDs…**                    | Same XML id appearing on more than one Node / Edge / CellSpace / layer. Each copy is a row of its own with a per-copy signature (z value for nodes, vertex count for cells, …). **Locate selected** previews each copy at its own coords. **Rename ID…** gives one copy a fresh id and re-targets only the edges geometrically incident on that exact (x, y, z). **Delete this copy** removes the picked copy(s); when the deleted copy is the unique node at its z layer, edges physically tied to that z are cascade-deleted. |
| **Search by ID / Name…**                   | Substring / regex search across CellSpaces (id + name), Nodes (id), Edges (id). Per-type filter checkboxes.                           |
| **Measure Distance**                       | Toggle. Click two points → dialog reports Euclidean distance in **cm** and **m**, plus Δx and Δy in cm.                              |
| **Repair: drop trailing duplicate vertices…** | Trims any `<pos>` siblings that simply repeat the start of the polygon ring (a defect of older versions of `_update_ring_coords`).   |
| **Repair: remove self-loop edges…**        | Deletes edges whose two endpoints are the same Node. A self-loop cannot change reachability, so routing is unaffected, but every traversal still walks it. Shows a preview grouped by the owning cell (with floor and room name) before deleting. These come from upstream converters that map each endpoint to `owner_cell(boundary)` without checking whether both boundaries belong to the *same* cell — a room with two or more openings then loops back on itself. Rebuilding the intended per-opening nodes needs boundary geometry the file usually does not carry, so removal is the only in-editor repair; regenerate from the source data to keep that detail. |
| **Repair: drop self-intersecting tail…**   | Heuristic last-resort — pops the last vertex iteratively until the ring stops self-intersecting.                                      |
| **Normalize Floor Z…**                     | Per-level dialog with a `Floor Z (cm)` and `Ceiling Z (cm)` field. Every cell on the floor is rebuilt with floor ring at Floor Z and ceiling ring at Ceiling Z; nodes move to Floor Z; edge endpoint Zs follow the node Zs. The override is persisted so subsequent New Cell / New Node use the right Z automatically. Default proposals come from the user's New-Floor override, then the most-common non-zero Z among existing cells. The writer fully **replaces** any stale CellSpace XML element with a fresh one carrying the new Z (no zombie geometry left behind). |

### View toggle: **Show off-floor anchors** (default OFF)

When a single floor is being viewed, vertical-edge endpoints on
*other* floors are normally hidden (only the current-floor edges
are in colour). Enable this toggle to show them: lower-floor
endpoints appear in **orange**, current-floor nodes that link
downward in **dodgerblue**.

---

## Selection panel

The right-side Selection panel shows the picked feature's
properties. For CellSpaces it includes Name, Class, Level, Function,
Layer, POI, dual Node, Center, vertex count.

For Nodes it shows ID, the dual cell's Name, coordinates, Level,
Layer, dual Space and connect list.

**While Cell Edit mode is active and a CellSpace is selected**, the
read-only panel switches to an **editable form**:

- **Name** (Entry, Enter to apply)
- **Class** (Combobox: `CellSpace` / `TransferSpace` /
  `ConnectionSpace` / `AnchorSpace`, free text allowed). Apply
  rewrites the **element's XML tag** in place so the subclass
  survives save / reload.
- **Func** (Combobox sourced from the Function Codes CSV — when
  codes are loaded, the field is *readonly*; entries are displayed
  as `code — label` with the underlying code stored)
- **POI** (Checkbutton)
- **Apply** button — commits to the model AND to the existing
  `<cellSpaceName>` / `<function>` / `<poi>` XML children, and
  retags the element when Class changes, then refreshes the
  canvas (function-code colour updates immediately).

---

## Saving and exporting

| File menu          | Behaviour                                                                                                                              |
|--------------------|----------------------------------------------------------------------------------------------------------------------------------------|
| **Save**           | In-place save back to the source path.                                                                                                 |
| **Save As…**       | Save to a new path; the floorplan sidecar JSON (per-floor image + extent **+ reference points**) is **carried over** so alignment is preserved. |
| **Export Floors…** | Multi-select dialog of floors → single combined IndoorGML output. **All Z values are preserved**. Edges are kept by Z-coordinate match (vertical edges are included if either endpoint touches the selected set). |

Save preserves namespace prefixes, structural elements, and existing
`gml:id` / `ns1:id` bindings of the source file. New features added
in a session use the **same** id namespace as already present.

The writer correctly handles re-emitting cells / nodes / edges that
were structurally changed (e.g. by Normalize Floor Z, Repair, Cell
Split, Class change, bulk apply): the stale element is removed from
the tree and replaced with a fresh one carrying the up-to-date
geometry and tag.

---

## Troubleshooting

- **"Failed to parse the file: multi-byte encodings are not supported"**  
  → Should not happen. The parser falls back to manual decode using
  the prolog's declared encoding (cp949, EUC-KR, Shift-JIS, …) and
  rewrites the prolog to UTF-8 in memory.

- **The canvas shrinks to a square**  
  → Resolved. The axes use `set_aspect('equal',
  adjustable='datalim')` with limits pre-computed from the canvas
  pixel ratio so the axes always fill the window rectangle.

- **Edits look correct in the viewer but vanish on reload**  
  → Was caused by an old `_update_ring_coords` bug that did not
  add / remove `<pos>` siblings when the new polygon had a
  different vertex count, plus an `_add_new_spaces_to_xml` guard
  that re-bound to an existing element instead of replacing its
  geometry. Both are fixed; if you have legacy files, run **Check →
  Repair: drop trailing duplicate vertices**.

- **Panning doesn't work while bulk-applying**  
  → Resolved. Bulk-apply modes (Function Codes → Multiple Cells,
  Cell → Class Bulk Apply) only intercept **left** clicks; right-
  click drag still pans, and the mouse wheel still zooms.

- **A click "doesn't do anything"**  
  → A floorplan Move / Resize mode may still be active; toggle it
  off. Single-click target modes are mutually exclusive, so the
  active edit mode (shown in the status line) is the one that
  receives the click.

- **Other-floor nodes appear in the current floor view**  
  → Resolved. The renderer and `expand_floor_graph` both consult
  the *dual cell's* level first when classifying a node — files
  that mislabel `node.level` while keeping `cell.level` correct
  still get filtered cleanly.

- **Cross-floor edge appeared without my making it**  
  → Older versions had a bug where the rename / delete of duplicate
  ids could re-target connects that shared only a z-level (not a
  physical (x, y, z) match). Now the retarget criterion requires
  an exact endpoint coordinate match. Use **Check → Find Duality
  Conflicts** + **Delete this copy** to clean up any leftover.

- **2F cells stuck at z=0 even after Normalize Floor Z**  
  → Make sure to **Save** after Normalize. Also ensure you typed
  the correct Floor Z and Ceiling Z for that level — both must be
  in cm, and the writer fully replaces the existing cell elements
  with fresh ones carrying the new Z.

- **The Selection panel showed `Name: —` even though I named the cell**  
  → Resolved. The writer emits the human-readable name as
  `<cellSpaceName>`; the parser now accepts both
  `<cellSpaceName>` and a legacy plain `<name>`.

- **Korean (or any CJK) cell names render as boxes on the canvas**  
  → Resolved. Tk widgets inherit `TkDefaultFont` and render CJK fine, but
  the Matplotlib canvas never goes through Tk — it defaults to DejaVu Sans,
  which contains no Hangul glyphs at all, so every label came out as tofu
  and each character emitted a "Glyph … missing from font(s)" warning (3477
  of them on a single floor of the hospital model). The app now picks the
  first installed CJK-capable family at import time — Malgun Gothic on
  Windows, Apple SD Gothic Neo on macOS, Noto Sans KR / NanumGothic on
  Linux — and prepends it to `font.sans-serif`, keeping DejaVu Sans behind
  it for symbols the CJK face lacks. If no such font is installed the app
  still runs (Latin labels are unaffected) and says so in the status bar.

- **Edge endpoints reference cells instead of nodes / re-pointing an edge didn't stick**  
  → Resolved. An IndoorGML Edge connects Nodes, but converters commonly
  write the dual CellSpace id instead (`#cell_X` where the node is
  `S_cell_X`). No XSD validator catches this: `xlink:href` is declared
  `xsd:anyURI`, so the schema only checks that the string looks like a URI,
  never what kind of element it points at. Such a file validates cleanly
  and still breaks any router that dereferences the endpoint.

  The bulk of these come from upstream — in a 17-floor hospital model all
  5917 `T_*` edges were cell-style while all 1483 editor-made `edge_new_*`
  edges were correct. But the editor could *introduce* them too:
  `normalize_edge_connects_to_node_ids()` passes through any reference it
  cannot resolve, and the new-edge writer emitted `edge.connects` verbatim,
  so an unresolved id was written straight into the file as a cell
  reference. Both the writer and the reader now go through
  `resolve_connect_ref_to_node_id()`, which follows the cell's `duality`
  link rather than guessing an `S_`+id naming convention (this model has
  cells like `VS_S_2d0c0 → S_2d0c0` that break the pattern). Opening a file
  that contains such references reports how many; saving writes them all
  back as Node references. Genuinely unresolvable ids are preserved so
  Check → Find Broken Edge Connections still reports them.

  The same gap meant changing an edge's endpoints in the editor never
  reached the file — the writer only ever updated geometry.

- **A cell's outline looks right in the editor but a validator rejects the ring**  
  → Resolved. `gml:LinearRing` must repeat its first position at the end.
  Dragging vertex 0 of a parsed cell used to move only that vertex, leaving
  the closing twin at the old coordinate, so the ring opened. The drag now
  moves both twins, and `_update_ring_coords` — the single point every
  geometry edit passes through — closes the ring before writing. Files that
  already contain open rings are repaired on the next Save.

- **Changing Class in the Selection panel didn't persist**  
  → Resolved. The Apply button now rewrites the XML element's tag
  (`<core:CellSpace>` ↔ `<core:TransferSpace>` /
  `<core:ConnectionSpace>` / `<core:AnchorSpace>`) in addition to
  updating the child elements.

- **Legend shows codes instead of names**  
  → Resolved. The legend now displays the second column of your
  function-codes CSV (the human-readable label). When a code has
  no separate label, the bare code is used as fallback.

---

## License

Apache License 2.0 
