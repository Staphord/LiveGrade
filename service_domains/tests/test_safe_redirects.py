from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase

from service_domains import realtime, team_series
from service_domains.safe_redirects import safe_redirect, user_message


class SafeRedirectTests(SimpleTestCase):
    def test_only_local_paths_are_followed(self):
        self.assertEqual(safe_redirect('/team/app/1/?step=roster')['Location'], '/team/app/1/?step=roster')
        for hostile in ('https://evil.example/', '//evil.example/', 'javascript:alert(1)'):
            self.assertEqual(safe_redirect(hostile)['Location'], '/')
        self.assertEqual(safe_redirect('https://evil.example/', fallback='/home/')['Location'], '/home/')

    def test_people_see_validation_text_but_never_raw_exceptions(self):
        self.assertEqual(user_message(ValidationError(['One', 'Two'])), 'One; Two')
        self.assertEqual(user_message(PermissionDenied('secret detail')), 'This action could not be completed.')
        self.assertEqual(user_message(RuntimeError('boom'), 'Try later.'), 'Try later.')

    def test_logged_values_cannot_forge_log_lines(self):
        self.assertEqual(realtime._loggable('a\r\nforged'), 'a  forged')
        self.assertEqual(len(realtime._loggable('x' * 500)), 80)

    def test_series_selections_accept_only_plain_positive_numbers(self):
        self.assertEqual(team_series.parse_series_ids(['3', '12', '3']), [3, 12])
        for bad in (['0'], ['01'], ['-1'], ['1.5'], ['١٢'], [''], ['9' * 20], ['9223372036854775808']):
            with self.assertRaises(team_series.InvalidSeriesSelection):
                team_series.parse_series_ids(bad)
