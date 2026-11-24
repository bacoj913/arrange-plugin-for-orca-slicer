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


def translate(x, y, z=0.0):
    return "1 0 0 0 1 0 0 0 1 %g %g %g" % (x, y, z)


def rot_z90(x, y, z=0.0):
    return "0 1 0 -1 0 0 0 0 1 %g %g %g" % (x, y, z)


def make_project(path, objects, config=None, plates=None):
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
        for i, (w, d, h, transforms) in enumerate(objects):
            mesh_id = 1
            obj_id = i + 2
            sub = '<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" %s>' \
                  '<resources><object id="%d" type="model">%s</object></resources>' \
                  '<build/></model>' % (NS, mesh_id, box_mesh(w, d, h))
            name = "3D/Objects/object_%d.model" % (i + 1)
            zf.writestr(name, sub)
            resources.append(
                '  <object id="%d" type="model"><components>'
                '<component p:path="/%s" objectid="%d" transform="%s"/>'
                '</components></object>\n' % (obj_id, name, mesh_id, translate(-w / 2, -d / 2, 0)))
            for t in transforms:
                build.append('  <item objectid="%d" transform="%s" printable="1"/>\n' % (obj_id, t))
            model_objs.append('  <object id="%d">\n    <metadata key="name" value="Part %d"/>\n'
                              '  </object>\n' % (obj_id, i + 1))
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
        s = dict(abh.DEFAULT_SETTINGS)
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
        abh.arrange_file(self.src, self.out, dict(abh.DEFAULT_SETTINGS))
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
            abh.arrange_file(self.src, self.out, dict(abh.DEFAULT_SETTINGS))

    def test_live_bboxes_used(self):
        make_project(self.src, [(20, 20, 10, [translate(50, 50)]),
                                (20, 20, 30, [translate(100, 50)])])
        boxes = [(40, 40, 0, 60, 60, 10), (90, 40, 0, 110, 60, 30)]
        abh.arrange_file(self.src, self.out, dict(abh.DEFAULT_SETTINGS), boxes)
        project, _ = read_result(self.out)
        self.assert_valid(project, abh.MIN_AUTO_GAP)
        with self.assertRaises(abh.ArrangeError):
            abh.arrange_file(self.src, self.out, dict(abh.DEFAULT_SETTINGS), boxes[:1])

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
        names = sorted(o["name"] for o in summary["layout"][0])
        self.assertEqual(names, ["Part 1", "Part 2", "Part 3", "Part 4"])


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
                                                 ProjectAfterSave=3, PresetSelected=4)
        self.boxes = []


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

        page.on_message(json.dumps({"type": "ready"}))
        state = page.sent[-1]
        self.assertEqual(state["type"], "state")
        self.assertEqual(state["out"], os.path.join(self.tmp.name, "brackets-arranged.3mf"))
        self.assertTrue(state["bedslinger"])

        page.on_message({"type": "arrange", "out": state["out"],
                         "settings": {"spacing": "5", "layout": "grid", "plates": "keep",
                                      "align_y": "off"}})
        result = page.sent[-1]
        self.assertTrue(result["ok"], result["text"])
        self.assertEqual(result["summary"]["objects"], 2)
        self.assertTrue(os.path.exists(state["out"]))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "export.3mf")))
        self.assertEqual(json.loads(page.config)["layout"], "grid")

        page.on_message({"type": "arrange", "out": "bad.txt", "settings": {}})
        self.assertFalse(page.sent[-1]["ok"])
        page.on_message({"type": "arrange", "out": state["out"],
                         "settings": {"spacing": "wide"}})
        self.assertIn("Spacing", page.sent[-1]["text"])


class CommandLineTest(Base):
    def test_main(self):
        make_project(self.src, [(20, 20, 10, [translate(50, 50)])])
        self.assertEqual(abh.main([self.src, "-o", self.out, "--spacing", "3",
                                   "--align-y", "on"]), 0)
        self.assertTrue(os.path.exists(self.out))


if __name__ == "__main__":
    unittest.main()
