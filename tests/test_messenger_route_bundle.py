"""The downloadable bundle runs the reviewed sources, without imports or writes."""
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bundle = load('diagnose_messenger_via_mac')
builder = load('build_messenger_route_bundle')


class BundleTests(unittest.TestCase):
    def test_generated_bundle_matches_reviewed_sources(self):
        self.assertEqual((ROOT/'scripts/diagnose_messenger_via_mac.py').read_text(),builder.render())
        self.assertEqual(bundle.PAIRING_SOURCE,(ROOT/'scripts/diagnose_whatsapp_pairing.py').read_text())
        self.assertEqual(bundle.ROUTE_SOURCE,(ROOT/'scripts/probe_messenger_via_mac.py').read_text())

    def test_route_runs_even_when_pairing_diagnosis_is_unavailable(self):
        with patch.object(bundle,'run_source',side_effect=[1,0]) as run:
            self.assertEqual(bundle.main(),1)
        self.assertEqual(run.call_args_list[0].args,(bundle.PAIRING_SOURCE,'pairing'))
        self.assertEqual(run.call_args_list[1].args,(bundle.ROUTE_SOURCE,'route'))
        self.assertTrue(run.call_args_list[0].kwargs['compact'])

    def test_sources_are_isolated_and_argv_is_restored(self):
        args=sys.argv
        source='import sys\nLOG_WINDOW_MINUTES=120\ndef main():\n assert LOG_WINDOW_MINUTES == 360\n assert sys.argv == ["fake", "--compact"]\n return 0\n'
        with patch.object(bundle.sys.stdout,'flush') as flush:
            self.assertEqual(bundle.run_source(source,'fake',True),0)
        flush.assert_called_once()
        self.assertIs(sys.argv,args)


if __name__ == '__main__':
    unittest.main()
