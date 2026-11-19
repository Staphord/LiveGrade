from django.test import SimpleTestCase
from django.urls import NoReverseMatch, Resolver404, resolve, reverse

from assessments.routing import websocket_urlpatterns


class CombinedAssessmentRouteRetirementTests(SimpleTestCase):
    removed_routes = {
        'combined_assessment_list': ('/panel/app/combined/', ()),
        'combined_assessment_create': ('/panel/app/combined/new/', ()),
        'combined_assessment_bulk_delete': ('/panel/app/combined/delete/', ()),
        'combined_assessment_detail': ('/panel/app/combined/1/', (1,)),
        'combined_assessment_live_state': ('/panel/app/combined/1/live-state/', (1,)),
        'combined_assessment_delete': ('/panel/app/combined/1/delete/', (1,)),
        'panel_hearing_table': ('/panel/app/hearings/all/', ()),
        'coassessment_dashboard': ('/coassessment/app/', ()),
        'legacy_assessments_root': ('/assessments/', ()),
    }
    removed_panel_names = {
        'panel_hearing_list', 'panel_hearing_create', 'panel_hearing_bulk_delete',
        'panel_hearing_delete', 'panel_hearing_live', 'panel_hearing_live_state',
        'panel_hearing_live_action', 'panel_hearing_live_display_toggle', 'panel_hearing_manage',
        'panel_candidate_import', 'panel_candidate_import_sample', 'panel_rubric_import',
        'panel_rubric_import_sample', 'panel_invitation_import', 'panel_invitation_import_sample',
        'panel_results_review', 'panel_oversight_list', 'panel_oversight_detail',
        'panel_recusal_export', 'panel_results_export', 'panel_hearing_participants',
        'panel_examiner', 'panel_live_display', 'panel_live_display_updates', 'panel_entrant_results',
    }

    def test_legacy_combined_assessment_routes_are_unregistered(self):
        for name, (path, args) in self.removed_routes.items():
            with self.subTest(name=name):
                with self.assertRaises(NoReverseMatch):
                    reverse(name, args=args)

                with self.assertRaises(Resolver404):
                    resolve(path)

    def test_unified_assessment_routes_replace_the_retired_workspaces(self):
        self.assertEqual(reverse('assessment_panel_examiners', args=[1]), '/assessment/app/1/panel/')
        self.assertEqual(reverse('assessment_moderation', args=[1]), '/assessment/app/1/moderation/')
        self.assertEqual(reverse('assessment_async_console', args=[1]), '/assessment/app/1/async/')
        self.assertEqual(resolve('/assessment/app/1/async/').url_name, 'assessment_async_console')

    def test_all_legacy_panel_route_names_are_unregistered(self):
        for name in self.removed_panel_names:
            with self.subTest(name=name), self.assertRaises(NoReverseMatch):
                reverse(name)

    def test_legacy_panel_and_coassessment_websocket_routes_are_unregistered(self):
        patterns = [str(pattern.pattern) for pattern in websocket_urlpatterns]
        self.assertFalse(any(pattern.startswith('ws/panel/') for pattern in patterns), patterns)
        self.assertFalse(any(pattern.startswith('ws/coassessment/') for pattern in patterns), patterns)
