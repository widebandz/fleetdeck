"""Isolated Trace handoff tests: no live messages, service requests or downloads."""
import json
import os
from pathlib import Path
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import adopt_cli as cli
import adopt_video as video


def receipt(source='imsg:1:4965'):
    return {'source': source, 'chat_id': 1, 'text': 'https://www.instagram.com/reel/DeFGs1_sk3Y/?dlrf=fixture',
        'record': {'chat_id': 1, 'is_group': False}}


class HandoffTests(unittest.TestCase):
    def test_receipt_identity_and_video_url_must_match_real_operator_channel(self):
        source = 'imsg:1:4965'
        self.assertEqual(cli.receipt_url(receipt(), source), 'https://www.instagram.com/reel/DeFGs1_sk3Y/')
        for changed in ({'chat_id': 2}, {'source': 'imsg:1:other'},
                        {'record': {'chat_id': 1, 'is_group': True}}, {'text': 'Copied pane text without a link'},
                        {'text': 'https://example.org/a.mp4 https://example.org/b.mp4'}):
            with self.subTest(changed=changed), self.assertRaises(video.VideoError):
                cli.receipt_url(dict(receipt(), **changed), source)

    def test_authenticated_terminal_receipt_preserves_its_transport(self):
        source = 'terminal:11111111-2222-3333-4444-555555555555'
        message = dict(receipt(source), chat_id=None, record={'origin': 'terminal', 'transport': 'ssh',
            'operator': {'uid': os.getuid()}}, terminal_delivery={'state': 'delivered'})
        self.assertIn('/DeFGs1_sk3Y/', cli.receipt_url(message, source))
        message['record']['operator']['uid'] = os.getuid() + 1
        with self.assertRaises(video.VideoError):
            cli.receipt_url(message, source)

    def test_submit_uses_persistent_service_queue_and_deduplicates_after_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            store = video.VideoStore(directory)
            calls = []
            def response(path, body=None):
                calls.append((path, body))
                if path == '/api/video/state':
                    return {'ok': True, 'token': 'private-request-token', 'preflight': {'worker_running': True}}
                if path == '/api/video/inspect':
                    identifier = str(len(calls)).zfill(32)
                    source = {'provider': 'instagram', 'source_id': 'DeFGs1_sk3Y', 'url': body['url'],
                        'public_url': body['url'], 'title': 'Pilot', 'retrieved_at': time.time()}
                    with store.db() as db:
                        db.execute('INSERT INTO inspections VALUES(?,?,?)', (identifier, time.time(), json.dumps(source)))
                    return {'ok': True, 'preview': {'inspection': identifier}}
                self.assertEqual(path, '/api/video/jobs')
                self.assertIs(body['confirmed'], True)
                return {'ok': True, 'job': store.enqueue(body['inspection'], body['confirmed'])}
            client = cli.Client()
            with patch.object(client, 'request', side_effect=response):
                first = client.submit('imsg:1:4965', receipt())
                second = client.submit('imsg:1:4965', receipt())
            store.close()
            reopened = video.VideoStore(directory)
            self.addCleanup(reopened.close)
            self.assertEqual(first['job_id'], second['job_id'])
            self.assertEqual(len(reopened.jobs()), 1)
            self.assertEqual(reopened.get(first['job_id'])['state'], 'queued')
            self.assertTrue(first['processing_continues_in_service'])
            self.assertNotIn('private-request-token', json.dumps(first))
            self.assertNotIn('dlrf=', json.dumps(first))

    def test_unavailable_worker_does_not_inspect_or_claim_a_queue_entry(self):
        client = cli.Client()
        with patch.object(client, 'request', return_value={'token': 'private', 'preflight': {'worker_running': False}}) as request:
            with self.assertRaisesRegex(video.VideoError, 'worker is unavailable'):
                client.submit('imsg:1:4965', receipt())
        self.assertEqual(request.call_count, 1)

    def test_transport_failure_has_no_completed_submission_claim(self):
        connection = types.SimpleNamespace(request=lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError(1, 'Operation not permitted')), close=lambda: None)
        with patch.object(cli.http.client, 'HTTPConnection', return_value=connection):
            with self.assertRaisesRegex(video.VideoError, 'No completed submission is confirmed'):
                cli.Client().ready()

    def test_actual_client_sends_token_to_closed_local_routes_and_reports_review_state(self):
        sent = []
        job = {'id': 'VID-0123456789abcdef', 'state': 'needs_attention', 'stage': 'download',
               'message': 'Source requires attention.', 'files': {}}
        replies = [{'ok': True, 'token': 'private-token', 'preflight': {'worker_running': True}},
                   {'ok': True, 'preview': {'inspection': '0' * 32}}, {'ok': True, 'job': job}]
        def connection(host, port, timeout):
            self.assertEqual((host, port), ('127.0.0.1', 8794))
            return types.SimpleNamespace(request=lambda *args, **kwargs: sent.append((args, kwargs)),
                getresponse=lambda: types.SimpleNamespace(status=200, read=lambda limit: json.dumps(replies.pop(0)).encode()), close=lambda: None)
        with patch.object(cli.http.client, 'HTTPConnection', side_effect=connection):
            result = cli.Client().submit('imsg:1:4965', receipt())
        self.assertEqual(sent[1][1]['headers']['X-Adopt-Token'], 'private-token')
        self.assertEqual(result['state'], 'needs_attention')
        self.assertFalse(result['processing_continues_in_service'])
        self.assertTrue(result['private_url'].endswith('#' + job['id']))


if __name__ == '__main__':
    unittest.main()
