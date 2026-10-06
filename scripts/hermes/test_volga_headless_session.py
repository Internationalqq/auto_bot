import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from volga_headless_session import viewport_capture


def png_header(path, width, height):
    path.write_bytes(b'\x89PNG\r\n\x1a\n'+struct.pack('>I',13)+b'IHDR'+struct.pack('>II',width,height))


class ViewportTests(unittest.TestCase):
    def test_browser_vision_full_page_becomes_viewport_preserving_annotation(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'screen.png';png_header(path,1280,720)
            backend=Mock(return_value={'success':True,'data':{'path':str(path),'annotations':[1]}})
            out=viewport_capture(backend,'task','screenshot',['--annotate','--full',str(path)],timeout=30)
            backend.assert_called_once_with('task','screenshot',['--annotate',str(path)],timeout=30)
            self.assertTrue(out['success']);self.assertEqual(out['image_dimensions'],[1280,720])
            self.assertEqual(out['data']['annotations'],[1])

    def test_actual_failed_dimensions_rejected_without_reading_pixel_data(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'huge.png';png_header(path,1280,117625)
            original=path.read_bytes()
            backend=Mock(return_value={'success':True,'data':{'path':str(path)}})
            out=viewport_capture(backend,'task','screenshot',['--full',str(path)])
            self.assertFalse(out['success']);self.assertNotIn('data',out)
            self.assertIn('browser_snapshot',out['error']);self.assertEqual(path.read_bytes(),original)

    def test_non_image_actions_and_access_errors_unchanged(self):
        result={'success':False,'error':'access denied'};backend=Mock(return_value=result)
        self.assertIs(viewport_capture(backend,'task','open',['https://example.com']),result)
        self.assertIs(viewport_capture(backend,'task','screenshot',['--full','x.png']),result)

    def test_missing_or_invalid_image_is_recoverable_tool_error(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'bad.png'
            backend=Mock(return_value={'success':True,'data':{'path':str(path)}})
            self.assertFalse(viewport_capture(backend,'task','screenshot',[str(path)])['success'])
            path.write_text('not a screenshot')
            self.assertFalse(viewport_capture(backend,'task','screenshot',[str(path)])['success'])


if __name__ == '__main__':unittest.main()
