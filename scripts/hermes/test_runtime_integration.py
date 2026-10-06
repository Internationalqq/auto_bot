"""Run with pytest inside the target Hermes checkout after deployment."""
from unittest.mock import Mock, patch
import json
from tools.computer_use import tool
from tools.computer_use.backend import CaptureResult, ActionResult
from tools.computer_use.cua_backend import CuaDriverBackend


def test_capture_recovery_reads_again_and_never_replays_input():
    backend=Mock()
    backend.capture.side_effect=[RuntimeError('No on-screen windows available'),CaptureResult(mode='ax',width=100,height=100,elements=[],app='Firefox')]
    with patch('tools.computer_use.team_runtime.policy_context',return_value=True), patch('tools.computer_use.team_runtime.recover_browser') as recover:
        tool._dispatch(backend,'capture',{'app':'Firefox','mode':'ax'})
        recover.assert_called_once_with(backend,'Firefox')
    assert backend.capture.call_count==2
    backend.click.assert_not_called()
    backend.type_text.assert_not_called()


def test_declined_capture_is_not_recovered():
    backend=Mock()
    backend.capture.side_effect=RuntimeError('permission denied')
    with patch('tools.computer_use.team_runtime.policy_context',return_value=True), patch('tools.computer_use.team_runtime.recover_browser') as recover:
        try:tool._dispatch(backend,'capture',{'app':'Firefox','mode':'ax'})
        except RuntimeError:pass
        else:assert False
        recover.assert_not_called()


def test_lock_error_not_reported_as_user_refusal():
    with patch.object(tool,'_session_auto_approve',False),patch.object(tool,'_always_allow',set()),patch.object(tool,'_approval_callback',return_value='deny:assigned_app_and_browser_lock_required'):
        out=json.loads(tool._request_approval('focus_app',{'app':'Firefox'}))
        assert out['error']=='approval_required'


def test_visible_window_precedes_stale_hidden_overlays():
    b=object.__new__(CuaDriverBackend)
    b._active_window_id=None
    b._session_id='test'
    b._allowed_apps=('Google Chrome',)
    b._session=Mock()
    hidden=dict(app_name='Google Chrome',pid=7,window_id=8,z_index=200,is_on_screen=False)
    visible=dict(app_name='Google Chrome',pid=7,window_id=9,z_index=1,is_on_screen=True)
    b._session.call_tool.return_value={'structuredContent':{'windows':[hidden,visible]}}
    b._select_content_window=Mock(return_value=(visible,{}))
    b.bring_to_front=Mock(return_value=ActionResult(ok=True,action='bring_to_front'))
    assert b.focus_app('Google Chrome',raise_window=True).ok
    assert [w['window_id'] for w in b._select_content_window.call_args.args[0]]==[9]
