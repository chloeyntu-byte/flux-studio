"""Schema-driven passive-model UI behavior without an X server or OA writes."""
import copy
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import app
import backend
from pack_support import EXAMPLE_MODEL, install_example_pack

try:
    from test_ui_rule_warnings import ui, library, Variable
except ImportError:
    from tests.test_ui_rule_warnings import ui, library, Variable


def models():
    shared_body = [
        dict(name='inner_diameter', label='Inner opening diameter', part='body',
             default=100, min=30, max=1000),
        dict(name='width', label='Winding width', part='body', default=8, min=.1, max=100),
        dict(name='turns', label='Turns', part='body', default=2, min=1, max=8, integer=True),
        dict(name='rule_policy', label='PDK rule checking', part='body',
             default='warn', options=['warn', 'strict']),
    ]
    result = {}
    # Schema-driven stand-ins: the UI must not branch on model names, so the
    # generic models here are synthetic and neutral.
    for model, title, tap, secondary in (
            ('synthetic_flat', 'Synthetic flat', False, False),
            ('synthetic_half_turn', 'Synthetic half-turn spiral', True, False),
            ('differential_inductor', 'Symmetric differential', True, False),
            ('synthetic_secondary', 'Synthetic with secondary', False, True)):
        schema = copy.deepcopy(shared_body)
        if model == 'synthetic_half_turn':
            next(field for field in schema if field['name'] == 'turns').update(integer=False, step=.5, unit='turns')
        if tap:
            schema.append(dict(name='center_tap', label='Include center TAP',
                               part='body', type='boolean', default=False))
        roles = ['body', 'crossing'] + (['tap'] if tap else []) + (['secondary'] if secondary else [])
        result[model] = dict(id=model, title=title, description='Schema-driven topology.',
                             parameter_schema=schema,
                             defaults={field['name']: field['default'] for field in schema},
                             layer_roles=roles)
    return result


def passive_ui(model='synthetic_half_turn'):
    entry = library(warnings=[])
    for name in models():
        entry['model_default_layers'][name] = dict(body='M4', crossing='M3', tap='M2',
            secondary='M3', pgs='M1', ring='M4', ground_route='M2')
    controller = ui([entry])
    controller.models = models()
    controller.current_model = model
    controller.model_selection = Variable(controller.models[model]['title'])
    controller.vars['model'].set(model)
    controller._render_body_fields()
    controller.configure_library('ExampleDesign', apply_defaults=True)
    return controller


class PassiveModelUITests(unittest.TestCase):
    def test_boolean_values_from_real_checkboxes_and_string_variables_are_typed(self):
        for value in (False, 0, '0', 'false', 'FALSE', 'off', ' no '):
            self.assertIs(app.boolean_value(value), False, value)
        for value in (True, 1, '1', 'true', 'TRUE', 'on', ' yes '):
            self.assertIs(app.boolean_value(value), True, value)
        for value in ('', 'unexpected', 2, None, []):
            self.assertIsNone(app.boolean_value(value), value)

    def test_dynamic_boolean_variables_use_the_controller_interpreter_and_false_default(self):
        controller = app.TransformerApp.__new__(app.TransformerApp)
        controller.root = tk.Tcl()
        controller.vars = {}
        controller._changed = Mock()
        controller.ensure_variable('center_tap', 'false', 'bool')
        self.assertIsInstance(controller.vars['center_tap'], tk.BooleanVar)
        self.assertIs(controller.vars['center_tap'].get(), False)
        controller.vars['center_tap'].set(True)
        controller._changed.assert_called_once()
        controller.ensure_variable('rule_policy', 'warn', 'enum')
        self.assertEqual(controller.vars['rule_policy'].get(), 'warn')

    def test_schema_handles_boolean_integer_and_policy_enum_without_model_name_branches(self):
        controller = passive_ui('differential_inductor')
        schema = controller.schema_for()
        self.assertEqual(schema['center_tap']['type'], 'bool')
        self.assertEqual(schema['turns']['type'], 'int')
        self.assertEqual(schema['rule_policy']['type'], 'enum')
        self.assertEqual(schema['rule_policy']['options'], ['warn', 'strict'])

    def test_fractional_turns_remain_float_across_integer_differential_model_switches(self):
        controller = passive_ui('synthetic_half_turn')
        controller.vars['turns'].set('1.5')
        self.assertEqual(controller.schema_for()['turns']['type'], 'float')
        self.assertEqual(controller.collect_design()['parameters']['turns'], 1.5)
        self.assertEqual(controller.local_errors(controller.collect_design()), [])
        controller.switch_model('differential_inductor')
        self.assertEqual(controller.schema_for()['turns']['type'], 'int')
        controller.vars['turns'].set('1.5')
        self.assertTrue(any(error['fields'] == ['turns'] for error in controller.local_errors(controller.collect_design())))
        controller.vars['turns'].set('3')
        self.assertIsInstance(controller.collect_design()['parameters']['turns'], int)
        controller.switch_model('synthetic_half_turn')
        self.assertEqual(controller.collect_design()['parameters']['turns'], 1.5)

    def test_tap_off_does_not_require_a_routing_metal_or_export_it(self):
        controller = passive_ui()
        controller.vars['tap'].set('deleted_layer')
        controller.vars['center_tap'].set('false')
        design = controller.collect_design()
        self.assertIs(design['parameters']['center_tap'], False)
        self.assertNotIn('tap', design['layers'])
        self.assertEqual(controller.local_errors(design), [])
        controller.update_controls()
        self.assertEqual(controller.field_widgets['tap'].options['state'], 'disabled')

    def test_tap_on_requires_a_real_selected_metal_and_retains_a_distinct_terminal_role(self):
        controller = passive_ui()
        controller.vars['center_tap'].set('1')
        controller.vars['tap'].set('deleted_layer')
        design = controller.collect_design()
        self.assertIs(design['parameters']['center_tap'], True)
        self.assertEqual(design['layers']['tap'], 'deleted_layer')
        self.assertTrue(any(error['fields'] == ['tap'] for error in controller.local_errors(design)))
        controller.vars['tap'].set('M2')
        self.assertEqual(controller.local_errors(controller.collect_design()), [])
        controller.update_controls()
        self.assertEqual(controller.field_widgets['tap'].options['state'], 'readonly')
        self.assertEqual(app.PARTS['tap'], 'Center TAP')

    def test_invalid_boolean_or_policy_is_located_without_silent_truth_coercion(self):
        controller = passive_ui()
        controller.vars['center_tap'].set('unexpected')
        controller.vars['rule_policy'].set('disabled')
        design = controller.collect_design()
        self.assertIsNone(design['parameters']['center_tap'])
        fields = [error['fields'] for error in controller.local_errors(design)]
        self.assertIn(['center_tap'], fields)
        self.assertIn(['rule_policy'], fields)

    def test_tap_autofallback_avoids_winding_underpass_and_pgs_metals(self):
        controller = passive_ui()
        controller.library['model_default_layers']['synthetic_half_turn'].pop('tap')
        controller.vars['tap'].set('missing')
        controller.vars['body'].set('M4')
        controller.vars['crossing'].set('M3')
        controller.vars['pgs'].set('M1')
        self.assertEqual(controller._preferred_layer('tap'), 'M2')
        controller.library['layers'] = [layer for layer in controller.library['layers'] if layer['name'] != 'M2']
        self.assertEqual(controller._preferred_layer('tap'), '')

    def test_all_generic_model_drafts_restore_tap_policy_and_keep_shield_parameters(self):
        controller = passive_ui()
        controller.vars['center_tap'].set(True)
        controller.vars['tap'].set('M2')
        controller.vars['rule_policy'].set('strict')
        controller.vars['turns'].set(3)
        controller.vars['pgs_extent'].set('83.5')
        controller.vars['ring_extent'].set('141')
        shared = {key: controller.vars[key].get() for key in app.SHARED_KEYS + app.SHARED_ROLES}
        for model in ('differential_inductor', 'synthetic_flat', 'synthetic_secondary'):
            controller.switch_model(model)
            self.assertEqual(controller.local_errors(controller.collect_design()), [])
            self.assertEqual({key: controller.vars[key].get() for key in shared}, shared)
        controller.switch_model('synthetic_half_turn')
        self.assertIs(controller.collect_design()['parameters']['center_tap'], True)
        self.assertEqual(controller.vars['tap'].get(), 'M2')
        self.assertEqual(controller.vars['rule_policy'].get(), 'strict')
        self.assertEqual(controller.vars['turns'].get(), 3)
        self.assertTrue(controller.dirty)
        controller.submit.assert_not_called()

    def test_tap_draft_change_does_not_replace_applied_geometry_before_apply(self):
        controller = passive_ui()
        controller.apply_parameters()
        snapshot = copy.deepcopy(controller.applied_design)
        controller._result(1, 'preview', controller.revision, True, dict(ok=True, errors=[], warnings=[],
            geometry=dict(polygons=[dict(layer='M4', points=[[0, 0], [1, 0], [1, 1], [0, 1]])], bounds=[[0, 0], [1, 1]])))
        controller.vars['center_tap'].set(True)
        controller.invalidate()
        self.assertEqual(controller.applied_design, snapshot)
        self.assertEqual(controller.generate_button.options['state'], 'disabled')
        controller.submit.reset_mock()
        controller.apply_parameters()
        self.assertTrue(controller.applied_design['parameters']['center_tap'])
        self.assertEqual(controller.applied_design['layers']['tap'], 'M2')
        self.assertEqual(controller.submit.call_args[0][0], 'preview')

    def test_policy_warning_allows_generate_but_real_via_error_does_not(self):
        controller = passive_ui()
        controller.vars['center_tap'].set(True)
        controller.apply_parameters()
        warning = dict(part='body', code='metal_width', message='Known PDK width limit exceeded.', fields=['width'])
        payload = dict(ok=True, errors=[], warnings=[warning],
            geometry=dict(polygons=[dict(layer='M4', points=[[0, 0], [1, 0], [1, 1], [0, 1]])], bounds=[[0, 0], [1, 1]]))
        controller._result(1, 'preview', controller.revision, True, payload)
        self.assertEqual(controller.generate_button.options['state'], 'normal')
        self.assertTrue(any(row['tags'] == ('warning',) for row in controller.error_tree.rows.values()))
        payload.update(ok=False, errors=[dict(part='tap', code='via_coverage', message='TAP native via has no full enclosure.', fields=['tap'])])
        controller._result(1, 'preview', controller.revision, True, payload)
        self.assertEqual(controller.generate_button.options['state'], 'disabled')

    def test_new_defaults_load_from_old_v2_files_without_weakening_other_required_fields(self):
        controller = passive_ui()
        design = controller.collect_design()
        design['parameters'].pop('center_tap')
        design['parameters'].pop('rule_policy')
        source = dict(schema='tfgen.design.v2', design=design)
        with patch('backend.normalize_design', side_effect=lambda value: copy.deepcopy(value)):
            normalized = controller.validate_imported(source)
        self.assertIs(normalized['parameters']['center_tap'], False)
        self.assertEqual(normalized['parameters']['rule_policy'], 'warn')
        self.assertNotIn('center_tap', source['design']['parameters'])
        source['design']['parameters'].pop('turns')
        with self.assertRaisesRegex(ValueError, 'turns'):
            controller.validate_imported(source)

    def test_import_does_not_silently_accept_a_string_tap_boolean(self):
        controller = passive_ui()
        design = controller.collect_design()
        design['parameters']['center_tap'] = 'false'
        with patch('backend.normalize_design', side_effect=lambda value: copy.deepcopy(value)):
            with self.assertRaisesRegex(ValueError, 'boolean'):
                controller.validate_imported(dict(schema='tfgen.design.v2', design=design))

    def test_tap_layer_has_a_distinct_preview_color_and_frozen_controls(self):
        controller = passive_ui()
        controller.geometry_design = dict(layers=dict(body='M4', crossing='M3', tap='M2'))
        self.assertEqual(controller.layer_color('M2'), '#e6a477')
        self.assertNotEqual(controller.layer_color('M2'), controller.layer_color('M4'))
        controller.vars['center_tap'].set(True)
        controller.generating = True
        controller.update_controls()
        self.assertEqual(controller.field_widgets['tap'].options['state'], 'disabled')


def pack_ui():
    """Controller holding the real model definitions, including the example pack's."""
    entry = library(warnings=[])
    entry['model_default_layers'] = {}
    controller = ui([entry])
    controller.models = {item['id']: item for item in backend.models()}
    controller.current_model = 'differential_inductor'
    controller.model_selection = Variable(controller.models['differential_inductor']['title'])
    controller.vars['model'].set('differential_inductor')
    controller.configure_library('ExampleDesign', apply_defaults=True)
    return controller


class StructurePackModelUITests(unittest.TestCase):
    def setUp(self):
        install_example_pack(self)

    def test_default_model_has_no_lead_mode_but_pack_models_expose_their_variants(self):
        controller = pack_ui()
        self.assertEqual(app.DEFAULT_MODEL, 'differential_inductor')
        self.assertEqual(controller.model_structures(), [])
        self.assertEqual(controller.collect_design()['structure'], 'layout')
        self.assertEqual(controller.model_structures(EXAMPLE_MODEL),
                         [('compact', 'Compact leads'), ('wide', 'Wide leads')])
        controller.switch_model(EXAMPLE_MODEL)
        self.assertEqual(controller.vars['structure'].get(), 'compact')
        self.assertEqual(controller.collect_design()['structure'], 'compact')
        controller.vars['structure'].set('wide')
        design = controller.collect_design()
        self.assertEqual(design['structure'], 'wide')
        self.assertEqual(controller.local_errors(design), [])
        design['structure'] = 'layout_unknown'
        self.assertTrue(any(error['fields'] == ['structure'] for error in controller.local_errors(design)))

    def test_variant_choice_survives_model_round_trip_and_resets_for_models_without_variants(self):
        controller = pack_ui()
        controller.switch_model(EXAMPLE_MODEL)
        controller.vars['structure'].set('wide')
        controller.switch_model('differential_inductor')
        self.assertEqual(controller.vars['structure'].get(), 'layout')
        controller.switch_model(EXAMPLE_MODEL)
        self.assertEqual(controller.vars['structure'].get(), 'wide')
        self.assertEqual(app.TransformerApp._structure_value(controller, EXAMPLE_MODEL, 'not-a-variant'), 'compact')

    def test_layer_defaults_follow_the_models_declared_positions(self):
        controller = pack_ui()
        controller.switch_model(EXAMPLE_MODEL)
        names = [layer['name'] for layer in controller.library['layers']]
        controller.library['default_layers'] = {}
        controller.library['model_default_layers'] = {}
        controller.vars['body'].set('')
        controller.vars['crossing'].set('')
        self.assertEqual(controller._preferred_layer('body'), names[-2])
        self.assertEqual(controller._preferred_layer('crossing'), names[-3])

    def test_lead_mode_buttons_are_rebuilt_from_the_selected_model(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest('No usable Tk display: %s' % exc)
        self.addCleanup(root.destroy)
        root.withdraw()
        from tkinter import ttk
        controller = pack_ui()
        controller.root = root
        controller.structure_frame = ttk.Frame(root)
        controller.structure_label = ttk.Label(root, text='Lead mode')
        controller.structure_label.grid()
        controller.vars['structure'] = tk.StringVar(root, value='compact')
        controller.structure_buttons = []
        controller.current_model = EXAMPLE_MODEL
        app.TransformerApp._render_structure_buttons(controller)
        self.assertEqual([button.cget('text') for button in controller.structure_buttons], ['Compact leads', 'Wide leads'])
        self.assertTrue(controller.structure_label.winfo_manager())
        controller.current_model = 'differential_inductor'
        app.TransformerApp._render_structure_buttons(controller)
        self.assertEqual(controller.structure_buttons, [])
        self.assertFalse(controller.structure_label.winfo_manager())

    def test_legacy_v1_file_reports_the_missing_structure_pack(self):
        controller = pack_ui()
        design = dict(library='ExampleDesign', cell='Old', structure='layout_v2',
                      parameters={'inner_diameter': 110}, layers={})
        with self.assertRaisesRegex(ValueError, 'structure pack'):
            controller.validate_imported(dict(schema='tfgen.design.v1', design=design))
        # A v2 file naming a pack model that is not loaded gets the same hint.
        with self.assertRaisesRegex(ValueError, 'not available'):
            controller.validate_imported(dict(schema='tfgen.design.v2', design=dict(
                design, model='some_missing_pack_model')))

    def test_legacy_v1_file_keeps_its_model_when_the_pack_is_installed(self):
        # Review regression: a v1 file must stay transformer_implicit, never be
        # normalized into the built-in differential inductor.
        from pack_support import register_temporary_model
        schema = [{'name': name, 'label': name, 'part': 'body', 'default': value, 'min': 0.1, 'max': 1000}
                  for name, value in (('inner_diameter', 110), ('inner_width', 10), ('outer_width', 10),
                                      ('gap', 8), ('leg_length', 45), ('center_leg_gap', 30), ('pair_leg_gap', 15))]
        register_temporary_model(self, dict(
            id='transformer_implicit', title='Legacy implicit', description='Synthetic stand-in.',
            defaults={field['name']: field['default'] for field in schema}, parameter_schema=schema,
            layer_roles=['body', 'crossing'],
            structures=[{'id': 'layout', 'title': 'Direct'}, {'id': 'layout_v2', 'title': 'Crossed'}]),
            lambda *args: {'ok': False, 'errors': [], 'warnings': [], 'parameters': {}, 'geometry': {}})
        controller = pack_ui()
        parameters = {field['name']: field['default'] for field in schema}
        parameters.update(inner_width=12, pgs_enabled=False, ring_enabled=False)
        design = dict(library='ExampleDesign', cell='Old', structure='layout_v2',
                      parameters=parameters, layers={'body': 'M4', 'crossing': 'M3', 'ground_route': 'M2'})
        loaded = controller.validate_imported(dict(schema='tfgen.design.v1', design=design))
        self.assertEqual((loaded['model'], loaded['structure']), ('transformer_implicit', 'layout_v2'))
        self.assertEqual(loaded['parameters']['inner_width'], 12)

    def test_pack_variant_round_trips_through_an_exported_design(self):
        controller = pack_ui()
        controller.switch_model(EXAMPLE_MODEL)
        controller.vars['structure'].set('wide')
        design = controller.collect_design()
        loaded = controller.validate_imported(dict(schema='tfgen.design.v2', design=design))
        self.assertEqual((loaded['model'], loaded['structure']), (EXAMPLE_MODEL, 'wide'))


if __name__ == '__main__':
    unittest.main()
