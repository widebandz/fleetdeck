#!/usr/bin/env python3
"""Existing yt-dlp extractors behind Adopt's pinned public-HTTPS transport.

No browser cookies, plugins, JavaScript runtimes, remote components, external
downloaders or postprocessors. The process only inspects or saves one stream;
the parent performs local media verification and recognition afterwards.
"""
import io
import json
from pathlib import Path
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET

from adopt_video import MAX_VIDEO, VideoError, public_request


def make_downloader(mode, byte_limit=MAX_VIDEO):
    # Imports stay lazy: the portal itself does not require yt-dlp's Python env.
    import yt_dlp
    from yt_dlp.globals import plugin_dirs
    from yt_dlp.networking.common import RequestHandler, Response
    from yt_dlp.networking.exceptions import HTTPError, RequestError

    plugin_dirs.value = []
    budget = {'bytes': 0, 'requests': 0}
    request_limit = 80 if mode == 'inspect' else 3000
    byte_limit = min(byte_limit, 32 * 1024**2) if mode == 'inspect' else byte_limit

    class PublicRH(RequestHandler):
        _SUPPORTED_URL_SCHEMES = ('https',)
        _SUPPORTED_PROXY_SCHEMES = ()

        def _check_extensions(self, extensions):
            # These optional hints do not change this sole transport's policy.
            for name in ('timeout', 'cookiejar', 'keep_header_casing', 'impersonate'):
                extensions.pop(name, None)

        def _send(self, request):
            budget['requests'] += 1
            if budget['requests'] > request_limit:
                raise RequestError('The source exceeded its request limit.')
            if request.method not in ('GET', 'HEAD', 'POST'):
                raise RequestError('Unsupported source request method.')
            if request.data is not None and (not isinstance(request.data, bytes) or len(request.data) > 128 * 1024):
                raise RequestError('The source request body exceeded its limit.')
            # Never import credentials, use a proxy, or forward browser cookies.
            headers = {key: value for key, value in self._get_headers(request).items()
                if key.lower() not in ('cookie', 'authorization', 'proxy-authorization',
                    'host', 'connection', 'accept-encoding', 'content-length')
                and isinstance(value, str) and len(value) <= 4096 and '\r' not in value and '\n' not in value}
            headers['Accept-Encoding'] = 'identity'
            try:
                connection, response = public_request(request.url, headers, request.method,
                    data=request.data, allow_errors=True)
            except VideoError as error:
                raise RequestError(str(error)) from error

            class BoundedStream(io.IOBase):
                def readable(self):
                    return True

                def read(self, size=-1):
                    remaining = byte_limit - budget['bytes']
                    amount = min(remaining + 1, size) if size is not None and size >= 0 else remaining + 1
                    try:
                        data = response.read(amount)
                    except Exception:
                        self.close()
                        raise
                    budget['bytes'] += len(data)
                    if budget['bytes'] > byte_limit:
                        self.close()
                        raise RequestError('The source exceeded its byte limit.')
                    return data

                def close(self):
                    response.close()
                    connection.close()
                    super().close()

            stream = BoundedStream()
            url = getattr(response, 'wb_url', request.url)
            response_headers = dict(response.getheaders())
            content_type = response.getheader('Content-Type', '').lower()
            manifest = urllib.parse.urlsplit(url).path.lower().endswith('.m3u8') or 'mpegurl' in content_type
            dash = urllib.parse.urlsplit(url).path.lower().endswith('.mpd') or 'dash+xml' in content_type
            if mode == 'inspect' or manifest or dash:
                try:
                    limit = 2 * 1024**2 if manifest or dash else 8 * 1024**2
                    payload = stream.read(limit + 1)
                    if len(payload) > limit:
                        raise RequestError('The source metadata exceeded its limit.')
                    if manifest:
                        validate_manifest(payload)
                    if dash:
                        validate_dash(payload)
                finally:
                    stream.close()
                stream = io.BytesIO(payload)
            result = Response(stream, url, response_headers, status=response.status)
            if response.status >= 400:
                raise HTTPError(result)
            return result

    class PublicYoutubeDL(yt_dlp.YoutubeDL):
        def build_request_director(self, handlers, preferences=None):
            # No default handler can fall back to an unchecked connection.
            return super().build_request_director([PublicRH])

    class PrivateLogger:
        def debug(self, message):
            pass

        def warning(self, message):
            pass

        def error(self, message):
            print(str(message), file=sys.stderr)

    parameters = {'quiet': True, 'no_warnings': True, 'logger': PrivateLogger(),
        'proxy': '', 'socket_timeout': 20, 'retries': 1, 'fragment_retries': 1,
        'extractor_retries': 1, 'skip_download': True, 'noplaylist': True,
        'playlistend': 2, 'extract_flat': 'in_playlist', 'lazy_playlist': True,
        'js_runtimes': {}, 'remote_components': [], 'postprocessors': [],
        'allow_unplayable_formats': False, 'fixup': 'never',
        'continuedl': False, 'overwrites': True, 'nopart': False,
        'concurrent_fragment_downloads': 1, 'skip_unavailable_fragments': False,
        'external_downloader': {}, 'max_filesize': byte_limit,
        'hls_use_mpegts': True, 'ffmpeg_location': str(Path.cwd() / 'no-external-downloader')}
    return PublicYoutubeDL(parameters)


def validate_manifest(payload):
    text = payload.decode('utf-8', errors='replace')
    if not text.lstrip().startswith('#EXTM3U'):
        raise VideoError('The HLS source returned an invalid manifest.')
    if re.search(r'#EXT-X-(?:SESSION-)?KEY:(?!METHOD=NONE(?:,|\s|$))', text):
        raise VideoError('Encrypted streams are unsupported. Provide an authorized downloadable file.')
    if '#EXT-X-ENDLIST' not in text and '#EXT-X-STREAM-INF' not in text:
        raise VideoError('This stream is live or incomplete. Use a completed video.')


def validate_dash(payload):
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise VideoError('The DASH source returned an invalid manifest.') from error
    if root.tag.rsplit('}', 1)[-1] != 'MPD' or root.get('type', 'static') != 'static':
        raise VideoError('This DASH stream is live or incomplete. Use a completed video.')
    if any(element.tag.rsplit('}', 1)[-1] == 'ContentProtection' for element in root.iter()):
        raise VideoError('Protected DASH streams are unsupported. Provide an authorized downloadable file.')


def inspect_video(url):
    with make_downloader('inspect') as downloader:
        info = downloader.extract_info(url, download=False)
        if not isinstance(info, dict):
            raise VideoError('No downloadable video was found on this page.')
        if info.get('_type') in ('playlist', 'multi_video'):
            entries = list(info.get('entries') or [])
            if len(entries) != 1 or info.get('playlist_count', 1) not in (None, 1):
                raise VideoError('Choose one video, not a playlist or multi-video page.')
            info = downloader.process_ie_result(entries[0], download=False)
        return downloader.sanitize_info(info)


def download_stream(request_path):
    from yt_dlp.downloader.dash import DashSegmentsFD
    from yt_dlp.downloader.external import FFmpegFD
    from yt_dlp.downloader.hls import HlsFD
    from yt_dlp.utils import DownloadError

    payload = json.loads(Path(request_path).read_text())
    target = payload['target']
    if not isinstance(target, str) or not re.fullmatch(r'acquired-\d+\.(?:mp4|m4a|webm|mov)', target):
        raise VideoError('Invalid local stream target.')
    info = payload['format']
    if not isinstance(info, dict) or info.get('has_drm') or info.get('is_live'):
        raise VideoError('Use a completed, unprotected video.')
    protocol = info.get('protocol')
    class CompletedHlsFD(HlsFD):
        @classmethod
        def can_download(cls, manifest, info_dict, allow_unplayable_formats=False):
            validate_manifest(manifest.encode())
            return super().can_download(manifest, info_dict, allow_unplayable_formats)

    downloader_class = {'m3u8_native': CompletedHlsFD, 'http_dash_segments': DashSegmentsFD}.get(protocol)
    if not downloader_class:
        raise VideoError('Unsupported segmented stream protocol.')

    def refuse_external(*args, **kwargs):
        raise DownloadError('This stream requires an external downloader; provide a direct downloadable video instead.')

    # Native HLS has an external fallback. Close it: all network must use PublicRH.
    FFmpegFD.real_download = refuse_external
    info = {key: value for key, value in info.items() if key in (
        'url', 'ext', 'protocol', 'format_id', 'width', 'height', 'vcodec', 'acodec',
        'http_headers', 'fragments', 'fragment_base_url', 'extra_param_to_segment_url')}
    info.update(id='adopt-stream', title='Adopt stream', is_live=False, has_drm=False)
    with make_downloader('download', int(payload['remaining_bytes'])) as downloader:
        native = downloader_class(downloader, downloader.params)
        if not native.download(target, info)[0] or not Path(target).is_file():
            raise VideoError('The source stream could not finish downloading.')
        Path(target).chmod(0o600)
    return {'bytes': Path(target).stat().st_size}


if __name__ == '__main__':
    try:
        if len(sys.argv) != 3 or sys.argv[1] not in ('inspect', 'download'):
            raise VideoError('Expected one inspection URL or a local stream request.')
        result = inspect_video(sys.argv[2]) if sys.argv[1] == 'inspect' else download_stream(sys.argv[2])
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
