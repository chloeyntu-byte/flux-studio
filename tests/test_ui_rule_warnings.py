"""PDK warning UI contracts without an X server, native probing or OA writes."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import app
import structures


class Variable:
    def __init__(self, value=''):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Widget:
    def __init__(self):
        self.options = {}

    def configure(self, **options):
        self.options.update(options)

    def grid(self):
        pass

    def grid_remove(self):
        pass

    def start(self, _interval):
        pass

    def stop(self):
        pass


class MessageTree:
    def __init__(self):
        self.rows = {}

    def get_children(self):
        return list(self.rows)

    def delete(self, row):
        del self.rows[row]

    def insert(self, _parent, _index, iid, values, tags):
        self.rows[iid] = dict(values=values, tags=tags)


def warning():
    return dict(part='technology', code='metal_spacing_unverified',
                message='M4: minimum spacing is unavailable in the attached PDK.', fields=['library'])


def library(workspace='/test/workspace one', name='ExampleDesign', warnings=None):
    return dict(name=name, workspace=workspace, cds_lib=workspace + '/cds.lib',
        library_path=workspace + '/design', technology='AttachedTech', available=True,
        layers=[dict(name='M1', order=1, min_width=0.2, min_spacing=0.2),
                dict(name='M2', order=2, min_width=0.2, min_spacing=0.2),
                dict(name='M3', order=3, min_width=0.3, min_spacing=0.3),
                dict(name='M4', order=4, min_width=None, min_spacing=None)],
        default_layers=dict(body='M3', crossing='M4', pgs='M1', ring='M4', ground_route='M2'),
        model_default_layers={
            'differential_inductor':dict(body='M3', crossing='M4', pgs='M1', ring='M4', ground_route='M2'),
            'example_tapped_inductor':dict(body='M4', crossing='M3', pgs='M1', ring='M4', ground_route='M2'),
            'synthetic_secondary':dict(body='M4', secondary='M3', crossing='M2', pgs='M1', ring='M4', ground_route='M2')},
        warnings=[warning()] if warnings is None else warnings)


def ui(libraries=None):
    controller = app.TransformerApp.__new__(app.TransformerApp)
    controller.root = SimpleNamespace()
    controller.context = dict(libraries=libraries or [library()], defaults={})
    controller.library = None
    controller.warned_library_provenances = set()
    controller.models = copy.deepcopy(app.FALLBACK_MODELS)
    controller.models[app.DEFAULT_MODEL].update(defaults=dict(app.DEFAULTS), parameter_schema=copy.deepcopy(structures.MODEL_DEFINITIONS[0]['parameter_schema']))
    controller.models.update({
        # Stand-ins for models a structure pack would register; only metadata is needed here.
        'example_tapped_inductor':dict(title='Tapped inductor', parameters=['width'], defaults={'width':8}, layer_roles=['body','crossing'],
                                       structures=[dict(id='compact', title='Compact leads'), dict(id='wide', title='Wide leads')]),
        'synthetic_secondary':dict(title='Secondary model', parameters=['width'], defaults={'width':8}, layer_roles=['body','secondary','crossing'])})
    controller.current_model = app.DEFAULT_MODEL
    controller.vars = {key:Variable('' if value is None else value) for key,value in app.DEFAULTS.items()}
    controller.vars.update({role:Variable('stale_layer') for role in app.ROLES})
    controller.vars.update(library=Variable(), cell=Variable('fresh_cell'), structure=Variable('layout'), model=Variable(controller.current_model))
    controller.body_drafts = {}
    controller.source_context = None
    controller.suspended = False
    controller.spine_follow = Variable(True)
    controller.preview_ok = False
    controller.dirty = True
    controller.generating = False
    controller.context_busy = False
    controller.preview_busy = False
    controller.preset_busy = False
    controller.closed = False
    controller.close_pending = False
    controller.applied_design = None
    controller.generated_target = None
    controller.revision = 0
    controller.preview_revision = -1
    controller.preview_task = 1
    controller.geometry = None
    controller.view = None
    controller.messages = {}
    controller.error_tree = MessageTree()
    controller.field_widgets = {role:Widget() for role in app.ROLES}
    controller.field_widgets.update(pgs_spine_width=Widget(), ground_route_width=Widget())
    for name in ('status','library_info','preview_state','draft_status','connection','runtime_info'):
        setattr(controller,name,Variable())
    for name in ('generate_button','apply_button','context_button','custom_button','refresh_button',
                 'export_button','import_button','preset_load','preset_save','spine_checkbox',
                 'ground_checkbox','validation_label','progress'):
        setattr(controller,name,Widget())
    controller.structure_buttons = []
    controller.pgs_shape_buttons = []
    controller.ring_shape_buttons = []
    controller.ensure_variable = lambda name,default=None,kind='float': controller.vars.setdefault(name,Variable('' if default is None else default))
    controller._render_body_fields = Mock(side_effect=lambda: [
        controller.ensure_variable(name, entry.get('default'), entry.get('type', 'float'))
        for name,entry in controller.schema_for().items()])
    controller.schedule_redraw = Mock()
    controller.refresh_legend = Mock()
    controller.fit_view = Mock()
    controller.update_metrics = Mock()
    controller.submit = Mock(return_value=1)
    return controller


class UIRuleWarningTests(unittest.TestCase):
    def setUp(self):
        self.popup = patch.object(app.messagebox, 'showwarning')
        self.showwarning = self.popup.start()
        self.addCleanup(self.popup.stop)

    def test_identified_metals_with_missing_rules_remain_selectable_and_apply_passes_local_checks(self):
        controller = ui()
        controller.configure_library('ExampleDesign', apply_defaults=True)
        self.assertEqual(controller.field_widgets['body'].options['values'], ['M1','M2','M3','M4'])
        controller.vars['body'].set('M4')
        controller.vars['crossing'].set('M3')
        self.assertEqual(controller.local_errors(controller.collect_design()), [])
        controller.apply_parameters()
        self.assertFalse(controller.dirty)
        self.assertEqual(controller.applied_design['layers']['body'], 'M4')
        controller.submit.assert_called_once()
        self.assertEqual(controller.submit.call_args[0][0], 'preview')

    def test_warning_popup_occurs_once_per_provenance_and_not_after_edit_or_recheck(self):
        controller = ui()
        controller.configure_library('ExampleDesign', apply_defaults=True)
        controller.configure_library('ExampleDesign')
        controller.vars['inner_diameter'].set('130')
        controller.invalidate()
        controller.render_messages([], [warning()])
        controller._show_library_warnings()
        self.assertEqual(self.showwarning.call_count, 1)
        self.assertEqual(len(controller.error_tree.rows), 1)
        self.assertIn('Apply and Generate remain available', self.showwarning.call_args[0][1])

    def test_equal_library_name_in_new_workspace_gets_its_own_single_warning(self):
        controller = ui()
        controller.configure_library('ExampleDesign', apply_defaults=True)
        replacement = library(workspace='/test/workspace two')
        controller.context['libraries'] = [replacement]
        controller.configure_library('ExampleDesign')
        controller.configure_library('ExampleDesign')
        self.assertEqual(self.showwarning.call_count, 2)

    def test_available_complete_pdk_has_no_popup_and_failed_library_keeps_error_gate(self):
        complete = library(warnings=[])
        controller = ui([complete])
        controller.configure_library('ExampleDesign', apply_defaults=True)
        self.showwarning.assert_not_called()
        complete['available'] = False
        complete['error'] = 'No native technology or via definitions.'
        controller.configure_library('ExampleDesign')
        self.assertTrue(controller.local_errors(controller.collect_design()))
        controller.update_controls()
        self.assertEqual(controller.apply_button.options['state'], 'disabled')
        self.assertEqual(controller.generate_button.options['state'], 'disabled')

    def test_switch_model_repairs_missing_and_invalid_saved_layers_using_that_models_defaults(self):
        controller = ui()
        controller.configure_library('ExampleDesign', apply_defaults=True)
        controller.body_drafts['synthetic_secondary'] = dict(parameters={'width':9},
            layers={'body':'deleted_metal','crossing':''}, library='ExampleDesign',
            provenance=app.library_provenance(controller.library), structure='layout')
        controller.switch_model('synthetic_secondary')
        self.assertEqual({role:controller.vars[role].get() for role in ('body','secondary','crossing')},
                         dict(body='M4', secondary='M3', crossing='M2'))
        self.assertEqual(controller.vars['width'].get(), 9)
        self.assertEqual(controller.field_widgets['secondary'].options['values'], ['M1','M2','M3','M4'])

    def test_manual_body_layer_and_shared_pgs_ring_drafts_survive_model_round_trip(self):
        controller = ui()
        controller.configure_library('ExampleDesign', apply_defaults=True)
        controller.vars['body'].set('M2')
        controller.vars['pgs_extent'].set('83.5')
        controller.vars['ring_extent'].set('141')
        controller.vars['pgs_shape'].set('circle')
        controller.vars['ring_shape'].set('octagon')
        controller.vars['pgs'].set('M1')
        controller.vars['ring'].set('M3')
        shared = {key:controller.vars[key].get() for key in app.SHARED_KEYS + app.SHARED_ROLES}
        controller.switch_model('synthetic_secondary')
        controller.switch_model('differential_inductor')
        self.assertEqual(controller.vars['body'].get(), 'M2')
        self.assertEqual({key:controller.vars[key].get() for key in shared}, shared)
        self.assertEqual(self.showwarning.call_count, 1)

    def test_body_draft_from_same_name_in_other_workspace_does_not_override_new_defaults(self):
        controller = ui()
        controller.configure_library('ExampleDesign', apply_defaults=True)
        controller.vars['body'].set('M4')
        controller.switch_model('example_tapped_inductor')
        controller.context['libraries'] = [library('/test/other workspace')]
        controller.configure_library('ExampleDesign')
        controller.switch_model('differential_inductor')
        self.assertEqual(controller.vars['body'].get(), 'M3')

    def test_warning_normalization_supports_legacy_strings_and_deduplicates_backend_echo(self):
        controller = ui()
        controller.configure_library('ExampleDesign', apply_defaults=True)
        controller.render_messages([], [warning(), 'Additional optional rule is unavailable.'])
        self.assertEqual(len(controller.error_tree.rows), 2)
        self.assertTrue(all(row['tags']==('warning',) for row in controller.error_tree.rows.values()))
        controller.render_messages([dict(part='via', code='via_missing', message='No real via definition.', fields=['crossing'])])
        self.assertEqual(len(controller.error_tree.rows), 2)
        self.assertIn('validation issues', controller.validation_label.options['text'])
        self.assertTrue(any(row['tags']==('error',) for row in controller.error_tree.rows.values()))

    def test_successful_preview_with_rule_warning_allows_generate_using_only_applied_snapshot(self):
        controller = ui()
        controller.configure_library('ExampleDesign', apply_defaults=True)
        controller.apply_parameters()
        payload = dict(ok=True, errors=[], warnings=[warning()], geometry=dict(
            polygons=[dict(layer='M3', points=[[0,0],[1,0],[1,1],[0,1]])], bounds=[[0,0],[1,1]]))
        controller._result(1, 'preview', controller.revision, True, payload)
        self.assertEqual(controller.generate_button.options['state'], 'normal')
        self.assertEqual(len(controller.error_tree.rows), 1)
        applied = copy.deepcopy(controller.applied_design)
        controller.generate()
        self.assertEqual(controller.submit.call_args[0], ('generate', applied))
        self.assertTrue(controller.generating)
        self.assertEqual(controller.generate_button.options['state'], 'disabled')
        self.assertEqual(self.showwarning.call_count, 1)

    def test_real_preview_error_still_blocks_generate_and_keeps_nonblocking_warning_visible(self):
        controller = ui()
        controller.configure_library('ExampleDesign', apply_defaults=True)
        controller.apply_parameters()
        controller._result(1, 'preview', controller.revision, True, dict(ok=False,
            errors=[dict(part='via', code='via_fit', message='The real via landing does not fit.', fields=['crossing'])],
            warnings=[warning()]))
        self.assertFalse(controller.preview_ok)
        self.assertEqual(controller.generate_button.options['state'], 'disabled')
        controller.submit.reset_mock()
        controller.generate()
        controller.submit.assert_not_called()
        self.assertEqual(len(controller.error_tree.rows), 2)


if __name__ == '__main__':
    unittest.main()
