# pineditor2D

A 2D IndoorGML editor for viewing and editing indoor space models based on the [IndoorGML 2](https://github.com/opengeospatial/IndoorGML-SWG) standard.

Built with Python, Tkinter, and Matplotlib.

---

## Features

- Open, edit, and save IndoorGML 2 files (`.xml` / `.gml`)
- Draw and edit **cell spaces** (rooms, corridors, etc.)
- Split a cell space into two by selecting two vertices
- Edit the **dual graph** — add, move, and delete nodes and edges
- Place **doors** between adjacent spaces
- Load a **floorplan image** as a reference background
- Assign **function codes** to spaces with color coding
- Multi-floor support with per-floor view
- Undo / Redo

---

## Requirements

```
Python 3.10+
matplotlib
numpy
lxml
Pillow
```

Install dependencies:

```bash
pip install matplotlib numpy lxml Pillow
```

---

## Running

```bash
python pineditor2D.py
```

Or use the standalone Windows executable `pineditor2D.exe` (no Python installation required).

---

## Usage

### File Operations

| Action | Menu |
|---|---|
| New model | File → New |
| Open IndoorGML file | File → Open... |
| Save | File → Save |
| Save to new file | File → Save As... |
| Export per floor | File → Split by Floor... |

---

### Navigation

| Action | How |
|---|---|
| Pan | Click and drag on empty area |
| Zoom | Mouse scroll wheel |
| Select floor | Floor dropdown at the top |

---

### Cell Menu

#### New Cell
Draw a new cell space polygon.
- Click to place vertices.
- **Double-click** (or click near the first vertex) to close the polygon.
- **Right-click** to cancel.

#### Cell Edit
Select and edit an existing cell.
- Click a cell to select it.
- Edit **name**, **function**, or **level** in the property panel on the right.
- Press **Delete** to remove the selected cell.

#### Cell Split
Split a cell into two parts along a line between two of its vertices.

1. Activate **Cell → Cell Split**.
2. Click the cell to split — its vertices are labelled with index numbers.
3. Click the **first vertex**.
4. Click the **second vertex** — the split is applied immediately.

**Result:**
- The original cell keeps its ID and name.
- The new cell is assigned the original ID / name with `_1` appended.
- A duality node is created at the centroid of each sub-cell.
- An edge between the two nodes is added automatically.

---

### Network Menu

#### Node Edit
Add, move, or delete dual graph nodes.  
*(Mutually exclusive with Edge Edit)*

| Action | How |
|---|---|
| Add node | Click on empty space |
| Move node | Click and drag |
| Select node | Click a node (highlighted with ★) |
| Delete node | Select, then press **Delete** |

#### Edge Edit
Connect or delete edges between nodes.  
*(Mutually exclusive with Node Edit)*

| Action | How |
|---|---|
| Add edge | Click first node, then click second node |
| Select edge | Click an edge line |
| Delete edge | Select, then press **Delete** |
| Cancel | Right-click |

---

### Door Menu

Activate **Door → New Door** and click on a boundary between two adjacent cells to place a door.

---

### Floorplan Menu

Load a background image to use as a drawing reference.

| Action | Description |
|---|---|
| Load Floorplan... | Load a PNG / JPG image |
| Move Floorplan | Drag to reposition |
| Resize Floorplan | Drag a corner to resize |
| Set Extent... | Enter coordinate bounds manually |
| Opacity... | Adjust image transparency |

---

### View Menu

| Option | Description |
|---|---|
| Show names | Toggle cell name labels |
| Show nodes | Toggle dual graph nodes |
| Show floorplan | Toggle background image |

---

## Keyboard Shortcuts

| Key | Action |
|---|---|
| `Ctrl+Z` | Undo |
| `Ctrl+Y` | Redo |
| `Delete` | Delete selected item |
| `Esc` | Cancel current operation |

---

## Output Format

Files are saved as **IndoorGML 2** XML.  
New items are written on save; existing items are updated in-place.

---

## License

MIT
