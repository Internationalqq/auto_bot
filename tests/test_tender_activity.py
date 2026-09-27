from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from autobot import tender_activity as activity


class ActivityTests(unittest.TestCase):
    def test_progress_assets_use_existing_tender_proxy_with_correct_mime(self):
        from flask import Flask
        from autobot.buyer_routes import blueprint
        app=Flask(__name__);app.register_blueprint(blueprint)
        with app.test_client() as client:
            for ext,mime in [('js','javascript'),('css','text/css')]:
                response=client.get('/tenders/progress.'+ext)
                self.assertEqual(response.status_code,200)
                self.assertIn(mime,response.content_type)
                response.close()
            self.assertEqual(client.get('/tenders/progress.py').status_code,404)

    def test_recent_work_survives_restart_and_ignores_background_updates(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(activity,'DATA_DIR',Path(temp)), patch.object(activity,'DB_PATH',Path(temp)/'activity.db'):
            with closing(sqlite3.connect(Path(temp)/'buyer_outbox.sqlite3')) as db, db:
                db.execute('CREATE TABLE buyer_campaigns(tender_id TEXT,created_at REAL,updated_at REAL)')
                db.execute("INSERT INTO buyer_campaigns VALUES ('12345678',100,999999)")
            self.assertEqual(activity.listing()['12345678']['acted_at'],100)
            with patch.object(activity.time,'time',return_value=200):activity.record('87654321','open')
            self.assertEqual(activity.listing()['87654321']['acted_at'],200)
            self.assertEqual(activity.listing()['12345678']['acted_at'],100)
            with self.assertRaises(ValueError):activity.record('../secret','open')
            with self.assertRaises(ValueError):activity.record('12345678','poll')
