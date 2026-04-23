# pineditor2D
IndoorGML 2.0 editor in Python. Note that it does NOT supprt
- support multi-layer space model
- 3D visualization.

# pineditor2D — User Manual

pineditor2D is a 2D IndoorGML editor for viewing and editing indoor space models based on the IndoorGML 2 standard.

---

## 1. Getting Started

Launch `pineditor2D.exe` (or `python pineditor2D.py`).  
Open an IndoorGML file via **File → Open...** or start a new model with **File → New**.

---

## 2. File Menu

| Action | Description |
|---|---|
| **New** | Create a new empty model |
| **Open...** | Open an existing IndoorGML (.xml / .gml) file |
| **Close** | Close the current model |
| **Save** | Save changes to the current file |
| **Save As...** | Save to a new file |
| **Split by Floor...** | Export each floor as a separate file |
| **Quit** | Exit the application |

---

## 3. Navigation

| Action | How |
|---|---|
| **Pan** | Click and drag on empty area |
| **Zoom in/out** | Mouse scroll wheel |
| **Select floor** | Use the floor dropdown at the top of the window |
| **Show all floors** | Select "All" in the floor dropdown |

---

## 4. View Menu

| Option | Description |
|---|---|
| **Show names** | Toggle cell space name labels |
| **Show nodes** | Toggle dual graph nodes |
| **Show floorplan** | Toggle background floorplan image visibility |

---

## 5. Cell Menu

### New Cell
Activates cell drawing mode.  
- Click on the canvas to add polygon vertices one by one.  
- **Double-click** (or click near the first vertex) to close and create the cell.  
- **Right-click** to cancel.

### Cell Edit
Activates cell selection mode.  
- Click a cell to select it (highlighted in red).  
- Edit the cell's **name**, **function**, or **level** in the property panel.  
- Press **Delete** to delete the selected cell.  
- **Right-click** to deselect.

### Cell Split
Splits a selected cell into two along a line connecting two of its vertices.

**Steps:**
1. Activate **Cell Split** from the Cell menu.
2. Click on the cell you want to split — its vertices are shown with index numbers.
3. Click the **first vertex** of the split line.
4. Click the **second vertex** — the cell is split immediately.

**Result:**
- The original cell keeps its ID and name.
- The new cell gets the ID and name of the original with `_1` appended.
- A new duality node is created inside the new cell.
- An edge is automatically added between the two duality nodes.

---

## 6. Network Menu

### Node Edit
Activates node editing mode (mutually exclusive with Edge Edit).

| Action | How |
|---|---|
| Add a node | Click on empty space |
| Move a node | Click and drag |
| Select a node | Click on a node (shown with a star ★) |
| Delete a node | Select it, then press **Delete** |

### Edge Edit
Activates edge editing mode (mutually exclusive with Node Edit).

| Action | How |
|---|---|
| Add an edge | Click first node, then click second node |
| Select an edge | Click on an edge line |
| Delete an edge | Select it, then press **Delete** |
| Cancel connection | **Right-click** |

---

## 7. Door Menu

### New Door
Activates door creation mode.  
- Click on the boundary between two adjacent cells to place a door.

---

## 8. Floor Menu

| Action | Description |
|---|---|
| **Add Floor from File...** | Merge floors from another IndoorGML file |
| **Delete Floor...** | Remove a floor and all its spaces/nodes/edges |

---

## 9. Floorplan Menu

Load and align a background floor plan image (.png / .jpg) to use as a reference.

| Action | Description |
|---|---|
| **Load Floorplan...** | Load an image file as background |
| **Move Floorplan** | Drag the background image to reposition |
| **Resize Floorplan** | Drag a corner to resize the background image |
| **Set Extent...** | Manually enter the coordinate extent of the image |
| **Opacity...** | Adjust the transparency of the background image |

---

## 10. Function Codes Menu

| Action | Description |
|---|---|
| **Load Function Codes...** | Load a CSV/JSON file mapping function codes to colors |
| **Apply Function...** | Assign a function code to the selected cell |

---

## 11. Keyboard Shortcuts

| Key | Action |
|---|---|
| **Ctrl + Z** | Undo |
| **Ctrl + Y** | Redo |
| **Delete** | Delete selected cell / node / edge |
| **Escape** | Cancel current operation |

---

## 12. Status Bar

The status bar at the bottom of the window shows:
- Current edit mode
- Selected item ID and properties
- Hints for the next action (e.g., "Click another vertex to split")

---

## 13. Output Format

Files are saved as **IndoorGML 2** XML (`.xml`).  
Newly created cells, nodes, and edges are written into the file on save.  
Items imported from the original file are updated in-place.

