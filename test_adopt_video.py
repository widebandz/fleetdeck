"""Isolated regressions: no source downloads, model calls or live Work writes."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from email.message import Message
import zipfile

import adopt_video as video
import adopt_server


class VideoTests(unittest.TestCase):
    @staticmethod
    def plan_fixture():
        return {'title': '<script>alert(1)</script>', 'goal': 'Produce a grounded report.',
                'skill_name': 'fixture-report-workflow', 'skill_description': 'Prepare a cited report when the operator requests it.',
                'inputs': ['Operator request'], 'success_criteria': ['Report is traceable to source evidence.'],
                'open_questions': ['Which output audience?'], 'steps': [{'title': 'Prepare report',
                'action': 'Draft the requested report.', 'deliverable': 'Cited draft report',
                'check': 'Every source claim has a citation.', 'evidence': ['S0001'], 'quote': 'Export a private report'}]}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='adopt-video-test-')
        self.addCleanup(self.temp.cleanup)
        self.store = video.VideoStore(self.temp.name)
        self.addCleanup(self.store.close)

    def job(self):
        source = {'url': 'https://example.org/demo.mp4?secret=private',
                  'public_url': 'https://example.org/demo.mp4', 'provider': 'direct',
                  'source_id': 'fixture', 'title': '<script>alert(1)</script>',
                  'retrieved_at': time.time()}
        with self.store.db() as db:
            db.execute('INSERT INTO inspections VALUES(?,?,?)', ('preview', time.time(), json.dumps(source)))
        return self.store.enqueue('preview', True)['id']

    def handler(self, path, body=None, **headers):
        handler = object.__new__(adopt_server.Handler)
        handler.server = types.SimpleNamespace(video=self.store)
        handler.path = path
        handler.command = 'POST' if body is not None else 'GET'
        handler.headers = Message()
        for key, value in {'Host': 'localhost:8794', **headers}.items():
            handler.headers[key] = value
        raw = json.dumps(body).encode() if body is not None else b''
        if 'Content-Length' not in headers:
            handler.headers['Content-Length'] = str(len(raw))
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.allowed = lambda: True
        handler.close_connection = False
        handler.result = None
        handler._send = lambda status, value, mime='application/json': setattr(handler, 'result', (status, value, mime))
        return handler

    def test_url_identity_and_access_boundaries(self):
        a = video.source_url('https://youtu.be/abcdefghijk?t=20')
        self.assertEqual(a, video.source_url('https://www.youtube.com/watch?v=abcdefghijk'))
        for url in ('http://example.org/a.mp4', 'https://user:pass@example.org/a.mp4',
                    'https://example.org:8000/a.mp4', 'https://youtube.com/playlist?list=123'):
            with self.subTest(url=url), self.assertRaises(video.VideoError):
                video.source_url(url)
        for ip in ('127.0.0.1', '100.93.175.16', '169.254.169.254', '::1'):
            with patch.object(video.socket, 'getaddrinfo', return_value=[(0, 0, 0, '', (ip, 443))]):
                with self.assertRaises(video.VideoError):
                    video.public_addresses('untrusted.example')

    def test_redirect_revalidates_destination(self):
        response = types.SimpleNamespace(status=302, getheader=lambda name: 'https://127.0.0.1/a.mp4')
        connection = types.SimpleNamespace(request=lambda *a, **k: None,
                                           getresponse=lambda: response, close=lambda: None)
        with patch.object(video, 'public_addresses', side_effect=[['93.184.216.34'], video.VideoError('Private host')]) as resolve:
            with patch.object(video.http.client, 'HTTPSConnection', return_value=connection):
                with self.assertRaisesRegex(video.VideoError, 'Private host'):
                    video.public_request('https://example.org/a.mp4')
        self.assertEqual(resolve.call_count, 2)

    def test_public_webpage_and_stream_urls_are_accepted_without_exporting_query(self):
        for url in ('https://vimeo.com/123456789', 'https://www.tiktok.com/@creator/video/123',
                    'https://x.com/creator/status/123', 'https://example.org/article?token=private',
                    'https://example.org/master.m3u8'):
            with self.subTest(url=url):
                clean, provider, identifier = video.source_url(url)
                self.assertEqual(provider, 'webpage')
                self.assertTrue(identifier)
                self.assertEqual(clean, url)
        self.assertEqual(video.source_url('https://instagram.com/p/DeccYbQveoj/')[1], 'instagram')
        self.assertEqual(video.source_url('https://youtube.com/live/abcdefghijk')[2], 'abcdefghijk')

    def test_webpage_inspection_uses_guarded_extractor_and_selects_public_video(self):
        metadata = {'id': 'public-clip', 'extractor_key': 'Fixture', 'title': 'Embedded video', 'duration': 12,
            'formats': [{'url': 'https://cdn.example/clip.webm?token=private', 'ext': 'webm',
                         'protocol': 'https', 'vcodec': 'vp9', 'acodec': 'opus'}]}
        with patch.object(video, 'public_addresses', return_value=['93.184.216.34']), patch.object(self.store, 'run', return_value=json.dumps(metadata)) as run:
            preview = self.store.inspect('https://example.org/page?token=private')
        self.assertIn('adopt_extract.py', run.call_args.args[0][1])
        self.assertNotIn('token=', preview['source_url'])
        with self.store.db() as db:
            source = json.loads(db.execute('SELECT payload FROM inspections WHERE id=?', (preview['inspection'],)).fetchone()[0])
        self.assertEqual(source['extractor'], 'Fixture')
        self.assertEqual(source['formats'][0]['ext'], 'webm')

    def test_webpage_rejects_drm_multiple_items_and_no_video(self):
        for metadata, message in [({'has_drm': True}, 'DRM protected'),
                ({'_type': 'playlist', 'entries': [{}, {}]}, 'single completed video'),
                ({'formats': []}, 'No supported HTTPS video')]:
            with self.subTest(metadata=metadata):
                with patch.object(video, 'public_addresses', return_value=['93.184.216.34']), patch.object(self.store, 'run', return_value=json.dumps(metadata)):
                    with self.assertRaisesRegex(video.VideoError, message):
                        self.store.inspect('https://example.org/page')

    def test_segmented_stream_handoff_is_bounded_and_final_media_is_probed(self):
        job_id = self.job()
        directory = self.store.directory(job_id)
        row = self.store._row(job_id)
        row['source'] = dict(row['source'], provider='webpage', formats=[{
            'url': 'https://cdn.example/vod.m3u8', 'ext': 'mp4', 'protocol': 'm3u8_native'}])
        probe = {'format': {'duration': '8'}, 'streams': [
            {'codec_type': 'video', 'width': 640, 'height': 360}, {'codec_type': 'audio'}]}
        def run(argv, cwd, timeout, offline=False):
            if argv[0] == 'ffprobe':
                self.assertTrue(offline)
                return json.dumps(probe)
            if argv[0] == 'ffmpeg':
                self.assertTrue(offline)
                Path(argv[-1]).write_bytes(b'remuxed fixture')
                return ''
            self.assertEqual(argv[-2], 'download')
            payload = json.loads(Path(argv[-1]).read_text())
            self.assertEqual(payload['remaining_bytes'], video.MAX_VIDEO)
            self.assertEqual(payload['target'], 'acquired-0.mp4')
            (directory / payload['target']).write_bytes(b'fixture stream')
            return '{}'
        with patch.object(self.store, 'run', side_effect=run), patch.object(video, 'public_request', side_effect=AssertionError('Native helper owns segmented requests')):
            outputs = self.store._download(row)
        self.assertIn('video', outputs)
        self.assertIn('_probe', outputs)

    def test_refresh_keeps_expiring_urls_but_discards_changed_stream_formats(self):
        source = {'url': 'https://example.org/page', 'formats': [
            {'format_id': 'video', 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'none', 'url': 'https://cdn.example/old'},
            {'format_id': 'audio', 'ext': 'm4a', 'vcodec': 'none', 'acodec': 'aac'}]}
        outputs = {'_stream0': {'path': 'video.mp4'}, '_stream1': {'path': 'audio.m4a'}, 'transcript': {'path': 'transcript.txt'}}
        refreshed = json.loads(json.dumps(source));refreshed['formats'][0]['url'] = 'https://cdn.example/new'
        self.assertEqual(video.refreshed_streams(outputs, source, refreshed), outputs)
        refreshed['formats'] = [{'format_id': 'combined', 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'aac'}]
        self.assertEqual(video.refreshed_streams(outputs, source, refreshed), {'transcript': outputs['transcript']})

    def test_outdated_youtube_extractor_has_an_actionable_failure(self):
        completed = types.SimpleNamespace(returncode=0, stdout='2026.07.04\n')
        with patch.object(video.shutil, 'which', return_value='/fixture/yt-dlp'), patch.object(video.subprocess, 'run', return_value=completed):
            self.assertEqual(self.store.extractor_status()['state'], 'outdated')
            with self.assertRaisesRegex(video.VideoError, 'YouTube downloader needs updating'):
                self.store.require_youtube_extractor()
        self.store.extractor_cache = None
        completed.stdout = '2026.08.19\n'
        with patch.object(video.shutil, 'which', return_value='/fixture/yt-dlp'), patch.object(video.subprocess, 'run', return_value=completed):
            self.assertEqual(self.store.extractor_status()['state'], 'current')
            self.store.require_youtube_extractor()

    def test_403_does_not_assert_the_user_lacks_source_permission(self):
        response = types.SimpleNamespace(status=403)
        connection = types.SimpleNamespace(request=lambda *a, **k: None, getresponse=lambda: response, close=lambda: None)
        with patch.object(video, 'public_addresses', return_value=['93.184.216.34']), patch.object(video.http.client, 'HTTPSConnection', return_value=connection):
            with self.assertRaisesRegex(video.VideoError, 'HTTP 403.*downloader compatibility'):
                video.public_request('https://example.org/a.mp4')

    def test_retry_refreshes_broken_youtube_client_even_when_url_is_fresh(self):
        job_id = self.job()
        row = self.store._row(job_id)
        source = dict(row['source'], provider='youtube',
                      formats=[{'url': 'https://media.googlevideo.com/old?c=ANDROID_VR', 'ext': 'mp4'}])
        refreshed = dict(source, formats=[{'url': 'https://media.googlevideo.com/fresh?c=VISIONOS', 'ext': 'mp4'}])
        row['source'] = source
        with self.store.db() as db:
            db.execute('INSERT INTO inspections VALUES(?,?,?)', ('fresh', time.time(), json.dumps(refreshed)))
        with patch.object(self.store, 'require_youtube_extractor'), patch.object(self.store, 'inspect', return_value={'inspection': 'fresh'}) as inspect:
            with patch.object(video, 'public_request', side_effect=video.VideoError('Fixture stops before download')) as request:
                with self.assertRaisesRegex(video.VideoError, 'Fixture stops'):
                    self.store._download(row)
                self.assertEqual(request.call_args.args[0], refreshed['formats'][0]['url'])
            inspect.assert_called_once()
        self.assertEqual(self.store._row(job_id)['source']['formats'], refreshed['formats'])

    def test_completed_media_retry_does_not_need_expired_provider_access(self):
        job_id = self.job()
        for provider in ('instagram', 'youtube'):
            with self.subTest(provider=provider):
                row = self.store._row(job_id)
                row['source'] = dict(row['source'], provider=provider, retrieved_at=0,
                    formats=[{'url': 'https://expired.example/saved', 'ext': 'mp4'}])
                path = self.store.directory(job_id) / 'saved-source.mp4'
                path.write_bytes(b'Already acquired video and audio')
                self.store.record(row['outputs'], '_stream0', path)
                probe = {'format': {'duration': '8'}, 'streams': [
                    {'codec_type': 'video', 'width': 640, 'height': 360}, {'codec_type': 'audio'}]}
                with patch.object(self.store, 'inspect', side_effect=AssertionError('Saved media must not need reinspection')):
                    with patch.object(self.store, 'require_youtube_extractor', side_effect=AssertionError('No new download required')):
                        with patch.object(video, 'public_request', side_effect=AssertionError('No new download required')):
                            with patch.object(self.store, 'run', return_value=json.dumps(probe)):
                                outputs = self.store._download(row)
                self.assertEqual((self.store.directory(job_id) / outputs['video']['path']).read_bytes(), path.read_bytes())

    def test_instagram_identity_drops_tracking_and_rejects_other_post_types(self):
        expected = ('https://www.instagram.com/reel/DeccYbQveoj/', 'instagram', 'DeccYbQveoj')
        self.assertEqual(video.source_url('https://www.instagram.com/reel/DeccYbQveoj/?obrf=tracking'), expected)
        self.assertEqual(video.source_url('https://instagram.com/reels/DeccYbQveoj/'), expected)
        for value in ('https://instagram.com/someprofile/', 'https://instagram.com/stories/person/123/',
                      'https://instagram.com/reel/DeccYbQveoj/extra/'):
            with self.subTest(value=value), self.assertRaises(video.VideoError):
                video.source_url(value)

    def test_instagram_inspection_and_download_keep_provider_boundary(self):
        metadata = {'id': 'DeccYbQveoj', 'title': 'Fixture reel', 'duration': 8,
            'formats': [{'format_id': 'fixture', 'ext': 'mp4', 'protocol': 'https',
                         'url': 'https://scontent.cdninstagram.com/reel.mp4?token=private',
                         'width': 720, 'height': 1280,
                         'http_headers': {'User-Agent': 'Fixture', 'Referer': 'https://www.instagram.com/',
                                          'Cookie': 'must-not-forward', 'Authorization': 'must-not-forward'}}]}
        with patch.object(video, 'public_addresses', return_value=['93.184.216.34']), patch.object(self.store, 'run', return_value=json.dumps(metadata)) as run:
            with patch.object(self.store, 'require_youtube_extractor', side_effect=AssertionError('YouTube gate must not block Instagram')):
                preview = self.store.inspect('https://www.instagram.com/reel/DeccYbQveoj/?obrf=tracking')
        self.assertIn('instagram', run.call_args.args[0])
        self.assertEqual(preview['source_url'], 'https://www.instagram.com/reel/DeccYbQveoj/')
        job_id = self.store.enqueue(preview['inspection'], True)['id']
        with patch.object(video, 'public_request', side_effect=video.VideoError('Fixture stops before networking')) as request:
            with self.assertRaisesRegex(video.VideoError, 'Fixture stops'):
                self.store._download(self.store._row(job_id))
        self.assertEqual(request.call_args.kwargs['allowed_hosts'], video.MEDIA_HOSTS['instagram'])
        headers = request.call_args.args[1]
        self.assertEqual(headers['Referer'], 'https://www.instagram.com/')
        self.assertNotIn('Cookie', headers)
        self.assertNotIn('Authorization', headers)

    def test_portrait_limit_and_instagram_redirect_hosts(self):
        self.assertTrue(video.bounded_dimensions(720, 1280))
        self.assertTrue(video.bounded_dimensions(1280, 720))
        self.assertFalse(video.bounded_dimensions(1080, 1920))
        self.assertFalse(video.bounded_dimensions(3000, 400))
        response = types.SimpleNamespace(status=302, getheader=lambda name: 'https://unrelated.example/video.mp4')
        connection = types.SimpleNamespace(request=lambda *a, **k: None, getresponse=lambda: response, close=lambda: None)
        with patch.object(video, 'public_addresses', return_value=['93.184.216.34']), patch.object(video.http.client, 'HTTPSConnection', return_value=connection):
            with self.assertRaisesRegex(video.VideoError, 'unexpected media host'):
                video.public_request('https://scontent.cdninstagram.com/video.mp4', allowed_hosts=video.MEDIA_HOSTS['instagram'])

    def test_missing_duration_is_deferred_and_numeric_text_is_normalized(self):
        metadata = {'title': 'Fixture reel', 'formats': [{'format_id': 'fixture', 'ext': 'mp4',
            'protocol': 'https', 'url': 'https://scontent.cdninstagram.com/reel.mp4', 'width': 720, 'height': 1280}]}
        for value, expected in [(None, None), ('8.25', 8.25), ('unknown', None), (0, None), (False, None), (float('nan'), None)]:
            with self.subTest(duration=value):
                metadata['duration'] = value
                with patch.object(video, 'public_addresses', return_value=['93.184.216.34']), patch.object(self.store, 'run', return_value=json.dumps(metadata)):
                    preview = self.store.inspect('https://www.instagram.com/reel/DeccYbQveoj/')
                self.assertEqual(preview['duration'], expected)
                with self.store.db() as db:
                    source = json.loads(db.execute('SELECT payload FROM inspections WHERE id=?', (preview['inspection'],)).fetchone()[0])
                self.assertEqual(source['duration_verification'], 'pending_media_probe' if expected is None else 'reported')

    def test_live_and_known_overlong_metadata_are_rejected_with_distinct_errors(self):
        for metadata, message in [({'duration': video.MAX_SECONDS + 1}, 'longer than the 90-minute'),
                                  ({'duration': 8, 'is_live': True}, 'live or upcoming'),
                                  ({'live_status': 'is_upcoming'}, 'live or upcoming')]:
            with self.subTest(metadata=metadata):
                with patch.object(video, 'public_addresses', return_value=['93.184.216.34']), patch.object(self.store, 'run', return_value=json.dumps(metadata)):
                    with self.assertRaisesRegex(video.VideoError, message):
                        self.store.inspect('https://www.instagram.com/reel/DeccYbQveoj/')

    def measured_download(self, duration):
        job_id = self.job()  # Fixture source intentionally has no reported duration.
        probe = {'format': {'duration': str(duration)},
                 'streams': [{'codec_type': 'video', 'width': 640, 'height': 360}, {'codec_type': 'audio'}]}
        response = io.BytesIO(b'fixture video bytes')
        connection = types.SimpleNamespace(close=lambda: None)
        with patch.object(video, 'public_request', return_value=(connection, response)), patch.object(self.store, 'run', return_value=json.dumps(probe)):
            self.store._step(job_id)
        return self.store._row(job_id)

    def test_actual_media_duration_is_recorded_before_transcription(self):
        row = self.measured_download(8.25)
        self.assertEqual(row['state'], 'queued')
        self.assertEqual(row['stage'], 'audio')
        self.assertEqual(row['source']['duration'], 8.25)
        self.assertIsNone(row['source']['reported_duration'])
        self.assertEqual(row['source']['duration_verification'], 'media_probe')
        self.assertIn('_probe', row['outputs'])

    def test_actual_media_over_limit_stops_without_starting_transcription(self):
        row = self.measured_download(video.MAX_SECONDS + 1)
        self.assertEqual(row['state'], 'needs_attention')
        self.assertEqual(row['stage'], 'download')
        self.assertIn('exceeds the 90-minute', row['message'])
        self.assertNotIn('audio', row['outputs'])
        self.assertNotIn('segments', row['outputs'])

    def test_dedup_and_confirmation_survive_reopen(self):
        job_id = self.job()
        with self.assertRaises(video.VideoError):
            self.store.enqueue('preview', False)
        second = video.VideoStore(self.temp.name)
        self.addCleanup(second.close)
        self.assertEqual(second.enqueue('preview', True)['id'], job_id)
        self.assertEqual(len(second.jobs()), 1)
        self.assertNotIn('secret', json.dumps(second.jobs()))

    def test_restart_recovers_working_job_and_cancel_is_durable(self):
        job_id = self.job()
        with self.store.db() as db:
            db.execute("UPDATE jobs SET state='working' WHERE id=?", (job_id,))
        # Exercise startup recovery without letting a worker download anything.
        with patch.object(video.threading.Thread, 'start'):
            self.store.start()
        self.assertEqual(self.store.get(job_id)['state'], 'queued')
        self.assertEqual(self.store.action(job_id, 'cancel')['state'], 'cancelled')
        self.assertEqual(video.VideoStore(self.temp.name).get(job_id)['state'], 'cancelled')

    def test_retry_uses_checkpoint_and_waiting_time_is_not_processing_time(self):
        job_id = self.job()
        directory = self.store.directory(job_id)
        evidence = directory / 'kept.bin'
        evidence.write_bytes(b'preserved')
        outputs = {}
        self.store.record(outputs, '_stream0', evidence)
        with self.store.db() as db:
            db.execute('UPDATE jobs SET created=?,outputs=? WHERE id=?', (time.time() - 86400, json.dumps(outputs), job_id))
        with patch.object(self.store, '_download', side_effect=video.VideoError('Transient failure', retry=True)):
            self.store._step(job_id)
        self.assertEqual(self.store.get(job_id)['state'], 'retry_scheduled')
        self.store.action(job_id, 'retry')
        with patch.object(self.store, '_download', return_value=outputs):
            self.store._step(job_id)
        self.assertEqual(self.store.get(job_id)['stage'], 'audio')
        self.assertTrue(self.store.valid(self.store._row(job_id), '_stream0'))

    def test_citations_reject_fabricated_quotes_and_segment_ids(self):
        segments = [{'id': 'S0001', 'text': 'The operator can export a private report.'}]
        data = {'requirements': [{'text': 'Export reports', 'kind': 'observed',
                                  'evidence': ['S0001'], 'quote': 'export a private report'}],
                'tasks': [{'title': 'Add export action', 'requirement_index': 0}], 'open_questions': []}
        requirements, tasks, _ = self.store.validate_analysis(data, segments)
        self.assertEqual(tasks[0]['requirements'], [requirements[0]['id']])
        data['requirements'][0]['quote'] = 'Unsupported invented functionality'
        with self.assertRaises(video.VideoError):
            self.store.validate_analysis(data, segments)
        data['requirements'][0]['evidence'] = ['S9999']
        with self.assertRaises(video.VideoError):
            self.store.validate_analysis(data, segments)

    def test_csrf_and_body_framing(self):
        h = self.handler('/api/video/jobs', {'inspection': 'anything', 'confirmed': True})
        h.do_POST()
        self.assertEqual(h.result[0], 403)
        h = self.handler('/api/video/jobs', {}, **{'X-Adopt-Token': self.store.token, 'Origin': 'https://evil.example'})
        h.do_POST()
        self.assertEqual(h.result[0], 403)
        h = self.handler('/api/video/jobs', {}, **{'Content-Length': '99999999'})
        h.do_POST()
        self.assertEqual(h.result[0], 413)
        self.assertTrue(h.close_connection)
        h = self.handler('/api/video/jobs', [])
        h.do_POST()
        self.assertEqual(h.result[0], 400)

    def test_overlapping_transcript_chunks_keep_order_and_clip_duration(self):
        job_id = self.job()
        directory = self.store.directory(job_id)
        model = directory / 'fixture-model.bin'
        model.write_bytes(b'fixture model')
        self.store.whisper_model = model
        audio = directory / 'audio.wav'
        audio.write_bytes(b'fixture audio')
        outputs = {}
        self.store.record(outputs, 'audio', audio)
        video.atomic_json(directory / 'ffprobe.json', {'format': {'duration': '30.5'}})
        recipe = {'model_sha256': video.sha(model), 'audio_sha256': video.sha(audio),
                  'language': 'en', 'first_chunk_seconds': 30, 'chunk_seconds': 120, 'overlap_seconds': 2}
        for chunk, text, end in [(0, 'Export a private report now', 29000),
                                 (1, 'report now with timestamps', 5000)]:
            video.atomic_json(directory / f'chunk-{chunk:03}.json', {'_recipe': recipe,
                'transcription': [{'offsets': {'from': 0, 'to': end}, 'text': text}]})
        row = self.store._row(job_id)
        row['outputs'] = outputs
        with patch.object(video.shutil, 'which', return_value='/fixture/whisper-cli'), patch.object(self.store, 'run', side_effect=AssertionError('Completed chunks must be reused')):
            result = self.store._transcribe(row)
        segments = json.loads((directory / result['segments']['path']).read_text())
        self.assertEqual(segments[1]['text'], 'with timestamps')
        self.assertEqual(segments[1]['start_ms'], 29000)
        self.assertEqual(segments[1]['end_ms'], 30500)
        self.assertTrue(segments[1]['boundary_review'])
        self.assertEqual(result['vtt']['mime'], 'text/vtt; charset=utf-8')

    def test_running_cancel_and_storage_budget_stop_before_next_stage(self):
        job_id = self.job()
        def cancel(row):
            self.store.action(job_id, 'cancel')
            return {}
        with patch.object(self.store, '_download', side_effect=cancel):
            self.store._step(job_id)
        self.assertEqual(self.store.get(job_id)['state'], 'cancelled')
        with self.store.db() as db:
            db.execute("UPDATE jobs SET state='queued',cancel=0 WHERE id=?", (job_id,))
        (self.store.directory(job_id) / 'existing.bin').write_bytes(b'large')
        with patch.object(video, 'MAX_JOB', 1), patch.object(self.store, '_download') as download:
            self.store._step(job_id)
            download.assert_not_called()
        self.assertEqual(self.store.get(job_id)['state'], 'needs_attention')

    def speech_fixture(self, duration):
        job_id = self.job()
        directory = self.store.directory(job_id)
        model = directory / 'fixture-model.bin'
        model.write_bytes(b'fixture model')
        self.store.whisper_model = model
        audio = directory / 'audio.wav'
        audio.write_bytes(b'fixture audio')
        outputs = {}
        self.store.record(outputs, 'audio', audio)
        video.atomic_json(directory / 'ffprobe.json', {'format': {'duration': str(duration)}})
        recipe = {'model_sha256': video.sha(model), 'audio_sha256': video.sha(audio),
                  'language': 'en', 'first_chunk_seconds': 30, 'chunk_seconds': 120, 'overlap_seconds': 2}
        video.atomic_json(directory / 'chunk-000.json', {'_recipe': recipe,
            'transcription': [{'offsets': {'from': 0, 'to': 8000}, 'text': 'First usable transcript.'}]})
        with self.store.db() as db:
            db.execute("UPDATE jobs SET stage='transcribe',outputs=? WHERE id=?", (json.dumps(outputs), job_id))
        return job_id

    def test_partial_transcript_is_readable_and_durable_when_later_asr_fails(self):
        job_id = self.speech_fixture(180)
        with patch.object(video.shutil, 'which', return_value='/fixture/whisper-cli'), patch.object(self.store, 'run', side_effect=video.VideoError('Later audio processing failed')):
            self.store._step(job_id)
        job = self.store.get(job_id)
        self.assertEqual(job['state'], 'needs_attention')
        self.assertEqual(job['transcript']['status'], 'partial')
        self.assertEqual(job['transcript']['processed_seconds'], 30)
        self.assertIn('transcript', job['files'])
        path, _ = self.store.file(job_id, 'transcript')
        self.assertIn('First usable transcript.', path.read_text())
        reopened = video.VideoStore(self.temp.name)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get(job_id)['transcript']['status'], 'partial')
        self.assertNotIn('requirements', job['files'])

    def test_complete_transcript_is_available_before_frames_or_model_work(self):
        job_id = self.speech_fixture(8)
        with patch.object(video.shutil, 'which', return_value='/fixture/whisper-cli'), patch.object(self.store, 'run', side_effect=AssertionError('Completed audio chunk must be reused')):
            self.store._step(job_id)
        job = self.store.get(job_id)
        self.assertEqual(job['state'], 'transcript_ready')
        self.assertEqual(job['stage'], 'plan')
        self.assertEqual(job['transcript']['status'], 'ready')
        self.assertIn('srt', job['files'])
        self.assertNotIn('requirements', job['files'])
        self.assertNotIn('frames', job['files'])

    def test_plan_rejects_invented_evidence_and_skill_paths(self):
        segments = [{'id': 'S0001', 'text': 'Export a private report.'}]
        candidate = self.plan_fixture()
        self.assertEqual(self.store.validate_plan(candidate, segments)['review_status'], 'draft')
        candidate['steps'][0]['evidence'] = ['S9999']
        with self.assertRaises(video.VideoError):
            self.store.validate_plan(candidate, segments)
        candidate = self.plan_fixture()
        candidate['skill_name'] = '../escape'
        with self.assertRaises(video.VideoError):
            self.store.validate_plan(candidate, segments)

    def test_organizing_waits_for_review_before_building_repo(self):
        job_id = self.speech_fixture(8)
        with patch.object(video.shutil, 'which', return_value='/fixture/whisper-cli'):
            self.store._step(job_id)
        self.assertEqual(self.store.get(job_id)['state'], 'transcript_ready')
        with self.assertRaises(video.VideoError):
            self.store.action(job_id, 'build-repo')
        queued = self.store.action(job_id, 'organize')
        self.assertEqual(queued['stage'], 'plan')
        candidate = self.plan_fixture()
        candidate['steps'][0]['quote'] = 'First usable transcript.'
        with patch.object(self.store, '_model_capacity'), patch.object(self.store, 'model_json', return_value={'response': json.dumps(candidate)}):
            self.store._step(job_id)
        ready = self.store.get(job_id)
        self.assertEqual(ready['state'], 'plan_review')
        self.assertIn('execution-plan', ready['files'])
        self.assertNotIn('repo', ready['files'])
        self.assertEqual(self.store.action(job_id, 'build-repo')['stage'], 'package')

    def test_tool_failure_diagnostics_stay_private(self):
        import sys
        directory = Path(self.temp.name)
        fixture = 'https://example.test/media?token=fixture-private-value'
        with self.assertRaises(video.VideoError) as caught:
            self.store.run([sys.executable, '-c', 'import sys;sys.stderr.write(sys.argv[1]);sys.exit(7)', fixture], directory, 5)
        self.assertNotIn('fixture-private-value', str(caught.exception))
        diagnostic = directory / 'last-tool-error.json'
        self.assertEqual(json.loads(diagnostic.read_text())['exit_code'], 7)
        self.assertIn(fixture, json.loads(diagnostic.read_text())['stderr'])
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)

    def test_file_integrity_and_partial_media(self):
        job_id = self.job()
        path = self.store.directory(job_id) / 'video.mp4'
        path.write_bytes(b'0123456789')
        outputs = {}
        self.store.record(outputs, 'video', path, 'video/mp4')
        with self.store.db() as db:
            db.execute('UPDATE jobs SET outputs=? WHERE id=?', (json.dumps(outputs), job_id))
        h = self.handler('/api/video/jobs/' + job_id + '/files/video', Range='bytes=2-5')
        h.send_response = lambda value: setattr(h, 'status', value)
        h.send_header = lambda *args: None
        h.end_headers = lambda: None
        h.video_get(h.path)
        self.assertEqual(h.status, 206)
        self.assertEqual(h.wfile.getvalue(), b'2345')
        path.write_bytes(b'XXXXXXXXXX')  # Same size still fails the content hash.
        with self.assertRaisesRegex(video.VideoError, 'changed'):
            self.store.file(job_id, 'video')
        with self.assertRaises(video.VideoError):
            self.store.file(job_id, '../jobs.sqlite3')

    def test_git_mode_remains_separate(self):
        h = self.handler('/repo')
        h.do_GET()
        self.assertEqual(h.result[0], 200)
        self.assertIn('/api/adopt', h.result[1])
        h = self.handler('/video')
        h.do_GET()
        self.assertIn(b'Video', h.result[1])

    def test_repository_is_real_git_and_excludes_media_and_signed_url(self):
        job_id = self.job()
        directory = self.store.directory(job_id)
        values = {'segments': [{'id': 'S0001', 'start_ms': 0, 'end_ms': 1000, 'text': 'Export a private report.'}],
                  'requirements': [], 'tasks': [], 'open-questions': [], 'frames': [], '_asr': {'model': 'fixture'},
                  'plan': self.plan_fixture()}
        outputs = {}
        for name, value in values.items():
            path = directory / (name + '.json')
            video.atomic_json(path, value)
            self.store.record(outputs, name, path)
        for name in ('video', 'audio', 'transcript', 'srt', 'vtt'):
            path = directory / (name + '.txt')
            path.write_text('fixture')
            self.store.record(outputs, name, path)
        row = self.store._row(job_id)
        row['outputs'] = outputs
        def local_git(argv, cwd, timeout, offline=False):
            self.assertEqual(argv[0], 'git')
            self.assertTrue(offline)
            # Only fixture-owned files and trusted Git; do not run source code.
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
            return subprocess.run(argv, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout
        with patch.object(self.store, 'run', side_effect=local_git), patch.object(self.store, '_publish_review'):
            packaged = self.store._package(row)
        with zipfile.ZipFile(directory / packaged['repo']['path']) as archive:
            names = archive.namelist()
            self.assertIn('fixture-report-workflow/.git/HEAD', names)
            self.assertIn('fixture-report-workflow/transcript.json', names)
            self.assertNotIn('fixture-report-workflow/video.txt', names)
            repo = directory / 'repository'
            self.assertEqual({p.name for p in repo.iterdir() if p.is_file()},
                             {'SKILL.md', 'execution-plan.md', 'transcript.json', 'source.json'})
            self.assertEqual({p.name for p in repo.iterdir() if p.is_dir()}, {'.git'})
            self.assertNotIn(b'secret=private', b''.join(archive.read(n) for n in names))
            report = (directory / 'review.html').read_bytes()
            self.assertNotIn(b'<script>alert', report)
            self.assertIn(b'&lt;script&gt;', report)


if __name__ == '__main__':
    unittest.main()
