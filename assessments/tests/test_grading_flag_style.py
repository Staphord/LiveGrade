"""A category that still needs a score says so with one quiet inline line, nothing else."""
import re

from assessments.tests.test_edges_lecturer_views import LecturerTestCase


class GradingFlagStyleTests(LecturerTestCase):

    def setUp(self):
        super().setUp()
        self.set_status('live')
        self.active_turn(self.red)
        self.student_client = self.client_class()
        self.student_client.post(f'/assess/{self.session.uuid}/join/', {'identifier': 'S3'})
        self.page = self.student_client.get(f'/assess/{self.session.uuid}/evaluate/')
        self.html = self.page.content.decode()

    def test_the_grading_page_has_one_group_block_and_one_per_member_of_the_presenting_group(self):
        self.assertEqual(self.html.count('class="category-block'), 1 + 2)

    def test_every_block_has_a_grade_this_category_line_hidden_until_it_is_flagged(self):
        self.assertEqual(self.html.count('<span class="category-error">'), self.html.count('class="category-block'))
        self.assertEqual(self.html.count('Grade this category</span>'), self.html.count('class="category-block'))
        self.assertRegex(self.html, r'\.category-error\{display:none;')
        self.assertRegex(self.html, r'\.category-block\.is-flagged \.category-error\{display:inline-flex\}')

    def test_the_line_shares_the_out_of_row_so_it_adds_no_height_and_out_of_stays_on_the_right(self):
        self.assertRegex(
            self.html,
            r'<span class="category-error">.*?Grade this category</span>\s*<span class="text-muted ms-auto">out of')

    def test_a_flagged_block_gets_no_outline_tint_tag_red_box_or_movement(self):
        for gone in ('outline:2px solid var(--danger)', 'category-shake', 'category-flash', 'category-flag',
                     'Needs a score'):
            self.assertNotIn(gone, self.html)
        self.assertIsNone(re.search(r'\.category-block\.is-flagged\{', self.html))
        self.assertIsNone(re.search(r'\.category-block\.is-flagged \.score-num', self.html))

    def test_the_form_message_still_says_how_many_are_left(self):
        self.assertIn('id="eval-validation-text"', self.html)
        self.assertIn("' category still needs' : ' categories still need'", self.html)

    def test_answering_a_block_still_clears_its_flag(self):
        self.assertIn("block.classList.remove('is-flagged');\n      updateProgress();", self.html)
