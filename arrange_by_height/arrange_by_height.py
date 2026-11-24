# /// script
# requires-python = ">=3.10"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Arrange by Height"
# description = "Arranges plates by object height, tallest in the middle. Adds an Arrange tab."
# author = "bacoj913"
# version = "1.3.0"
# ///
"""Arrange by Height: an OrcaSlicer plugin and command-line tool.

The OrcaSlicer plugin API is read-only, so the plugin cannot move objects in the open
project. It exports a copy of the project, arranges that copy and writes it to a new 3MF
file or over the saved project file (keeping a backup). Open the file again to see it.

Layout rules:
- Compact: the tallest object goes in the center of the plate and shorter objects go
  around it, so upper layers only visit the center of the plate.
- Rows: rows along X, tallest row in the middle and the tallest object in the middle of
  each row. On a bedslinger the bed moves in Y, so rows along X keep bed moves short.
- Grid: equal cells, tallest objects in the center cells.
- Only the selected plate, or every plate, is arranged. Objects that no longer fit go to
  new plates at the end.
- Adaptive layer height and Smooth set each arranged object's layer height profile, as
  the Variable Layer Height tool does.
- The object list is set per plate from shortest to tallest. With print sequence
  "By object", OrcaSlicer prints in object list order, so tall objects print last.
- Spacing is a fixed value in mm, or Auto: OrcaSlicer's own arrange spacing for the plate's
  print sequence.
- Align to Y axis turns each long object so its long side runs along Y, as OrcaSlicer's
  arrange does for bedslinger printers. Auto turns it on when the printer structure is i3.

Command line (no OrcaSlicer needed):
    python arrange_by_height.py project.3mf [-o out.3mf] [--spacing auto|MM]
        [--layout compact|rows|grid] [--plates all|regroup|current] [--plate N]
        [--align-y auto|on|off] [--adaptive QUALITY] [--smooth RADIUS [--keep-min]]
"""

import argparse
import json
import math
import os
import re
import shutil
import sys
import zipfile
import xml.etree.ElementTree as ET

try:
    import orca  # OrcaSlicer's embedded module; only present inside OrcaSlicer.
    if not hasattr(orca, "pages") or not hasattr(orca, "plugin"):
        orca = None  # An unrelated package named "orca".
except ImportError:
    orca = None

MODEL_PATH = "3D/3dmodel.model"
MODEL_SETTINGS_PATH = "Metadata/model_settings.config"
PROJECT_SETTINGS_PATH = "Metadata/project_settings.config"
LAYER_HEIGHTS_PATH = "Metadata/layer_heights_profile.txt"
# Files that refer to objects by their 1-based place in the object list.
LINE_INDEXED_PATHS = (LAYER_HEIGHTS_PATH, "Metadata/brim_ear_points.txt")
XML_INDEXED_PATHS = ("Metadata/layer_config_ranges.xml", "Metadata/cut_information.xml")

# Values from OrcaSlicer's source (PartPlate.cpp, Print.hpp, PartPlate.hpp).
PLATE_GAP = 1.0 / 5.0
MAX_OUTER_NOZZLE_DIAMETER = 4.0
MAX_PLATE_COUNT = 36
BED_SHRINK = 1.0             # mm kept from the bed edge (ArrangeParams::bed_shrink_x)
BED_SHRINK_SEQ_PRINT = 5.0   # the same, printing by object
DEFAULT_SETTINGS = {
    "spacing": "auto",       # "auto" or a number in mm
    "edge_margin": "auto",   # "auto" or a number in mm
    "layout": "compact",     # "compact", "rows" or "grid"
    "plates": "current",     # "current" (the plate in plate_index), "keep" or "regroup"
    "output": "new",         # "new" file or "replace" the project file
    "align_y": "auto",       # "auto", "on" or "off"
    "adaptive": False,       # adaptive layer height for the arranged objects
    "adaptive_quality": 0.5,  # 0 = quality (thin layers) ... 1 = speed (thick layers)
    "smooth": False,         # smooth the layer height profile
    "smooth_radius": 5,      # 1 ... 10
    "smooth_keep_min": False,
    "share_layers": True,    # one layer height profile for all objects on a plate
}
MIN_AUTO_GAP = 2.0           # mm, used for "By layer" when there is no brim
ROW_BAND = 20.0              # mm, row height used to order objects of equal height
ALIGN_MAX_RATIO = 0.66       # OrcaSlicer leaves an object as is above this axis ratio
EPS = 1e-6

# Plate metadata that is no longer valid once objects move.
STALE_PLATE_KEYS = {
    "gcode_file", "thumbnail_file", "thumbnail_no_light_file", "top_file", "pick_file",
    "pattern_bbox_file", "pattern_file", "label_object_enabled", "is_sliced_valid",
}


class ArrangeError(Exception):
    """A problem the user must fix, with a message for the user."""


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

class Item:
    """One object instance: one <item> in the 3MF build."""

    def __init__(self, index, object_id, instance_index, bbox):
        self.index = index                    # position in <build>
        self.object_id = object_id            # 3MF object id
        self.instance_index = instance_index  # instance number within the object
        self.bbox = bbox                      # (x0, y0, z0, x1, y1, z1), world mm
        self.plate = 0                        # plate index, 0-based
        self.x = None                         # new footprint min x, plate coordinates
        self.y = None
        self.transform = None                 # 3MF item transform, 3x4 rows
        self.identify_id = None
        self.rotated = 0.0                    # radians turned about Z by Align to Y axis
        self.moved = False                    # placed by this run
        self.old_origin = (0.0, 0.0)          # world origin of the plate the item was on

    @property
    def width(self):
        return self.bbox[3] - self.bbox[0]

    @property
    def depth(self):
        return self.bbox[4] - self.bbox[1]

    @property
    def height(self):
        return self.bbox[5] - self.bbox[2]

    def __repr__(self):
        return "Item(%d, obj=%s, %.1fx%.1fx%.1f, plate=%d)" % (
            self.index, self.object_id, self.width, self.depth, self.height, self.plate)


class Bed:
    """The printable area of one plate, from the project settings."""

    def __init__(self, polygon, exclude=None):
        self.polygon = polygon
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        self.x0, self.x1 = min(xs), max(xs)
        self.y0, self.y1 = min(ys), max(ys)
        self.exclude = exclude or []          # list of (x0, y0, x1, y1)
        self.is_rect = _is_rectangle(polygon)
        # PartPlateList keeps the plate size as int; plates are laid out on that stride.
        self.stride_x = int(self.x1 - self.x0 + EPS) * (1.0 + PLATE_GAP)
        self.stride_y = int(self.y1 - self.y0 + EPS) * (1.0 + PLATE_GAP)

    @property
    def center(self):
        return ((self.x0 + self.x1) / 2.0, (self.y0 + self.y1) / 2.0)

    def contains_rect(self, x0, y0, x1, y1, margin):
        if (x0 < self.x0 + margin - EPS or y0 < self.y0 + margin - EPS
                or x1 > self.x1 - margin + EPS or y1 > self.y1 - margin + EPS):
            return False
        if not self.is_rect:
            for corner in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
                if not _point_in_polygon(corner, self.polygon):
                    return False
        for ex in self.exclude:
            if (x0 < ex[2] + margin and x1 > ex[0] - margin
                    and y0 < ex[3] + margin and y1 > ex[1] - margin):
                return False
        return True


def plate_columns(count):
    """Columns of the plate grid for `count` plates (compute_colum_count in OrcaSlicer)."""
    value = math.sqrt(count)
    rounded = float(round(value))
    return int(rounded + 1) if value > rounded else int(rounded)


def plate_origin(index, cols, bed):
    """World offset of plate `index` (compute_shape_position in OrcaSlicer)."""
    row, col = divmod(index, cols)
    return col * bed.stride_x, -row * bed.stride_y


def plate_of_point(x, y, cols, bed):
    col = int(math.floor((x - bed.x0) / bed.stride_x))
    row = int(math.floor((bed.y1 - y) / bed.stride_y))
    if col < 0 or row < 0 or col >= cols:
        return None
    return row * cols + col


def _is_rectangle(poly):
    if len(poly) != 4:
        return False
    xs = sorted(set(round(p[0], 4) for p in poly))
    ys = sorted(set(round(p[1], 4) for p in poly))
    return len(xs) == 2 and len(ys) == 2


def _point_in_polygon(pt, poly):
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % n]
        if (ay > y) != (by > y):
            xi = ax + (y - ay) * (bx - ax) / (by - ay)
            if x < xi + EPS:
                inside = not inside
    return inside


def convex_hull(points):
    """Convex hull of 2D points, counter-clockwise (monotone chain)."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def y_align_angle(hull):
    """Rotation (radians, counter-clockwise) that turns the outline's long axis onto Y.

    Same rule as OrcaSlicer (update_selected_items_axis_align in Arrange.cpp): the long
    axis comes from the outline's second moments of area, and an outline whose two axes
    are close in size (ratio above 0.66) is not turned. Returns 0 for no rotation.
    """
    n = len(hull)
    if n < 3:
        return 0.0
    a = cx = cy = sxx = syy = sxy = 0.0
    for i in range(n):
        x0, y0 = hull[i - 1]
        x1, y1 = hull[i]
        d = x0 * y1 - x1 * y0
        a += d
        cx += (x0 + x1) * d
        cy += (y0 + y1) * d
        sxx += (x0 * x0 + x0 * x1 + x1 * x1) * d
        syy += (y0 * y0 + y0 * y1 + y1 * y1) * d
        sxy += (x0 * y1 + 2 * x0 * y0 + 2 * x1 * y1 + x1 * y0) * d
    if abs(a) < EPS:
        return 0.0
    area = a / 2.0
    cx /= 6.0 * area
    cy /= 6.0 * area
    mxx = sxx / 12.0 / area - cx * cx      # variance along X
    myy = syy / 12.0 / area - cy * cy      # variance along Y
    mxy = sxy / 24.0 / area - cx * cy
    big, small = max(abs(mxx), abs(myy)), min(abs(mxx), abs(myy))
    if big < EPS or small / big > ALIGN_MAX_RATIO:
        return 0.0
    angle = math.pi / 2.0 - math.atan2(2.0 * mxy, mxx - myy) / 2.0
    # Fold to (-pi/2, pi/2]: turning by pi gives the same footprint.
    while angle > math.pi / 2.0 + EPS:
        angle -= math.pi
    while angle <= -math.pi / 2.0 + EPS:
        angle += math.pi
    return 0.0 if abs(angle) < 1e-3 else angle


def rotate_z(transform, angle, pivot):
    """Turn a 3MF transform by `angle` about the vertical line through `pivot` (x, y)."""
    c, s = math.cos(angle), math.sin(angle)
    m = [row[:] for row in transform]
    for r in range(4):
        x, y = m[r][0], m[r][1]
        if r == 3:
            x -= pivot[0]
            y -= pivot[1]
        m[r][0] = x * c - y * s
        m[r][1] = x * s + y * c
        if r == 3:
            m[r][0] += pivot[0]
            m[r][1] += pivot[1]
    return m


def rotate_points(points, angle, pivot):
    c, s = math.cos(angle), math.sin(angle)
    out = []
    for x, y in points:
        x -= pivot[0]
        y -= pivot[1]
        out.append((x * c - y * s + pivot[0], x * s + y * c + pivot[1]))
    return out


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _num(value, default=0.0):
    if isinstance(value, list):
        value = value[0] if value else default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _str(value, default=""):
    if isinstance(value, list):
        value = value[0] if value else default
    return default if value is None else str(value)


def parse_points(values):
    """Parse ["0x0", "256x0", ...] into [(0, 0), (256, 0), ...]."""
    if isinstance(values, str):
        values = values.split(",")
    points = []
    for value in values or []:
        if isinstance(value, (list, tuple)):
            points.append((float(value[0]), float(value[1])))
            continue
        parts = str(value).strip().split("x")
        if len(parts) == 2:
            points.append((float(parts[0]), float(parts[1])))
    return points


def bed_from_config(config):
    polygon = parse_points(config.get("printable_area"))
    if len(polygon) < 3:
        raise ArrangeError("The project has no printable area (printable_area).")
    exclude = []
    ex_points = parse_points(config.get("bed_exclude_area"))
    if len(ex_points) >= 3:
        xs = [p[0] for p in ex_points]
        ys = [p[1] for p in ex_points]
        if max(xs) - min(xs) > EPS and max(ys) - min(ys) > EPS:
            exclude.append((min(xs), min(ys), max(xs), max(ys)))
    return Bed(polygon, exclude)


def auto_gap(config, print_sequence, items):
    """Spacing OrcaSlicer's arrange uses (update_selected_items_inflation in Arrange.cpp)."""
    if print_sequence == "by object":
        nozzle_height = _num(config.get("nozzle_height"), 4.0)
        if items and all(it.height < nozzle_height for it in items):
            return MAX_OUTER_NOZZLE_DIAMETER + 0.01
        return _num(config.get("extruder_clearance_radius"), 40.0) + 0.01
    gap = 2.0 * brim_extent(config)
    if per_object_skirt(config):
        gap += 2.0 * skirt_extent(config)
    return max(gap, MIN_AUTO_GAP)


def align_enabled(settings, config):
    """Whether to align objects to Y: "on", "off", or "auto" for bedslinger printers."""
    value = settings.get("align_y", "auto")
    if value in (True, "on"):
        return True
    if value in (False, "off"):
        return False
    return _str(config.get("printer_structure")) == "i3"


def brim_extent(config):
    """How far a brim reaches from an object."""
    if _str(config.get("brim_type"), "auto_brim") == "no_brim":
        return 0.0
    return _num(config.get("brim_width")) + _num(config.get("brim_object_gap"))


def skirt_extent(config):
    """How far a skirt reaches past the brim (get_real_skirt_dist in PrintConfig.cpp)."""
    loops = int(_num(config.get("skirt_loops"), 0))
    shield = _str(config.get("draft_shield"), "disabled") not in ("disabled", "0", "")
    if not ((int(_num(config.get("skirt_height"), 0)) > 0 and loops > 0) or shield):
        return 0.0
    if shield and loops == 0:
        loops = 1
    nozzle = max([_at(config.get("nozzle_diameter"), i) for i in range(
        len(config.get("nozzle_diameter") or [0.4]))] or [0.4]) or 0.4
    width = 0.0
    for key in ("initial_layer_line_width", "line_width"):
        value = _str(config.get(key), "")
        width = (nozzle * _num(value[:-1]) / 100.0 if value.endswith("%")
                 else _num(value))
        if width > 0:
            break
    if width <= 0:
        width = nozzle
    return _num(config.get("skirt_distance")) + loops * width


def per_object_skirt(config):
    return _str(config.get("skirt_type"), "combined") == "perobject"


def edge_margin(config, print_sequence="by layer"):
    """Room OrcaSlicer's arrange leaves at the bed edge: 1 mm (5 mm printing by object),
    the skirt and the brim (update_arrange_params in Arrange.cpp)."""
    base = BED_SHRINK_SEQ_PRINT if print_sequence == "by object" else BED_SHRINK
    return base + skirt_extent(config) + brim_extent(config)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def _overlaps(a, b, gap):
    return (a[0] < b[2] + gap - EPS and a[2] > b[0] - gap + EPS
            and a[1] < b[3] + gap - EPS and a[3] > b[1] - gap + EPS)


def _candidates(placed, w, d, gap):
    """Spots for a w x d item that touch an item already placed (at `gap` distance)."""
    out = []
    for p in placed:
        pcx, pcy = (p[0] + p[2]) / 2.0, (p[1] + p[3]) / 2.0
        for y in (p[1], p[3] - d, pcy - d / 2.0):
            out.append((p[2] + gap, y))
            out.append((p[0] - gap - w, y))
        for x in (p[0], p[2] - w, pcx - w / 2.0):
            out.append((x, p[3] + gap))
            out.append((x, p[1] - gap - d))
    return out


def layout_compact(items, bed, gap, margin):
    """Place items from the center out, tallest first.

    The items are packed as one cluster: each item goes to the free spot nearest the
    tallest item, as long as the cluster still fits the bed. The cluster is then centered
    on the bed. Returns the items that did not fit.
    """
    order = sorted(items, key=lambda i: (-i.height, -(i.width * i.depth), i.index))
    room_w = bed.x1 - bed.x0 - 2 * margin
    room_d = bed.y1 - bed.y0 - 2 * margin
    placed = []          # (x0, y0, x1, y1) around the origin
    box = None           # cluster bounds
    rejected = []
    for it in order:
        w, d = it.width, it.depth
        if not placed:
            spots = [(-w / 2.0, -d / 2.0)]
        else:
            spots = _candidates(placed, w, d, gap)
            spots.sort(key=lambda c: (c[0] + w / 2.0) ** 2 + (c[1] + d / 2.0) ** 2)
        spot = None
        for x, y in spots:
            rect = (x, y, x + w, y + d)
            grown = rect if box is None else (min(box[0], rect[0]), min(box[1], rect[1]),
                                              max(box[2], rect[2]), max(box[3], rect[3]))
            if grown[2] - grown[0] > room_w + EPS or grown[3] - grown[1] > room_d + EPS:
                continue
            if any(_overlaps(rect, p, gap) for p in placed):
                continue
            spot, box = rect, grown
            break
        if spot is None:
            rejected.append(it)
            continue
        placed.append(spot)
        it.x, it.y = spot[0], spot[1]
    done = [i for i in order if i not in rejected]
    if done and not _fit_cluster(done, box, bed, margin):
        # Exclusion areas or a round bed: place items one by one inside the bed.
        return _layout_bounded(items, bed, gap, margin)
    return rejected


def _fit_cluster(items, box, bed, margin, step=5.0):
    """Move the cluster onto the bed, as close to the center as the bed allows."""
    cx, cy = bed.center
    w, d = box[2] - box[0], box[3] - box[1]
    lo_x, hi_x = bed.x0 + margin, bed.x1 - margin - w
    lo_y, hi_y = bed.y0 + margin, bed.y1 - margin - d
    if lo_x > hi_x + EPS or lo_y > hi_y + EPS:
        return False
    xs = _steps(lo_x, hi_x, step)
    ys = _steps(lo_y, hi_y, step)
    offsets = [(x, y) for x in xs for y in ys]
    offsets.sort(key=lambda o: (o[0] + w / 2.0 - cx) ** 2 + (o[1] + d / 2.0 - cy) ** 2)
    for ox, oy in offsets:
        dx, dy = ox - box[0], oy - box[1]
        if all(bed.contains_rect(i.x + dx, i.y + dy, i.x + dx + i.width, i.y + dy + i.depth,
                                 margin) for i in items):
            for i in items:
                i.x += dx
                i.y += dy
            return True
    return False


def _steps(lo, hi, step):
    """lo..hi in `step` increments, plus the middle and both ends."""
    if hi <= lo:
        return [lo]
    values = {lo, hi, (lo + hi) / 2.0}
    v = lo
    while v < hi:
        values.add(v)
        v += step
    return sorted(values)


def _layout_bounded(items, bed, gap, margin):
    """Place items one by one inside the bed, tallest nearest the center.

    Used when the cluster cannot be moved onto the bed as a whole.
    Returns the items that did not fit.
    """
    cx, cy = bed.center
    placed = []
    rejected = []
    for it in sorted(items, key=lambda i: (-i.height, -(i.width * i.depth), i.index)):
        w, d = it.width, it.depth
        lo_x, hi_x = bed.x0 + margin, bed.x1 - margin - w
        lo_y, hi_y = bed.y0 + margin, bed.y1 - margin - d
        if lo_x > hi_x + EPS or lo_y > hi_y + EPS:
            rejected.append(it)
            continue
        spots = [(cx - w / 2.0, cy - d / 2.0)] + _candidates(placed, w, d, gap)
        # Spots pulled inside the bed, and a coarse scan of the bed for the first items.
        spots += [(min(max(x, lo_x), hi_x), min(max(y, lo_y), hi_y)) for x, y in spots]
        spots += [(x, y) for x in _steps(lo_x, hi_x, 10.0) for y in _steps(lo_y, hi_y, 10.0)]
        spots.sort(key=lambda c: (c[0] + w / 2.0 - cx) ** 2 + (c[1] + d / 2.0 - cy) ** 2)
        spot = None
        for x, y in spots:
            rect = (x, y, x + w, y + d)
            if not bed.contains_rect(rect[0], rect[1], rect[2], rect[3], margin):
                continue
            if any(_overlaps(rect, p, gap) for p in placed):
                continue
            spot = rect
            break
        if spot is None:
            rejected.append(it)
            continue
        placed.append(spot)
        it.x, it.y = spot[0], spot[1]
    return rejected


def _center_out(seq):
    """Reorder a tallest-first list so the first is in the middle, then right, left, ..."""
    out = []
    for k, it in enumerate(seq):
        if k % 2:
            out.append(it)
        else:
            out.insert(0, it)
    return out


def layout_rows(items, bed, gap, margin):
    """Place items in rows along X, tallest row in the middle of the bed.

    Rows run side to side, so on a bedslinger (bed moving in Y) the bed moves little
    while the nozzle prints along a row. Each row has its tallest item in the middle.
    Returns the items that did not fit.
    """
    room_w = bed.x1 - bed.x0 - 2 * margin
    room_d = bed.y1 - bed.y0 - 2 * margin
    order = sorted(items, key=lambda i: (-i.height, i.index))
    rows, row, width = [], [], 0.0
    for it in order:
        need = it.width if not row else width + gap + it.width
        if row and need > room_w + EPS:
            rows.append(row)
            row, need = [], it.width
        row.append(it)
        width = need
    if row:
        rows.append(row)
    # Drop the shortest rows until the rows fit the bed's depth.
    rejected = []
    while rows and (sum(max(i.depth for i in r) for r in rows)
                    + gap * (len(rows) - 1)) > room_d + EPS:
        rejected.extend(rows.pop())
    # Tallest row in the middle, then behind, in front, ...
    rows = _center_out(rows)
    y = 0.0
    for r in rows:
        row_d = max(i.depth for i in r)
        x = 0.0
        for it in _center_out(r):
            it.x = x
            it.y = y + (row_d - it.depth) / 2.0
            x += it.width + gap
        # Center the row on X = 0.
        shift = -(x - gap) / 2.0
        for it in r:
            it.x += shift
        y += row_d + gap
    done = [i for r in rows for i in r]
    if done:
        box = (min(i.x for i in done), min(i.y for i in done),
               max(i.x + i.width for i in done), max(i.y + i.depth for i in done))
        if not _fit_cluster(done, box, bed, margin):
            return _layout_bounded(items, bed, gap, margin)
    return rejected


def layout_grid(items, bed, gap, margin):
    """Place items on a grid of equal cells; the tallest items take the center cells.

    Returns the items that did not fit.
    """
    if not items:
        return []
    cell_w = max(i.width for i in items) + gap
    cell_d = max(i.depth for i in items) + gap
    usable_w = bed.x1 - bed.x0 - 2 * margin + gap
    usable_d = bed.y1 - bed.y0 - 2 * margin + gap
    cols = max(int((usable_w + EPS) // cell_w), 0)
    rows = max(int((usable_d + EPS) // cell_d), 0)
    if cols == 0 or rows == 0:
        return list(items)
    # Use the smallest grid with room for every item, close to square.
    n = len(items)
    best = None
    for c in range(1, cols + 1):
        r = min(rows, int(math.ceil(n / float(c))))
        if c * r < min(n, cols * rows):
            continue
        score = (c * cell_w - gap) * (r * cell_d - gap)
        if best is None or score < best[0] - EPS:
            best = (score, c, r)
    _, cols, rows = best
    cx, cy = bed.center
    gx0 = cx - (cols * cell_w - gap) / 2.0
    gy0 = cy - (rows * cell_d - gap) / 2.0
    cells = []
    for r in range(rows):
        for c in range(cols):
            x = gx0 + c * cell_w
            y = gy0 + r * cell_d
            dist = (x + (cell_w - gap) / 2.0 - cx) ** 2 + (y + (cell_d - gap) / 2.0 - cy) ** 2
            cells.append((dist, r, c, x, y))
    cells.sort()
    order = sorted(items, key=lambda i: (-i.height, i.index))
    rejected = []
    free = [cell for cell in cells
            if bed.contains_rect(cell[3], cell[4], cell[3] + cell_w - gap,
                                 cell[4] + cell_d - gap, margin)]
    for it in order:
        if not free:
            rejected.append(it)
            continue
        _, _, _, x, y = free.pop(0)
        # Center the item in its cell.
        it.x = x + (cell_w - gap - it.width) / 2.0
        it.y = y + (cell_d - gap - it.depth) / 2.0
    return rejected


def arrange(items, bed, plate_count, settings, config, plate_sequences=None):
    """Assign items to plates and place them. Returns the new plate count."""
    layouts = {"grid": layout_grid, "rows": layout_rows}
    layout = layouts.get(settings.get("layout"), layout_compact)
    plate_sequences = plate_sequences or {}
    global_sequence = _str(config.get("print_sequence"), "by layer")

    def margin_for(plate):
        value = settings.get("edge_margin", "auto")
        if value in (None, "", "auto"):
            return edge_margin(config, plate_sequences.get(plate, global_sequence))
        return max(float(value), 0.0)

    margin = min(margin_for(p) for p in range(max(plate_count, 1)))

    def gap_for(plate, plate_items):
        spacing = settings.get("spacing", "auto")
        if spacing in (None, "", "auto"):
            return auto_gap(config, plate_sequences.get(plate, global_sequence), plate_items)
        return max(float(spacing), 0.0)

    for it in items:
        if (it.width > bed.x1 - bed.x0 - 2 * margin + EPS
                or it.depth > bed.y1 - bed.y0 - 2 * margin + EPS):
            raise ArrangeError("Object %s (%.0f x %.0f mm) is larger than the bed." % (
                it.object_id, it.width, it.depth))

    def fill_plates(queue, plates):
        """Put the tallest-first `queue` on new plates. Each plate takes the longest run of
        the queue that fits, so the objects on one plate are of similar height."""
        queue = sorted(queue, key=lambda i: (-i.height, i.index))
        while queue:
            plate = len(plates)
            if plate >= MAX_PLATE_COUNT:
                raise ArrangeError("The objects need more than %d plates." % MAX_PLATE_COUNT)
            lo, hi = 0, len(queue)          # queue[:lo] fits; find the largest such lo
            while lo < hi:
                mid = (lo + hi + 1) // 2
                trial = queue[:mid]
                if layout(trial, bed, gap_for(plate, trial), margin_for(plate)):
                    hi = mid - 1
                else:
                    lo = mid
            if lo == 0:
                raise ArrangeError("Object %s does not fit on a plate." % queue[0].object_id)
            chosen = queue[:lo]
            layout(chosen, bed, gap_for(plate, chosen), margin_for(plate))
            for it in chosen:
                it.plate = plate
            plates.append(chosen)
            queue = queue[lo:]

    plates = []
    if settings.get("plates") == "current":
        target = int(settings.get("plate_index", 0))
        count = max([plate_count] + [it.plate + 1 for it in items])
        if not 0 <= target < count:
            raise ArrangeError("Plate %d does not exist." % (target + 1))
        groups = {}
        for it in items:
            groups.setdefault(it.plate, []).append(it)
        plates = [groups.get(p, []) for p in range(count)]
        for it in items:
            if it.plate != target:
                it.x, it.y = it.bbox[0], it.bbox[1]      # other plates stay as they are
        chosen = plates[target]
        if not chosen:
            raise ArrangeError("Plate %d is empty." % (target + 1))
        for it in chosen:
            it.moved = True
        overflow = layout(chosen, bed, gap_for(target, chosen), margin_for(target))
        for it in overflow:
            chosen.remove(it)
        # Objects that do not fit go to new plates at the end.
        fill_plates(overflow, plates)
    elif settings.get("plates") == "regroup":
        for it in items:
            it.moved = True
        fill_plates(items, plates)
    else:
        for it in items:
            it.moved = True
        groups = {}
        for it in items:
            groups.setdefault(it.plate, []).append(it)
        plates = [groups.get(p, []) for p in range(max([plate_count] + [p + 1 for p in groups]))]
        overflow = []
        for plate, plate_items in enumerate(plates):
            rejected = layout(plate_items, bed, gap_for(plate, plate_items),
                              margin_for(plate))
            for it in rejected:
                plate_items.remove(it)
            overflow.extend(rejected)
        # Objects that do not fit go to new plates at the end.
        fill_plates(overflow, plates)
    for plate, plate_items in enumerate(plates):
        for it in plate_items:
            it.plate = plate
    return max(len(plates), 1)


def object_order(items):
    """Object ids in the new object list order.

    By plate, then shortest to tallest. Objects of equal height are ordered in rows
    (back to front, alternating direction) to keep travel short. Objects this run did not
    move keep their place in the list; the moved objects are sorted into the places the
    moved objects had.
    """
    original = []
    for it in sorted(items, key=lambda i: i.index):
        if it.object_id not in original:
            original.append(it.object_id)
    moved = set(it.object_id for it in items if it.moved)
    sorted_moved = iter(_sorted_objects([it for it in items if it.object_id in moved]))
    return [next(sorted_moved) if oid in moved else oid for oid in original]


def _sorted_objects(items):
    first = {}
    for it in items:
        key = (it.plate, round(it.height, 1))
        if it.object_id not in first or key < first[it.object_id][0]:
            first[it.object_id] = (key, it)

    def sort_key(object_id):
        (plate, height), it = first[object_id]
        row = int(-(it.y or 0.0) // ROW_BAND)
        x = it.x or 0.0
        return (plate, height, row, x if row % 2 == 0 else -x)

    return sorted(first, key=sort_key)


# ---------------------------------------------------------------------------
# Layer heights (port of OrcaSlicer's Slicing.cpp and SlicingAdaptive.cpp)
# ---------------------------------------------------------------------------

SLICE_EPSILON = 1e-4             # EPSILON in libslic3r
MIN_LAYER_HEIGHT = 0.01
MIN_LAYER_HEIGHT_DEFAULT = 0.07
LAYER_HEIGHT_CHANGE_STEP = 0.04


class SlicingParams:
    """The parts of OrcaSlicer's SlicingParameters the layer height tools use."""

    def __init__(self, config, object_config, object_height):
        def value(key, default=0.0):
            v = object_config.get(key)
            return _num(v if v not in (None, "") else config.get(key), default)

        self.layer_height = value("layer_height", 0.2)
        first = value("initial_layer_print_height", 0.0)
        self.first_object_layer_height = first if first > 0 else self.layer_height
        raft = int(value("raft_layers", 0))
        # With a raft the first object layer is a normal layer and may change.
        self.first_layer_fixed = raft == 0
        if raft:
            self.first_object_layer_height = self.layer_height
        nozzle = max(int(value("extruder", 1)), 1) - 1
        lo = _at(config.get("min_layer_height"), nozzle)
        lo = MIN_LAYER_HEIGHT_DEFAULT if lo == 0 else max(MIN_LAYER_HEIGHT, lo)
        hi = _at(config.get("max_layer_height"), nozzle)
        if hi == 0:
            hi = 0.75 * (_at(config.get("nozzle_diameter"), nozzle) or 0.4)
        hi = max(lo, hi)
        self.min_layer_height = min(max(MIN_LAYER_HEIGHT, lo), self.layer_height)
        self.max_layer_height = max(hi, self.layer_height)
        self.object_height = object_height


def _at(values, index, default=0.0):
    if isinstance(values, str):
        values = values.split(",")
    if not values:
        return default
    try:
        return float(values[min(index, len(values) - 1)])
    except (TypeError, ValueError):
        return default


def _faces(triangles, z_min):
    """(z_low, z_high, |n.z|, |n.xy|) for each triangle, sorted by z span."""
    faces = []
    for a, b, c in triangles:
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length == 0:
            continue
        zs = (a[2] - z_min, b[2] - z_min, c[2] - z_min)
        faces.append((min(zs), max(zs), abs(nz) / length, math.hypot(nx, ny) / length))
    faces.sort()
    return faces


def _slope_height(face, deviation):
    """layer_height_from_slope in SlicingAdaptive.cpp."""
    n_cos, n_sin = face[2], face[3]
    limit = 1.44 * deviation * math.sqrt(n_sin / n_cos) if n_cos > 1e-5 else float("inf")
    return min(deviation / 0.184, limit)


def _deviation(params, quality):
    """The allowed surface deviation for a Quality / Speed value."""
    if quality < 0.5:
        return params.min_layer_height + (
            params.layer_height - params.min_layer_height) * 2.0 * quality
    return params.max_layer_height + (
        params.layer_height - params.max_layer_height) * 2.0 * (1.0 - quality)


def _next_layer_height(faces, params, print_z, deviation, state):
    """SlicingAdaptive::next_layer_height. `state` holds the first face index to visit."""
    height = params.max_layer_height
    i = state[0]
    first_hit = False
    while i < len(faces):
        z0, z1 = faces[i][0], faces[i][1]
        if z0 >= print_z:
            break
        if z1 > print_z:
            if not first_hit:
                first_hit = True
                state[0] = i
            if z1 >= print_z + SLICE_EPSILON:
                height = min(height, _slope_height(faces[i], deviation))
        i += 1
    height = max(height, params.min_layer_height)
    if height > params.min_layer_height:
        while i < len(faces):
            z0, z1 = faces[i][0], faces[i][1]
            if z0 >= print_z + height:
                break
            if z1 >= print_z + SLICE_EPSILON:
                reduced = _slope_height(faces[i], deviation)
                z_diff = z0 - print_z
                if reduced < z_diff:
                    height = z_diff
                elif reduced < height:
                    height = reduced
            i += 1
        height = max(height, params.min_layer_height)
    return height


def adaptive_profile(triangles, params, quality, height_ranges=()):
    """layer_height_profile_adaptive in Slicing.cpp: [z0, h0, z1, h1, ...]."""
    z_min = min(min(t[0][2], t[1][2], t[2][2]) for t in triangles)
    deviation = _deviation(params, quality)
    # A face that allows the maximum layer height never lowers a layer (steep walls), so
    # leaving it out gives the same profile, much faster for tall parts.
    faces = [f for f in _faces(triangles, z_min)
             if _slope_height(f, deviation) < params.max_layer_height]
    first = params.first_object_layer_height
    profile = [0.0, first]
    if params.first_layer_fixed:
        profile += [first, first]
    print_z = first
    state = [0]
    while print_z + SLICE_EPSILON < params.object_height:
        height = min(_next_layer_height(faces, params, print_z, deviation, state),
                     params.max_layer_height)
        last = profile[-1]
        if last < height and height - last > LAYER_HEIGHT_CHANGE_STEP:
            height = last + LAYER_HEIGHT_CHANGE_STEP
        elif last > height and last - height > LAYER_HEIGHT_CHANGE_STEP:
            height = last - LAYER_HEIGHT_CHANGE_STEP
        for lo, hi, h in height_ranges:
            if lo <= print_z <= hi:
                height = h
                break
        profile += [print_z, height]
        print_z += height
    z_gap = params.object_height - profile[-2]
    if z_gap > 0:
        profile += [params.object_height,
                    min(max(z_gap, params.min_layer_height), params.max_layer_height)]
    return profile


def smooth_profile(profile, params, radius=5, keep_min=False):
    """smooth_height_profile in Slicing.cpp: a biased Gaussian blur, applied six times."""
    radius = max(int(radius), 1)
    size = 2 * radius + 1
    sigma = 0.3 * (radius - 1) + 0.8
    two_sq_sigma = 2.0 * sigma * sigma
    scale = 1.0 / math.sqrt(math.pi * two_sq_sigma)
    kernel = [scale * math.exp(-((i - radius) ** 2) / two_sq_sigma) for i in range(size)]
    skip = 4 if params.first_layer_fixed else 0
    delta_h = params.max_layer_height - params.min_layer_height
    inv_delta_h = 1.0 / delta_h if delta_h != 0 else 1.0
    max_dz_band = radius * params.layer_height

    def blur(prof):
        n = len(prof)
        if n - skip < 6:
            return list(prof)
        out = list(prof[:skip])
        for i in range(skip, n, 2):
            zi, hi = prof[i], prof[i + 1]
            begin = max(i - 2 * radius, skip)
            end = min(i + 2 * radius, n - 2)
            total = weight_total = 0.0
            for j in range(begin, end + 1, 2):
                k = radius + (j - i) // 2
                if abs(zi - prof[j]) * params.layer_height <= max_dz_band:
                    dh = abs(params.max_layer_height - prof[j + 1])
                    w = kernel[k] * math.sqrt(dh * inv_delta_h)
                    total += w * prof[j + 1]
                    weight_total += w
            h = hi if weight_total == 0 else total / weight_total
            h = min(max(h, params.min_layer_height), params.max_layer_height)
            if keep_min:
                h = min(h, hi)
            out += [zi, h]
        return out

    for _ in range(6):
        profile = blur(profile)
    return profile


def merge_finest(profiles):
    """layer_height_profile_merge_finest in Slicing.cpp: the finest height any profile asks
    for at each Z, so objects that share it print their layers at the same Z."""
    tallest = max(profiles, key=lambda p: p[-2])
    top = tallest[-2]
    cursors = [0] * len(profiles)

    def finest_at(z):
        height = float("inf")
        for k, prof in enumerate(profiles):
            end = len(prof) - 2
            prof_top = prof[end]
            if z > prof_top + SLICE_EPSILON:
                last = prof[end - 1]
                height = min(height, math.sqrt(
                    last * last + 2.0 * LAYER_HEIGHT_CHANGE_STEP * (z - prof_top)))
                continue
            i = cursors[k]
            while i + 2 < end and prof[i + 2] <= z:
                i += 2
            cursors[k] = i
            if i + 2 < end:
                t = (z - prof[i]) / (prof[i + 2] - prof[i])
                height = min(height, prof[i + 1] + (prof[i + 3] - prof[i + 1]) * t)
            else:
                height = min(height, prof[i + 1])
        return height

    out = [0.0, tallest[1]]
    print_z = tallest[1]
    if (len(tallest) >= 4 and abs(tallest[2] - print_z) < SLICE_EPSILON
            and abs(tallest[3] - print_z) < SLICE_EPSILON):
        out += [print_z, print_z]
    while print_z + SLICE_EPSILON < top:
        height = finest_at(print_z)
        if height < SLICE_EPSILON:
            break
        out += [print_z, height]
        print_z += height
    out += [top, out[-2]]
    return out


def fit_to_height(profile, object_height):
    """layer_height_profile_fit_to_height in Slicing.cpp."""
    out = []
    i = 0
    while i < len(profile) and profile[i] < object_height - SLICE_EPSILON:
        out += [profile[i], profile[i + 1]]
        i += 2
    height = profile[-1]
    if i < len(profile) and i >= 2:
        z1, z2 = profile[i - 2], profile[i]
        t = min(max((object_height - z1) / (z2 - z1), 0.0), 1.0) if z2 != z1 else 0.0
        height = profile[i - 1] + (profile[i + 1] - profile[i - 1]) * t
    out += [object_height, height]
    return out


def default_profile(params, height_ranges=()):
    """The profile OrcaSlicer uses for an object without one: the layer height, with
    height range modifiers."""
    out = [0.0, params.layer_height]
    for lo, hi, h in sorted(height_ranges):
        out += [lo, params.layer_height, lo, h, hi, h, hi, params.layer_height]
    out += [params.object_height, params.layer_height]
    return out


def valid_profile(profile, params):
    """Whether OrcaSlicer would use this profile (PrintObject::update_layer_height_profile)."""
    return (profile is not None and len(profile) >= 4 and len(profile) % 2 == 0
            and abs(profile[-2] - params.object_height) <= 1e-3
            and abs(profile[1] - params.first_object_layer_height) < 1e-6)


def object_layers(params, profile):
    """generate_object_layers in Slicing.cpp: [(bottom z, top z), ...]."""
    out = []
    print_z = 0.0
    if params.first_layer_fixed:
        print_z = params.first_object_layer_height
        out.append((0.0, print_z))
    idx = 0
    n = len(profile)
    slice_z = print_z + 0.5 * params.min_layer_height
    while slice_z < params.object_height:
        height = params.min_layer_height
        if idx < n:
            nxt = idx + 2
            while nxt < n and slice_z >= profile[nxt]:
                idx = nxt
                nxt += 2
            z1, h1 = profile[idx], profile[idx + 1]
            height = h1
            if nxt < n:
                z2, h2 = profile[nxt], profile[nxt + 1]
                height = h1 + (h2 - h1) * (slice_z - z1) / (z2 - z1) if z2 != z1 else h1
        height = max(height, params.min_layer_height)
        slice_z = print_z + 0.5 * height
        if slice_z >= params.object_height:
            break
        out.append((print_z, print_z + height))
        print_z += height
        slice_z = print_z + 0.5 * params.min_layer_height
    return out


class CrossSection:
    """Outline length and solid area of an object's horizontal cross-sections.

    Along each triangle, the cut segment's ends move linearly with Z, so the outline
    length is piecewise linear and the area piecewise quadratic in Z. Both are summed with
    a sweep over the triangles' Z ranges instead of cutting each layer.
    """

    def __init__(self, triangles):
        z_min = min(min(t[0][2], t[1][2], t[2][2]) for t in triangles)
        events = []
        for tri in triangles:
            a, b, c = tri
            nx = (b[1] - a[1]) * (c[2] - a[2]) - (b[2] - a[2]) * (c[1] - a[1])
            ny = (b[2] - a[2]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[2] - a[2])
            if abs(nx) < 1e-12 and abs(ny) < 1e-12:
                continue
            v0, v1, v2 = sorted(((p[0], p[1], p[2] - z_min) for p in tri), key=lambda p: p[2])
            for lo, hi, e1, e2 in ((v0, v1, (v0, v1), (v0, v2)), (v1, v2, (v1, v2), (v0, v2))):
                z0, z1 = lo[2], hi[2]
                if z1 - z0 < 1e-9:
                    continue
                pa, pb = self._line(e1)
                qa, qb = self._line(e2)
                # Segment p -> q: material on the left, so the outward normal is on the right.
                zm = 0.5 * (z0 + z1)
                dx = (qa[0] + qb[0] * zm) - (pa[0] + pb[0] * zm)
                dy = (qa[1] + qb[1] * zm) - (pa[1] + pb[1] * zm)
                sign = 1.0 if nx * dy - ny * dx > 0 else -1.0
                c0 = sign * (pa[0] * qa[1] - pa[1] * qa[0])
                c1 = sign * (pa[0] * qb[1] + pb[0] * qa[1] - pa[1] * qb[0] - pb[1] * qa[0])
                c2 = sign * (pb[0] * qb[1] - pb[1] * qb[0])
                l0 = math.hypot((qa[0] + qb[0] * z0) - (pa[0] + pb[0] * z0),
                                (qa[1] + qb[1] * z0) - (pa[1] + pb[1] * z0))
                l1 = math.hypot((qa[0] + qb[0] * z1) - (pa[0] + pb[0] * z1),
                                (qa[1] + qb[1] * z1) - (pa[1] + pb[1] * z1))
                m = (l1 - l0) / (z1 - z0)
                coef = (m, l0 - m * z0, c0, c1, c2)
                events.append((z0, 1, coef))
                events.append((z1, -1, coef))
        events.sort(key=lambda e: e[0])
        self.events = events
        self.height = max(max(t[0][2], t[1][2], t[2][2]) for t in triangles) - z_min

    @staticmethod
    def _line(edge):
        """(x, y) = a + b * z along an edge."""
        p, q = edge
        dz = q[2] - p[2]
        bx, by = (q[0] - p[0]) / dz, (q[1] - p[1]) / dz
        return (p[0] - bx * p[2], p[1] - by * p[2]), (bx, by)

    def sample(self, zs):
        """[(outline length, area)] at each Z (ascending)."""
        acc = [0.0] * 5
        out = []
        i = 0
        for z in zs:
            while i < len(self.events) and self.events[i][0] <= z:
                _, sign, coef = self.events[i]
                for k in range(5):
                    acc[k] += sign * coef[k]
                i += 1
            length = acc[0] * z + acc[1]
            area = 0.5 * (acc[4] * z * z + acc[3] * z + acc[2])
            out.append((max(length, 0.0), max(area, 0.0)))
        return out


class TimeModel:
    """A rough print time model, used to compare two versions of the same plates.

    Per layer and object: walls (outline length x wall loops) and infill (the area inside
    the walls; solid near the top and bottom), each at its speed, capped by the filament's
    maximum volumetric speed. Per layer: travel between the objects, a retraction for each
    object, a layer change, and the minimum layer time for cooling. Acceleration, support,
    bridges and the purge are left out, so the absolute time is lower than OrcaSlicer's.
    """

    def __init__(self, config):
        self.c = config
        self.nozzle = _at(config.get("nozzle_diameter"), 0) or 0.4
        self.loops = max(int(_num(config.get("wall_loops"), 2)), 1)
        self.density = _num(str(_str(config.get("sparse_infill_density"), "15%")).rstrip("%"),
                            15.0) / 100.0
        self.max_flow = _at(config.get("filament_max_volumetric_speed"), 0) or 15.0
        self.cooling = _str(config.get("slow_down_for_layer_cooling"), "1") not in ("0", "false")
        self.layer_time = _at(config.get("slow_down_layer_time"), 0) if self.cooling else 0.0
        self.travel = self.speed("travel_speed", 300.0)
        self.retract = 0.0
        length = _at(config.get("retraction_length"), 0)
        rspeed = _at(config.get("retraction_speed"), 0) or 30.0
        if length:
            self.retract = 2.0 * length / rspeed
        self.top_shell = (int(_num(config.get("top_shell_layers"), 3)),
                          _num(config.get("top_shell_thickness"), 0.0))
        self.bottom_shell = (int(_num(config.get("bottom_shell_layers"), 3)),
                             _num(config.get("bottom_shell_thickness"), 0.0))
        self.layer_height = _num(config.get("layer_height"), 0.2)

    def speed(self, key, default):
        return _num(self.c.get(key), default) or default

    def width(self, key):
        value = _str(self.c.get(key), "")
        if not value or value in ("0", "0%"):
            value = _str(self.c.get("line_width"), "")
        if value.endswith("%"):
            return self.nozzle * _num(value[:-1], 112.5) / 100.0
        return _num(value, 0.0) or self.nozzle * 1.125

    def _rate(self, speed, width, height):
        return min(speed, self.max_flow / max(width * height, 1e-6))

    def object_layer(self, length, area, height, first, solid):
        """Seconds to print one layer of one object."""
        if area <= 0 or length <= 0:
            return 0.0
        w_out, w_in = self.width("outer_wall_line_width"), self.width("inner_wall_line_width")
        if first:
            s_out = s_in = s_fill = self.speed("initial_layer_speed", 50.0)
            s_fill = self.speed("initial_layer_infill_speed", s_fill)
        else:
            s_out = self.speed("outer_wall_speed", 100.0)
            s_in = self.speed("inner_wall_speed", 150.0)
            s_fill = (self.speed("internal_solid_infill_speed", 150.0) if solid
                      else self.speed("sparse_infill_speed", 150.0))
        # Walls cannot cover more than the area: thin parts get fewer loops.
        wall_len = length * self.loops
        cap = area / max(w_in, 1e-6)
        scale = min(1.0, cap / wall_len) if wall_len > 0 else 0.0
        t = scale * length * (1.0 / self._rate(s_out, w_out, height)
                              + (self.loops - 1) / self._rate(s_in, w_in, height))
        fill_area = max(area - length * (w_out + (self.loops - 1) * w_in), 0.0)
        if fill_area > 0:
            w_fill = self.width("sparse_infill_line_width")
            fill_len = fill_area / w_fill * (1.0 if solid or first else self.density)
            t += fill_len / self._rate(s_fill, w_fill, height)
        return t

    def is_solid(self, bottom, top, height_above, height_below):
        layers, thickness = self.bottom_shell
        if height_below < max(layers * self.layer_height, thickness) - SLICE_EPSILON:
            return True
        layers, thickness = self.top_shell
        return height_above < max(layers * self.layer_height, thickness) - SLICE_EPSILON

    def plate_time(self, objects):
        """objects: [(center x, center y, layers, samples)] -> (seconds, plate layers)."""
        by_z = {}
        for k, (_x, _y, layers, samples) in enumerate(objects):
            top = layers[-1][1] if layers else 0.0
            for (z0, z1), (length, area) in zip(layers, samples):
                solid = self.is_solid(z0, z1, top - z1, z0)
                t = self.object_layer(length, area, z1 - z0, z0 < SLICE_EPSILON, solid)
                key = round(z1, 4)
                entry = by_z.setdefault(key, [0.0, set()])
                entry[0] += t
                entry[1].add(k)
        total = 0.0
        tours = {}
        for key in sorted(by_z):
            t, present = by_z[key]
            t = max(t, self.layer_time)
            members = frozenset(present)
            if members not in tours:
                tours[members] = self._tour([objects[k][:2] for k in sorted(members)])
            total += t + tours[members] / self.travel + self.retract * len(members) + 0.2
        return total, len(by_z)

    @staticmethod
    def _tour(points):
        """Length of a nearest-neighbor loop through the points."""
        if len(points) < 2:
            return 0.0
        left = list(points[1:])
        cur = points[0]
        length = 0.0
        while left:
            nxt = min(left, key=lambda p: (p[0] - cur[0]) ** 2 + (p[1] - cur[1]) ** 2)
            length += math.hypot(nxt[0] - cur[0], nxt[1] - cur[1])
            left.remove(nxt)
            cur = nxt
        return length + math.hypot(cur[0] - points[0][0], cur[1] - points[0][1])


def format_duration(seconds):
    minutes = int(round(seconds / 60.0))
    if minutes < 60:
        return "%d min" % minutes
    return "%d h %02d min" % divmod(minutes, 60)


def parse_profiles(text):
    """{object index: [floats]} from layer_heights_profile.txt."""
    out = {}
    for line in (text or "").splitlines():
        head, _, body = line.partition("|")
        key, _, index = head.partition("=")
        if key.strip() != "object_id" or not body:
            continue
        try:
            out[int(index)] = [float(v) for v in body.split(";")]
        except ValueError:
            continue
    return out


def format_profiles(profiles):
    lines = []
    for index in sorted(profiles):
        values = profiles[index]
        if len(values) >= 4 and len(values) % 2 == 0:
            lines.append("object_id=%d|%s" % (index, ";".join("%f" % v for v in values)))
    return "\n".join(lines) + ("\n" if lines else "")


def parse_height_ranges(xml_text, index):
    """[(min_z, max_z, layer_height)] of one object from layer_config_ranges.xml."""
    ranges = []
    if not xml_text:
        return ranges
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ranges
    for obj in root.iter("object"):
        if obj.get("id") != str(index):
            continue
        for rng in obj.findall("range"):
            for opt in rng.findall("option"):
                if opt.get("opt_key") == "layer_height":
                    try:
                        ranges.append((float(rng.get("min_z")), float(rng.get("max_z")),
                                       float(opt.text)))
                    except (TypeError, ValueError):
                        pass
    return ranges


def remap_line_indexed(text, mapping):
    out = []
    for line in text.splitlines():
        m = re.match(r"(\s*object_id\s*=\s*)(\d+)(\|.*)", line)
        if m and int(m.group(2)) in mapping:
            line = "%s%d%s" % (m.group(1), mapping[int(m.group(2))], m.group(3))
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def remap_xml_indexed(text, mapping):
    def sub(m):
        old = int(m.group(2))
        return "%s%d%s" % (m.group(1), mapping.get(old, old), m.group(3))
    return re.sub(r'(<object\b[^>]*?\bid=")(\d+)(")', sub, text)


# ---------------------------------------------------------------------------
# 3MF reading
# ---------------------------------------------------------------------------

_ITEM_RE = re.compile(r"<item\b[^>]*?/>|<item\b[^>]*?>.*?</item>", re.S)
_BUILD_RE = re.compile(r"(<build\b[^>]*>)(.*?)(</build>)", re.S)
_ATTR_RE = r'\b%s\s*=\s*"([^"]*)"'


def _attr(text, name):
    m = re.search(_ATTR_RE % re.escape(name), text)
    return m.group(1) if m else None


def parse_transform(text):
    """3MF transform "m00 m01 m02 m10 ... m32" as a 3x4 row-major list of rows."""
    if not text:
        return [[1, 0, 0], [0, 1, 0], [0, 0, 1], [0, 0, 0]]
    v = [float(x) for x in text.split()]
    return [v[0:3], v[3:6], v[6:9], v[9:12]]


def apply_transform(m, p):
    x, y, z = p
    return (x * m[0][0] + y * m[1][0] + z * m[2][0] + m[3][0],
            x * m[0][1] + y * m[1][1] + z * m[2][1] + m[3][1],
            x * m[0][2] + y * m[1][2] + z * m[2][2] + m[3][2])


def compose(a, b):
    """Transform that applies a, then b (3MF row-vector order)."""
    out = []
    for r in range(3):
        out.append([sum(a[r][k] * b[k][c] for k in range(3)) for c in range(3)])
    out.append([sum(a[3][k] * b[k][c] for k in range(3)) + b[3][c] for c in range(3)])
    return out


def format_transform(m):
    vals = m[0] + m[1] + m[2] + m[3]
    return " ".join(_fmt(v) for v in vals)


def _fmt(v):
    s = "%.9f" % v
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def read_build_items(model_xml):
    """Return [(object_id, path, transform_text)] for each <item> in <build>."""
    m = _BUILD_RE.search(model_xml)
    if not m:
        raise ArrangeError("The 3MF file has no build section.")
    out = []
    for item in _ITEM_RE.findall(m.group(2)):
        out.append((_attr(item, "objectid"), _attr(item, "p:path") or "", _attr(item, "transform")))
    return out


def _local(tag):
    return tag.rsplit("}", 1)[-1]


class MeshLibrary:
    """Reads object geometry from the 3MF to compute world bounding boxes."""

    def __init__(self, zf):
        self.zf = zf
        self.objects = {}   # (path, id) -> {"vertices": [...]} or {"components": [...]}
        self.loaded = set()

    def _load(self, path):
        if path in self.loaded:
            return
        self.loaded.add(path)
        name = path.lstrip("/")
        if name not in self.zf.namelist():
            return
        current = None
        verts = None
        with self.zf.open(name) as fh:
            for event, el in ET.iterparse(fh, events=("start", "end")):
                tag = _local(el.tag)
                if event == "start":
                    if tag == "object":
                        current = {"id": el.get("id"), "vertices": None, "triangles": [],
                                   "components": []}
                    elif tag == "vertices":
                        verts = []
                    continue
                if tag == "vertex" and verts is not None:
                    verts.append((float(el.get("x")), float(el.get("y")), float(el.get("z"))))
                    el.clear()
                elif tag == "vertices":
                    current["vertices"] = verts
                    verts = None
                elif tag == "triangle":
                    if current is not None:
                        current["triangles"].append(
                            (int(el.get("v1")), int(el.get("v2")), int(el.get("v3"))))
                    el.clear()
                elif tag == "component" and current is not None:
                    cpath = None
                    for k, v in el.attrib.items():
                        if _local(k) == "path":
                            cpath = v
                    current["components"].append(
                        (el.get("objectid"), cpath or path, parse_transform(el.get("transform"))))
                elif tag == "object" and current is not None:
                    self.objects[(path, current["id"])] = current
                    current = None
                    el.clear()

    def _object(self, path, object_id):
        self._load(path)
        obj = self.objects.get((path, object_id))
        if obj is None:
            raise ArrangeError("Object %s is missing from %s." % (object_id, path))
        return obj

    def points(self, path, object_id, transform, depth=0, parts=None):
        """Every vertex of the object, in world coordinates.

        `parts` lists, for each top-level component, whether it is a model part; modifiers
        and other volumes are left out.
        """
        if depth > 10:
            raise ArrangeError("The 3MF file has circular components.")
        obj = self._object(path, object_id)
        out = [apply_transform(transform, v) for v in obj["vertices"] or ()]
        for k, (cid, cpath, ctrans) in enumerate(obj["components"]):
            if parts is not None and k < len(parts) and not parts[k]:
                continue
            out.extend(self.points(cpath, cid, compose(ctrans, transform), depth + 1))
        return out

    def triangles(self, path, object_id, transform, depth=0, parts=None):
        """Every triangle of the object as three world-coordinate vertices."""
        if depth > 10:
            raise ArrangeError("The 3MF file has circular components.")
        obj = self._object(path, object_id)
        out = []
        if obj["vertices"]:
            verts = [apply_transform(transform, v) for v in obj["vertices"]]
            out.extend((verts[a], verts[b], verts[c]) for a, b, c in obj["triangles"])
        for k, (cid, cpath, ctrans) in enumerate(obj["components"]):
            if parts is not None and k < len(parts) and not parts[k]:
                continue
            out.extend(self.triangles(cpath, cid, compose(ctrans, transform), depth + 1))
        return out

    def bbox(self, path, object_id, transform, parts=None):
        pts = self.points(path, object_id, transform, parts=parts)
        if not pts:
            return None
        xs, ys, zs = zip(*pts)
        return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))

    def footprint(self, path, object_id, transform, parts=None):
        """Convex outline (x, y) seen from above, and the z range, in world coordinates."""
        # Instances that differ only by position share one outline.
        linear = tuple(tuple(round(v, 9) for v in row) for row in transform[:3])
        key = (path, object_id, linear, tuple(parts or ()))
        cache = self.__dict__.setdefault("_footprints", {})
        if key not in cache:
            local = [row[:] for row in transform[:3]] + [[0.0, 0.0, 0.0]]
            pts = self.points(path, object_id, local, parts=parts)
            if not pts:
                cache[key] = None
            else:
                cache[key] = (convex_hull([(p[0], p[1]) for p in pts]),
                              min(p[2] for p in pts), max(p[2] for p in pts))
        base = cache[key]
        if base is None:
            return None
        hull, z0, z1 = base
        tx, ty, tz = transform[3]
        return [(x + tx, y + ty) for x, y in hull], z0 + tz, z1 + tz


def _merge(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), min(a[2], b[2]),
            max(a[3], b[3]), max(a[4], b[4]), max(a[5], b[5]))


def read_plates(settings_xml):
    """Read the plate list.

    Returns ({(object_id, instance_index): (plate, identify_id)}, plate_count,
    {plate: print_sequence}).
    """
    if not settings_xml:
        return {}, 1, {}
    root = ET.fromstring(settings_xml)
    mapping, sequences = {}, {}
    plates = root.findall("plate")
    for idx, plate in enumerate(plates):
        meta = {m.get("key"): m.get("value") for m in plate.findall("metadata")}
        if meta.get("print_sequence"):
            sequences[idx] = meta["print_sequence"]
        for inst in plate.findall("model_instance"):
            im = {m.get("key"): m.get("value") for m in inst.findall("metadata")}
            try:
                mapping[(im.get("object_id"), int(im.get("instance_id", "0")))] = (
                    idx, im.get("identify_id"))
            except ValueError:
                pass
    return mapping, max(len(plates), 1), sequences


def read_object_info(settings_xml):
    """Per-object data from model_settings.config.

    Returns {object_id: {"name", "parts", "config"}}: "parts" lists, in volume order,
    whether each volume is a model part; "config" holds the object's own settings.
    """
    info = {}
    if not settings_xml:
        return info
    for obj in ET.fromstring(settings_xml).findall("object"):
        config = {m.get("key"): m.get("value") for m in obj.findall("metadata")}
        parts = [p.get("subtype", "normal_part") == "normal_part" for p in obj.findall("part")]
        info[obj.get("id")] = {"name": config.get("name") or "", "parts": parts or None,
                               "config": config}
    return info


class Project:
    """A 3MF project opened for arranging."""

    def __init__(self, path):
        self.path = path
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if MODEL_PATH not in names:
                raise ArrangeError("%s is not a 3MF project." % os.path.basename(path))
            self.model_xml = zf.read(MODEL_PATH).decode("utf-8")
            self.settings_xml = (zf.read(MODEL_SETTINGS_PATH).decode("utf-8")
                                 if MODEL_SETTINGS_PATH in names else "")
            self.config = (json.loads(zf.read(PROJECT_SETTINGS_PATH).decode("utf-8"))
                           if PROJECT_SETTINGS_PATH in names else {})
        self.build = read_build_items(self.model_xml)
        self.bed = bed_from_config(self.config)
        mapping, self.plate_count, self.plate_sequences = read_plates(self.settings_xml)
        self.info = read_object_info(self.settings_xml)
        self.names = {oid: i["name"] for oid, i in self.info.items()}
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            self.indexed_files = {n: zf.read(n).decode("utf-8", "replace")
                                  for n in LINE_INDEXED_PATHS + XML_INDEXED_PATHS
                                  if n in names}
        self.items = []
        counts = {}
        for index, (object_id, _path, transform) in enumerate(self.build):
            inst = counts.get(object_id, 0)
            counts[object_id] = inst + 1
            it = Item(index, object_id, inst, None)
            it.transform = parse_transform(transform)
            it.plate, it.identify_id = mapping.get((object_id, inst), (-1, None))
            self.items.append(it)

    def load_bboxes(self, bboxes=None, align_y=False, only_plate=None):
        """Set each item's world bounding box, from `bboxes` or from the meshes.

        With `align_y`, long objects are first turned so their long side runs along Y
        (only those on `only_plate` when it is given).
        """
        if align_y:
            self._align_y(only_plate)
        elif bboxes is not None:
            if len(bboxes) != len(self.items):
                raise ArrangeError("Object count does not match the exported project.")
            for it, box in zip(self.items, bboxes):
                it.bbox = tuple(box)
        else:
            with zipfile.ZipFile(self.path) as zf:
                lib = MeshLibrary(zf)
                for it, (object_id, path, _t) in zip(self.items, self.build):
                    it.bbox = lib.bbox(path or "/" + MODEL_PATH, object_id, it.transform,
                                       parts=self._parts(object_id))
                    if it.bbox is None:
                        raise ArrangeError("Object %s has no geometry." % object_id)
        self._to_plate_coordinates()

    def _parts(self, object_id):
        return (self.info.get(object_id) or {}).get("parts")

    def _object_index(self, order):
        """{object_id: 1-based place in the object list} for an object order."""
        return {oid: i + 1 for i, oid in enumerate(order)}

    def _original_order(self):
        order = []
        for it in sorted(self.items, key=lambda i: i.index):
            if it.object_id not in order:
                order.append(it.object_id)
        return order

    def _geometry(self):
        """Per object: model-part triangles of its first instance and slicing parameters."""
        if getattr(self, "_geom", None) is not None:
            return self._geom
        first_item = {}
        for it in sorted(self.items, key=lambda i: i.index):
            first_item.setdefault(it.object_id, it)
        old_index = self._object_index(self._original_order())
        existing = parse_profiles(self.indexed_files.get(LAYER_HEIGHTS_PATH))
        ranges_xml = self.indexed_files.get("Metadata/layer_config_ranges.xml")
        geom = {}
        with zipfile.ZipFile(self.path) as zf:
            lib = MeshLibrary(zf)
            for object_id, it in first_item.items():
                if not any(i.moved for i in self.items if i.object_id == object_id):
                    continue
                path = self.build[it.index][1] or "/" + MODEL_PATH
                tris = lib.triangles(path, object_id, it.transform, parts=self._parts(object_id))
                if not tris:
                    continue
                zs = [v[2] for t in tris for v in t]
                obj_config = (self.info.get(object_id) or {}).get("config") or {}
                params = SlicingParams(self.config, obj_config, max(zs) - min(zs))
                ranges = parse_height_ranges(ranges_xml, old_index[object_id])
                before = existing.get(old_index[object_id])
                has_profile = valid_profile(before, params)
                if not has_profile:
                    before = default_profile(params, ranges)
                geom[object_id] = {"tris": tris, "params": params, "ranges": ranges,
                                   "before": before, "has_profile": has_profile}
        self._geom = geom
        return geom

    def layer_heights(self, settings):
        """Adaptive and smoothed layer heights for the moved objects.

        Returns {object_id: profile}. As in the Variable Layer Height tool, objects on one
        plate share one profile unless `share_layers` is off ("Each separately"); objects
        whose first layer differs keep their own. Smooth without Adaptive smooths the
        current profile.
        """
        adaptive = bool(settings.get("adaptive"))
        smooth = bool(settings.get("smooth"))
        if not (adaptive or smooth):
            return {}
        quality = min(max(float(settings.get("adaptive_quality", 0.5)), 0.0), 1.0)
        radius = min(max(int(settings.get("smooth_radius", 5)), 1), 10)
        keep_min = bool(settings.get("smooth_keep_min"))
        share = settings.get("share_layers", True) not in (False, "off", "0")
        geom = self._geometry()
        base = {}
        for object_id, g in geom.items():
            if adaptive:
                base[object_id] = adaptive_profile(g["tris"], g["params"], quality, g["ranges"])
            elif share or g["has_profile"]:
                # Shared: the others follow the smoothed profile, as in OrcaSlicer.
                base[object_id] = g["before"]
        groups = {}
        for it in self.items:
            if it.object_id not in base:
                continue
            p = geom[it.object_id]["params"]
            key = (it.plate, round(p.first_object_layer_height, 6), p.first_layer_fixed)
            if share:
                groups.setdefault(key, [])
                if it.object_id not in groups[key]:
                    groups[key].append(it.object_id)
            else:
                groups[(it.object_id,)] = [it.object_id]
        out = {}
        for members in groups.values():
            members = [m for m in members if m not in out]
            if not members:
                continue
            if len(members) == 1:
                prof = base[members[0]]
                g = geom[members[0]]
                out[members[0]] = (smooth_profile(prof, g["params"], radius, keep_min)
                                   if smooth else prof)
                continue
            tallest = max(members, key=lambda m: geom[m]["params"].object_height)
            merged = merge_finest([base[m] for m in members])
            if smooth:
                merged = smooth_profile(merged, geom[tallest]["params"], radius, keep_min)
            for m in members:
                out[m] = fit_to_height(merged, geom[m]["params"].object_height)
        return out

    def estimate(self):
        """Estimated print time of the arranged plates, before and after."""
        geom = self._geometry()
        model = TimeModel(self.config)
        samples = {}

        def object_entry(it, cx, cy, profile_key):
            g = geom[it.object_id]
            profile = g[profile_key]
            layers = object_layers(g["params"], profile)
            key = (it.object_id, profile_key)
            if key not in samples:
                if "section" not in g:
                    g["section"] = CrossSection(g["tris"])
                samples[key] = g["section"].sample([0.5 * (a + b) for a, b in layers])
            return (cx, cy, layers, samples[key])

        result = {}
        for which in ("before", "after"):
            plates = {}
            for it in self.items:
                if not it.moved or it.object_id not in geom:
                    continue
                if which == "before":
                    plate = it.old_plate
                    cx = (it.bbox[0] + it.bbox[3]) / 2.0
                    cy = (it.bbox[1] + it.bbox[4]) / 2.0
                    key = "before"
                else:
                    plate = it.plate
                    cx, cy = it.x + it.width / 2.0, it.y + it.depth / 2.0
                    geom[it.object_id]["after"] = self.profiles.get(
                        it.object_id, geom[it.object_id]["before"])
                    key = "after"
                plates.setdefault(plate, []).append(object_entry(it, cx, cy, key))
            seconds = layers = 0
            for objs in plates.values():
                t, n = model.plate_time(objs)
                seconds += t
                layers += n
            result[which] = seconds
            result[which + "_layers"] = layers
        return result

    def _indexed_files(self, new_order):
        """The files keyed by object list place, renumbered for `new_order`."""
        old_index = self._object_index(self._original_order())
        new_index = self._object_index(new_order)
        mapping = {old_index[oid]: new_index[oid] for oid in old_index}
        files = {}
        for name, text in self.indexed_files.items():
            if name in LINE_INDEXED_PATHS:
                files[name] = remap_line_indexed(text, mapping)
            else:
                files[name] = remap_xml_indexed(text, mapping)
        if self.profiles:
            merged = parse_profiles(files.get(LAYER_HEIGHTS_PATH))
            for object_id, profile in self.profiles.items():
                merged[new_index[object_id]] = profile
            files[LAYER_HEIGHTS_PATH] = format_profiles(merged)
        return files

    def _align_y(self, only_plate=None):
        cols = plate_columns(self.plate_count)
        with zipfile.ZipFile(self.path) as zf:
            lib = MeshLibrary(zf)
            for it, (object_id, path, _t) in zip(self.items, self.build):
                fp = lib.footprint(path or "/" + MODEL_PATH, object_id, it.transform,
                                   parts=self._parts(object_id))
                if fp is None:
                    raise ArrangeError("Object %s has no geometry." % object_id)
                hull, z0, z1 = fp
                xs, ys = zip(*hull)
                pivot = ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)
                if it.plate < 0:
                    p = plate_of_point(pivot[0], pivot[1], cols, self.bed)
                    it.plate = p if p is not None and p < self.plate_count else 0
                skip = only_plate is not None and it.plate != only_plate
                angle = 0.0 if skip else y_align_angle(hull)
                if angle:
                    it.transform = rotate_z(it.transform, angle, pivot)
                    it.rotated = angle
                    hull = rotate_points(hull, angle, pivot)
                    xs, ys = zip(*hull)
                it.bbox = (min(xs), min(ys), z0, max(xs), max(ys), z1)

    def _to_plate_coordinates(self):
        cols = plate_columns(self.plate_count)
        for it in self.items:
            if it.plate < 0:
                cx = (it.bbox[0] + it.bbox[3]) / 2.0
                cy = (it.bbox[1] + it.bbox[4]) / 2.0
                p = plate_of_point(cx, cy, cols, self.bed)
                it.plate = p if p is not None and p < self.plate_count else 0
            # Store the box relative to its plate.
            ox, oy = plate_origin(it.plate, cols, self.bed)
            it.old_origin = (ox, oy)
            b = it.bbox
            it.bbox = (b[0] - ox, b[1] - oy, b[2], b[3] - ox, b[4] - oy, b[5])

    def arrange(self, settings):
        before = {it.index: it.plate for it in self.items}
        for it in self.items:
            it.old_plate = it.plate
        self.new_plate_count = arrange(self.items, self.bed, self.plate_count, settings,
                                       self.config, self.plate_sequences)
        moved = [it for it in self.items if it.moved]
        self.changed_plates = set(it.plate for it in moved) | set(before[it.index] for it in moved)
        self.profiles = self.layer_heights(settings)
        if self.new_plate_count != self.plate_count:
            # The plate grid changes, so OrcaSlicer moves every plate: redo all thumbnails.
            self.changed_plates = set(range(self.new_plate_count))

    def write(self, out_path):
        cols = plate_columns(self.new_plate_count)
        # New transforms: shift each item so its footprint lands at (x, y) on its plate.
        new_transform = {}
        for it in self.items:
            # Both boxes are relative to a plate; add each plate's world origin.
            ox, oy = plate_origin(it.plate, cols, self.bed)
            m = [row[:] for row in it.transform]
            m[3][0] += (it.x + ox) - (it.bbox[0] + it.old_origin[0])
            m[3][1] += (it.y + oy) - (it.bbox[1] + it.old_origin[1])
            new_transform[it.index] = format_transform(m)
        model_xml = self._rewrite_build(new_transform)
        settings_xml = self._rewrite_plates()
        indexed = self._indexed_files(object_order(self.items))
        tmp = out_path + ".tmp"
        with zipfile.ZipFile(self.path) as src, \
                zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                if info.filename == MODEL_PATH:
                    dst.writestr(info, model_xml.encode("utf-8"))
                elif info.filename == MODEL_SETTINGS_PATH and settings_xml is not None:
                    dst.writestr(info, settings_xml.encode("utf-8"))
                elif info.filename in indexed:
                    dst.writestr(info, indexed.pop(info.filename).encode("utf-8"))
                else:
                    dst.writestr(info, src.read(info.filename))
            for name, text in indexed.items():   # a layer height file the project did not have
                dst.writestr(name, text.encode("utf-8"))
        os.replace(tmp, out_path)

    def _rewrite_build(self, new_transform):
        m = _BUILD_RE.search(self.model_xml)
        body = m.group(2)
        raw_items = _ITEM_RE.findall(body)
        indent_m = re.match(r"\s*", body)
        indent = indent_m.group(0) if indent_m else "\n  "
        by_object = {}
        for index, raw in enumerate(raw_items):
            new_raw = re.sub(_ATTR_RE % "transform", 'transform="%s"' % new_transform[index], raw)
            if new_raw == raw and _attr(raw, "transform") is None:
                new_raw = re.sub(r"<item\b", '<item transform="%s"' % new_transform[index], raw, 1)
            by_object.setdefault(self.items[index].object_id, []).append(new_raw)
        ordered = []
        for object_id in object_order(self.items):
            ordered.extend(by_object[object_id])   # instances keep their relative order
        tail = body[len(body.rstrip()):]
        new_body = "".join(indent + raw for raw in ordered) + (tail or "\n ")
        return self.model_xml[:m.start(2)] + new_body + self.model_xml[m.end(2):]

    def _rewrite_plates(self):
        if not self.settings_xml:
            return None
        blocks = list(re.finditer(r"[ \t]*<plate>.*?</plate>\s*", self.settings_xml, re.S))
        templates = [b.group(0) for b in blocks]
        order = object_order(self.items)
        rank = {oid: i for i, oid in enumerate(order)}
        out = []
        for plate in range(self.new_plate_count):
            if plate < len(templates):
                text = templates[plate]
                text = re.sub(r"[ \t]*<model_instance>.*?</model_instance>\s*", "", text,
                              flags=re.S)
                if plate in self.changed_plates:
                    for key in STALE_PLATE_KEYS:
                        text = re.sub(r'[ \t]*<metadata key="%s" value="[^"]*"\s*/>\s*' % key,
                                      "", text)
                text = re.sub(r'(<metadata key="plater_id" value=")[^"]*(")',
                              r"\g<1>%d\g<2>" % (plate + 1), text)
            else:
                text = ('  <plate>\n    <metadata key="plater_id" value="%d"/>\n'
                        '    <metadata key="plater_name" value=""/>\n'
                        '    <metadata key="locked" value="false"/>\n  </plate>\n' % (plate + 1))
            members = [it for it in self.items if it.plate == plate]
            members.sort(key=lambda i: (rank[i.object_id], i.instance_index))
            inst_text = "".join(_instance_xml(it) for it in members)
            close = text.rindex("</plate>")
            text = text[:close].rstrip(" \t") + inst_text + "  " + text[close:]
            out.append(text)
        new_block = "".join(out)
        if blocks:
            start, end = blocks[0].start(), blocks[-1].end()
            return self.settings_xml[:start] + new_block + self.settings_xml[end:]
        close = self.settings_xml.rfind("</config>")
        if close < 0:
            return None
        return self.settings_xml[:close] + new_block + self.settings_xml[close:]


def _instance_xml(it):
    text = ('    <model_instance>\n'
            '      <metadata key="object_id" value="%s"/>\n'
            '      <metadata key="instance_id" value="%d"/>\n' % (it.object_id, it.instance_index))
    if it.identify_id:
        text += '      <metadata key="identify_id" value="%s"/>\n' % it.identify_id
    return text + '    </model_instance>\n'


def arrange_file(in_path, out_path, settings, bboxes=None):
    """Arrange `in_path` and write `out_path`. Returns a short summary."""
    project = Project(in_path)
    align = align_enabled(settings, project.config)
    only = int(settings.get("plate_index", 0)) if settings.get("plates") == "current" else None
    project.load_bboxes(bboxes, align_y=align, only_plate=only)
    project.arrange(settings)
    project.write(out_path)
    bed = project.bed
    return {
        "objects": len(project.items),
        "plates": project.new_plate_count,
        "plates_before": project.plate_count,
        "aligned": align,
        "layer_heights": len(project.profiles),
        "estimate": project.estimate(),
        "rotated": sum(1 for it in project.items if it.rotated),
        "bed": [bed.x0, bed.y0, bed.x1, bed.y1],
        "changed": sorted(p + 1 for p in project.changed_plates),
        "layout": [
            {"plate": p + 1, "objects": [
                {"name": project.names.get(it.object_id) or "Object %s" % it.object_id,
                 "x": round(it.x, 2), "y": round(it.y, 2), "w": round(it.width, 2),
                 "d": round(it.depth, 2), "h": round(it.height, 2)}
                for it in project.items if it.plate == p]}
            for p in sorted(set(it.plate for it in project.items if it.moved))],
    }


# ---------------------------------------------------------------------------
# OrcaSlicer plugin
# ---------------------------------------------------------------------------

PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
body { font-family: var(--orca-font, sans-serif); margin: 0; }
.wrap { display: flex; gap: 20px; padding: 16px; align-items: flex-start; }
.side { width: 320px; flex: none; }
.main { flex: 1; min-width: 0; }
h2 { margin: 0 0 12px; font-size: 16px; }
h3 { margin: 0 0 8px; font-size: 14px; }
fieldset { border: 1px solid var(--orca-border, #ccc); margin: 0 0 12px; padding: 8px 12px; }
legend { font-weight: 600; }
label { display: block; margin: 4px 0; }
.note { color: var(--orca-muted, #666); font-size: 12px; margin: 2px 0 4px 22px; }
.note.flat { margin-left: 0; }
input[type=number] { width: 70px; }
input[type=text] { width: 100%; box-sizing: border-box; }
select { min-width: 90px; }
.slider { display: flex; align-items: center; gap: 8px; margin: 2px 0 6px 22px;
  font-size: 12px; color: var(--orca-muted, #666); }
.slider span { width: 92px; flex: none; }
.slider input { flex: 1; min-width: 0; accent-color: var(--orca-accent, #009688); }
.slider output { width: 32px; text-align: right; color: var(--orca-fg, #000); }
.indent { margin-left: 22px; font-size: 13px; }
.off { opacity: 0.45; }
#arrange { width: 100%; padding: 6px; font-weight: 600; }
#status { margin: 0 0 12px; min-height: 1.2em; white-space: pre-wrap; }
#status.error { color: #d9534f; }
.plates { display: flex; flex-wrap: wrap; gap: 16px; }
.plate { width: 340px; }
.plate svg { width: 340px; height: 340px; display: block; }
.plate .bed { fill: none; stroke: var(--orca-border, #999); }
.plate rect.obj { stroke: rgba(0, 0, 0, 0.6); stroke-width: 0.6; }
.plate text { font-size: 7px; fill: #111; text-anchor: middle; dominant-baseline: middle; }
.legend { display: flex; align-items: center; gap: 8px; font-size: 12px; margin: 0 0 12px; }
.legend[hidden] { display: none; }
.legend .bar { width: 140px; height: 10px;
  background: linear-gradient(90deg, hsl(210,70%,60%), hsl(110,70%,52%), hsl(20,70%,45%)); }
</style></head><body>
<div class="wrap">
<div class="side">
<h2>Arrange by Height</h2>
<fieldset><legend>Spacing</legend>
  <label><input type="radio" name="spacing" value="auto"> Auto</label>
  <div class="note">By layer: brim width on each side (and a per-object skirt), at least
    2 mm. By object: extruder clearance radius.</div>
  <label><input type="radio" name="spacing" value="manual"> Manual
    <input type="number" id="gap" min="0" step="0.5"> mm</label>
</fieldset>
<fieldset><legend>Edge Margin</legend>
  <label><input type="radio" name="edge" value="auto"> Auto</label>
  <div class="note">Skirt, brim and 1 mm (5 mm by object), as OrcaSlicer's arrange.</div>
  <label><input type="radio" name="edge" value="manual"> Manual
    <input type="number" id="edge" min="0" step="0.5"> mm</label>
  <div class="note">From the bed edge to the objects.</div>
</fieldset>
<fieldset><legend>Layout</legend>
  <label><input type="radio" name="layout" value="compact"> Compact</label>
  <div class="note">Tallest object in the center, shorter objects packed around it.</div>
  <label><input type="radio" name="layout" value="rows"> Rows</label>
  <div class="note">Rows along X, tallest row in the middle. Best for bedslingers:
    the bed moves less.</div>
  <label><input type="radio" name="layout" value="grid"> Grid</label>
  <div class="note">Equal cells, tallest objects in the center cells. Best for similar parts.</div>
</fieldset>
<fieldset><legend>Plates</legend>
  <label><input type="radio" name="plates" value="current"> Current plate only</label>
  <div class="note">Other plates are not changed. <span id="plateinfo"></span></div>
  <label><input type="radio" name="plates" value="keep"> All plates, objects stay on their
    plate</label>
  <label><input type="radio" name="plates" value="regroup"> All plates, regroup by
    height</label>
  <div class="note">Tallest objects on plate 1. Objects of similar height share a plate.</div>
</fieldset>
<fieldset><legend>Align to Y Axis</legend>
  <select id="align"><option value="auto">Auto</option><option value="on">On</option>
    <option value="off">Off</option></select>
  <div class="note flat">Turns long objects so their long side runs along Y.
    Auto: on for bedslinger printers (printer structure i3).
    <span id="printer"></span></div>
</fieldset>
<fieldset><legend>Layer Height</legend>
  <label><input type="checkbox" id="adaptive"> Adaptive layer height</label>
  <div class="slider"><span>Quality / Speed</span>
    <input type="range" id="quality" min="0" max="1" step="0.01">
    <output id="quality_out"></output></div>
  <label><input type="checkbox" id="smooth"> Smooth</label>
  <div class="slider"><span>Radius</span>
    <input type="range" id="radius" min="1" max="10" step="1">
    <output id="radius_out"></output></div>
  <label class="indent"><input type="checkbox" id="keep_min"> Keep min</label>
  <label><input type="checkbox" id="share"> Same layers for all objects on a plate</label>
  <div class="note">One profile per plate, with the finest layer any object needs, as in
    the Variable Layer Height tool with "Each separately" off. Off: each object gets its
    own layers, which adds plate layers and often print time.</div>
  <div class="note flat">Same as Adaptive and Smooth in the Variable Layer Height tool, for
    every object that is arranged.</div>
</fieldset>
<fieldset><legend>Save To</legend>
  <label><input type="radio" name="output" value="replace"> Project file</label>
  <div class="note">Replaces the saved project. A backup is kept as
    &lt;project&gt;-backup.3mf.</div>
  <label><input type="radio" name="output" value="new"> New file</label>
  <input type="text" id="out">
  <div class="note flat">The open project is not updated: open the file again
    after arranging.</div>
</fieldset>
<button id="arrange">Arrange</button>
</div>
<div class="main">
  <h3>Result</h3>
  <div id="status">Set the options and click Arrange.</div>
  <div class="legend" id="legend" hidden><span>Short</span><span class="bar"></span>
    <span>Tall</span></div>
  <div class="plates" id="plates"></div>
</div>
</div>
<script>
let outEdited = false;
function pick(name, value) {
  document.querySelectorAll('input[name=' + name + ']').forEach(function (el) {
    el.checked = el.value === value;
  });
}
function value(name) {
  const el = document.querySelector('input[name=' + name + ']:checked');
  return el ? el.value : null;
}
function setStatus(text, error) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.className = error ? 'error' : '';
}
function applyState(st) {
  const s = st.settings || {};
  if (s.spacing) {
    pick('spacing', s.spacing === 'auto' ? 'auto' : 'manual');
    document.getElementById('gap').value = s.spacing === 'auto' ? s.manual_gap : s.spacing;
  }
  if (s.layout) pick('layout', s.layout);
  if (s.edge_margin !== undefined) {
    pick('edge', s.edge_margin === 'auto' ? 'auto' : 'manual');
    document.getElementById('edge').value = s.edge_margin === 'auto' ? s.manual_edge
                                                                      : s.edge_margin;
  }
  if (s.plates) pick('plates', s.plates);
  if (s.align_y) document.getElementById('align').value = s.align_y;
  if (s.output) pick('output', s.output);
  if (s.adaptive !== undefined) document.getElementById('adaptive').checked = !!s.adaptive;
  if (s.adaptive_quality !== undefined) {
    document.getElementById('quality').value = s.adaptive_quality;
  }
  if (s.smooth !== undefined) document.getElementById('smooth').checked = !!s.smooth;
  if (s.smooth_radius !== undefined) document.getElementById('radius').value = s.smooth_radius;
  if (s.share_layers !== undefined) document.getElementById('share').checked = !!s.share_layers;
  if (s.smooth_keep_min !== undefined) {
    document.getElementById('keep_min').checked = !!s.smooth_keep_min;
  }
  updateLayerControls();
  if (st.plate !== undefined) {
    document.getElementById('plateinfo').textContent = 'Selected: plate ' + st.plate + '.';
  }
  if (st.out && !outEdited) document.getElementById('out').value = st.out;
  if (st.printer !== undefined) {
    document.getElementById('printer').textContent = st.printer
      ? 'Current printer: ' + (st.bedslinger ? 'bedslinger (Auto is on).'
                                             : 'not a bedslinger (Auto is off).')
      : '';
  }
}
function updateLayerControls() {
  const q = document.getElementById('quality'), r = document.getElementById('radius');
  document.getElementById('quality_out').textContent = Number(q.value).toFixed(2);
  document.getElementById('radius_out').textContent = r.value;
  const a = document.getElementById('adaptive').checked;
  const sm = document.getElementById('smooth').checked;
  q.disabled = !a;
  q.parentNode.className = 'slider' + (a ? '' : ' off');
  r.disabled = !sm;
  r.parentNode.className = 'slider' + (sm ? '' : ' off');
  document.getElementById('keep_min').disabled = !sm;
  document.getElementById('keep_min').parentNode.className = 'indent' + (sm ? '' : ' off');
}
['adaptive', 'smooth', 'quality', 'radius'].forEach(function (id) {
  document.getElementById(id).addEventListener('input', updateLayerControls);
});
function color(f) {
  const hue = f < 0.5 ? 210 - 200 * f : 110 - 180 * (f - 0.5);
  return 'hsl(' + Math.round(hue) + ',70%,' + Math.round(60 - 15 * f) + '%)';
}
function esc(t) {
  return String(t).replace(/[&<>"]/g, function (c) {
    return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c];
  });
}
function drawPlates(sum) {
  const box = document.getElementById('plates');
  box.innerHTML = '';
  if (!sum || !sum.layout) return;
  const b = sum.bed, w = b[2] - b[0], d = b[3] - b[1];
  let hmax = 0;
  sum.layout.forEach(function (pl) {
    pl.objects.forEach(function (o) { hmax = Math.max(hmax, o.h); });
  });
  sum.layout.forEach(function (entry) {
    const pl = entry.objects;
    let svg = '<svg viewBox="' + (b[0] - 2) + ' ' + (-b[3] - 2) + ' ' + (w + 4) + ' ' +
      (d + 4) + '">';
    svg += '<rect class="bed" x="' + b[0] + '" y="' + (-b[3]) + '" width="' + w +
      '" height="' + d + '"/>';
    pl.forEach(function (o) {
      const f = hmax > 0 ? o.h / hmax : 0;
      svg += '<g><title>' + esc(o.name) + ': ' + o.w + ' x ' + o.d + ' x ' + o.h + ' mm</title>' +
        '<rect class="obj" x="' + o.x + '" y="' + (-(o.y + o.d)) + '" width="' + o.w +
        '" height="' + o.d + '" fill="' + color(f) + '"/>';
      if (o.w >= 8 && o.d >= 6) {
        svg += '<text x="' + (o.x + o.w / 2) + '" y="' + (-(o.y + o.d / 2)) + '">' +
          Math.round(o.h) + '</text>';
      }
      svg += '</g>';
    });
    svg += '</svg>';
    const div = document.createElement('div');
    div.className = 'plate';
    div.innerHTML = '<h3>Plate ' + entry.plate + ' (' + pl.length + ')</h3>' + svg;
    box.appendChild(div);
  });
  document.getElementById('legend').hidden = false;
}
function settings() {
  const manual = value('spacing') === 'manual';
  return {
    spacing: manual ? document.getElementById('gap').value : 'auto',
    manual_gap: document.getElementById('gap').value,
    edge_margin: value('edge') === 'manual' ? document.getElementById('edge').value : 'auto',
    manual_edge: document.getElementById('edge').value,
    layout: value('layout'),
    plates: value('plates'),
    align_y: document.getElementById('align').value,
    output: value('output'),
    adaptive: document.getElementById('adaptive').checked,
    adaptive_quality: Number(document.getElementById('quality').value),
    smooth: document.getElementById('smooth').checked,
    smooth_radius: Number(document.getElementById('radius').value),
    smooth_keep_min: document.getElementById('keep_min').checked,
    share_layers: document.getElementById('share').checked
  };
}
document.getElementById('out').addEventListener('input', function () { outEdited = true; });
document.getElementById('arrange').onclick = function () {
  setStatus('Arranging...');
  document.getElementById('arrange').disabled = true;
  // Let the page repaint before the plugin starts working.
  setTimeout(function () {
    orca.postMessage({type: 'arrange', settings: settings(),
                      out: document.getElementById('out').value});
  }, 50);
};
orca.onMessage(function (msg) {
  if (typeof msg === 'string') msg = JSON.parse(msg);
  if (msg.type === 'state') {
    applyState(msg);
  } else if (msg.type === 'result') {
    document.getElementById('arrange').disabled = false;
    setStatus(msg.text, !msg.ok);
    if (msg.ok) drawPlates(msg.summary);
  }
});
pick('spacing', 'auto');
pick('edge', 'auto');
document.getElementById('edge').value = 5;
pick('layout', 'compact');
pick('plates', 'current');
pick('output', 'new');
document.getElementById('quality').value = 0.5;
document.getElementById('radius').value = 5;
document.getElementById('share').checked = true;
updateLayerControls();
orca.postMessage({type: 'ready'});
</script></body></html>
"""

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
 stroke="#009688" stroke-width="2" stroke-linejoin="round">
<rect x="9.5" y="3" width="5" height="18"/><rect x="3" y="11" width="4.5" height="10"/>
<rect x="16.5" y="11" width="4.5" height="10"/></svg>
"""


def _default_output(project_path, storage_dir):
    if project_path:
        base, _ext = os.path.splitext(project_path)
        return base + "-arranged.3mf"
    return os.path.join(storage_dir, "arranged.3mf")


def _live_bboxes():
    """World bounding boxes of every instance, in the order OrcaSlicer writes the build."""
    boxes = []
    model = orca.host.model()
    for obj in model.objects():
        for inst in obj.instances():
            bb = inst.bounding_box()
            boxes.append(tuple(bb.min) + tuple(bb.max))
    return boxes


def _clamp_number(value, lo, hi, default):
    try:
        return min(max(float(value), lo), hi)
    except (TypeError, ValueError):
        return default


def _clean_settings(raw, saved):
    """Settings from the page, checked. Raises ArrangeError for a bad value."""
    raw = raw or {}
    spacing = raw.get("spacing", "auto")
    if spacing != "auto":
        try:
            spacing = str(max(float(spacing), 0.0))
        except (TypeError, ValueError):
            raise ArrangeError("Spacing must be a number in mm.")
    edge = raw.get("edge_margin", "auto")
    if edge != "auto":
        try:
            edge = str(max(float(edge), 0.0))
        except (TypeError, ValueError):
            raise ArrangeError("Edge margin must be a number in mm.")
    return {
        "spacing": spacing,
        "edge_margin": edge,
        "manual_edge": str(raw.get("manual_edge") or saved.get("manual_edge") or "5"),
        "manual_gap": str(raw.get("manual_gap") or saved.get("manual_gap") or "6"),
        "layout": (raw.get("layout") if raw.get("layout") in ("compact", "rows", "grid")
                   else "compact"),
        "plates": (raw.get("plates") if raw.get("plates") in ("current", "keep", "regroup")
                   else "current"),
        "output": raw.get("output") if raw.get("output") in ("new", "replace") else "new",
        "align_y": raw.get("align_y") if raw.get("align_y") in ("auto", "on", "off") else "auto",
        "adaptive": bool(raw.get("adaptive")),
        "adaptive_quality": _clamp_number(raw.get("adaptive_quality"), 0.0, 1.0, 0.5),
        "smooth": bool(raw.get("smooth")),
        "smooth_radius": int(_clamp_number(raw.get("smooth_radius"), 1, 10, 5)),
        "smooth_keep_min": bool(raw.get("smooth_keep_min")),
        "share_layers": bool(raw.get("share_layers", True)),
    }


def estimate_text(est):
    """One line comparing the estimated print time before and after."""
    before, after = est["before"], est["after"]
    if before <= 0:
        return "Estimated print time: not available."
    delta = after - before
    sign = "+" if delta >= 0 else "-"
    return ("Estimated print time of the arranged plates: %s before, %s after (%s%s, %s%d%%). "
            "Plate layers: %d before, %d after. Rough estimate: slice to confirm." % (
                format_duration(before), format_duration(after), sign,
                format_duration(abs(delta)), sign, round(abs(delta) / before * 100),
                est["before_layers"], est["after_layers"]))


def _count(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def run_live(raw_settings, out_path, saved):
    """Arrange the open project. Returns (ok, text, summary, settings)."""
    try:
        settings = _clean_settings(raw_settings, saved)
        plater = orca.host.plater()
        model = orca.host.model()
        if model.object_count() == 0:
            raise ArrangeError("The project has no objects.")
        backup = None
        if settings["output"] == "replace":
            out_path = plater.project_path()
            if not out_path:
                raise ArrangeError("Save the project first, or choose New file.")
            backup = os.path.splitext(out_path)[0] + "-backup.3mf"
        out_path = (out_path or "").strip()
        if not out_path.lower().endswith(".3mf"):
            raise ArrangeError("The output file must end in .3mf.")
        run = dict(settings, plate_index=model.current_plate_index())
        storage = orca.host.plugin.storage()
        export_path = os.path.join(storage, "export.3mf")
        try:
            plater.export_3mf_copy(export_path)
            if backup and os.path.exists(out_path):
                shutil.copy2(out_path, backup)
            # Sizes from the live model are exact and fast; the exported build lists the
            # instances in the same order. Align to Y axis reads the meshes instead.
            summary = arrange_file(export_path, out_path, run, _live_bboxes())
        finally:
            try:
                os.remove(export_path)
            except OSError:
                pass
    except Exception as err:  # Shown to the user; the plugin must not crash OrcaSlicer.
        return False, str(err) or err.__class__.__name__, None, None
    text = "Arranged %s on %s." % (_count(summary["objects"], "object"),
                                   _count(summary["plates"], "plate"))
    if summary["aligned"]:
        text += " Turned %s to the Y axis." % _count(summary["rotated"], "object")
    if summary["layer_heights"]:
        text += " Set layer heights for %s." % _count(summary["layer_heights"], "object")
    text += "\n" + estimate_text(summary["estimate"])
    if backup:
        text += ("\nReplaced %s (backup: %s).\nTo see the result, open the project again "
                 "with File > Recent Projects. If OrcaSlicer asks to save changes, click "
                 "Don't Save, or the result is overwritten." % (out_path, os.path.basename(backup)))
    else:
        text += "\nSaved to %s\nOpen it with File > Open Project." % out_path
    return True, text, summary, settings


if orca is not None:

    class ArrangeByHeightPage(orca.pages.PagesPluginCapabilityBase):
        """The Arrange by Height tab in the main window."""

        def get_name(self):
            return "Arrange"

        def get_icon(self):
            try:
                path = os.path.join(orca.host.plugin.storage(), "arrange_by_height.svg")
                if not os.path.exists(path):
                    with open(path, "w") as fh:
                        fh.write(ICON_SVG)
                return path
            except Exception:
                return ""

        def get_ui(self):
            return PAGE_HTML

        def get_default_config(self):
            return dict(DEFAULT_SETTINGS, manual_gap="6", manual_edge="5")

        def _saved(self):
            try:
                config = json.loads(self.get_config() or "{}")
            except ValueError:
                config = {}
            merged = dict(DEFAULT_SETTINGS, manual_gap="6", manual_edge="5")
            merged.update({k: v for k, v in config.items() if k in merged})
            return merged

        def _push_state(self):
            state = {"type": "state", "settings": self._saved()}
            try:
                state["out"] = _default_output(orca.host.plater().project_path(),
                                               orca.host.plugin.storage())
            except Exception:
                pass
            try:
                state["plate"] = orca.host.model().current_plate_index() + 1
            except Exception:
                pass
            try:
                structure = orca.host.preset_bundle().full_config_value("printer_structure")
                state["printer"] = bool(structure)
                state["bedslinger"] = _str(structure) == "i3"
            except Exception:
                pass
            self.post_message(state)

        def on_message(self, data):
            if isinstance(data, str):
                data = json.loads(data)
            kind = (data or {}).get("type")
            if kind == "ready":
                self._push_state()
            elif kind == "arrange":
                ok, text, summary, settings = run_live(
                    data.get("settings"), data.get("out"), self._saved())
                if settings:
                    self.save_config(json.dumps(settings))
                self.post_message({"type": "result", "ok": ok, "text": text,
                                   "summary": summary})

        def on_lifecycle_event(self, event, ctx):
            if event in (orca.LifecycleEvent.ProjectOpened, orca.LifecycleEvent.NewProject,
                         orca.LifecycleEvent.ProjectAfterSave,
                         orca.LifecycleEvent.PresetSelected,
                         orca.LifecycleEvent.PlateSelected):
                try:
                    self._push_state()
                except Exception:
                    pass

    @orca.plugin
    class ArrangeByHeightPlugin(orca.base):
        def register_capabilities(self):
            orca.register_capability(ArrangeByHeightPage)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description="Arrange a 3MF project by object height.")
    parser.add_argument("input", help="OrcaSlicer 3MF project")
    parser.add_argument("-o", "--output", help="output file (default: <input>-arranged.3mf)")
    parser.add_argument("--spacing", default="auto", help='gap between objects in mm, or "auto"')
    parser.add_argument("--layout", choices=("compact", "rows", "grid"), default="compact")
    parser.add_argument("--edge-margin", default="auto",
                        help='room at the bed edge in mm, or "auto"')
    parser.add_argument("--plates", choices=("all", "regroup", "current"), default="all",
                        help="all: every plate, objects stay on their plate; "
                             "current: only the plate given with --plate")
    parser.add_argument("--plate", type=int, default=1, help="plate number for --plates current")
    parser.add_argument("--adaptive", type=float, metavar="QUALITY",
                        help="adaptive layer height, 0 (quality) to 1 (speed)")
    parser.add_argument("--smooth", type=int, metavar="RADIUS",
                        help="smooth the layer height profile, radius 1 to 10")
    parser.add_argument("--keep-min", action="store_true", help="smooth: keep min")
    parser.add_argument("--each-separately", action="store_true",
                        help="give each object its own layer heights")
    parser.add_argument("--align-y", choices=("auto", "on", "off"), default="auto",
                        help="turn long objects along Y (auto: on for i3 printers)")
    args = parser.parse_args(argv)
    if args.edge_margin != "auto":
        try:
            float(args.edge_margin)
        except ValueError:
            parser.error('--edge-margin must be a number or "auto"')
    if args.spacing != "auto":
        try:
            float(args.spacing)
        except ValueError:
            parser.error('--spacing must be a number or "auto"')
    out = args.output or os.path.splitext(args.input)[0] + "-arranged.3mf"
    settings = {"spacing": args.spacing, "layout": args.layout, "edge_margin": args.edge_margin,
                "plates": "keep" if args.plates == "all" else args.plates,
                "plate_index": args.plate - 1, "align_y": args.align_y,
                "adaptive": args.adaptive is not None,
                "adaptive_quality": args.adaptive if args.adaptive is not None else 0.5,
                "smooth": args.smooth is not None, "smooth_radius": args.smooth or 5,
                "smooth_keep_min": args.keep_min, "share_layers": not args.each_separately}
    try:
        summary = arrange_file(args.input, out, settings)
    except ArrangeError as err:
        print("Error: %s" % err, file=sys.stderr)
        return 1
    print("Arranged %s on %s: %s" % (_count(summary["objects"], "object"),
                                     _count(summary["plates"], "plate"), out))
    print(estimate_text(summary["estimate"]))
    return 0


if __name__ == "__main__" and orca is None:
    sys.exit(main())
