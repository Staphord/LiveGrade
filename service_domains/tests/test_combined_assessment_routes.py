from django.test import SimpleTestCase
from django.urls import NoReverseMatch, Resolver404, resolve, reverse


class CombinedAssessmentRouteRetirementTests(SimpleTestCase):
    removed_routes = {
        'combined_assessment_list': ('/panel/app/combined/', ()),
        'combined_assessment_create': ('/panel/app/combined/new/', ()),
        'combined_assessment_bulk_delete': ('/panel/app/combined/delete/', ()),
        'combined_assessment_detail': ('/panel/app/combined/1/', (1,)),
        'combined_assessment_live_state': ('/panel/app/combined/1/live-state/', (1,)),
        'combined_assessment_delete': ('/panel/app/combined/1/delete/', (1,)),
    }

    def test_legacy_combined_assessment_routes_are_unregistered(self):
        for name, (path, args) in self.removed_routes.items():
            with self.subTest(name=name):
                with self.assertRaises(NoReverseMatch):
                    reverse(name, args=args)

                with self.assertRaises(Resolver404):
                    resolve(path)

    def test_panel_and_coassessment_routes_remain_registered(self):
        self.assertEqual(reverse('panel_hearing_table'), '/panel/app/hearings/all/')
        self.assertEqual(reverse('coassessment_dashboard'), '/coassessment/app/')
        self.assertEqual(resolve('/coassessment/app/').url_name, 'coassessment_dashboard')
