# /// script
# requires-python = ">=3.10"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Arrange by Height"
# description = "Arranges each plate with the tallest objects in the center and writes a new 3MF."
# author = "bacoj913"
# version = "1.0.0"
# ///
"""Arrange by Height: an OrcaSlicer plugin and command-line tool.

The OrcaSlicer plugin API is read-only, so the plugin cannot move objects in the open
project. It exports a copy of the project, arranges that copy and writes a new 3MF file.
Open the new file with File > Open Project.

Layout rules:
- Each plate is laid out from the center out. The tallest object goes in the center and
  shorter objects go around it, so upper layers only visit the center of the plate.
- The object list is set per plate from shortest to tallest. With print sequence
  "By object", OrcaSlicer prints in object list order, so tall objects print last.
- Spacing is a fixed value in mm, or Auto: OrcaSlicer's own arrange spacing for the plate's
  print sequence.
- Align to Y axis turns each long object so its long side runs along Y, as OrcaSlicer's
  arrange does for bedslinger printers. Auto turns it on when the printer structure is i3.

Command line (no OrcaSlicer needed):
    python arrange_by_height.py project.3mf [-o out.3mf] [--spacing auto|MM]
        [--layout compact|grid] [--plates keep|regroup] [--align-y auto|on|off]
"""

import argparse
import json
import math
import os
import re
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

# Values from OrcaSlicer's source (PartPlate.cpp, Print.hpp, PartPlate.hpp).
PLATE_GAP = 1.0 / 5.0
MAX_OUTER_NOZZLE_DIAMETER = 4.0
MAX_PLATE_COUNT = 36
DEFAULT_SETTINGS = {
    "spacing": "auto",       # "auto" or a number in mm
    "layout": "compact",     # "compact" or "grid"
    "plates": "keep",        # "keep" or "regroup"
    "align_y": "auto",       # "auto", "on" or "off"
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
    brim = 0.0
    if _str(config.get("brim_type"), "auto_brim") != "no_brim":
        brim = _num(config.get("brim_width")) + _num(config.get("brim_object_gap"))
    return max(2.0 * brim, MIN_AUTO_GAP)


def align_enabled(settings, config):
    """Whether to align objects to Y: "on", "off", or "auto" for bedslinger printers."""
    value = settings.get("align_y", "auto")
    if value in (True, "on"):
        return True
    if value in (False, "off"):
        return False
    return _str(config.get("printer_structure")) == "i3"


def edge_margin(config):
    """Room left at the bed edge for a brim."""
    if _str(config.get("brim_type"), "auto_brim") == "no_brim":
        return 0.0
    return _num(config.get("brim_width")) + _num(config.get("brim_object_gap"))


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
    layout = layout_grid if settings.get("layout") == "grid" else layout_compact
    margin = edge_margin(config)
    plate_sequences = plate_sequences or {}
    global_sequence = _str(config.get("print_sequence"), "by layer")

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
                if layout(trial, bed, gap_for(plate, trial), margin):
                    hi = mid - 1
                else:
                    lo = mid
            if lo == 0:
                raise ArrangeError("Object %s does not fit on a plate." % queue[0].object_id)
            chosen = queue[:lo]
            layout(chosen, bed, gap_for(plate, chosen), margin)
            for it in chosen:
                it.plate = plate
            plates.append(chosen)
            queue = queue[lo:]

    plates = []
    if settings.get("plates") == "regroup":
        fill_plates(items, plates)
    else:
        groups = {}
        for it in items:
            groups.setdefault(it.plate, []).append(it)
        plates = [groups.get(p, []) for p in range(max([plate_count] + [p + 1 for p in groups]))]
        overflow = []
        for plate, plate_items in enumerate(plates):
            rejected = layout(plate_items, bed, gap_for(plate, plate_items), margin)
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
    (back to front, alternating direction) to keep travel short.
    """
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
                        current = {"id": el.get("id"), "vertices": None, "components": []}
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

    def points(self, path, object_id, transform, depth=0):
        """Every vertex of the object, in world coordinates."""
        if depth > 10:
            raise ArrangeError("The 3MF file has circular components.")
        self._load(path)
        obj = self.objects.get((path, object_id))
        if obj is None:
            raise ArrangeError("Object %s is missing from %s." % (object_id, path))
        out = [apply_transform(transform, v) for v in obj["vertices"] or ()]
        for cid, cpath, ctrans in obj["components"]:
            out.extend(self.points(cpath, cid, compose(ctrans, transform), depth + 1))
        return out

    def bbox(self, path, object_id, transform):
        pts = self.points(path, object_id, transform)
        if not pts:
            return None
        xs, ys, zs = zip(*pts)
        return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))

    def footprint(self, path, object_id, transform):
        """Convex outline (x, y) seen from above, and the z range, in world coordinates."""
        # Instances that differ only by position share one outline.
        linear = tuple(tuple(round(v, 9) for v in row) for row in transform[:3])
        key = (path, object_id, linear)
        cache = self.__dict__.setdefault("_footprints", {})
        if key not in cache:
            local = [row[:] for row in transform[:3]] + [[0.0, 0.0, 0.0]]
            pts = self.points(path, object_id, local)
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


def read_object_names(settings_xml):
    """{object_id: name} from model_settings.config."""
    names = {}
    if not settings_xml:
        return names
    for obj in ET.fromstring(settings_xml).findall("object"):
        for m in obj.findall("metadata"):
            if m.get("key") == "name":
                names[obj.get("id")] = m.get("value") or ""
    return names


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
        self.names = read_object_names(self.settings_xml)
        self.items = []
        counts = {}
        for index, (object_id, _path, transform) in enumerate(self.build):
            inst = counts.get(object_id, 0)
            counts[object_id] = inst + 1
            it = Item(index, object_id, inst, None)
            it.transform = parse_transform(transform)
            it.plate, it.identify_id = mapping.get((object_id, inst), (-1, None))
            self.items.append(it)

    def load_bboxes(self, bboxes=None, align_y=False):
        """Set each item's world bounding box, from `bboxes` or from the meshes.

        With `align_y`, long objects are first turned so their long side runs along Y.
        """
        if align_y:
            self._align_y()
        elif bboxes is not None:
            if len(bboxes) != len(self.items):
                raise ArrangeError("Object count does not match the exported project.")
            for it, box in zip(self.items, bboxes):
                it.bbox = tuple(box)
        else:
            with zipfile.ZipFile(self.path) as zf:
                lib = MeshLibrary(zf)
                for it, (object_id, path, _t) in zip(self.items, self.build):
                    it.bbox = lib.bbox(path or "/" + MODEL_PATH, object_id, it.transform)
                    if it.bbox is None:
                        raise ArrangeError("Object %s has no geometry." % object_id)
        self._to_plate_coordinates()

    def _align_y(self):
        with zipfile.ZipFile(self.path) as zf:
            lib = MeshLibrary(zf)
            for it, (object_id, path, _t) in zip(self.items, self.build):
                fp = lib.footprint(path or "/" + MODEL_PATH, object_id, it.transform)
                if fp is None:
                    raise ArrangeError("Object %s has no geometry." % object_id)
                hull, z0, z1 = fp
                xs, ys = zip(*hull)
                pivot = ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)
                angle = y_align_angle(hull)
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
        self.new_plate_count = arrange(self.items, self.bed, self.plate_count, settings,
                                       self.config, self.plate_sequences)

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
        tmp = out_path + ".tmp"
        with zipfile.ZipFile(self.path) as src, \
                zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                if info.filename == MODEL_PATH:
                    dst.writestr(info, model_xml.encode("utf-8"))
                elif info.filename == MODEL_SETTINGS_PATH and settings_xml is not None:
                    dst.writestr(info, settings_xml.encode("utf-8"))
                else:
                    dst.writestr(info, src.read(info.filename))
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
                for key in STALE_PLATE_KEYS:
                    text = re.sub(r'[ \t]*<metadata key="%s" value="[^"]*"\s*/>\s*' % key, "", text)
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
    project.load_bboxes(bboxes, align_y=align)
    project.arrange(settings)
    project.write(out_path)
    bed = project.bed
    return {
        "objects": len(project.items),
        "plates": project.new_plate_count,
        "plates_before": project.plate_count,
        "aligned": align,
        "rotated": sum(1 for it in project.items if it.rotated),
        "bed": [bed.x0, bed.y0, bed.x1, bed.y1],
        "layout": [
            [{"name": project.names.get(it.object_id) or "Object %s" % it.object_id,
              "x": round(it.x, 2), "y": round(it.y, 2),
              "w": round(it.width, 2), "d": round(it.depth, 2), "h": round(it.height, 2)}
             for it in project.items if it.plate == p]
            for p in range(project.new_plate_count)],
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
.legend .bar { width: 140px; height: 10px;
  background: linear-gradient(90deg, hsl(210,70%,60%), hsl(110,70%,52%), hsl(20,70%,45%)); }
</style></head><body>
<div class="wrap">
<div class="side">
<h2>Arrange by Height</h2>
<fieldset><legend>Spacing</legend>
  <label><input type="radio" name="spacing" value="auto"> Auto</label>
  <div class="note">By layer: brim width on each side, at least 2 mm.
    By object: extruder clearance radius.</div>
  <label><input type="radio" name="spacing" value="manual"> Manual
    <input type="number" id="gap" min="0" step="0.5"> mm</label>
</fieldset>
<fieldset><legend>Layout</legend>
  <label><input type="radio" name="layout" value="compact"> Compact</label>
  <div class="note">Tallest object in the center, shorter objects packed around it.</div>
  <label><input type="radio" name="layout" value="grid"> Grid</label>
  <div class="note">Equal cells, tallest objects in the center cells. Best for similar parts.</div>
</fieldset>
<fieldset><legend>Plates</legend>
  <label><input type="radio" name="plates" value="keep"> Keep objects on their plates</label>
  <label><input type="radio" name="plates" value="regroup"> Regroup plates by height</label>
  <div class="note">Tallest objects on plate 1. Objects of similar height share a plate.</div>
</fieldset>
<fieldset><legend>Align to Y Axis</legend>
  <select id="align"><option value="auto">Auto</option><option value="on">On</option>
    <option value="off">Off</option></select>
  <div class="note flat">Turns long objects so their long side runs along Y.
    Auto: on for bedslinger printers (printer structure i3).
    <span id="printer"></span></div>
</fieldset>
<fieldset><legend>Output File</legend>
  <input type="text" id="out">
  <div class="note flat">Open it with File &gt; Open Project after arranging.</div>
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
  if (s.plates) pick('plates', s.plates);
  if (s.align_y) document.getElementById('align').value = s.align_y;
  if (st.out && !outEdited) document.getElementById('out').value = st.out;
  if (st.printer !== undefined) {
    document.getElementById('printer').textContent = st.printer
      ? 'Current printer: ' + (st.bedslinger ? 'bedslinger (Auto is on).'
                                             : 'not a bedslinger (Auto is off).')
      : '';
  }
}
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
  sum.layout.forEach(function (pl) { pl.forEach(function (o) { hmax = Math.max(hmax, o.h); }); });
  sum.layout.forEach(function (pl, i) {
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
    div.innerHTML = '<h3>Plate ' + (i + 1) + ' (' + pl.length + ')</h3>' + svg;
    box.appendChild(div);
  });
  document.getElementById('legend').hidden = false;
}
function settings() {
  const manual = value('spacing') === 'manual';
  return {
    spacing: manual ? document.getElementById('gap').value : 'auto',
    manual_gap: document.getElementById('gap').value,
    layout: value('layout'),
    plates: value('plates'),
    align_y: document.getElementById('align').value
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
pick('layout', 'compact');
pick('plates', 'keep');
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


def _clean_settings(raw, saved):
    """Settings from the page, checked. Raises ArrangeError for a bad value."""
    raw = raw or {}
    spacing = raw.get("spacing", "auto")
    if spacing != "auto":
        try:
            spacing = str(max(float(spacing), 0.0))
        except (TypeError, ValueError):
            raise ArrangeError("Spacing must be a number in mm.")
    return {
        "spacing": spacing,
        "manual_gap": str(raw.get("manual_gap") or saved.get("manual_gap") or "6"),
        "layout": raw.get("layout") if raw.get("layout") in ("compact", "grid") else "compact",
        "plates": raw.get("plates") if raw.get("plates") in ("keep", "regroup") else "keep",
        "align_y": raw.get("align_y") if raw.get("align_y") in ("auto", "on", "off") else "auto",
    }


def _count(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def run_live(raw_settings, out_path, saved):
    """Arrange the open project. Returns (ok, text, summary, settings)."""
    try:
        settings = _clean_settings(raw_settings, saved)
        out_path = (out_path or "").strip()
        if not out_path.lower().endswith(".3mf"):
            raise ArrangeError("The output file must end in .3mf.")
        plater = orca.host.plater()
        if orca.host.model().object_count() == 0:
            raise ArrangeError("The project has no objects.")
        storage = orca.host.plugin.storage()
        export_path = os.path.join(storage, "export.3mf")
        try:
            plater.export_3mf_copy(export_path)
            # Sizes from the live model are exact and fast; the exported build lists the
            # instances in the same order. Align to Y axis reads the meshes instead.
            summary = arrange_file(export_path, out_path, settings, _live_bboxes())
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
    text += "\nSaved to %s\nOpen it with File > Open Project." % out_path
    return True, text, summary, settings


if orca is not None:

    class ArrangeByHeightPage(orca.pages.PagesPluginCapabilityBase):
        """The Arrange by Height tab in the main window."""

        def get_name(self):
            return "Arrange by Height"

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
            return dict(DEFAULT_SETTINGS, manual_gap="6")

        def _saved(self):
            try:
                config = json.loads(self.get_config() or "{}")
            except ValueError:
                config = {}
            merged = dict(DEFAULT_SETTINGS, manual_gap="6")
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
                         orca.LifecycleEvent.PresetSelected):
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
    parser.add_argument("--layout", choices=("compact", "grid"), default="compact")
    parser.add_argument("--plates", choices=("keep", "regroup"), default="keep")
    parser.add_argument("--align-y", choices=("auto", "on", "off"), default="auto",
                        help="turn long objects along Y (auto: on for i3 printers)")
    args = parser.parse_args(argv)
    if args.spacing != "auto":
        try:
            float(args.spacing)
        except ValueError:
            parser.error('--spacing must be a number or "auto"')
    out = args.output or os.path.splitext(args.input)[0] + "-arranged.3mf"
    settings = {"spacing": args.spacing, "layout": args.layout, "plates": args.plates,
                "align_y": args.align_y}
    try:
        summary = arrange_file(args.input, out, settings)
    except ArrangeError as err:
        print("Error: %s" % err, file=sys.stderr)
        return 1
    print("Arranged %s on %s: %s" % (_count(summary["objects"], "object"),
                                     _count(summary["plates"], "plate"), out))
    return 0


if __name__ == "__main__" and orca is None:
    sys.exit(main())
