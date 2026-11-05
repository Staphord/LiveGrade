from datetime import timedelta
from decimal import Decimal
import hashlib
import uuid
from unittest.mock import patch

from django.core.cache import cache
from django.core import signing
from django.core.exceptions import ValidationError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.testing import make_org, make_user
from service_domains.models import (ShowcaseCategory, ShowcaseCertificate, ShowcaseEvent,
                                    ShowcaseExhibit, ShowcaseRating, ShowcaseJudgeCriterion)
from service_domains.operations import (transition_showcase_event,
    issue_showcase_judge_invitation, replace_showcase_invited_judge_assignments,
    replace_showcase_judge_criteria, submit_showcase_judge_evaluation)
from service_domains.operations import showcase_awards


class ShowcasePublicRatingFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        self.org = make_org('Showcase public org')
        self.owner = make_user('showcase-public-owner')
        self.event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Community Expo', status=ShowcaseEvent.Status.OPEN)
        self.category = ShowcaseCategory.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Science')
        self.exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, category=self.category,
            name='Water filter', description='A small clean-water prototype')
        self.other_exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, category=self.category, name='Solar oven')
        self.url = reverse('showcase_rate_exhibit', args=(self.exhibit.public_id,))
        self.other_url = reverse('showcase_rate_exhibit', args=(self.other_exhibit.public_id,))
        self.label_url = reverse('showcase_exhibit_label', args=(self.exhibit.public_id,))
        self.qr_url = reverse('showcase_exhibit_qr', args=(self.exhibit.public_id,))

    @property
    def device_cookie_name(self):
        return f'livegrade_showcase_device_{self.event.public_id.hex}'

    def test_get_issues_persistent_event_bound_httponly_device_cookie(self):
        self.event.branding = {'tagline': 'Build a brighter future', 'accent': 'purple'}
        self.event.save(update_fields=('branding',))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Water filter')
        nonce = response.wsgi_request.csp_nonce
        self.assertContains(response, f'<style nonce="{nonce}">')
        self.assertIn(f"'nonce-{nonce}'", response['Content-Security-Policy-Report-Only'])
        self.assertContains(response, 'Community Expo')
        self.assertContains(response, 'Build a brighter future')
        self.assertContains(response, '#5b3f8c')
        self.assertContains(response, 'does not collect names, contact details, or written comments')
        self.assertNotContains(response, 'name="comment"')
        cookie = response.cookies[self.device_cookie_name]
        self.assertTrue(cookie['httponly'])
        self.assertEqual(cookie['samesite'], 'Lax')
        self.assertEqual(cookie['path'], '/showcase/exhibits/')
        self.assertEqual(int(cookie['max-age']), 7 * 24 * 60 * 60)
        self.assertEqual(ShowcaseRating.objects.count(), 0)
        self.assertIn('Cache-Control', response)
        self.assertIn('no-store', response['Cache-Control'])

    @patch('service_domains.views.allow_public_event_request', return_value=False)
    def test_visitor_rating_network_limit_returns_retry_after_without_issuing_cookie(self, throttle):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response)
        self.assertNotIn(self.device_cookie_name, response.cookies)
        self.assertEqual(ShowcaseRating.objects.count(), 0)
        self.assertEqual(throttle.call_args.kwargs['service'], 'showcase-rating')
        self.assertEqual(throttle.call_args.kwargs['event_id'], self.event.public_id)

    @patch('service_domains.views.allow_public_event_request', return_value=None)
    def test_visitor_rating_fails_closed_if_shared_abuse_limiter_is_unavailable(self, throttle):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 503)
        self.assertIn('Retry-After', response)
        self.assertNotIn(self.device_cookie_name, response.cookies)
        self.assertEqual(ShowcaseRating.objects.count(), 0)

    def test_event_tagline_is_rendered_as_escaped_text(self):
        self.event.branding = {'tagline': '<script>unsafe</script>', 'accent': 'blue'}
        self.event.save(update_fields=('branding',))
        response = self.client.get(self.url)
        self.assertContains(response, '&lt;script&gt;unsafe&lt;/script&gt;')
        self.assertNotContains(response, '<script>unsafe</script>')

    def test_unknown_exhibit_ids_do_not_disclose_a_page_or_generate_qr(self):
        missing = uuid.uuid4()
        for name in ('showcase_rate_exhibit', 'showcase_exhibit_label', 'showcase_exhibit_qr'):
            response = self.client.get(reverse(name, args=(missing,)))
            self.assertEqual(response.status_code, 404)

    def test_each_exhibit_has_a_printable_label_and_png_qr_for_its_rating_url(self):
        label = self.client.get(self.label_url)
        self.assertEqual(label.status_code, 200)
        self.assertContains(label, 'Use your browser’s Print command')
        self.assertContains(label, self.url)
        self.assertContains(label, 'alt="QR code for rating Water filter"')
        qr = self.client.get(self.qr_url)
        self.assertEqual(qr.status_code, 200)
        self.assertEqual(qr['Content-Type'], 'image/png')
        self.assertTrue(qr.content.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertEqual(ShowcaseRating.objects.count(), 0)

    def test_same_cookie_is_reused_for_all_exhibits_and_duplicate_is_denied(self):
        self.client.get(self.url)
        token = self.client.cookies[self.device_cookie_name].value
        self.assertTrue(ShowcaseRating.digest_device_key(token, self.event))
        again = self.client.get(self.other_url)
        self.assertEqual(again.status_code, 200)
        self.assertNotIn(self.device_cookie_name, again.cookies)
        self.assertEqual(self.client.cookies[self.device_cookie_name].value, token)

        submitted = self.client.post(self.url, {'stars': '5', 'comment': 'Useful prototype'})
        self.assertContains(submitted, 'Your rating has been recorded.')
        rating = ShowcaseRating.objects.get()
        self.assertEqual(rating.exhibit, self.exhibit)
        self.assertEqual(rating.device_digest, hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(rating.created_by, self.owner)
        self.assertEqual(rating.comment, '')

        duplicate = self.client.post(self.url, {'stars': '3', 'comment': ''})
        self.assertEqual(duplicate.status_code, 400)
        self.assertContains(duplicate, 'already rated this exhibit', status_code=400)
        self.assertEqual(ShowcaseRating.objects.count(), 1)

        other_exhibit_rating = self.client.post(self.other_url, {'stars': '4', 'comment': ''})
        self.assertContains(other_exhibit_rating, 'Your rating has been recorded.')
        self.assertEqual(ShowcaseRating.objects.count(), 2)

    def test_invalid_event_cookie_is_rotated_and_old_token_is_not_accepted(self):
        self.client.get(self.url)
        old_token = self.client.cookies[self.device_cookie_name].value
        self.client.cookies[self.device_cookie_name] = signing.Signer().sign('invalid')
        response = self.client.post(self.url, {'stars': '5', 'comment': ''})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'link expired', status_code=400)
        fresh_token = response.cookies[self.device_cookie_name].value
        self.assertNotEqual(fresh_token, old_token)
        self.assertEqual(ShowcaseRating.objects.count(), 0)

    def test_minor_consent_and_event_window_are_checked_on_every_request(self):
        self.event.includes_minors = True
        self.event.consent_confirmed_at = None
        self.event.save(update_fields=('includes_minors', 'consent_confirmed_at'))
        self.assertEqual(self.client.get(self.url).status_code, 404)

        self.event.consent_confirmed_at = timezone.now()
        self.event.starts_at = timezone.now() + timedelta(minutes=1)
        self.event.save(update_fields=('consent_confirmed_at', 'starts_at'))
        self.assertEqual(self.client.get(self.url).status_code, 404)

        self.event.starts_at = None
        self.event.ends_at = timezone.now() - timedelta(seconds=1)
        self.event.save(update_fields=('starts_at', 'ends_at'))
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, {'stars': '5'}).status_code, 404)

    def test_event_closed_after_page_load_is_rechecked_before_saving(self):
        self.client.get(self.url)
        self.event.status = ShowcaseEvent.Status.CLOSED
        self.event.save(update_fields=('status',))
        response = self.client.post(self.url, {'stars': '5', 'comment': ''})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ShowcaseRating.objects.count(), 0)

    def test_csrf_and_form_validation_apply_to_public_ratings(self):
        client = Client(enforce_csrf_checks=True)
        response = client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.post(self.url, {'stars': '5'}).status_code, 403)
        csrf = client.cookies['livegrade_csrftoken'].value
        valid = client.post(self.url, {'stars': '5', 'comment': ''}, HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(ShowcaseRating.objects.count(), 1)

        invalid = Client()
        invalid.get(self.url)
        invalid_response = invalid.post(self.url, {'stars': '6', 'comment': 'x' * 501})
        self.assertEqual(invalid_response.status_code, 400)
        self.assertEqual(ShowcaseRating.objects.count(), 1)

    def test_public_results_require_release_disclose_aggregates_and_use_cache(self):
        results_url = reverse('showcase_public_results', args=(self.event.public_id,))
        self.assertEqual(self.client.get(results_url).status_code, 404)

        self.client.get(self.url)
        first_rating = self.client.post(self.url, {'stars': '5', 'comment': 'Private note'})
        self.assertContains(first_rating, 'Your rating has been recorded.')
        self.client.get(self.other_url)
        second_rating = self.client.post(self.other_url, {'stars': '3', 'comment': 'Another note'})
        self.assertContains(second_rating, 'Your rating has been recorded.')
        self.assertEqual(ShowcaseRating.objects.count(), 2)
        self.event.status = ShowcaseEvent.Status.PUBLISHED
        self.event.public_results = True
        self.event.save(update_fields=('status', 'public_results'))

        response = self.client.get(results_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Water filter')
        self.assertContains(response, 'Average rating: 5 out of 5 from 1 ratings.')
        self.assertNotContains(response, 'Private note')
        self.assertNotContains(response, 'Another note')
        self.assertIn('no-store', response['Cache-Control'])

        with patch('service_domains.views.showcase_awards', side_effect=AssertionError('cache miss')):
            cached = self.client.get(results_url)
        self.assertContains(cached, 'Water filter')

        self.event.public_results = False
        self.event.save(update_fields=('public_results',))
        self.assertEqual(self.client.get(results_url).status_code, 404)

    def test_public_results_disclose_ties_and_empty_award_state(self):
        self.client.get(self.url)
        self.client.post(self.url, {'stars': '5', 'comment': ''})
        self.client.get(self.other_url)
        self.client.post(self.other_url, {'stars': '5', 'comment': ''})
        self.event.status = ShowcaseEvent.Status.PUBLISHED
        self.event.public_results = True
        self.event.save(update_fields=('status', 'public_results'))
        response = self.client.get(reverse('showcase_public_results', args=(self.event.public_id,)))
        self.assertContains(response, 'The audience favorite is tied:')
        self.assertContains(response, 'No judge scorecards were recorded for this category.')

        empty_event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Quiet Showcase', status=ShowcaseEvent.Status.PUBLISHED,
            public_results=True)
        ShowcaseCategory.objects.create(organization_id=self.org.id, created_by=self.owner,
            event=empty_event, name='Unrated category')
        empty = self.client.get(reverse('showcase_public_results', args=(empty_event.public_id,)))
        self.assertContains(empty, 'No audience ratings were recorded.')
        self.assertContains(empty, 'No judge scorecards were recorded for this category.')

    def test_public_results_remain_available_if_shared_cache_read_and_write_fail(self):
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status',))
        rating = ShowcaseRating(event=self.event, exhibit=self.exhibit,
            device_digest='a' * 64, stars=5, organization_id=self.org.id,
            created_by=self.owner)
        rating.full_clean()
        rating.save(force_insert=True)
        self.event.status = ShowcaseEvent.Status.PUBLISHED
        self.event.public_results = True
        self.event.save(update_fields=('status', 'public_results'))
        results_url = reverse('showcase_public_results', args=(self.event.public_id,))
        with self.assertLogs('service_domains.views', level='WARNING') as cache_logs:
            with patch('service_domains.views.cache.get', side_effect=RuntimeError('cache unavailable')), \
                 patch('service_domains.views.cache.set', side_effect=RuntimeError('cache unavailable')):
                response = self.client.get(results_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Average rating: 5 out of 5 from 1 ratings.')
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(len(cache_logs.output), 2)
        self.assertNotIn('cache unavailable', '\n'.join(cache_logs.output))

    def test_known_answer_awards_match_manual_means_and_counts(self):
        """Keep an explicit, independently hand-calculated award fixture."""
        self.event.status = ShowcaseEvent.Status.DRAFT
        self.event.save(update_fields=('status', 'updated_at'))
        third_exhibit = ShowcaseExhibit.objects.create(
            organization_id=self.org.id, created_by=self.owner, event=self.event,
            category=self.category, name='Wind turbine')
        ratings = {
            self.exhibit: [5, 4, 3],       # 12 / 3 = 4.00
            self.other_exhibit: [5, 5],    # 10 / 2 = 5.00 (category winner)
            third_exhibit: [4, 4, 4, 4],   # 16 / 4 = 4.00
        }
        for exhibit, stars_values in ratings.items():
            for stars in stars_values:
                ShowcaseRating.objects.create(
                    event=self.event, exhibit=exhibit,
                    device_digest=uuid.uuid4().hex * 2, stars=stars,
                    organization_id=self.org.id, created_by=self.owner)

        criteria = replace_showcase_judge_criteria(self.event, [{
            'name': 'Project quality', 'description': '', 'weight': Decimal('100'),
            'scale': 'points', 'max_points': Decimal('100'), 'checklist_items': [],
        }], organization=self.org.as_claim(), user=self.owner)
        judge_scores = {
            self.exhibit: [90, 92],       # 91.00 (category judge winner)
            self.other_exhibit: [89, 89], # 89.00 (audience favorite)
            third_exhibit: [84],          # 84.00
        }
        invitations = {}
        tokens = []
        for exhibit, scores in judge_scores.items():
            for score in scores:
                invitation, token = issue_showcase_judge_invitation(
                    self.event, f'judge-{len(tokens)}@example.org',
                    timezone.now() + timedelta(hours=1),
                    organization=self.org.as_claim(), user=self.owner)
                invitations[invitation.pk] = [exhibit.pk]
                tokens.append((token, exhibit, score))
        replace_showcase_invited_judge_assignments(self.event, invitations,
            organization=self.org.as_claim(), user=self.owner)
        self.event.status = ShowcaseEvent.Status.OPEN
        self.event.save(update_fields=('status', 'updated_at'))
        for token, exhibit, score in tokens:
            submit_showcase_judge_evaluation(token, exhibit.public_id,
                {str(criteria[0].pk): str(score)})

        self.event.status = ShowcaseEvent.Status.PUBLISHED
        self.event.public_results = True
        self.event.save(update_fields=('status', 'public_results'))
        awards = showcase_awards(self.event)
        category = awards['categories'][0]
        # Manual tally keeps the judge award independent from visitor choice.
        self.assertEqual(category['winner'], self.exhibit)
        self.assertEqual(category['mean'], Decimal('91.00'))
        self.assertEqual(category['judge_count'], 2)
        self.assertEqual(awards['audience_favorite'], self.other_exhibit)
        self.assertEqual(awards['audience_favorite_mean'], 5)
        self.assertEqual(awards['audience_favorite_rating_count'], 2)
        self.assertEqual(ShowcaseRating.objects.filter(exhibit=self.exhibit).count(), 3)
        self.assertEqual(ShowcaseRating.objects.filter(exhibit=third_exhibit).count(), 4)

        response = self.client.get(reverse('showcase_public_results', args=(self.event.public_id,)))
        self.assertContains(response, '<strong>Water filter</strong>')
        self.assertContains(response, 'Average judge score: 91.00 out of 100 from 2 scorecards.')
        self.assertContains(response, '<strong>Solar oven</strong>')
        self.assertContains(response, 'Average rating: 5 out of 5 from 2 ratings.')

    def test_publish_issues_winner_certificates_with_public_verification_references(self):
        self.event.minimum_judges_per_exhibit = 0
        self.event.save(update_fields=('minimum_judges_per_exhibit',))
        self.event.branding = {'tagline': 'Build a brighter future', 'accent': 'green'}
        self.event.save(update_fields=('branding',))
        self.client.get(self.url)
        self.client.post(self.url, {'stars': '5', 'comment': ''})
        self.client.get(self.other_url)
        self.client.post(self.other_url, {'stars': '3', 'comment': ''})

        transition_showcase_event(self.event, ShowcaseEvent.Status.CLOSED,
            organization=self.org.as_claim(), user=self.owner, reason='End ratings.')
        transition_showcase_event(self.event, ShowcaseEvent.Status.PUBLISHED,
            organization=self.org.as_claim(), user=self.owner, reason='Publish awards.')
        certificates = list(ShowcaseCertificate.objects.filter(event=self.event)
            .select_related('exhibit', 'category').order_by('award_type'))
        self.assertEqual(len(certificates), 1)
        by_type = {certificate.award_type: certificate for certificate in certificates}
        self.assertEqual(by_type[ShowcaseCertificate.AwardType.AUDIENCE].exhibit, self.exhibit)

        results_url = reverse('showcase_public_results', args=(self.event.public_id,))
        results = self.client.get(results_url)
        self.assertContains(results, 'View verifiable Audience Favorite certificate')
        self.assertContains(results, 'Build a brighter future')
        self.assertContains(results, '#17683a')
        verify_url = reverse('showcase_certificate_verify',
            args=(by_type[ShowcaseCertificate.AwardType.AUDIENCE].public_id,))
        certificate_page = self.client.get(verify_url)
        self.assertEqual(certificate_page.status_code, 200)
        self.assertContains(certificate_page, 'Audience Favorite')
        self.assertContains(certificate_page, 'Water filter')
        self.assertContains(certificate_page, 'Build a brighter future')
        self.assertContains(certificate_page, '#17683a')
        self.assertContains(certificate_page, str(by_type[ShowcaseCertificate.AwardType.AUDIENCE].public_id))
        self.assertNotContains(certificate_page, 'Private note')
        self.assertIn('public, max-age=300', certificate_page['Cache-Control'])

        transition_showcase_event(self.event, ShowcaseEvent.Status.CLOSED,
            organization=self.org.as_claim(), user=self.owner, reason='Revoke public results.')
        self.assertEqual(self.client.get(verify_url).status_code, 200)
        self.assertEqual(ShowcaseCertificate.objects.filter(event=self.event).count(), 1)
        original_ids = set(ShowcaseCertificate.objects.filter(event=self.event)
            .values_list('public_id', flat=True))
        transition_showcase_event(self.event, ShowcaseEvent.Status.PUBLISHED,
            organization=self.org.as_claim(), user=self.owner, reason='Republish verified awards.')
        self.assertEqual(set(ShowcaseCertificate.objects.filter(event=self.event)
            .values_list('public_id', flat=True)), original_ids)
        self.assertEqual(ShowcaseCertificate.objects.filter(event=self.event).count(), 1)

    def test_tied_awards_do_not_issue_winner_certificates(self):
        self.event.minimum_judges_per_exhibit = 0
        self.event.save(update_fields=('minimum_judges_per_exhibit',))
        self.client.get(self.url)
        self.client.post(self.url, {'stars': '5', 'comment': ''})
        self.client.get(self.other_url)
        self.client.post(self.other_url, {'stars': '5', 'comment': ''})
        transition_showcase_event(self.event, ShowcaseEvent.Status.CLOSED,
            organization=self.org.as_claim(), user=self.owner, reason='End ratings.')
        transition_showcase_event(self.event, ShowcaseEvent.Status.PUBLISHED,
            organization=self.org.as_claim(), user=self.owner, reason='Publish tied awards.')
        self.assertFalse(ShowcaseCertificate.objects.filter(event=self.event).exists())

    def test_certificate_reference_is_unlisted_and_non_disclosing_when_unknown(self):
        self.assertEqual(self.client.get(reverse('showcase_certificate_verify',
            args=(uuid.uuid4(),))).status_code, 404)

    def test_certificate_model_enforces_award_relationships_and_immutability(self):
        other_event = ShowcaseEvent.objects.create(organization_id=self.org.id,
            created_by=self.owner, name='Other Expo')
        other_category = ShowcaseCategory.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=other_event, name='Other category')
        different_category = ShowcaseCategory.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=self.event, name='Different category')
        other_exhibit = ShowcaseExhibit.objects.create(organization_id=self.org.id,
            created_by=self.owner, event=other_event, category=other_category, name='Other exhibit')

        invalid = [
            (ShowcaseCertificate(event=self.event, exhibit=other_exhibit,
                award_type=ShowcaseCertificate.AwardType.AUDIENCE, award_title='Audience Favorite',
                organization_id=self.org.id, created_by=self.owner), 'must belong to this event'),
            (ShowcaseCertificate(event=self.event, exhibit=self.exhibit, category=other_category,
                award_type=ShowcaseCertificate.AwardType.CATEGORY, award_title='Other Award',
                organization_id=self.org.id, created_by=self.owner), 'category must belong to this event'),
            (ShowcaseCertificate(event=self.event, exhibit=self.exhibit, category=different_category,
                award_type=ShowcaseCertificate.AwardType.CATEGORY, award_title='Mismatched Award',
                organization_id=self.org.id, created_by=self.owner), 'must match the exhibit category'),
            (ShowcaseCertificate(event=self.event, exhibit=self.exhibit,
                award_type=ShowcaseCertificate.AwardType.CATEGORY, award_title='Missing category',
                organization_id=self.org.id, created_by=self.owner), 'must identify its category'),
            (ShowcaseCertificate(event=self.event, exhibit=self.exhibit, category=self.category,
                award_type=ShowcaseCertificate.AwardType.AUDIENCE, award_title='Wrong category',
                organization_id=self.org.id, created_by=self.owner), 'cannot be tied to one category'),
            (ShowcaseCertificate(event=self.event, exhibit=self.exhibit, category=self.category,
                award_type=ShowcaseCertificate.AwardType.CATEGORY, award_title=' ',
                organization_id=self.org.id, created_by=self.owner), 'needs an award title'),
        ]
        for certificate, message in invalid:
            with self.subTest(message=message), self.assertRaisesMessage(ValidationError, message):
                certificate.clean()

        certificate = ShowcaseCertificate.objects.create(event=self.event, exhibit=self.exhibit,
            category=self.category, award_type=ShowcaseCertificate.AwardType.CATEGORY,
            award_title='Science Award', organization_id=self.org.id, created_by=self.owner)
        certificate.award_title = 'Changed award'
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            certificate.save()
        with self.assertRaisesMessage(ValidationError, 'immutable'):
            certificate.delete()
