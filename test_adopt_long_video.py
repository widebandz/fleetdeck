"""Long-video budgets, durable transcript evidence and repo-reference regressions."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import adopt_video as video
from adopt_references import repository_references


class LongVideoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='adopt-long-test-')
        self.addCleanup(self.temp.cleanup)
        self.store = video.VideoStore(self.temp.name)
        self.addCleanup(self.store.close)

    def job(self, duration=3600):
        source = {'url': 'https://example.org/fixture.mp4', 'public_url': 'https://example.org/fixture.mp4',
            'provider': 'direct', 'source_id': str(time.time_ns()), 'duration': duration,
            'title': 'Long fixture', 'retrieved_at': time.time(),
            'metadata': {'description': 'Source repo: https://gitlab.com/team/sample'}}
        identifier = str(time.time_ns())
        with self.store.db() as db:
            db.execute('INSERT INTO inspections VALUES(?,?,?)', (identifier, time.time(), json.dumps(source)))
        return self.store.enqueue(identifier, True)['id']

    def save_segments(self, job_id, segments):
        row = self.store._row(job_id)
        path = self.store.directory(job_id) / 'segments.json'
        video.atomic_json(path, segments)
        self.store.record(row['outputs'], 'segments', path)
        with self.store.db() as db:
            db.execute('UPDATE jobs SET outputs=? WHERE id=?', (json.dumps(row['outputs']), job_id))
        return self.store._row(job_id)

    def test_one_hour_inspection_selects_a_format_within_the_source_budget(self):
        metadata = {'id': 'hour', 'title': 'One-hour fixture', 'duration': 3600,
            'formats': [{'url': 'https://cdn.example/high.mp4', 'ext': 'mp4', 'protocol': 'https',
                'vcodec': 'avc1', 'acodec': 'aac', 'width': 1280, 'height': 720, 'tbr': 2200},
                {'url': 'https://cdn.example/small.mp4', 'ext': 'mp4', 'protocol': 'https',
                'vcodec': 'avc1', 'acodec': 'aac', 'width': 640, 'height': 360, 'tbr': 600}]}
        with patch.object(video, 'public_addresses', return_value=['93.184.216.34']), patch.object(self.store, 'run', return_value=json.dumps(metadata)):
            preview = self.store.inspect('https://example.org/hour')
        with self.store.db() as db:
            source = json.loads(db.execute('SELECT payload FROM inspections WHERE id=?', (preview['inspection'],)).fetchone()[0])
        self.assertEqual(source['formats'][0]['height'], 360)
        self.assertLess(source['estimated_source_bytes'], video.MAX_VIDEO)
        self.assertEqual(preview['limits']['minutes'], 90)

    def test_actual_one_hour_probe_is_accepted_before_transcription(self):
        job_id = self.job()
        import io
        import types
        probe = {'format': {'duration': '3600.12'}, 'streams': [
            {'codec_type': 'video', 'width': 640, 'height': 360}, {'codec_type': 'audio'}]}
        with patch.object(video, 'public_request', return_value=(types.SimpleNamespace(close=lambda: None), io.BytesIO(b'fixture'))):
            with patch.object(self.store, 'run', return_value=json.dumps(probe)):
                self.store._step(job_id)
        row = self.store._row(job_id)
        self.assertEqual((row['state'], row['stage']), ('queued', 'audio'))
        self.assertEqual(row['source']['duration'], 3600.12)
        self.assertNotIn('segments', row['outputs'])

    def test_unknown_long_formats_prefer_a_reasonable_small_video_and_still_keep_audio(self):
        videos = [{'ext': 'mp4', 'url': 'https://cdn.example/' + str(height), 'vcodec': 'avc1',
            'acodec': 'none', 'width': round(height * 16 / 9), 'height': height} for height in (720, 360, 240)]
        audio = {'ext': 'm4a', 'url': 'https://cdn.example/audio', 'vcodec': 'none', 'acodec': 'aac', 'abr': 64}
        selected, estimate = video.choose_media(videos, [*videos, audio], 'webpage', 3600)
        self.assertEqual(selected[0]['height'], 360)
        self.assertEqual(selected[1], audio)
        self.assertIsNone(estimate)
        with self.assertRaisesRegex(video.VideoError, '500 MiB'):
            video.choose_media([dict(videos[0], filesize=video.MAX_VIDEO + 1)], [audio], 'webpage', 3600)

    def test_long_job_has_a_larger_bounded_active_budget(self):
        for duration, expected in ((60, video.SHORT_ACTIVE_SECONDS), (3600, video.LONG_ACTIVE_SECONDS)):
            job_id = self.job(duration)
            with patch.object(self.store, '_download', side_effect=lambda row: self.assert_budget(row, expected)):
                self.store._step(job_id)
            self.assertEqual(self.store.get(job_id)['state'], 'queued')

    def assert_budget(self, row, expected):
        self.assertEqual(self.store.active_budget_seconds, expected)
        self.assertLessEqual(self.store.stage_deadline - time.monotonic(), expected)
        return row['outputs']

    def test_references_preserve_description_spoken_url_and_unresolved_timestamp(self):
        source = {'public_url': 'https://example.org/video', 'metadata': {
            'description': 'Code: https://github.com/acme/tool.git/tree/main and https://gitlab.com/group/subgroup/repo/-/tree/main'}}
        segments = [{'id': 'S0001', 'start_ms': 3540000, 'end_ms': 3544000,
            'text': 'Use github dot com slash acme slash tool.'},
            {'id': 'S0002', 'start_ms': 3550000, 'end_ms': 3554000,
             'text': 'The other repo is called Something Useful.'}]
        result = repository_references(source, segments)
        by_url = {item['url']: item for item in result['repositories']}
        self.assertIn('https://github.com/acme/tool', by_url)
        self.assertIn('https://gitlab.com/group/subgroup/repo', by_url)
        spoken = next(e for e in by_url['https://github.com/acme/tool']['evidence'] if e['kind'] == 'transcript')
        self.assertEqual(spoken['start_ms'], 3540000)
        self.assertEqual(spoken['quote'], segments[0]['text'])
        self.assertEqual(result['unresolved_mentions'][0]['segment_id'], 'S0002')
        self.assertTrue(all(item['status'] == 'unverified' for item in result['repositories']))

    def test_spoofed_hosts_credentials_and_non_repo_routes_are_not_candidates(self):
        text = 'https://evilgithub.com/acme/tool https://github.com.evil.example/acme/tool '
        text += 'https://user:secret@github.com/acme/tool https://github.com/topics/python'
        self.assertEqual(repository_references({'metadata': {'description': text}}, [])['repositories'], [])

    def test_description_references_are_saved_before_media_processing(self):
        job_id = self.job()
        public = self.store.get(job_id)
        self.assertIn('references', public['files'])
        path, _ = self.store.file(job_id, 'references')
        self.assertEqual(json.loads(path.read_text())['repositories'][0]['url'], 'https://gitlab.com/team/sample')

    def test_hour_of_checkpoints_resumes_without_regressing_partial_text_and_finds_late_repo(self):
        job_id = self.job()
        directory = self.store.directory(job_id)
        model = directory / 'model.bin';model.write_bytes(b'fixture model');self.store.whisper_model = model
        audio = directory / 'audio.wav';audio.write_bytes(b'fixture audio')
        row = self.store._row(job_id);self.store.record(row['outputs'], 'audio', audio)
        recipe = {'model_sha256': video.sha(model), 'audio_sha256': video.sha(audio), 'language': 'en',
            'first_chunk_seconds': video.FIRST_CHUNK_SECONDS, 'chunk_seconds': video.CHUNK_SECONDS,
            'overlap_seconds': video.CHUNK_OVERLAP_SECONDS}
        recipe_hash = hashlib.sha256(json.dumps(recipe, sort_keys=True).encode()).hexdigest()
        row['outputs']['_transcript'] = {'status': 'partial', 'processed_seconds': 1800, 'recipe_sha256': recipe_hash}
        video.atomic_json(directory / 'ffprobe.json', {'format': {'duration': '3600'}})
        chunks = video.speech_chunks(3600)
        for index, (start, length) in enumerate(chunks):
            text = ('The code is at https://github.com/acme/hour-demo.' if index == len(chunks) - 1
                    else 'Part ' + str(index) + ' describes the workflow.')
            video.atomic_json(directory / f'chunk-{index:03}.json', {'_recipe': recipe,
                'transcription': [{'offsets': {'from': 0, 'to': round(length * 1000)}, 'text': text}]})
        with self.store.db() as db:
            db.execute("UPDATE jobs SET stage='transcribe',outputs=? WHERE id=?", (json.dumps(row['outputs']), job_id))
        original = self.store.reference_outputs
        def publish(outputs, path, source, segments):
            self.assertGreaterEqual(segments[-1]['end_ms'], 1800000)
            return original(outputs, path, source, segments)
        with patch.object(self.store, 'run', side_effect=AssertionError('Completed chunks must be reused')):
            with patch.object(self.store, 'reference_outputs', side_effect=publish):
                self.store._step(job_id)
        row = self.store._row(job_id)
        self.assertEqual(row['state'], 'transcript_ready')
        self.assertEqual(row['outputs']['_transcript']['processed_seconds'], 3600)
        references = json.loads(self.store.file(job_id, 'references')[0].read_text())
        late = next(item for item in references['repositories'] if item['url'].endswith('/hour-demo'))
        self.assertGreater(late['evidence'][0]['start_ms'], 3500000)

    def test_long_plan_evidence_is_bounded_and_includes_the_end_of_the_video(self):
        requirements = [{'id': f'R{i:03}', 'kind': 'observed', 'evidence': [f'S{i:04}'],
            'text': 'Requirement detail. ' * 100, 'quote': 'Quoted detail. ' * 30} for i in range(1, 501)]
        encoded, coverage = video.compact_requirements(requirements)
        self.assertLessEqual(len(encoded), 24000)
        self.assertEqual(coverage['total_requirements'], 500)
        self.assertIn('R001', coverage['selected_ids'])
        self.assertIn('R500', coverage['selected_ids'])
        self.assertLess(coverage['included_requirements'], 500)

    def test_long_plan_preserves_full_requirements_and_records_selected_coverage(self):
        job_id = self.job()
        segments = [{'id': f'S{i:04}', 'start_ms': i * 5000, 'end_ms': i * 5000 + 4000,
            'text': 'Review this source evidence. ' + 'Detailed context. ' * 25} for i in range(1, 601)]
        row = self.save_segments(job_id, segments)
        directory = self.store.directory(job_id)
        requirements = [{'id': f'R{i:03}', 'kind': 'observed', 'evidence': [f'S{i:04}'],
            'text': 'Required detail. ' * 100, 'quote': 'Review this source evidence.'} for i in range(1, 501)]
        path = directory / 'requirements.json';video.atomic_json(path, requirements)
        outputs = dict(row['outputs']);self.store.record(outputs, 'requirements', path)
        plan = {'title': 'Long workflow', 'goal': 'Review cited evidence', 'skill_name': 'long-workflow',
            'skill_description': 'Review the cited long workflow when asked.', 'inputs': ['Operator scope'],
            'success_criteria': ['Evidence is reviewed.'], 'open_questions': [], 'steps': [{'title': 'Review',
            'action': 'Review the cited part.', 'deliverable': 'Reviewed evidence', 'check': 'Quote matches.',
            'evidence': ['S0001'], 'quote': 'Review this source evidence.'}]}
        with patch.object(self.store, '_extract', return_value=outputs), patch.object(self.store, '_model_capacity'):
            with patch.object(self.store, 'model_json', return_value={'response': json.dumps(plan)}) as model:
                result = self.store._plan(row)
        prompt = model.call_args.args[2]['prompt']
        self.assertLess(len(prompt), 30000)
        self.assertEqual(len(json.loads(path.read_text())), 500)
        planned = json.loads((directory / result['plan']['path']).read_text())
        self.assertEqual(planned['origin']['evidence_coverage']['total_requirements'], 500)
        self.assertIn('Evidence coverage', (directory / result['execution-plan']['path']).read_text())

    def test_extraction_checkpoints_are_bound_to_the_transcript_and_batch(self):
        job_id = self.job()
        row = self.save_segments(job_id, [{'id': 'S0001', 'start_ms': 0, 'end_ms': 1000, 'text': 'Review the source.'}])
        directory = self.store.directory(job_id)
        video.atomic_json(directory / 'analysis-000.json', {'_recipe': {'transcript_sha256': 'old'}})
        data = {'requirements': [{'text': 'Review the source', 'kind': 'observed',
            'evidence': ['S0001'], 'quote': 'Review the source.'}], 'tasks': [], 'open_questions': []}
        with patch.object(self.store, '_model_capacity'):
            with patch.object(self.store, 'model_json', return_value={'response': json.dumps(data)}) as model:
                self.store._extract(row)
                self.assertEqual(model.call_count, 1)
            with patch.object(self.store, 'model_json', side_effect=AssertionError('Valid checkpoint must be reused')):
                self.store._extract(row)


if __name__ == '__main__':
    unittest.main()
