"""A `{# #}` comment that spans lines is printed on the page, not hidden.

Django's `{# #}` is a single-line comment. Opened on one line and closed on
another it is not a comment at all - the template engine emits every character
of it as text, braces and hash included, wherever it sits in the markup. It
renders without an error, so nothing fails and nobody notices until a reader
points at the explanation now sitting in the middle of a card.

Multi-line explanations belong in `{% comment %}`, which is a tag and cannot do
this. (Same guard as DevPerf's, where it was found three times.)
"""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

#: A line that opens `{#` without closing it. Anything else - a complete
#: single-line comment, or a `{#` inside a longer line that also closes - is
#: fine.
UNCLOSED = re.compile(r'\{#(?![^\n]*#\})')


def template_files():
    yield from (Path(settings.BASE_DIR) / 'templates').rglob('*.html')


class TemplateCommentTests(SimpleTestCase):

    def test_no_comment_is_left_open_at_the_end_of_a_line(self):
        offenders = []
        for path in template_files():
            for number, line in enumerate(path.read_text(errors='ignore').splitlines(), 1):
                if UNCLOSED.search(line):
                    offenders.append(f'{path.relative_to(settings.BASE_DIR)}:{number}')

        self.assertEqual(
            offenders, [],
            'These open {# without closing it on the same line, so Django '
            'prints them on the page. Use {% comment %} for anything that '
            'spans lines: ' + ', '.join(offenders))

    def test_the_sweep_reads_real_templates(self):
        """Guard on the guard: a pattern that matched nothing would pass above."""
        found = list(template_files())

        self.assertGreater(len(found), 20)
        self.assertTrue(any(p.name == 'base.html' for p in found))

    def test_the_pattern_catches_the_shape_it_is_looking_for(self):
        self.assertTrue(UNCLOSED.search('  {# opened and left open'))
        self.assertIsNone(UNCLOSED.search('  {# closed properly #}'))
        self.assertIsNone(UNCLOSED.search('<div>{# a note #}</div>'))
