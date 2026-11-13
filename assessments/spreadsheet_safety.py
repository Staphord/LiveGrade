"""Helpers for safely exporting user-controlled text to spreadsheets."""

import unicodedata


def spreadsheet_safe_text(value):
    """Prefix formula-like text so spreadsheet applications keep it literal.

    The prefix is applied to the original text rather than a stripped copy so
    the exported value remains legible and preserves the user's input.
    """
    text = '' if value is None else str(value)
    probe = text
    while probe and (probe[0].isspace() or unicodedata.category(probe[0]) in {'Cc', 'Cf'}):
        probe = probe[1:]
    if probe.startswith(('=', '+', '-', '@', '\t', '\r', '\n')):
        return "'" + text
    return text
