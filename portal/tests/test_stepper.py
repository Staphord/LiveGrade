from django.template.loader import render_to_string
from django.test import SimpleTestCase

from portal.stepper import LOCKED_HINT, build_steps, steps_from_setup, unsaved_steps


class BuildStepsTests(SimpleTestCase):
    def test_numbers_current_and_defaults(self):
        steps = build_steps([{'key': 'a', 'label': 'A'}, {'key': 'b', 'label': 'B'}], 'b')

        self.assertEqual([s['number'] for s in steps], [1, 2])
        self.assertEqual([s['current'] for s in steps], [False, True])
        self.assertFalse(any(s['locked'] or s['disabled'] or s['done'] for s in steps))
        self.assertEqual(steps[0]['hint'], '')
        self.assertEqual(steps[0]['href'], '')

    def test_no_active_step_marks_nothing_current(self):
        steps = build_steps([{'key': 'a', 'label': 'A'}])
        self.assertFalse(steps[0]['current'])

    def test_locked_step_is_disabled_by_default_and_never_done(self):
        step = build_steps([{'key': 'a', 'label': 'A', 'locked': True, 'done': True}])[0]

        self.assertTrue(step['locked'])
        self.assertTrue(step['disabled'])
        self.assertFalse(step['done'])
        self.assertEqual(step['hint'], LOCKED_HINT)

    def test_locked_step_can_stay_clickable(self):
        step = build_steps([{'key': 'a', 'label': 'A', 'locked': True, 'disabled': False}])[0]

        self.assertTrue(step['locked'])
        self.assertFalse(step['disabled'])
        self.assertEqual(step['hint'], '')

    def test_locked_after_first_overrides_spec_flags(self):
        steps = build_steps([
            {'key': 'a', 'label': 'A', 'done': True},
            {'key': 'b', 'label': 'B', 'done': True, 'disabled': False},
        ], 'a', locked_after_first=True, hint='Save first')

        self.assertTrue(steps[0]['done'])
        self.assertFalse(steps[0]['locked'])
        self.assertTrue(steps[1]['locked'])
        self.assertTrue(steps[1]['disabled'])
        self.assertFalse(steps[1]['done'])
        self.assertEqual(steps[1]['hint'], 'Save first')

    def test_optional_icon_final_and_href_pass_through(self):
        step = build_steps([{'key': 'a', 'label': 'A', 'optional': True, 'icon': 'bi-x',
                             'final': True, 'href': '#a', 'desc': 'D'}])[0]

        self.assertTrue(step['optional'] and step['final'])
        self.assertEqual((step['icon'], step['href'], step['desc']), ('bi-x', '#a', 'D'))


class StepHelpersTests(SimpleTestCase):
    def test_steps_from_setup_maps_complete_description_and_optional(self):
        steps = steps_from_setup([
            {'key': 'a', 'label': 'A', 'complete': True, 'description': 'Desc'},
            {'key': 'b', 'label': 'B', 'complete': False, 'optional': True},
        ], 'b')

        self.assertTrue(steps[0]['done'])
        self.assertEqual(steps[0]['desc'], 'Desc')
        self.assertTrue(steps[1]['current'])
        self.assertTrue(steps[1]['optional'])
        self.assertFalse(steps[1]['locked'])

    def test_steps_from_setup_can_lock_after_first(self):
        steps = steps_from_setup([
            {'key': 'a', 'label': 'A', 'complete': False},
            {'key': 'b', 'label': 'B', 'complete': False},
        ], 'a', locked_after_first=True)
        self.assertEqual([s['locked'] for s in steps], [False, True])

    def test_unsaved_steps_opens_only_the_first(self):
        steps = unsaved_steps([('a', 'A', 'First'), ('b', 'B')], hint='Save')

        self.assertTrue(steps[0]['current'])
        self.assertEqual(steps[0]['desc'], 'First')
        self.assertEqual(steps[1]['desc'], '')
        self.assertTrue(steps[1]['disabled'])
        self.assertEqual(steps[1]['hint'], 'Save')


class StepperPartialTests(SimpleTestCase):
    def render(self, steps, **extra):
        return render_to_string('partials/_stepper.html', {'steps': steps, 'csp_nonce': 'n', **extra})

    def test_renders_every_state(self):
        html = self.render(build_steps([
            {'key': 'a', 'label': 'Done step', 'done': True, 'desc': 'Why', 'href': '#a'},
            {'key': 'b', 'label': 'Current step', 'optional': True},
            {'key': 'c', 'label': 'Locked step', 'locked': True},
            {'key': 'd', 'label': 'Final', 'icon': 'bi-rocket', 'final': True,
             'locked': True, 'disabled': False},
        ], 'b'), stepper_label='Event steps')

        self.assertIn('aria-label="Event steps"', html)
        self.assertIn('<a href="#a"', html)
        self.assertIn('is-done', html)
        self.assertIn('Complete: ', html)
        self.assertIn('bi-check-lg', html)
        self.assertIn('Why', html)
        self.assertIn('data-step="b" data-panel-step="b" aria-current="step"', html)
        self.assertIn('(optional)', html)
        self.assertIn(f'disabled title="{LOCKED_HINT}"', html)
        self.assertIn('bi bi-rocket', html)
        self.assertIn('is-final', html)
        self.assertEqual(html.count('aria-current="step"'), 1)

    def test_default_label_and_shared_script(self):
        html = self.render(build_steps([{'key': 'a', 'label': 'A'}], 'a'))

        self.assertIn('aria-label="Setup steps"', html)
        self.assertIn('<button type="button"', html)
        self.assertIn('window.DevPerfStepper', html)
        self.assertIn('nonce="n"', html)
