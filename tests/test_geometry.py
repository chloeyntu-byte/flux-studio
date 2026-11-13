import copy
import math
import json
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from geometry import DEFAULTS, PARAMETER_SCHEMA, polygon_distance, validate_geometry, _point_inside
from structures import generate_model


def technology():
    return {"grid": 0.005, "layers": [
        {"name": "M1", "order": 1, "min_width": 0.1, "min_spacing": 0.1, "min_area": 0.04},
        {"name": "M8", "order": 8, "min_width": 0.4, "min_spacing": 0.4},
        {"name": "M9", "order": 9, "min_width": 0.8, "min_spacing": 0.8},
        {"name": "AP", "order": 10, "min_width": 2, "min_spacing": 2},
    ], "vias": [
        {"name": "AP_M9", "lower": "M9", "upper": "AP", "cut_layer": "VIA9", "cut_size": [3, 3], "cut_spacing": [3, 3], "lower_enclosure": [1.5, 1.5], "upper_enclosure": [1.5, 1.5]},
        {"name": "M9_M8", "lower": "M8", "upper": "M9", "cut_layer": "VIA8", "cut_size": [0.4, 0.4], "cut_spacing": [0.4, 0.4], "lower_enclosure": [0.2, 0.2], "upper_enclosure": [0.2, 0.2]},
    ]}


# Common-engine contracts (PGS, ground ring, native vias, grid, rule screening,
# short detection) exercised through the built-in differential inductor.
LAYERS = {"body": "M8", "crossing": "M9", "tap": "M8", "pgs": "M1", "ring": "AP", "ground_route": "M1"}



def assert_grid_octilinear(test, result, tech):
    """Check each exported mask edge on integer DBU; union outlines and angle tolerances cannot hide seams."""
    grid = Decimal(str(tech['grid']))

    def integer_points(points, label):
        converted = []
        for point in points:
            integers = []
            for coordinate in point:
                units = Decimal(str(coordinate)) / grid
                test.assertEqual(units, units.to_integral_value(), (label, 'off-grid', point))
                integers.append(int(units))
            converted.append(integers)
        return converted

    def edges(points, closed, label):
        integers = integer_points(points, label)
        endpoints = integers[1:] + integers[:1] if closed else integers[1:]
        for a, b in zip(integers, endpoints):
            dx, dy = b[0] - a[0], b[1] - a[1]
            test.assertTrue(dx == 0 or dy == 0 or abs(dx) == abs(dy),
                            (label, 'non-octilinear final edge', a, b, (dx, dy)))

    for index, polygon in enumerate(result['geometry']['polygons']):
        label = (index, polygon['layer'], polygon['part'], polygon.get('feature'), polygon.get('net'))
        edges(polygon['points'], True, label)
        if polygon.get('centerline'):
            edges(polygon['centerline'], False, label + ('path',))
    for index, via in enumerate(result['geometry']['vias']):
        integer_points(via['bbox'], ('via', index, via.get('via_def')))
        integer_points([via['origin']], ('via origin', index))
    for port in result['geometry']['ports']:
        integer_points(port['bbox'], ('port', port['name']))


def stack_layers(tech):
    metals = sorted(tech['layers'], key=lambda rule: rule['order'])
    return dict(body=metals[-2]['name'], crossing=metals[-3]['name'], tap=metals[-4]['name'],
                pgs=metals[0]['name'], ring=metals[-1]['name'], ground_route=metals[1]['name'])


def kits():
    fixtures = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
    for kit in ('generic_d', 'generic_a', 'generic_b', 'generic_c'):
        with open(os.path.join(fixtures, kit + '.json')) as stream:
            yield kit, json.load(stream)


class GeometryTests(unittest.TestCase):
    def generate(self, parameters=None, tech=None, layers=None):
        return generate_model(parameters or {}, layers or LAYERS, tech or technology(), 'differential_inductor')

    def assert_ok(self, result):
        self.assertTrue(result["ok"], result["errors"])

    def codes(self, result):
        return {e["code"] for e in result["errors"]}

    def test_default_differential_inductor_is_one_continuous_winding_with_shields(self):
        result = self.generate()
        self.assert_ok(result)
        geometry = result["geometry"]
        self.assertEqual(geometry["metrics"]["winding_components"], {"L": 1})
        self.assertEqual({p["name"] for p in geometry["ports"]}, {"L1", "L2", "GND_PGS", "GND_RING"})
        self.assertEqual({p["part"] for p in geometry["polygons"]}, {"body", "crossing", "legs", "pgs", "ring"})
        self.assertEqual({p["net"] for p in geometry["polygons"] if p["part"] in ("pgs", "ring")}, {"GND"})

    def test_every_final_mask_and_path_is_octilinear_for_all_shared_shapes(self):
        for kit, tech in kits():
            layers = stack_layers(tech)
            for pgs in ('square', 'octagon', 'circle'):
                for ring in ('square', 'octagon'):
                    with self.subTest(kit=kit, pgs=pgs, ring=ring):
                        result = self.generate({'pgs_shape': pgs, 'ring_shape': ring,
                                                'pgs_connect_ring': True, 'ring_width': 7}, tech=tech, layers=layers)
                        self.assert_ok(result)
                        assert_grid_octilinear(self, result, tech)
                        self.assertEqual(result['geometry']['metrics']['winding_components'], {'L': 1})

    def test_fractional_parameters_keep_exact_grid_directions_and_mirrors(self):
        for parameters in ({'inner_diameter': 110.03}, {'width': 9.01, 'spacing': 9.01},
                           {'terminal_gap': 20.01, 'leg_length': 45.03},
                           {'pgs_extent': 95.03, 'pgs_width': .41, 'pgs_spine_width': .81,
                            'ring_shape': 'octagon', 'ring_width': 5.01}):
            for turns in (1, 3):
                with self.subTest(parameters=parameters, turns=turns):
                    result = self.generate(dict(parameters, turns=turns))
                    self.assert_ok(result)
                    assert_grid_octilinear(self, result, technology())
                    arcs = [p for p in result['geometry']['polygons'] if p.get('feature') == 'symmetric_half_turn']
                    shapes = {frozenset(tuple(xy) for xy in polygon['points']) for polygon in arcs}
                    for polygon in arcs:
                        self.assertIn(frozenset((-x, y) for x, y in polygon['points']), shapes)

    def test_pgs_and_winding_mirror_on_the_manufacturing_grid(self):
        result = self.generate()
        self.assert_ok(result)
        arcs = [p for p in result["geometry"]["polygons"] if p.get("feature") == "symmetric_half_turn"]
        arc_sets = [frozenset(tuple(xy) for xy in p["points"]) for p in arcs]
        for poly in arcs:
            self.assertIn(frozenset((-x, y) for x, y in poly["points"]), arc_sets)
        pgs = [p for p in result["geometry"]["polygons"] if p["part"] == "pgs"]
        sets = [frozenset(tuple(xy) for xy in p["points"]) for p in pgs]
        for poly in pgs:
            self.assertIn(frozenset((-x, y) for x, y in poly["points"]), sets)
            self.assertIn(frozenset((x, -y) for x, y in poly["points"]), sets)

    def test_missing_via_and_layer_order_are_rejected(self):
        tech = technology()
        tech["vias"] = []
        result = self.generate(tech=tech)
        self.assertFalse(result["ok"])
        self.assertIn("no_native_via_chain", self.codes(result))
        result = self.generate(layers=dict(LAYERS, pgs="AP"))
        self.assertFalse(result["ok"])
        self.assertIn("pgs_layer_order", self.codes(result))

    def test_native_via_array_uses_real_array_spacing(self):
        tech = technology()
        tech["vias"][1].update(cut_size=[1, 1], cut_spacing=[0.2, 0.2], array_cut_spacing=[2, 2])
        result = self.generate(tech=tech)
        self.assert_ok(result)
        via = next(v for v in result["geometry"]["vias"] if v["via_def"] == "M9_M8")
        self.assertEqual(via["cut_spacing"], [2, 2])
        self.assertGreater(via["rows"] * via["columns"], 1)
        # The same footprint with the plain (denser) spacing holds more cuts.
        dense = technology()
        dense["vias"][1].update(cut_size=[1, 1], cut_spacing=[0.2, 0.2])
        plain = next(v for v in self.generate(tech=dense)["geometry"]["vias"] if v["via_def"] == "M9_M8")
        self.assertGreaterEqual(plain["rows"] * plain["columns"], via["rows"] * via["columns"])

    def test_native_cut_resources_are_rejected_without_underfilling(self):
        tech = technology()
        tiny = {"cut_size": [0.01, 0.01], "cut_spacing": [0.01, 0.01], "lower_enclosure": [0.005, 0.005], "upper_enclosure": [0.005, 0.005]}
        tech["vias"][1].update(tiny)
        result = self.generate(tech=tech)
        self.assertFalse(result["ok"])
        self.assertIn("array_too_large", self.codes(result))
        self.assertTrue(result["geometry"]["vias"])

    def test_multilevel_vias_have_intermediate_metal_and_continuity(self):
        result = self.generate({"width": 14, "spacing": 20, "inner_diameter": 200, "ring_extent": 300},
                               layers=dict(LAYERS, body="M8", crossing="AP", ring="AP"))
        self.assert_ok(result)
        self.assertGreaterEqual(len(result["geometry"]["vias"]), 2)
        self.assertTrue(any(p["layer"] == "M9" and p.get("landing") for p in result["geometry"]["polygons"]))
        self.assertEqual(result["geometry"]["metrics"]["winding_components"], {"L": 1})

    def test_unmanufacturable_dimensions_are_rejected(self):
        for params in ({"inner_diameter": 29}, {"width": 0.05}, {"spacing": 0.01}, {"terminal_gap": 250},
                       {"pgs_width": 3}, {"leg_length": float("nan")}, {"turns": 9}, {"turns": 1.5}):
            self.assertFalse(self.generate(params)["ok"], params)

    def test_width_dependent_spacing_table_is_applied(self):
        tech = technology()
        tech["layers"][1]["spacing_tables"] = [{"axes": ["width", "length"], "default": 0.4, "entries": [{"threshold": [4, 20], "value": 12}]}]
        result = self.generate({"rule_policy": "strict"}, tech=tech)
        self.assertFalse(result["ok"])
        self.assertTrue({"turn_spacing", "terminal_spacing", "metal_min_spacing"} & self.codes(result), result["errors"])

    def test_crossing_via_offset_is_rejected(self):
        tech = technology()
        tech["vias"][1]["upper_offset"] = [0.1, 0]
        result = self.generate(tech=tech)
        self.assertFalse(result["ok"])
        self.assertIn("unsupported_via_offset", self.codes(result))

    def test_pgs_spine_independent_width_and_auto_follow(self):
        result = self.generate({"pgs_spine_width": 0.8})
        self.assert_ok(result)
        pgs = result["geometry"]["polygons"]
        self.assertEqual({p["width"] for p in pgs if p.get("feature") == "spine"}, {0.8})
        self.assertEqual({p["width"] for p in pgs if p.get("feature") == "arm"}, {0.4})
        result = self.generate({"pgs_width": 0.6, "pgs_spine_width": ""})
        self.assert_ok(result)
        self.assertIsNone(result["parameters"]["pgs_spine_width"])
        self.assertEqual(result["geometry"]["metrics"]["pgs_spine_width"], 0.6)

    def test_exact_metal_width_boundaries_need_grid_margin(self):
        tech = technology()
        tech["layers"][0]["min_diagonal_width"] = 0.19
        self.assertFalse(self.generate({"pgs_width": 0.19, "rule_policy": "strict"}, tech=tech)["ok"])
        tech = technology()
        tech["layers"][1]["max_width"] = 8
        result = self.generate({"width": 8, "rule_policy": "strict"}, tech=tech)
        self.assertFalse(result["ok"])
        self.assert_ok(self.generate({"width": 7.99, "rule_policy": "strict"}, tech=tech))

    def test_small_real_gap_does_not_merge_connectivity(self):
        geometry = {"polygons": [{"points": [[0, 0], [1, 0], [1, 1], [0, 1]], "layer": "M9", "part": "body", "net": "P"},
                                  {"points": [[1.005, 0], [2.005, 0], [2.005, 1], [1.005, 1]], "layer": "M9", "part": "body", "net": "P"},
                                  {"points": [[10, 0], [11, 0], [11, 1], [10, 1]], "layer": "M9", "part": "body", "net": "S"}], "vias": []}
        errors, components = validate_geometry(geometry, technology())
        self.assertEqual(components["P"], 2)
        self.assertIn("disconnected_winding", {e["code"] for e in errors})

    def test_ring_gap_is_exact_and_same_layer_long_legs_are_rejected(self):
        for metal in ("AP", "M1"):
            result = self.generate({"leg_length": 100}, layers=dict(LAYERS, ring=metal))
            self.assert_ok(result)
            metrics = result["geometry"]["metrics"]
            self.assertAlmostEqual(metrics["ring_inner_radius"], 103)
            self.assertGreater(metrics["actual_ring_body_gap"], 0)
        result = self.generate({"leg_length": 100}, layers=dict(LAYERS, ring="M8"))
        self.assertFalse(result["ok"])
        self.assertIn("same_layer_short", self.codes(result))
        self.assertTrue(all("ring_extent" in e["fields"] for e in result["errors"] if e["part"] == "ring"))
        self.assert_ok(self.generate({"leg_length": 100, "ring_extent": 200}, layers=dict(LAYERS, ring="M8")))

    def test_removed_via_and_deliberate_short_are_detected(self):
        result = self.generate()
        geometry = copy.deepcopy(result["geometry"])
        geometry["vias"] = []
        errors, components = validate_geometry(geometry, technology())
        self.assertGreater(components["L"], 1)
        self.assertIn("disconnected_winding", {e["code"] for e in errors})
        geometry = copy.deepcopy(result["geometry"])
        bad = copy.deepcopy(next(p for p in geometry["polygons"] if p["part"] == "body"))
        bad["net"] = "X"
        geometry["polygons"].append(bad)
        errors, _ = validate_geometry(geometry, technology())
        self.assertIn("same_layer_short", {e["code"] for e in errors})

    def test_missing_via_landing_coverage_is_detected(self):
        result = self.generate()
        geometry = copy.deepcopy(result["geometry"])
        # Move the first native via off all landings.
        geometry["vias"][0]["origin"] = [0, 0]
        errors, _ = validate_geometry(geometry, technology())
        self.assertIn("via_coverage", {e["code"] for e in errors})

    def test_connector_label_does_not_exempt_arbitrary_or_separate_pad_spacing(self):
        tech = technology()

        def rectangle(x0, x1, **metadata):
            polygon = {'points': [[x0, 0], [x1, 0], [x1, 1], [x0, 1]],
                       'layer': 'M9', 'net': 'P', 'part': 'crossing', 'width': 1,
                       'connector_id': 'same-label'}
            polygon.update(metadata)
            return polygon

        # A label by itself does not establish a shared strip/pad envelope.
        cases = (({}, {}), ({'landing': True}, {'landing': True}),
                 ({'path_piece': 'segment'}, {}))
        for first_metadata, second_metadata in cases:
            with self.subTest(first=first_metadata, second=second_metadata):
                geometry = {'polygons': [rectangle(0, 1, **first_metadata),
                                         rectangle(1.1, 2.1, **second_metadata)], 'vias': []}
                self.assertAlmostEqual(polygon_distance(geometry['polygons'][0]['points'],
                                                       geometry['polygons'][1]['points']), .1)
                errors, _ = validate_geometry(geometry, tech, expected_windings=())
                self.assertIn('same_net_spacing', {error['code'] for error in errors})
        # Even the precise strip/landing metadata must never suppress a
        # different-net physical contact merely because the label matches.
        geometry = {'polygons': [rectangle(0, 1, landing=True),
                                 rectangle(.5, 1.5, path_piece='segment', net='S')], 'vias': []}
        errors, _ = validate_geometry(geometry, tech, expected_windings=())
        self.assertIn('same_layer_short', {error['code'] for error in errors})

    def test_native_via_landings_keep_square_shared_overlap_and_full_arrays(self):
        tech = technology()
        for via in tech['vias']:
            via.update(cut_size=[.1, .1], cut_spacing=[.1, .1], lower_enclosure=[.05, .05], upper_enclosure=[.05, .05])
        for parameters in ({}, {'width': 1, 'spacing': 1, 'terminal_gap': 1}, {'width': 10, 'turns': 3}):
            with self.subTest(parameters=parameters):
                result = self.generate(dict(parameters, rule_policy='warn'), tech=tech)
                self.assert_ok(result)
                polygons = result['geometry']['polygons']
                vias = [via for via in result['geometry']['vias'] if via['part'] == 'crossing']
                self.assertTrue(vias)
                for via in vias:
                    self.assertGreaterEqual(via['columns'], 1)
                    self.assertGreaterEqual(via['rows'], 1)
                    paired = []
                    for layer, enclosure in ((via['lower'], via['lower_enclosure']),
                                             (via['upper'], via['upper_enclosure'])):
                        x, y = via['origin']
                        cut_w = via['columns'] * via['cut_size'][0] + (via['columns'] - 1) * via['cut_spacing'][0]
                        cut_h = via['rows'] * via['cut_size'][1] + (via['rows'] - 1) * via['cut_spacing'][1]
                        corners = [(x + sx * (cut_w / 2 + enclosure[0]), y + sy * (cut_h / 2 + enclosure[1]))
                                   for sx in (-1, 1) for sy in (-1, 1)]
                        landings = [poly for poly in polygons if poly.get('landing') and
                                    poly['layer'] == layer and poly['net'] == via['net'] and
                                    all(_point_inside(point, poly['points']) for point in corners)]
                        self.assertEqual(len(landings), 1, (via, layer))
                        xs = [point[0] for point in landings[0]['points']]
                        ys = [point[1] for point in landings[0]['points']]
                        width, height = max(xs) - min(xs), max(ys) - min(ys)
                        self.assertAlmostEqual(width, height, delta=tech['grid'])
                        self.assertGreaterEqual(min(width, height) + 1e-8, result['parameters']['width'])
                        # Native cuts fill the real shared square; no array is silently reduced.
                        expected_cols = int(math.floor((width - 2 * enclosure[0] + via['cut_spacing'][0] + 1e-8) /
                                                       (via['cut_size'][0] + via['cut_spacing'][0])))
                        expected_rows = int(math.floor((height - 2 * enclosure[1] + via['cut_spacing'][1] + 1e-8) /
                                                       (via['cut_size'][1] + via['cut_spacing'][1])))
                        self.assertEqual((via['columns'], via['rows']), (expected_cols, expected_rows))
                        paired.append(frozenset(tuple(point) for point in landings[0]['points']))
                    self.assertEqual(paired[0], paired[1])

    def test_square_pgs_center_and_outline(self):
        result = self.generate({"pgs_shape": "square"})
        self.assert_ok(result)
        center = next(p for p in result["geometry"]["polygons"] if p.get("feature") == "center")
        self.assertEqual(center["points"], [[-1, -1], [1, -1], [1, 1], [-1, 1]])
        self.assertGreater(result["geometry"]["metrics"]["pgs_arm_count"], 112)

    def test_pgs_physical_extent_is_stable_with_independent_widths(self):
        for shape in ("square", "octagon"):
            for extent in (95, 95.03):
                for arm_width, spine_width in ((0.4, 0.4), (0.4, 2), (1.2, 0.4), (1.2, 2)):
                    result = self.generate({"pgs_shape": shape, "pgs_extent": extent, "pgs_width": arm_width, "pgs_spine_width": spine_width})
                    self.assert_ok(result)
                    actual = result["geometry"]["metrics"]["actual_pgs_extent"]
                    self.assertLessEqual(actual, extent + 0.005)
                    self.assertLessEqual(abs(actual - extent), 0.1)
                    points = [xy for p in result["geometry"]["polygons"] if p["part"] == "pgs" for xy in p["points"]]
                    self.assertTrue(all(abs(x) <= extent + 0.005 and abs(y) <= extent + 0.005 for x, y in points))
                    if shape == "octagon":
                        self.assertTrue(all(abs(x) + abs(y) <= math.sqrt(2) * extent + 0.01 for x, y in points))

    def test_circle_pgs_and_octagonal_ring_obey_physical_outlines(self):
        result = self.generate({"pgs_shape": "circle", "ring_shape": "octagon", "pgs_spine_width": 0.8})
        self.assert_ok(result)
        self.assertTrue(all(math.hypot(x, y) <= 95.01 for p in result["geometry"]["polygons"] if p["part"] == "pgs" for x, y in p["points"]))
        ring = [p for p in result["geometry"]["polygons"] if p["part"] == "ring"]
        self.assertGreaterEqual(len(ring), 4)
        assert_grid_octilinear(self, result, technology())
        self.assertTrue(all(not _point_inside([0, 0], polygon['points']) for polygon in ring))
        area = sum(abs(sum(a[0] * b[1] - b[0] * a[1]
                           for a, b in zip(polygon['points'], polygon['points'][1:] + polygon['points'][:1]))) / 2
                   for polygon in ring)
        expected_area = 8 * (math.sqrt(2) - 1) * (108 ** 2 - 103 ** 2)
        self.assertAlmostEqual(area, expected_area, delta=8 * 108 * technology()['grid'])
        sets = [frozenset(tuple(xy) for xy in p["points"]) for p in ring]
        for poly in ring:
            self.assertIn(frozenset((-x, y) for x, y in poly["points"]), sets)
        for x, y in [xy for p in ring for xy in p["points"]]:
            self.assertLessEqual(max(abs(x), abs(y)), 108.005)
            self.assertLessEqual(abs(x) + abs(y), 108 * math.sqrt(2) + 0.01)

    def test_shared_physical_extents_stay_fixed_and_legacy_imports_once(self):
        for diameter in (110, 125):
            result = self.generate({"inner_diameter": diameter, "ring_extent": 103})
            self.assert_ok(result)
            self.assertEqual(result["geometry"]["metrics"]["ring_inner_radius"], 103)
            self.assertEqual(result["parameters"]["pgs_extent"], 95)
        old = self.generate({"ring_gap": 35})
        self.assert_ok(old)
        extent = old["geometry"]["metrics"]["body_extent"]
        self.assertAlmostEqual(old["parameters"]["ring_extent"], extent + 35)
        saved = dict(old["parameters"], inner_diameter=125)
        restored = self.generate(saved)
        self.assert_ok(restored)
        self.assertEqual(restored["parameters"]["ring_extent"], old["parameters"]["ring_extent"])
        self.assertNotIn("ring_gap", {p["name"] for p in PARAMETER_SCHEMA})

    def test_numeric_bool_and_ground_boolean_types_are_rejected(self):
        for params in ({"width": True}, {"pgs_extent": True}, {"ring_extent": False},
                       {"pgs_connect_ring": True, "ground_route_width": True}, {"pgs_connect_ring": 1}):
            self.assertFalse(self.generate(params)["ok"], params)

    def test_four_synthetic_stacks_default_and_two_endpoint_ground_vias(self):
        for kit, tech in kits():
            layers = stack_layers(tech)
            with self.subTest(kit=kit):
                self.assert_ok(self.generate(tech=tech, layers=layers))
                result = self.generate({"pgs_connect_ring": True, "ring_width": 7}, tech=tech, layers=layers)
                self.assert_ok(result)
                vias = [v for v in result["geometry"]["vias"] if v["part"] == "pgs"]
                self.assertEqual({v["origin"][0] for v in vias}, {0})
                self.assertEqual({v["origin"][1] for v in vias}, {0, 106.5})
                self.assertTrue(all(v["rows"] > 0 and v["columns"] >= 2 for v in vias))

    def test_ground_layer_conflicts_and_endpoint_fit_are_actionable(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "generic_d.json")) as handle:
            tech = json.load(handle)
        layers = stack_layers(tech)
        metals = [rule["name"] for rule in sorted(tech["layers"], key=lambda rule: rule["order"])]
        for layer in (layers["pgs"], layers["body"], layers["crossing"]):
            result = self.generate({"pgs_connect_ring": True, "ring_width": 7}, tech=tech, layers=dict(layers, ground_route=layer))
            self.assertIn("ground_route_conflict", self.codes(result), layer)
        result = self.generate({"pgs_connect_ring": True, "ring_width": 7}, tech=tech, layers=dict(layers, ground_route=layers["ring"]))
        self.assertIn("ground_route_ring_same_layer", self.codes(result))
        # A routing metal used only by an intermediate body via landing is
        # forbidden too, even if no winding role names it directly.
        result = self.generate({"pgs_connect_ring": True, "ring_width": 7}, tech=tech,
                               layers=dict(layers, body=metals[-4], crossing=metals[-2], tap=metals[-5], ground_route=metals[-3]))
        self.assertIn("ground_route_conflict", self.codes(result))
        result = self.generate({"pgs_connect_ring": True, "ring_width": 7}, tech=tech,
                               layers=dict(layers, ring=metals[2], ground_route=metals[2]))
        self.assertIn("ground_route_ring_same_layer", self.codes(result))
        result = self.generate({"pgs_connect_ring": True}, tech=tech, layers=dict(layers, ground_route=metals[1]))
        self.assertIn("ring_via_fit", self.codes(result))
        error = next(e for e in result["errors"] if e["code"] == "ring_via_fit")
        self.assertIn("ring", error["message"].lower())
        self.assertIn("ring_width", error["fields"])


if __name__ == "__main__":
    unittest.main()
