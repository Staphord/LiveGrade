"""Run the dependency-free browser transport state tests when Node is available."""
from pathlib import Path
import shutil
import subprocess
from unittest import skipUnless

from django.test import SimpleTestCase


@skipUnless(shutil.which('node'), 'Node is required for the JavaScript transport tests')
class SubmissionTransportTests(SimpleTestCase):
    def test_transport_state_machine(self):
        script = Path(__file__).parent / 'js/test_submission_transport.cjs'
        result = subprocess.run(['node', '--test', str(script)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-6000:])
