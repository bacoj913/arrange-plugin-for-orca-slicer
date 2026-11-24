"""Tests for arrange_by_height. Run: python -m unittest discover tests"""

import json
import math
import os
import sys
import tempfile
import time
import unittest
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "arrange_by_height"))
import arrange_by_height as abh  # noqa: E402

NS = ('xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
      'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"')


def box_mesh(w, d, h):
    """A box from (0, 0, 0) to (w, d, h) as 3MF <mesh> text."""
    verts = [(x, y, z) for z in (0, h) for y in (0, d) for x in (0, w)]
    tris = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
            (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    v = "".join('<vertex x="%g" y="%g" z="%g"/>' % p for p in verts)
    t = "".join('<triangle v1="%d" v2="%d" v3="%d"/>' % p for p in tris)
    return "<mesh><vertices>%s</vertices><triangles>%s</triangles></mesh>" % (v, t)


def pyramid_mesh(w, d, h):
    """A pyramid on a w x d base, apex at height h."""
    verts = [(0, 0, 0), (w, 0, 0), (w, d, 0), (0, d, 0), (w / 2.0, d / 2.0, h)]
    tris = [(0, 2, 1), (0, 3, 2), (0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)]
    v = "".join('<vertex x="%g" y="%g" z="%g"/>' % p for p in verts)
    t = "".join('<triangle v1="%d" v2="%d" v3="%d"/>' % p for p in tris)
    return "<mesh><vertices>%s</vertices><triangles>%s</triangles></mesh>" % (v, t)


def translate(x, y, z=0.0):
    return "1 0 0 0 1 0 0 0 1 %g %g %g" % (x, y, z)


def rot_z90(x, y, z=0.0):
    return "0 1 0 -1 0 0 0 0 1 %g %g %g" % (x, y, z)


def make_project(path, objects, config=None, plates=None, extra_files=None, parts=None):
    """Write an OrcaSlicer-style 3MF.

    objects: list of (w, d, h, [transform, ...]) - one entry per object, one transform per
    instance. Each object's mesh lives in its own file, as OrcaSlicer writes it.
    plates: list of lists of (object_index, instance_index), or None for one plate.
    """
    cfg = {
        "printable_area": ["0x0", "200x0", "200x200", "0x200"],
        "print_sequence": "by layer",
        "brim_type": "no_brim",
        "brim_width": "5",
        "brim_object_gap": "0.1",
        "extruder_clearance_radius": "40",
        "nozzle_height": "4",
        "bed_exclude_area": ["0x0"],
    }
    cfg.update(config or {})
    resources, build, model_objs = [], [], []
    with zipfile.ZipFile(path, "w") as zf:
        for i, entry in enumerate(objects):
            w, d, h, transforms = entry[:4]
            shape = entry[4] if len(entry) > 4 else "box"
            mesh = pyramid_mesh(w, d, h) if shape == "pyramid" else box_mesh(w, d, h)
            mesh_id = 1
            obj_id = i + 2
            sub_objects = '<object id="%d" type="model">%s</object>' % (mesh_id, mesh)
            components = '<component p:path="/%s" objectid="%d" transform="%s"/>'
            name = "3D/Objects/object_%d.model" % (i + 1)
            comp = components % (name, mesh_id, translate(-w / 2, -d / 2, 0))
            if shape == "modifier":
                # A large modifier box next to the part.
                sub_objects += '<object id="2" type="model">%s</object>' % box_mesh(80, 80, 5)
                comp += components % (name, 2, translate(w, 0, 0))
            sub = '<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" %s>' \
                  '<resources>%s</resources><build/></model>' % (NS, sub_objects)
            zf.writestr(name, sub)
            resources.append(
                '  <object id="%d" type="model"><components>%s</components></object>\n'
                % (obj_id, comp))
            for t in transforms:
                build.append('  <item objectid="%d" transform="%s" printable="1"/>\n' % (obj_id, t))
            part_xml = ""
            if shape == "modifier":
                part_xml = ('    <part id="1" subtype="normal_part"/>\n'
                            '    <part id="2" subtype="modifier_part"/>\n')
            model_objs.append('  <object id="%d">\n    <metadata key="name" value="Part %d"/>\n'
                              '%s  </object>\n' % (obj_id, i + 1, part_xml))
        model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" %s>\n'
                 ' <resources>\n%s </resources>\n <build>\n%s </build>\n</model>\n'
                 % (NS, "".join(resources), "".join(build)))
        zf.writestr(abh.MODEL_PATH, model)
        if plates is None:
            plates = [[(i, k) for i, o in enumerate(objects) for k in range(len(o[3]))]]
        plate_xml = []
        for p, members in enumerate(plates):
            inst = "".join(
                '    <model_instance>\n      <metadata key="object_id" value="%d"/>\n'
                '      <metadata key="instance_id" value="%d"/>\n'
                '      <metadata key="identify_id" value="%d"/>\n    </model_instance>\n'
                % (oi + 2, k, 100 + oi * 10 + k) for oi, k in members)
            plate_xml.append(
                '  <plate>\n    <metadata key="plater_id" value="%d"/>\n'
                '    <metadata key="plater_name" value="Plate %d"/>\n'
                '    <metadata key="locked" value="false"/>\n'
                '    <metadata key="thumbnail_file" value="Metadata/plate_%d.png"/>\n'
                '%s  </plate>\n' % (p + 1, p + 1, p + 1, inst))
        settings = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n%s%s'
                    '  <assemble>\n  </assemble>\n</config>\n'
                    % ("".join(model_objs), "".join(plate_xml)))
        zf.writestr(abh.MODEL_SETTINGS_PATH, settings)
        zf.writestr(abh.PROJECT_SETTINGS_PATH, json.dumps(cfg))
        zf.writestr("Metadata/plate_1.png", b"png")
        for name, text in (extra_files or {}).items():
            zf.writestr(name, text)


def read_result(path):
    """Re-read a written project: items with world boxes and plate list."""
    project = abh.Project(path)
    with zipfile.ZipFile(path) as zf:
        lib = abh.MeshLibrary(zf)
        for it, (object_id, p, _t) in zip(project.items, project.build):
            it.bbox = lib.bbox(p or "/" + abh.MODEL_PATH, object_id, it.transform)
        settings = zf.read(abh.MODEL_SETTINGS_PATH).decode()
    return project, settings


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "in.3mf")
        self.out = os.path.join(self.tmp.name, "out.3mf")

    def tearDown(self):
        self.tmp.cleanup()

    def run_arrange(self, objects, settings=None, **kw):
        make_project(self.src, objects, **kw)
        s = dict(abh.DEFAULT_SETTINGS, plates="keep")
        s.update(settings or {})
        summary = abh.arrange_file(self.src, self.out, s)
        project, plate_xml = read_result(self.out)
        return summary, project, plate_xml

    def assert_valid(self, project, gap):
        bed = project.bed
        cols = abh.plate_columns(project.plate_count)
        by_plate = {}
        for it in project.items:
            cx = (it.bbox[0] + it.bbox[3]) / 2
            cy = (it.bbox[1] + it.bbox[4]) / 2
            p = abh.plate_of_point(cx, cy, cols, bed)
            self.assertIsNotNone(p)
            self.assertEqual(p, it.plate, "plate list does not match position for %r" % it)
            ox, oy = abh.plate_origin(p, cols, bed)
            b = (it.bbox[0] - ox, it.bbox[1] - oy, it.bbox[3] - ox, it.bbox[4] - oy)
            self.assertGreaterEqual(b[0], bed.x0 - 1e-6)
            self.assertGreaterEqual(b[1], bed.y0 - 1e-6)
            self.assertLessEqual(b[2], bed.x1 + 1e-6)
            self.assertLessEqual(b[3], bed.y1 + 1e-6)
            by_plate.setdefault(p, []).append(b)
        for rects in by_plate.values():
            for i, a in enumerate(rects):
                for b in rects[i + 1:]:
                    sep = max(b[0] - a[2], a[0] - b[2], b[1] - a[3], a[1] - b[3])
                    self.assertGreaterEqual(sep, gap - 1e-4, "objects closer than %g mm" % gap)
        return by_plate


class GeometryTest(unittest.TestCase):
    def test_plate_columns(self):
        self.assertEqual([abh.plate_columns(n) for n in (1, 2, 3, 4, 5, 9, 10)],
                         [1, 2, 2, 2, 3, 3, 4])

    def test_plate_of_point(self):
        bed = abh.Bed([(0, 0), (200, 0), (200, 200), (0, 200)])
        self.assertEqual(abh.plate_of_point(100, 100, 2, bed), 0)
        self.assertEqual(abh.plate_of_point(100 + 240, 100, 2, bed), 1)
        self.assertEqual(abh.plate_of_point(100, 100 - 240, 2, bed), 2)
        self.assertEqual(abh.plate_of_point(100 + 240, 100 - 240, 2, bed), 3)

    def test_bbox_through_components_and_rotation(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.3mf")
            make_project(path, [(30, 10, 5, [rot_z90(50, 60)])])
            project = abh.Project(path)
            project.load_bboxes()
            it = project.items[0]
            self.assertAlmostEqual(it.width, 10)
            self.assertAlmostEqual(it.depth, 30)
            self.assertAlmostEqual(it.height, 5)
            self.assertAlmostEqual((it.bbox[0] + it.bbox[3]) / 2, 50)
            self.assertAlmostEqual((it.bbox[1] + it.bbox[4]) / 2, 60)

    def test_parse_points(self):
        self.assertEqual(abh.parse_points(["0x0", "1.5x2"]), [(0, 0), (1.5, 2)])
        self.assertEqual(abh.parse_points("0x0,3x4"), [(0, 0), (3, 4)])


class LayoutTest(Base):
    def test_tallest_in_center_and_spacing(self):
        objects = [(20, 20, h, [translate(10 + 25 * i, 20)]) for i, h in
                   enumerate([5, 50, 10, 30, 2, 15])]
        _, project, _ = self.run_arrange(objects, {"spacing": "4"})
        self.assert_valid(project, 4)
        center = project.bed.center

        def dist(it):
            return math.hypot((it.bbox[0] + it.bbox[3]) / 2 - center[0],
                              (it.bbox[1] + it.bbox[4]) / 2 - center[1])
        tallest = max(project.items, key=lambda i: i.height)
        self.assertLess(dist(tallest), 1e-3 + 12)
        shortest = min(project.items, key=lambda i: i.height)
        self.assertGreater(dist(shortest), dist(tallest))

    def test_object_list_shortest_first(self):
        heights = [5, 50, 10, 30]
        objects = [(20, 20, h, [translate(15 + 30 * i, 20)]) for i, h in enumerate(heights)]
        _, project, plate_xml = self.run_arrange(objects)
        self.assertEqual([round(it.height) for it in project.items], sorted(heights))
        root = ET.fromstring(plate_xml)
        ids = [m.get("value") for m in root.iter("metadata") if m.get("key") == "object_id"]
        self.assertEqual(ids, [it.object_id for it in project.items])

    def test_grid_layout(self):
        objects = [(20, 20, h, [translate(15, 15 + 25 * i)]) for i, h in
                   enumerate([1, 2, 3, 4, 5, 6, 7, 8, 9])]
        _, project, _ = self.run_arrange(objects, {"layout": "grid", "spacing": "5"})
        self.assert_valid(project, 5)
        tallest = max(project.items, key=lambda i: i.height)
        self.assertAlmostEqual((tallest.bbox[0] + tallest.bbox[3]) / 2, 100)
        self.assertAlmostEqual((tallest.bbox[1] + tallest.bbox[4]) / 2, 100)
        xs = sorted(set(round(it.bbox[0], 3) for it in project.items))
        self.assertEqual(len(xs), 3)

    def test_auto_spacing_by_object(self):
        objects = [(20, 20, 30, [translate(20, 20)]), (20, 20, 10, [translate(60, 20)])]
        _, project, _ = self.run_arrange(objects, config={"print_sequence": "by object"})
        self.assert_valid(project, 40)

    def test_auto_spacing_by_layer_brim(self):
        objects = [(20, 20, 30, [translate(20, 20)]), (20, 20, 10, [translate(60, 20)])]
        _, project, _ = self.run_arrange(objects, config={"brim_type": "outer_only"})
        self.assert_valid(project, 10.2)
        for it in project.items:
            self.assertGreaterEqual(it.bbox[0], 5.1 - 1e-6)

    def test_plate_print_sequence_override(self):
        make_project(self.src, [(20, 20, 30, [translate(20, 20)]),
                                (20, 20, 10, [translate(60, 20)])])
        with zipfile.ZipFile(self.src) as zf:
            files = {n: zf.read(n) for n in zf.namelist()}
        files[abh.MODEL_SETTINGS_PATH] = files[abh.MODEL_SETTINGS_PATH].replace(
            b'<metadata key="locked" value="false"/>',
            b'<metadata key="locked" value="false"/>\n'
            b'    <metadata key="print_sequence" value="by object"/>')
        with zipfile.ZipFile(self.src, "w") as zf:
            for n, data in files.items():
                zf.writestr(n, data)
        abh.arrange_file(self.src, self.out, dict(abh.DEFAULT_SETTINGS, plates="keep"))
        project, plate_xml = read_result(self.out)
        self.assert_valid(project, 40)
        self.assertIn('key="print_sequence" value="by object"', plate_xml)

    def test_exclude_area(self):
        objects = [(30, 30, 10, [translate(100, 100)])]
        _, project, _ = self.run_arrange(
            objects, config={"bed_exclude_area": ["0x0", "200x0", "200x120", "0x120"]})
        self.assertGreaterEqual(project.items[0].bbox[1], 120 - 1e-6)


class PlateTest(Base):
    def test_keep_plates(self):
        bed_stride = 240
        objects = [(20, 20, 10, [translate(50, 50)]), (20, 20, 40, [translate(150, 150)]),
                   (20, 20, 25, [translate(50 + bed_stride, 50)])]
        plates = [[(0, 0), (1, 0)], [(2, 0)]]
        summary, project, plate_xml = self.run_arrange(objects, plates=plates)
        self.assertEqual(summary["plates"], 2)
        by_plate = self.assert_valid(project, abh.MIN_AUTO_GAP)
        self.assertEqual(len(by_plate[0]), 2)
        self.assertEqual(len(by_plate[1]), 1)
        self.assertIn('value="Plate 2"', plate_xml)
        self.assertNotIn("thumbnail_file", plate_xml)
        self.assertIn('key="identify_id" value="110"', plate_xml)

    def test_keep_plates_overflow(self):
        objects = [(90, 90, 10 + i, [translate(50, 50)]) for i in range(6)]
        summary, project, _ = self.run_arrange(objects, {"spacing": "5"})
        self.assertEqual(summary["plates"], 2)
        self.assert_valid(project, 5)

    def test_regroup_by_height(self):
        heights = [5, 80, 6, 70, 7, 75, 8, 90]
        objects = [(90, 90, h, [translate(50, 50)]) for h in heights]
        summary, project, _ = self.run_arrange(objects, {"plates": "regroup", "spacing": "5"})
        self.assertEqual(summary["plates"], 2)
        self.assert_valid(project, 5)
        plate0 = sorted(round(it.height) for it in project.items if it.plate == 0)
        plate1 = sorted(round(it.height) for it in project.items if it.plate == 1)
        self.assertEqual(plate0, [70, 75, 80, 90])
        self.assertEqual(plate1, [5, 6, 7, 8])

    def test_multi_instance_object(self):
        objects = [(20, 20, 30, [translate(20, 20), translate(60, 20), translate(100, 20)]),
                   (20, 20, 5, [translate(140, 20)])]
        _, project, plate_xml = self.run_arrange(objects)
        self.assert_valid(project, abh.MIN_AUTO_GAP)
        self.assertEqual([it.object_id for it in project.items], ["3", "2", "2", "2"])
        self.assertEqual(plate_xml.count("<model_instance>"), 4)

    def test_object_too_large(self):
        make_project(self.src, [(250, 20, 10, [translate(100, 100)])])
        with self.assertRaises(abh.ArrangeError):
            abh.arrange_file(self.src, self.out, dict(abh.DEFAULT_SETTINGS, plates="keep"))

    def test_live_bboxes_used(self):
        make_project(self.src, [(20, 20, 10, [translate(50, 50)]),
                                (20, 20, 30, [translate(100, 50)])])
        boxes = [(40, 40, 0, 60, 60, 10), (90, 40, 0, 110, 60, 30)]
        abh.arrange_file(self.src, self.out, dict(abh.DEFAULT_SETTINGS, plates="keep"),
                         boxes)
        project, _ = read_result(self.out)
        self.assert_valid(project, abh.MIN_AUTO_GAP)
        with self.assertRaises(abh.ArrangeError):
            abh.arrange_file(self.src, self.out, dict(abh.DEFAULT_SETTINGS, plates="keep"),
                             boxes[:1])

    def test_other_files_copied(self):
        self.run_arrange([(20, 20, 10, [translate(50, 50)])])
        with zipfile.ZipFile(self.out) as zf:
            self.assertEqual(zf.read("Metadata/plate_1.png"), b"png")
            self.assertIn("3D/Objects/object_1.model", zf.namelist())


class SpeedTest(Base):
    def test_many_objects(self):
        objects = [(8 + (i % 5) * 3, 8 + (i % 7) * 2, 2 + (i * 37) % 60,
                    [translate(10 + (i % 10) * 18, 10 + (i // 10) * 18)]) for i in range(120)]
        make_project(self.src, objects)
        start = time.time()
        abh.arrange_file(self.src, self.out, {"spacing": "3", "layout": "compact",
                                              "plates": "regroup"})
        elapsed = time.time() - start
        project, _ = read_result(self.out)
        self.assert_valid(project, 3)
        self.assertLess(elapsed, 20.0)


class AlignTest(Base):
    def test_hull(self):
        pts = [(0, 0), (2, 0), (2, 2), (0, 2), (1, 1), (1, 0)]
        self.assertEqual(sorted(abh.convex_hull(pts)), [(0, 0), (0, 2), (2, 0), (2, 2)])

    def test_angle(self):
        along_x = [(0, 0), (40, 0), (40, 10), (0, 10)]
        self.assertAlmostEqual(abs(abh.y_align_angle(along_x)), math.pi / 2)
        along_y = [(0, 0), (10, 0), (10, 40), (0, 40)]
        self.assertEqual(abh.y_align_angle(along_y), 0.0)
        square = [(0, 0), (20, 0), (20, 20), (0, 20)]
        self.assertEqual(abh.y_align_angle(square), 0.0)
        near_square = [(0, 0), (22, 0), (22, 20), (0, 20)]
        self.assertEqual(abh.y_align_angle(near_square), 0.0)
        c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
        tilted = [(x * c - y * s, x * s + y * c) for x, y in along_x]
        turned = abh.rotate_points(tilted, abh.y_align_angle(tilted), (0, 0))
        xs, ys = zip(*turned)
        self.assertAlmostEqual(max(xs) - min(xs), 10, places=6)
        self.assertAlmostEqual(max(ys) - min(ys), 40, places=6)

    def test_rotate_z_keeps_pivot(self):
        m = abh.parse_transform(translate(5, 6, 1))
        r = abh.rotate_z(m, math.pi / 2, (5, 6))
        self.assertEqual([round(v, 9) for v in abh.apply_transform(r, (0, 0, 0))], [5, 6, 1])
        self.assertEqual([round(v, 9) for v in abh.apply_transform(r, (1, 0, 0))], [5, 7, 1])

    def long_parts(self):
        return [(60, 12, 5 + i, [translate(40, 20 + 20 * i)]) for i in range(4)]

    def test_auto_on_for_bedslinger(self):
        summary, project, _ = self.run_arrange(self.long_parts(),
                                               config={"printer_structure": "i3"})
        self.assertTrue(summary["aligned"])
        self.assertEqual(summary["rotated"], 4)
        for it in project.items:
            self.assertAlmostEqual(it.width, 12, places=6)
            self.assertAlmostEqual(it.depth, 60, places=6)
        self.assert_valid(project, abh.MIN_AUTO_GAP)

    def test_auto_off_for_corexy(self):
        summary, project, _ = self.run_arrange(self.long_parts(),
                                               config={"printer_structure": "corexy"})
        self.assertFalse(summary["aligned"])
        for it in project.items:
            self.assertAlmostEqual(it.width, 60, places=6)

    def test_forced_on_and_off(self):
        summary, project, _ = self.run_arrange(self.long_parts(), {"align_y": "on"})
        self.assertEqual(summary["rotated"], 4)
        summary, project, _ = self.run_arrange(self.long_parts(), {"align_y": "off"},
                                               config={"printer_structure": "i3"})
        self.assertEqual(summary["rotated"], 0)

    def test_already_aligned_not_turned(self):
        objects = [(12, 60, 5, [translate(40, 40)]), (60, 12, 5, [rot_z90(100, 100)])]
        summary, project, _ = self.run_arrange(objects, {"align_y": "on"})
        self.assertEqual(summary["rotated"], 0)

    def test_summary_layout(self):
        summary, _, _ = self.run_arrange(self.long_parts(), {"align_y": "on"})
        self.assertEqual(summary["bed"], [0, 0, 200, 200])
        self.assertEqual(summary["layout"][0]["plate"], 1)
        names = sorted(o["name"] for o in summary["layout"][0]["objects"])
        self.assertEqual(names, ["Part 1", "Part 2", "Part 3", "Part 4"])


class RowsTest(Base):
    def test_rows_along_x(self):
        heights = [10, 90, 20, 80, 30, 70, 40, 60, 50, 15, 25, 35]
        objects = [(20, 20, h, [translate(15 + 15 * i, 15)]) for i, h in enumerate(heights)]
        _, project, _ = self.run_arrange(objects, {"layout": "rows", "spacing": "5"},
                                         config={"printable_area": ["0x0", "130x0",
                                                                    "130x200", "0x200"]})
        self.assert_valid(project, 5)
        rows = {}
        for it in project.items:
            rows.setdefault(round(it.bbox[1], 3), []).append(it)
        self.assertEqual(sorted(len(r) for r in rows.values()), [2, 5, 5])
        # The tallest row is in the middle, and the tallest object in the middle of it.
        ys = sorted(rows)
        middle = rows[ys[1]]
        self.assertEqual(max(i.height for i in middle), 90)
        middle.sort(key=lambda i: i.bbox[0])
        self.assertEqual(round(middle[2].height), 90)
        self.assertAlmostEqual((middle[2].bbox[0] + middle[2].bbox[3]) / 2, 65)

    def test_rows_overflow(self):
        objects = [(90, 90, 10 + i, [translate(50, 50)]) for i in range(6)]
        summary, project, _ = self.run_arrange(objects, {"layout": "rows", "spacing": "5"})
        self.assertEqual(summary["plates"], 2)
        self.assert_valid(project, 5)


class CurrentPlateTest(Base):
    def objects(self):
        stride = 240
        return [(20, 20, 10, [translate(30, 30)]), (20, 20, 40, [translate(170, 170)]),
                (20, 20, 25, [translate(30 + stride, 30)]),
                (20, 20, 50, [translate(170 + stride, 170)]),
                (60, 12, 5, [translate(100 + stride, 100)])]

    def test_only_current_plate_moves(self):
        plates = [[(0, 0), (1, 0)], [(2, 0), (3, 0), (4, 0)]]
        make_project(self.src, self.objects(), plates=plates,
                     config={"printer_structure": "i3"})
        before = abh.Project(self.src)
        before.load_bboxes()
        summary = abh.arrange_file(self.src, self.out, dict(
            abh.DEFAULT_SETTINGS, plates="current", plate_index=1, spacing="4"))
        project, plate_xml = read_result(self.out)
        self.assertEqual(summary["plates"], 2)
        self.assertEqual(summary["changed"], [2])
        self.assertEqual([p["plate"] for p in summary["layout"]], [2])
        old = {it.object_id: it.bbox for it in before.items}
        for it in project.items:
            if it.object_id in ("2", "3"):
                # Plate 1: untouched, same place, same list position.
                for a, b in zip(it.bbox, old[it.object_id]):
                    self.assertAlmostEqual(a, b, places=6)
        self.assertEqual([it.object_id for it in project.items][:2], ["2", "3"])
        # Plate 2: shortest first in the list, the long part turned along Y.
        self.assertEqual([it.object_id for it in project.items][2:], ["6", "4", "5"])
        turned = [it for it in project.items if it.object_id == "6"][0]
        self.assertAlmostEqual(turned.depth, 60, places=6)
        self.assertEqual(summary["rotated"], 1)
        self.assert_valid(project, 4)
        # Plate 1 keeps its thumbnail; plate 2 loses it.
        self.assertIn("Metadata/plate_1.png", plate_xml)
        self.assertNotIn("Metadata/plate_2.png", plate_xml)

    def test_current_plate_overflow(self):
        objects = [(20, 20, 10, [translate(30, 30)])] + [
            (90, 90, 10 + i, [translate(50 + 240, 50)]) for i in range(6)]
        plates = [[(0, 0)], [(i, 0) for i in range(1, 7)]]
        make_project(self.src, objects, plates=plates)
        summary = abh.arrange_file(self.src, self.out, dict(
            abh.DEFAULT_SETTINGS, plates="current", plate_index=1, spacing="5"))
        self.assertEqual(summary["plates"], 3)
        self.assertEqual(summary["changed"], [1, 2, 3])
        project, _ = read_result(self.out)
        self.assert_valid(project, 5)
        self.assertEqual(sum(1 for it in project.items if it.plate == 0), 1)

    def test_empty_or_missing_plate(self):
        make_project(self.src, self.objects()[:2])
        with self.assertRaises(abh.ArrangeError):
            abh.arrange_file(self.src, self.out, dict(
                abh.DEFAULT_SETTINGS, plates="current", plate_index=3))


class LayerHeightTest(Base):
    def params(self, height):
        return abh.SlicingParams({"layer_height": "0.2", "initial_layer_print_height": "0.2",
                                  "min_layer_height": ["0.08"], "max_layer_height": ["0.28"],
                                  "nozzle_diameter": ["0.4"]}, {}, height)

    def mesh(self, shape, w=40, d=40, h=20):
        make_project(self.src, [(w, d, h, [translate(50, 50)], shape)])
        with zipfile.ZipFile(self.src) as zf:
            lib = abh.MeshLibrary(zf)
            return lib.triangles("/" + abh.MODEL_PATH, "2", abh.parse_transform(
                translate(50, 50)))

    def check(self, profile, params):
        self.assertEqual(profile[:4], [0.0, 0.2, 0.2, 0.2])
        self.assertEqual(len(profile) % 2, 0)
        zs, hs = profile[0::2], profile[1::2]
        self.assertEqual(zs, sorted(zs))
        self.assertAlmostEqual(zs[-1] + 0, zs[-1])
        for h in hs:
            self.assertGreaterEqual(h, params.min_layer_height - 1e-9)
            self.assertLessEqual(h, params.max_layer_height + 1e-9)
        return zs, hs

    def test_params(self):
        p = self.params(20)
        self.assertEqual((p.min_layer_height, p.max_layer_height), (0.08, 0.28))
        q = abh.SlicingParams({"layer_height": "0.2", "nozzle_diameter": ["0.4"]},
                              {"layer_height": "0.12"}, 5)
        self.assertEqual(q.layer_height, 0.12)
        self.assertAlmostEqual(q.min_layer_height, 0.07)
        self.assertAlmostEqual(q.max_layer_height, 0.3)

    def test_box_uses_thick_layers(self):
        params = self.params(20)
        zs, hs = self.check(abh.adaptive_profile(self.mesh("box"), params, 0.5), params)
        # Vertical walls: layers grow by 0.04 steps to the maximum.
        self.assertAlmostEqual(hs[2], 0.24, places=6)
        self.assertAlmostEqual(max(hs), 0.28, places=6)
        # The last layer ends at the top of the object.
        self.assertAlmostEqual(zs[-2] + hs[-2], 20, places=4)

    def test_slope_uses_thinner_layers(self):
        params = self.params(20)
        tris = self.mesh("pyramid", 80, 80, 20)
        fine = abh.adaptive_profile(tris, params, 0.0)
        coarse = abh.adaptive_profile(tris, params, 1.0)
        self.check(fine, params)
        self.check(coarse, params)
        self.assertGreater(len(fine), len(coarse))
        box = abh.adaptive_profile(self.mesh("box"), params, 0.5)
        mid = abh.adaptive_profile(tris, params, 0.5)
        self.assertGreater(len(mid), len(box))

    def test_height_range_overrides(self):
        params = self.params(20)
        prof = abh.adaptive_profile(self.mesh("box"), params, 0.5, [(5.0, 10.0, 0.1)])
        inside = [h for z, h in zip(prof[0::2], prof[1::2]) if 5.2 <= z <= 9.8]
        self.assertTrue(inside)
        self.assertTrue(all(abs(h - 0.1) < 1e-9 for h in inside))

    def test_smooth(self):
        params = self.params(20)
        prof = abh.adaptive_profile(self.mesh("pyramid", 80, 80, 20), params, 0.0)
        smooth = abh.smooth_profile(prof, params, 5, False)
        keep = abh.smooth_profile(prof, params, 5, True)
        self.assertEqual(len(smooth), len(prof))
        self.assertEqual(smooth[:4], prof[:4])

        def roughness(p):
            hs = p[5::2]
            return sum(abs(a - b) for a, b in zip(hs, hs[1:]))
        self.assertLessEqual(roughness(smooth), roughness(prof) + 1e-9)
        for a, b in zip(keep[1::2], prof[1::2]):
            self.assertLessEqual(a, b + 1e-9)

    def test_written_for_current_plate(self):
        objects = [(20, 20, 10, [translate(30, 30)]), (20, 20, 15, [translate(270, 30)]),
                   (60, 60, 20, [translate(400, 100)], "pyramid")]
        make_project(self.src, objects, plates=[[(0, 0)], [(1, 0), (2, 0)]])
        summary = abh.arrange_file(self.src, self.out, dict(
            abh.DEFAULT_SETTINGS, plates="current", plate_index=1, adaptive=True,
            adaptive_quality=0.3, smooth=True, smooth_radius=3))
        self.assertEqual(summary["layer_heights"], 2)
        with zipfile.ZipFile(self.out) as zf:
            profiles = abh.parse_profiles(zf.read(abh.LAYER_HEIGHTS_PATH).decode())
        project, _ = read_result(self.out)
        order = [it.object_id for it in project.items]
        self.assertEqual(sorted(profiles), [order.index("3") + 1, order.index("4") + 1])
        pyramid = profiles[order.index("4") + 1]
        self.assertAlmostEqual(pyramid[-2], 20, places=3)

    def test_smooth_alone_uses_existing_profile(self):
        objects = [(20, 20, 10, [translate(30, 30)]), (20, 20, 5, [translate(80, 30)])]
        existing = "object_id=1|0;0.2;0.2;0.2;2;0.08;4;0.28;6;0.08;8;0.28;10;0.2\n"
        make_project(self.src, objects, extra_files={abh.LAYER_HEIGHTS_PATH: existing})
        summary = abh.arrange_file(self.src, self.out, dict(
            abh.DEFAULT_SETTINGS, plates="keep", smooth=True, smooth_radius=2,
            share_layers=False))
        self.assertEqual(summary["layer_heights"], 1)
        with zipfile.ZipFile(self.out) as zf:
            profiles = abh.parse_profiles(zf.read(abh.LAYER_HEIGHTS_PATH).decode())
        # The short object now comes first, so the profile moves to place 2.
        self.assertEqual(list(profiles), [2])
        self.assertNotEqual(profiles[2][5], 0.08)

    def test_existing_files_follow_their_objects(self):
        objects = [(20, 20, 30, [translate(30, 30)]), (20, 20, 5, [translate(80, 30)])]
        extra = {
            abh.LAYER_HEIGHTS_PATH: "object_id=1|0;0.2;30;0.2\nobject_id=2|0;0.2;5;0.1\n",
            "Metadata/brim_ear_points.txt": "object_id=2|1 2 3 4\n",
            "Metadata/layer_config_ranges.xml":
                '<?xml version="1.0"?>\n<objects>\n <object id="1">\n'
                '  <range min_z="1" max_z="2"><option opt_key="layer_height">0.1</option>'
                '</range>\n </object>\n</objects>\n',
        }
        make_project(self.src, objects, extra_files=extra)
        abh.arrange_file(self.src, self.out, dict(abh.DEFAULT_SETTINGS, plates="keep"))
        with zipfile.ZipFile(self.out) as zf:
            heights = zf.read(abh.LAYER_HEIGHTS_PATH).decode()
            ears = zf.read("Metadata/brim_ear_points.txt").decode()
            ranges = zf.read("Metadata/layer_config_ranges.xml").decode()
        # The object list is now [short, tall].
        self.assertEqual(abh.parse_profiles(heights), {2: [0, 0.2, 30, 0.2],
                                                       1: [0, 0.2, 5, 0.1]})
        self.assertEqual(ears, "object_id=1|1 2 3 4\n")
        self.assertIn('<object id="2">', ranges)

    def test_modifier_not_counted(self):
        make_project(self.src, [(20, 20, 10, [translate(50, 50)], "modifier")])
        project = abh.Project(self.src)
        project.load_bboxes()
        self.assertAlmostEqual(project.items[0].width, 20)
        self.assertAlmostEqual(project.items[0].height, 10)


def tube_triangles(w, wall, h, x=0.0, y=0.0):
    """A square tube (open top and bottom rims), outer side w."""
    def quad(a, b, c, d):
        return [(a, b, c), (a, c, d)]
    o = [(x, y), (x + w, y), (x + w, y + w), (x, y + w)]
    i = [(x + wall, y + wall), (x + w - wall, y + wall), (x + w - wall, y + w - wall),
         (x + wall, y + w - wall)]
    t = []
    for k in range(4):
        a, b = o[k], o[(k + 1) % 4]
        t += quad((a[0], a[1], 0), (b[0], b[1], 0), (b[0], b[1], h), (a[0], a[1], h))
        a, b = i[k], i[(k + 1) % 4]
        t += quad((b[0], b[1], 0), (a[0], a[1], 0), (a[0], a[1], h), (b[0], b[1], h))
        n = (k + 1) % 4
        t += quad((o[k][0], o[k][1], h), (o[n][0], o[n][1], h), (i[n][0], i[n][1], h),
                  (i[k][0], i[k][1], h))
        t += quad((o[k][0], o[k][1], 0), (i[k][0], i[k][1], 0), (i[n][0], i[n][1], 0),
                  (o[n][0], o[n][1], 0))
    return t


class SharedLayersTest(Base):
    CONFIG = {"layer_height": "0.2", "initial_layer_print_height": "0.2",
              "min_layer_height": ["0.08"], "max_layer_height": ["0.28"],
              "nozzle_diameter": ["0.4"]}

    def test_cross_section(self):
        box = abh.CrossSection(LayerHeightTest.mesh(self, "box", 20, 30, 10))
        for length, area in box.sample([0.1, 5.0, 9.9]):
            self.assertAlmostEqual(length, 100, places=6)
            self.assertAlmostEqual(area, 600, places=6)
        tube = abh.CrossSection(tube_triangles(20, 1.6, 50, 10, 10))
        length, area = tube.sample([25.0])[0]
        self.assertAlmostEqual(length, 80 + 4 * 16.8, places=6)
        self.assertAlmostEqual(area, 400 - 16.8 ** 2, places=6)
        pyramid = abh.CrossSection(LayerHeightTest.mesh(self, "pyramid", 40, 40, 20))
        length, area = pyramid.sample([10.0])[0]
        self.assertAlmostEqual(length, 80, places=6)
        self.assertAlmostEqual(area, 400, places=6)

    def test_merge_and_fit(self):
        a = [0.0, 0.2, 0.2, 0.2, 5.0, 0.28, 10.0, 0.28]
        b = [0.0, 0.2, 0.2, 0.2, 2.0, 0.1, 4.0, 0.1]
        merged = abh.merge_finest([a, b])
        self.assertEqual(merged[:4], [0.0, 0.2, 0.2, 0.2])
        self.assertAlmostEqual(merged[-2], 10.0)
        heights = dict(zip(merged[0::2], merged[1::2]))
        for z, h in heights.items():
            if 2.0 <= z <= 4.0:
                self.assertLessEqual(h, 0.1 + 1e-9)
        fit = abh.fit_to_height(merged, 4.0)
        self.assertAlmostEqual(fit[-2], 4.0)
        self.assertTrue(all(z <= 4.0 for z in fit[0::2]))

    def test_object_layers(self):
        params = abh.SlicingParams(self.CONFIG, {}, 10.0)
        layers = abh.object_layers(params, abh.default_profile(params))
        self.assertEqual(len(layers), 50)
        self.assertEqual(layers[0], (0.0, 0.2))
        self.assertAlmostEqual(layers[-1][1], 10.0, places=6)

    def objects(self):
        # A pyramid and a short cone-like pyramid: different adaptive profiles.
        return [(60, 60, 20, [translate(60, 60)], "pyramid"),
                (40, 40, 12, [translate(150, 150)], "pyramid")]

    def test_shared_profile_aligns_layers(self):
        # Small parts with a minimum layer time: extra plate layers cost the most.
        objects = [(16, 16, 20, [translate(60, 60)], "pyramid"),
                   (12, 12, 12, [translate(150, 150)], "pyramid")]
        make_project(self.src, objects, config=dict(self.CONFIG, slow_down_layer_time=["8"]))
        shared = abh.arrange_file(self.src, self.out, dict(
            abh.DEFAULT_SETTINGS, plates="keep", adaptive=True, adaptive_quality=0.3))
        with zipfile.ZipFile(self.out) as zf:
            profiles = abh.parse_profiles(zf.read(abh.LAYER_HEIGHTS_PATH).decode())
        low, high = sorted(profiles.values(), key=lambda p: p[-2])
        self.assertAlmostEqual(low[-2], 12, places=3)
        # The shorter object follows the taller one's profile up to its own top.
        self.assertEqual(low[:-2], high[:len(low) - 2])
        separate = abh.arrange_file(self.src, self.out, dict(
            abh.DEFAULT_SETTINGS, plates="keep", adaptive=True, adaptive_quality=0.3,
            share_layers=False))
        self.assertLess(shared["estimate"]["after_layers"],
                        separate["estimate"]["after_layers"])
        self.assertLess(shared["estimate"]["after"], separate["estimate"]["after"])

    def test_estimate(self):
        make_project(self.src, self.objects(), config=self.CONFIG)
        plain = abh.arrange_file(self.src, self.out, dict(abh.DEFAULT_SETTINGS, plates="keep"))
        est = plain["estimate"]
        self.assertGreater(est["before"], 0)
        # Only the layout changed: same layers, a similar time.
        self.assertEqual(est["before_layers"], est["after_layers"])
        self.assertEqual(est["before_layers"], 100)
        self.assertLess(abs(est["after"] - est["before"]), 0.05 * est["before"])
        fine = abh.arrange_file(self.src, self.out, dict(
            abh.DEFAULT_SETTINGS, plates="keep", adaptive=True, adaptive_quality=0.0))
        self.assertGreater(fine["estimate"]["after"], est["after"])

    def test_estimate_text(self):
        text = abh.estimate_text({"before": 3600, "after": 2700, "before_layers": 470,
                                  "after_layers": 397})
        self.assertIn("1 h 00 min before, 45 min after (-15 min, -25%)", text)
        self.assertIn("470 before, 397 after", text)

    def test_format_duration(self):
        self.assertEqual(abh.format_duration(59 * 60), "59 min")
        self.assertEqual(abh.format_duration(125 * 60 + 10), "2 h 05 min")


class EdgeMarginTest(Base):
    def min_edge(self, project):
        """Smallest distance from a part to the edge of its own plate."""
        bed = project.bed
        cols = abh.plate_columns(project.plate_count)
        out = []
        for it in project.items:
            ox, oy = abh.plate_origin(it.plate, cols, bed)
            b = (it.bbox[0] - ox, it.bbox[1] - oy, it.bbox[3] - ox, it.bbox[4] - oy)
            out.append(min(b[0] - bed.x0, b[1] - bed.y0, bed.x1 - b[2], bed.y1 - b[3]))
        return min(out)

    def many(self):
        # Enough parts to reach the bed edges.
        return [(45, 45, 10 + i, [translate(30, 30)]) for i in range(16)]

    def test_skirt_extent(self):
        cfg = {"skirt_loops": "2", "skirt_height": "1", "skirt_distance": "3",
               "nozzle_diameter": ["0.4"], "initial_layer_line_width": "0.5"}
        self.assertAlmostEqual(abh.skirt_extent(cfg), 4.0)
        cfg["initial_layer_line_width"] = "125%"
        self.assertAlmostEqual(abh.skirt_extent(cfg), 4.0)
        self.assertEqual(abh.skirt_extent(dict(cfg, skirt_loops="0")), 0.0)
        self.assertAlmostEqual(abh.skirt_extent(dict(cfg, skirt_loops="0",
                                                     draft_shield="enabled")), 3.5)

    def test_auto_counts_skirt_and_brim(self):
        cfg = {"brim_type": "outer_only", "brim_width": "3", "brim_object_gap": "0",
               "skirt_loops": "1", "skirt_height": "1", "skirt_distance": "2",
               "initial_layer_line_width": "0.5"}
        _, project, _ = self.run_arrange(self.many(), {"spacing": "2"}, config=cfg)
        self.assertGreaterEqual(self.min_edge(project), 1 + 2.5 + 3 - 1e-6)
        self.assertEqual(abh.edge_margin(dict(cfg), "by object"), 5 + 2.5 + 3)

    def test_manual(self):
        summary, project, _ = self.run_arrange(self.many(), {"spacing": "2",
                                                             "edge_margin": "12"})
        self.assertGreaterEqual(self.min_edge(project), 12 - 1e-6)
        self.assertEqual(summary["plates"], 2)
        # 4 x 45 mm + 3 x 2 mm = 186 mm: all fit on the 200 mm bed only with a small margin.
        summary, project, _ = self.run_arrange(self.many(), {"spacing": "2",
                                                             "edge_margin": "0"})
        self.assertEqual(summary["plates"], 1)

    def test_per_object_skirt_gap(self):
        cfg = {"skirt_type": "perobject", "skirt_loops": "1", "skirt_height": "1",
               "skirt_distance": "2", "initial_layer_line_width": "0.5"}
        objects = [(20, 20, 10 + i, [translate(30 + 25 * i, 30)]) for i in range(4)]
        _, project, _ = self.run_arrange(objects, config=cfg)
        self.assert_valid(project, 5.0)


class FakeOrca:
    """The parts of OrcaSlicer's embedded `orca` module the plugin uses."""

    def __init__(self, tmp, project_path, export_source):
        import types
        self.module = types.ModuleType("orca")
        test = self

        class Base:
            def __init__(self):
                self.config = "{}"
                self.sent = []

            def get_config(self):
                return self.config

            def save_config(self, text):
                self.config = text
                return True

            def post_message(self, data):
                self.sent.append(data)

        class BBox:
            def __init__(self, b):
                self.min, self.max = b[:3], b[3:]

        class Inst:
            def __init__(self, b):
                self.b = b

            def bounding_box(self):
                return BBox(self.b)

        class Obj:
            def __init__(self, boxes):
                self.boxes = boxes

            def instances(self):
                return [Inst(b) for b in self.boxes]

        class Model:
            def objects(self):
                return [Obj(b) for b in test.boxes]

            def object_count(self):
                return len(test.boxes)

            def current_plate_index(self):
                return test.plate

        class Plater:
            def project_path(self):
                return project_path

            def export_3mf_copy(self, path):
                import shutil
                shutil.copy(export_source, path)

        class Bundle:
            def full_config_value(self, key):
                return "i3" if key == "printer_structure" else None

        host = types.SimpleNamespace(
            model=Model, plater=Plater, preset_bundle=Bundle,
            plugin=types.SimpleNamespace(storage=lambda: tmp))
        m = self.module
        m.host = host
        m.pages = types.SimpleNamespace(PagesPluginCapabilityBase=Base)
        m.base = object
        m.plugin = lambda cls: cls
        m.registered = []
        m.register_capability = m.registered.append
        m.LifecycleEvent = types.SimpleNamespace(ProjectOpened=1, NewProject=2,
                                                 ProjectAfterSave=3, PresetSelected=4,
                                                 PlateSelected=5)
        self.boxes = []
        self.plate = 0


class PluginTest(Base):
    def load_plugin(self, fake):
        import importlib.util
        sys.modules["orca"] = fake.module
        try:
            spec = importlib.util.spec_from_file_location(
                "abh_plugin", os.path.join(os.path.dirname(__file__), "..",
                                           "arrange_by_height", "arrange_by_height.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            del sys.modules["orca"]
        return mod

    def test_page_flow(self):
        make_project(self.src, [(20, 20, 10, [translate(50, 50)]),
                                (20, 20, 30, [translate(100, 50)])])
        project_path = os.path.join(self.tmp.name, "brackets.3mf")
        fake = FakeOrca(self.tmp.name, project_path, self.src)
        fake.boxes = [[(40, 40, 0, 60, 60, 10)], [(90, 40, 0, 110, 60, 30)]]
        mod = self.load_plugin(fake)
        mod.ArrangeByHeightPlugin().register_capabilities()
        self.assertEqual(fake.module.registered, [mod.ArrangeByHeightPage])
        page = mod.ArrangeByHeightPage()
        self.assertIn("Arrange by Height", page.get_ui())
        self.assertTrue(page.get_icon().endswith(".svg"))
        self.assertEqual(page.get_name(), "Arrange")

        page.on_message(json.dumps({"type": "ready"}))
        state = page.sent[-1]
        self.assertEqual(state["type"], "state")
        self.assertEqual(state["out"], os.path.join(self.tmp.name, "brackets-arranged.3mf"))
        self.assertTrue(state["bedslinger"])
        self.assertEqual(state["plate"], 1)

        page.on_message({"type": "arrange", "out": state["out"],
                         "settings": {"spacing": "5", "layout": "grid", "plates": "keep",
                                      "align_y": "off"}})
        result = page.sent[-1]
        self.assertTrue(result["ok"], result["text"])
        self.assertEqual(result["summary"]["objects"], 2)
        self.assertTrue(os.path.exists(state["out"]))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "export.3mf")))
        self.assertEqual(json.loads(page.config)["layout"], "grid")

        # Replace the project file: a backup is kept.
        import shutil
        shutil.copy(self.src, project_path)
        page.on_message({"type": "arrange", "out": "",
                         "settings": {"output": "replace", "plates": "current",
                                      "layout": "rows"}})
        result = page.sent[-1]
        self.assertTrue(result["ok"], result["text"])
        self.assertIn("Don't Save", result["text"])
        self.assertIn("Estimated print time", result["text"])
        backup = os.path.join(self.tmp.name, "brackets-backup.3mf")
        with open(backup, "rb") as a, open(self.src, "rb") as b:
            self.assertEqual(a.read(), b.read())
        with zipfile.ZipFile(project_path) as zf:
            self.assertIn(abh.MODEL_PATH, zf.namelist())
        self.assertNotEqual(open(project_path, "rb").read(), open(self.src, "rb").read())

        page.on_message({"type": "arrange", "out": "bad.txt", "settings": {}})
        self.assertFalse(page.sent[-1]["ok"])
        page.on_message({"type": "arrange", "out": state["out"],
                         "settings": {"spacing": "wide"}})
        self.assertIn("Spacing", page.sent[-1]["text"])


class CommandLineTest(Base):
    def test_main(self):
        make_project(self.src, [(20, 20, 10, [translate(50, 50)])])
        self.assertEqual(abh.main([self.src, "-o", self.out, "--spacing", "3",
                                   "--align-y", "on", "--layout", "rows",
                                   "--plates", "current", "--plate", "1", "--edge-margin", "4",
                                   "--adaptive", "0.4", "--smooth", "4", "--keep-min"]), 0)
        with zipfile.ZipFile(self.out) as zf:
            self.assertIn(abh.LAYER_HEIGHTS_PATH, zf.namelist())
        self.assertTrue(os.path.exists(self.out))


if __name__ == "__main__":
    unittest.main()
