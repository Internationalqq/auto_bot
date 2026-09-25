"""Run with the installed Hermes Python; only mocked driver actions."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.home()/'.hermes/hermes-agent'))
from unittest.mock import Mock
import unittest
from tools.computer_use.cua_backend import CuaDriverBackend


class PointerWindowTests(unittest.TestCase):
    def test_pointer_actions_use_captured_window_and_refuse_missing_window(self):
        cases = [('click', {'x':10, 'y':20}),
                 ('click', {'element':7}),
                 ('click', {'x':10, 'y':20, 'click_count':2}),
                 ('drag', {'from_xy':(1,2), 'to_xy':(3,4)}),
                 ('scroll', {'direction':'down', 'x':10, 'y':20})]
        for method, args in cases:
            with self.subTest(method=method, args=args):
                backend = object.__new__(CuaDriverBackend)
                backend._active_pid = 123
                backend._active_window_id = 456
                backend._action = Mock()
                getattr(backend, method)(**args)
                self.assertEqual(backend._action.call_args.args[1]['window_id'], 456)
                self.assertEqual(backend._action.call_args.args[1]['pid'], 123)
                backend._active_window_id = None
                backend._action.reset_mock()
                self.assertFalse(getattr(backend, method)(**args).ok)
                backend._action.assert_not_called()


if __name__ == '__main__': unittest.main()
