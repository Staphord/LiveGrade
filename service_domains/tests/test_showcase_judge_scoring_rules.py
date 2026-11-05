from decimal import Decimal
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from service_domains.rules import showcase_judge_score


def criterion(*, scale='points', weight='100', maximum='10', items=None, pk=1):
    return SimpleNamespace(pk=pk, scale=scale, weight=Decimal(weight),
        max_points=Decimal(maximum), checklist_items=[] if items is None else items)


class ShowcaseJudgeScoringRuleTests(SimpleTestCase):
    def test_requires_exactly_the_configured_criteria_and_full_weight(self):
        point = criterion()
        for criteria, scores, message in (
            ([], {}, 'criteria and scores are required'),
            ([point], [], 'criteria and scores are required'),
            ([point], {}, 'exactly once'),
            ([point], {'1': '10', '2': '5'}, 'exactly once'),
            ([criterion(weight='99')], {'1': '10'}, 'total exactly 100'),
        ):
            with self.subTest(message=message), self.assertRaisesMessage(ValidationError, message):
                showcase_judge_score(criteria, scores)

    def test_normalizes_points_stars_pass_fail_and_checklist(self):
        criteria = [
            criterion(pk=1, weight='40', maximum='10'),
            criterion(pk=2, scale='stars', weight='20'),
            criterion(pk=3, scale='pass_fail', weight='20'),
            criterion(pk=4, scale='checklist', weight='20', items=['A', 'B', 'C']),
        ]
        score = showcase_judge_score(criteria, {
            '1': Decimal('8'), '2': '4', '3': 'pass', '4': [0, '2', 2],
        })
        self.assertEqual(score, Decimal('81.33'))
        self.assertEqual(showcase_judge_score([criterion()], {'1': '0'}), Decimal('0.00'))
        self.assertEqual(showcase_judge_score(
            [criterion(scale='pass_fail')], {'1': 'fail'}), Decimal('0.00'))

    def test_rejects_invalid_points_and_star_values(self):
        points = criterion()
        for value in (True, 'not-a-number', 'NaN', '-0.01', '10.01'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                showcase_judge_score([points], {'1': value})
        zero_maximum = criterion(maximum='0')
        with self.assertRaisesMessage(ValidationError, 'criterion maximum'):
            showcase_judge_score([zero_maximum], {'1': '0'})

        stars = criterion(scale='stars')
        for value in (True, '1.5', '0', '6'):
            with self.subTest(stars=value), self.assertRaises(ValidationError):
                showcase_judge_score([stars], {'1': value})

    def test_rejects_invalid_pass_fail_and_checklist_values(self):
        pass_fail = criterion(scale='pass_fail')
        with self.assertRaisesMessage(ValidationError, 'pass or fail'):
            showcase_judge_score([pass_fail], {'1': 'yes'})

        checklist = criterion(scale='checklist', items=['A', 'B'])
        with self.assertRaisesMessage(ValidationError, 'at least one configured item'):
            showcase_judge_score([criterion(scale='checklist')], {'1': []})
        for value in ('0', ['x'], ['01'], [2], [True]):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                showcase_judge_score([checklist], {'1': value})

    def test_unknown_scale_fails_closed(self):
        with self.assertRaisesMessage(ValidationError, 'supported Showcase judge scale'):
            showcase_judge_score([criterion(scale='unsupported')], {'1': '0'})
