"""Transport checks using the existing yt-dlp environment; no real network."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import adopt_extract as extract
import adopt_video as video


class Reply(io.BytesIO):
    status = 200
    wb_url = 'https://example.org/final'

    def getheaders(self):
        return [('Content-Type', 'text/html')]

    def getheader(self, key, default=None):
        return dict(self.getheaders()).get(key, default)


class ManifestTests(unittest.TestCase):
    def test_live_and_protected_dash_manifests_are_rejected(self):
        for text in ('<MPD type="dynamic"/>', '<MPD><Period><ContentProtection/></Period></MPD>'):
            with self.subTest(text=text), self.assertRaises(video.VideoError):
                extract.validate_dash(text.encode())
        extract.validate_dash(b'<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period/></MPD>')

    def test_live_and_encrypted_manifests_are_rejected(self):
        for text in ('#EXTM3U\n#EXTINF:2,\npart.ts',
                     '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXT-X-ENDLIST',
                     '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES\n#EXT-X-ENDLIST'):
            with self.subTest(text=text), self.assertRaises(video.VideoError):
                extract.validate_manifest(text.encode())
        extract.validate_manifest(b'#EXTM3U\n#EXTINF:2,\npart.ts\n#EXT-X-ENDLIST')


@unittest.skipUnless(importlib.util.find_spec('yt_dlp'), 'Run with existing yt-dlp Python for transport checks')
class TransportTests(unittest.TestCase):
    def test_only_guarded_handler_and_no_execution_components(self):
        with extract.make_downloader('inspect') as downloader:
            self.assertEqual(list(downloader._request_director.handlers), ['Public'])
            self.assertEqual(downloader.params['js_runtimes'], {})
            self.assertFalse(downloader.params['remote_components'])
            self.assertEqual(downloader.params['postprocessors'], [])

    def test_private_and_non_https_destinations_fail_without_connection(self):
        for url in ('https://127.0.0.1/secret', 'https://100.93.175.16/secret',
                    'https://[::1]/secret', 'file:///etc/passwd', 'http://example.org/a'):
            with self.subTest(url=url), extract.make_downloader('inspect') as downloader:
                with patch.object(video.http.client, 'HTTPSConnection', side_effect=AssertionError('No connection allowed')):
                    with self.assertRaises(Exception):
                        downloader.urlopen(url)

    def test_redirect_is_revalidated_by_the_same_pinned_transport(self):
        class Redirect(Reply):
            status = 302

            def getheader(self, key, default=None):
                return 'https://127.0.0.1/secret' if key == 'Location' else default

        import types
        connection = types.SimpleNamespace(request=lambda *args, **kwargs: None,
            getresponse=lambda: Redirect(), close=lambda: None)
        with extract.make_downloader('inspect') as downloader:
            with patch.object(video, 'public_addresses', side_effect=[['93.184.216.34'], video.VideoError('Private host')]) as resolve:
                with patch.object(video.http.client, 'HTTPSConnection', return_value=connection):
                    with self.assertRaises(Exception):
                        downloader.urlopen('https://example.org/page')
                self.assertEqual(resolve.call_count, 2)

    def test_bounded_response_and_credential_headers(self):
        import types
        from yt_dlp.networking.common import Request
        connection = types.SimpleNamespace(close=lambda: None)
        with extract.make_downloader('inspect', 16) as downloader:
            with patch.object(extract, 'public_request', return_value=(connection, Reply(b'x' * 32))):
                with self.assertRaises(Exception):
                    downloader.urlopen('https://example.org/page')
        with extract.make_downloader('inspect') as downloader:
            with patch.object(extract, 'public_request', return_value=(connection, Reply(b'page'))) as request:
                response = downloader.urlopen(Request('https://example.org/page', headers={
                    'Cookie': 'private', 'Authorization': 'private', 'Referer': 'https://example.org/'}))
                self.assertEqual(response.read(), b'page')
                sent = {k.lower(): v for k, v in request.call_args.args[1].items()}
                self.assertNotIn('cookie', sent)
                self.assertNotIn('authorization', sent)
                self.assertEqual(response.url, Reply.wb_url)

    def test_real_generic_extractor_resolves_embedded_video(self):
        import types
        page = b'<html><head><title>Embedded clip</title></head><body><video controls><source src="https://cdn.example/clip.mp4" type="video/mp4"></video></body></html>'
        def request(url, *args, **kwargs):
            self.assertEqual(url, 'https://example.org/article')
            response = Reply(page)
            response.wb_url = url
            return types.SimpleNamespace(close=lambda: None), response
        with patch.object(extract, 'public_request', side_effect=request):
            info = extract.inspect_video('https://example.org/article')
        self.assertEqual(info['formats'][0]['url'], 'https://cdn.example/clip.mp4')

    def test_native_hls_and_dash_use_only_guarded_transport(self):
        import types
        cases = [({'protocol': 'm3u8_native', 'url': 'https://cdn.example/vod.m3u8', 'ext': 'mp4'},
            {'https://cdn.example/vod.m3u8': b'#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nfirst.ts\n#EXTINF:2,\nsecond.ts\n#EXT-X-ENDLIST',
             'https://cdn.example/first.ts': b'first', 'https://cdn.example/second.ts': b'second'}, b'firstsecond'),
            ({'protocol': 'http_dash_segments', 'url': 'https://cdn.example/stream', 'ext': 'mp4',
              'fragments': [{'url': 'https://cdn.example/init'}, {'url': 'https://cdn.example/part'}]},
             {'https://cdn.example/init': b'init', 'https://cdn.example/part': b'part'}, b'initpart')]
        for fmt, responses, expected in cases:
            with self.subTest(protocol=fmt['protocol']), tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory):
                def request(url, *args, **kwargs):
                    reply = Reply(responses[url]);reply.wb_url = url
                    return types.SimpleNamespace(close=lambda: None), reply
                payload = Path(directory) / 'request.json'
                payload.write_text(json.dumps({'format': fmt, 'target': 'acquired-0.mp4', 'remaining_bytes': 1024 * 1024}))
                with patch.object(extract, 'public_request', side_effect=request):
                    extract.download_stream(payload)
                self.assertEqual(Path('acquired-0.mp4').read_bytes(), expected)


if __name__ == '__main__':
    unittest.main()
