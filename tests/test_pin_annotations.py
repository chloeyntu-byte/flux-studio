import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app
import emx_adapter
from pack_support import EXAMPLE_MODEL, install_example_pack


class PinAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.geometry = {'ports': [
            {'name': 'LEG_CUSTOM', 'layer': 'M1', 'bbox': [[10, 20], [14, 24]], 'kind': 'signal'},
            {'name': 'TAP', 'layer': 'M1', 'bbox': [[10, 21], [14, 25]], 'kind': 'tap'},
            {'name': 'GND_RING', 'layer': 'M2', 'bbox': [[80, 70], [84, 74]], 'kind': 'ground'},
        ]}

    def test_actual_regions_names_and_visibility(self):
        rows = app.pin_annotations(self.geometry, {}, lambda x, y: (x, y), 100, 120)
        self.assertEqual({r['name'] for r in rows}, {'LEG_CUSTOM', 'TAP', 'GND_RING'})
        self.assertEqual(next(r for r in rows if r['name'] == 'LEG_CUSTOM')['region'], (10, 20, 14, 24))
        hidden = app.pin_annotations(self.geometry, {'M1': False}, lambda x, y: (x, y), 100, 120)
        self.assertEqual([r['name'] for r in hidden], ['GND_RING'])

    def test_clipping_and_layer_colors(self):
        colors = app.pin_layer_colors(self.geometry)
        self.assertNotEqual(colors['M1'], colors['M2'])
        self.assertEqual(colors, app.pin_layer_colors(self.geometry))
        many = {'ports': [{'layer': 'custom{}'.format(i)} for i in range(30)]}
        self.assertEqual(len(set(app.pin_layer_colors(many).values())), 30)
        self.assertEqual(app.pin_annotations(self.geometry, {}, lambda x, y: (x + 200, y), 100, 120), [])
        # A partly visible real rectangle remains visible; its label is not clamped to the border.
        rows = app.pin_annotations(self.geometry, {}, lambda x, y: (x - 12, y), 100, 120)
        self.assertEqual(next(r for r in rows if r['name'] == 'LEG_CUSTOM')['point'][0], 0)

    def test_label_background_avoids_every_pin_rectangle(self):
        pin = {'region': (60, 60, 64, 64)}
        regions = [pin['region'], (50, 40, 75, 56)]
        dx, dy, box, frameless = app.pin_label_offset(pin, (50, 55, 74, 71), regions, [], 140, 140)
        self.assertFalse(frameless)
        self.assertEqual(box, (49 + dx, 54 + dy, 75 + dx, 72 + dy))
        for r in regions:
            self.assertTrue(box[2] <= r[0] - 2 or box[0] >= r[2] + 2 or
                            box[3] <= r[1] - 2 or box[1] >= r[3] + 2)
        # A pin filling the viewport still receives a transparent label.
        _, _, box, frameless = app.pin_label_offset(
            {'region': (-100, -100, 200, 200)}, (50, 55, 74, 71),
            [(-100, -100, 200, 200)], [], 140, 140)
        self.assertTrue(frameless)
        self.assertTrue(4 <= box[0] < box[2] <= 136 and 25 <= box[1] < box[3] <= 115)

    def test_label_at_clipped_edges_and_crowded_pins(self):
        regions = [(-5, 36, 5, 42), (5, 36, 11, 42), (91, 96, 105, 102)]
        labels = []
        for region in regions:
            x, y = (region[0] + region[2]) / 2, (region[1] + region[3]) / 2
            _, _, box, frameless = app.pin_label_offset(
                {'region': region}, (x - 8, y - 7, x + 8, y + 7), regions, labels, 100, 120)
            self.assertTrue(4 <= box[0] < box[2] <= 96 and 25 <= box[1] < box[3] <= 95)
            if not frameless:
                for r in regions:
                    self.assertTrue(box[2] <= r[0] - 2 or box[0] >= r[2] + 2 or
                                    box[3] <= r[1] - 2 or box[1] >= r[3] + 2)
            labels.append(box)

    def test_tiny_pin_near_footer_uses_clamped_visible_candidate(self):
        region = (50, 103, 54, 107)
        _, _, box, frameless = app.pin_label_offset(
            {'region': region}, (42, 98, 62, 112), [region], [], 100, 120)
        self.assertFalse(frameless)
        self.assertLessEqual(box[3], 95)
        self.assertTrue(box[2] <= region[0] - 2 or box[0] >= region[2] + 2 or
                        box[3] <= region[1] - 2 or box[1] >= region[3] + 2)

    def test_redraw_fills_each_actual_region_above_geometry_without_leaders(self):
        class Canvas:
            def __init__(self): self.items = []
            def winfo_width(self): return 100
            def winfo_height(self): return 120
            def delete(self, *args): pass
            def bbox(self, item): return (0, 0, 10, 10)
            def tag_lower(self, *args): pass
            def move(self, *args): pass
            def itemconfigure(self, *args, **kwargs): pass
            def __getattr__(self, name):
                if name.startswith('create_'):
                    def record(*args, **kwargs):
                        self.items.append((name, args, kwargs))
                        return len(self.items)
                    return record
                raise AttributeError(name)
        canvas = Canvas()
        geometry = dict(self.geometry, polygons=[{'layer': 'M1', 'points': [[0, 0], [3, 0], [3, 3]]}], vias=[])
        ui = SimpleNamespace(canvas=canvas, geometry=geometry, view=[0, 0, 100, 120],
                             scale=lambda: 1, xy=lambda x, y: (x, y), preview_library={},
                             layer_visible={'M2': False}, layer_color=lambda layer: '#aaaaaa',
                             geometry_design={}, models={}, dirty=False, preview_ok=True,
                             preview_revision=1, revision=1)
        app.TransformerApp.redraw(ui)
        ports = [(i, args, kw) for i, (kind, args, kw) in enumerate(canvas.items)
                 if kind == 'create_rectangle' and 'port' in kw.get('tags', ())]
        self.assertEqual([item[1] for item in ports], [(10, 20, 14, 24), (10, 21, 14, 25)])
        self.assertTrue(all(kw['fill'] == app.pin_layer_colors(geometry)['M1'] for _, _, kw in ports))
        polygon_index = next(i for i, item in enumerate(canvas.items) if item[0] == 'create_polygon')
        self.assertTrue(all(i > polygon_index for i, _, _ in ports))
        self.assertFalse(any('port_leader' in kw.get('tags', ()) for _, _, kw in canvas.items))
        labels = [kw for kind, _, kw in canvas.items if kind == 'create_text'
                  and 'port_label' in kw.get('tags', ())]
        self.assertTrue(labels)
        self.assertTrue(all(kw['font'] == ('liberation sans', -8) for kw in labels))

    def test_emx_source_uses_contract_for_applied_geometry(self):
        ui = SimpleNamespace(context_busy=False, preview_busy=False, generating=False,
                             dirty=False, applied_design={'model': 'test'}, preview_ok=True,
                             preview_revision=7, revision=7, preview_library={}, library={},
                             geometry=self.geometry)
        with patch('backend.capture_generation_context', return_value={'design': {'model': 'test'}}):
            source = app.TransformerApp.emx_source(ui)
        self.assertEqual(source['port_contract'], emx_adapter.port_contract(self.geometry))
        self.assertEqual(source['port_contract']['signals'], ['LEG_CUSTOM', 'TAP'])
        self.assertEqual(source['port_contract']['grounds'], ['GND_RING'])
        self.assertEqual([d['driven'] for d in source['port_contract']['definitions']], [True, True, False])
        ui.dirty = True
        with self.assertRaises(ValueError):
            app.TransformerApp.emx_source(ui)

    def test_real_geometry_models_and_ground_connection(self):
        import backend
        import geometry
        import structures
        from test_structures import profile
        tech = profile()
        install_example_pack(self)
        for model in ('differential_inductor', EXAMPLE_MODEL):
            for connected in (False, True):
                for tap in ((False, True) if model == 'differential_inductor' else (True,)):
                    with self.subTest(model=model, connected=connected, tap=tap):
                        params = {'pgs_connect_ring': connected, 'center_tap': tap,
                                  'ring_extent': 180, 'ring_width': 7}
                        layers = backend.layer_defaults(tech, model)
                        if model == EXAMPLE_MODEL:
                            result = backend.MODEL_BUILDERS[model](params, layers, tech, 'compact')
                        else:
                            result = structures.generate_model(params, layers, tech, model)
                        self.assertTrue(result['ok'], result['errors'])
                        masks = result['geometry']
                        ui = SimpleNamespace(context_busy=False, preview_busy=False, generating=False,
                            dirty=False, applied_design={'model': model}, preview_ok=True,
                            preview_revision=1, revision=1, preview_library={}, library={}, geometry=masks)
                        with patch('backend.capture_generation_context', return_value={'design': {'model': model}}):
                            contract = app.TransformerApp.emx_source(ui)['port_contract']
                        self.assertEqual(contract, emx_adapter.port_contract(masks))
                        self.assertEqual(set(contract['signals'] + contract['grounds']),
                                         {p['name'] for p in masks['ports']})
                        self.assertEqual(bool([p for p in masks['ports'] if p.get('kind') == 'tap']), tap)
                        for p in masks['ports']:
                            self.assertEqual(p['name'] in contract['grounds'], p['kind'] == 'ground')
                        rows = app.pin_annotations(masks, {}, lambda x, y: (x + 300, y + 300), 600, 600)
                        self.assertEqual(len(rows), len(masks['ports']))
                        layer = masks['ports'][0]['layer']
                        hidden = app.pin_annotations(masks, {layer: False}, lambda x, y: (x + 300, y + 300), 600, 600)
                        self.assertTrue(all(p['layer'] != layer for p in hidden))
                        self.assertEqual(len(hidden), sum(p['layer'] != layer for p in masks['ports']))

    def test_repeated_pin_regions_are_preserved(self):
        import copy
        masks = copy.deepcopy(self.geometry)
        other = copy.deepcopy(masks['ports'][0])
        other['bbox'] = [[15, 20], [19, 24]]
        masks['ports'].append(other)
        contract = emx_adapter.port_contract(masks)
        row = next(p for p in contract['terminals'] if p['name'] == 'LEG_CUSTOM')
        self.assertEqual([r['bbox'] for r in row['regions']], [[[10, 20], [14, 24]], [[15, 20], [19, 24]]])
        annotations = app.pin_annotations(masks, {}, lambda x, y: (x, y), 100, 120)
        self.assertEqual(len([p for p in annotations if p['name'] == 'LEG_CUSTOM']), 2)


if __name__ == '__main__':
    unittest.main()
