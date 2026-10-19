#!/usr/bin/env python3
"""Receipt-backed local handoff to Adopt's existing persistent worker.

No browser, model, shell, service restart, messaging or new background process.
URLs come from the real operator receipt, never from source-video instructions.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from adopt_video import JOB_ID, VideoError, source_url

PRIVATE_APP = 'https://brainwave.tailacfa70.ts.net:8794/video'


def receipt_url(message, source):
    if not isinstance(message, dict) or message.get('source') != source:
        raise VideoError('The original Work receipt could not be verified.')
    record = message.get('record') or {}
    if source.startswith('terminal:'):
        operator = record.get('operator') or {}
        authorized = (record.get('origin') == 'terminal' and operator.get('uid') == os.getuid()
            and record.get('transport') in ('ssh', 'terminal')
            and (message.get('terminal_delivery') or {}).get('state') == 'delivered')
    else:
        authorized = (bool(re.fullmatch(r'imsg:1:\d+', source)) and message.get('chat_id') == 1
            and record.get('chat_id') == 1 and record.get('is_group') is False)
    if not authorized:
        raise VideoError('Use the original private operator receipt delivered to Trace.')
    urls = []
    for raw in re.findall(r'https://[^\s<>"\x27]+', str(message.get('text') or ''), re.IGNORECASE):
        try:
            canonical = source_url(raw.rstrip('.,;:!?)]}'))[0]
        except VideoError:
            continue
        if canonical not in urls:
            urls.append(canonical)
    if len(urls) != 1:
        raise VideoError('The receipt must identify one HTTPS video link. Ask which video if there are several.')
    return urls[0]


def load_receipt(source):
    if not re.fullmatch(r'(?:imsg:1:\d+|terminal:[a-fA-F0-9-]{36})', source):
        raise VideoError('Use an existing Work receipt ID, not a copied message or a new receipt.')
    command = Path.home() / 'bin/wb-work'
    try:
        result = subprocess.run([str(command), 'message', source, '--json'],
            capture_output=True, text=True, timeout=15, check=True)
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise VideoError('The original Work receipt is unavailable; no video was submitted.')


class Client:
    def __init__(self, port=8794):
        self.port = port
        self.token = ''

    def request(self, path, body=None):
        # A fixed loopback destination and closed route set prevent URL forwarding.
        connection = http.client.HTTPConnection('127.0.0.1', self.port,
            timeout=105 if path == '/api/video/inspect' else 10)
        try:
            headers = {'Host': 'localhost:' + str(self.port)}
            if body is not None:
                headers.update({'Content-Type': 'application/json', 'X-Adopt-Token': self.token})
            connection.request('GET' if body is None else 'POST', path,
                body=None if body is None else json.dumps(body), headers=headers)
            response = connection.getresponse()
            raw = response.read(4 * 1024**2 + 1)
            if len(raw) > 4 * 1024**2:
                raise VideoError('Adopt returned an oversized response.')
            value = json.loads(raw)
            if response.status != 200 or not isinstance(value, dict) or value.get('ok') is False:
                raise VideoError(value.get('error', 'Adopt did not accept the request.') if isinstance(value, dict)
                                 else 'Adopt returned an invalid response.')
            return value
        except (OSError, ValueError, http.client.HTTPException) as error:
            suffix = ' (operation denied by this session)' if isinstance(error, PermissionError) else ''
            raise VideoError('The local Adopt service could not be reached' + suffix + '. No completed submission is confirmed.')
        finally:
            connection.close()

    def ready(self):
        state = self.request('/api/video/state')
        self.token = state.get('token', '')
        if not self.token:
            raise VideoError('Adopt did not provide its local request token.')
        return state

    @staticmethod
    def summarize(job):
        if not isinstance(job, dict) or not JOB_ID.fullmatch(job.get('id', '')):
            raise VideoError('Adopt did not return a verifiable job ID.')
        return {'job_id': job['id'], 'state': job['state'], 'stage': job['stage'],
            'message': job.get('message', ''), 'transcript': job.get('transcript', {}),
            'private_url': PRIVATE_APP + '#' + job['id'],
            'transcript_url': ('https://brainwave.tailacfa70.ts.net:8794' + job['files']['transcript']
                               if job.get('files', {}).get('transcript') else None)}

    def submit(self, source, message):
        url = receipt_url(message, source)
        state = self.ready()
        if not state.get('preflight', {}).get('worker_running'):
            raise VideoError('Adopt is reachable but its video worker is unavailable. No video was queued.')
        preview = self.request('/api/video/inspect', {'url': url}).get('preview', {})
        inspection = preview.get('inspection')
        if not isinstance(inspection, str) or not re.fullmatch(r'[a-f0-9]{32}', inspection):
            raise VideoError('The source could not be inspected; no video was queued.')
        job = self.request('/api/video/jobs', {'inspection': inspection, 'confirmed': True}).get('job')
        return dict(self.summarize(job), source_receipt=source,
                    submission='confirmed_by_adopt',
                    processing_continues_in_service=job['state'] in ('queued', 'working', 'retry_scheduled'))

    def status(self, identifier):
        if not JOB_ID.fullmatch(identifier):
            raise VideoError('Use a real Adopt job ID.')
        return self.summarize(self.request('/api/video/jobs/' + identifier).get('job'))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Hand a real operator video receipt to persistent local Adopt.')
    parser.add_argument('--port', type=int, default=int(os.environ.get('WB_ADOPT_PORT', '8794')))
    commands = parser.add_subparsers(dest='command', required=True)
    submit = commands.add_parser('submit');submit.add_argument('--source', required=True)
    status = commands.add_parser('status');status.add_argument('job')
    commands.add_parser('ready')
    args = parser.parse_args(argv)
    client = Client(args.port)
    try:
        if args.command == 'submit':
            result = client.submit(args.source, load_receipt(args.source))
        elif args.command == 'status':
            result = client.status(args.job)
        else:
            state = client.ready()
            result = {'service_reachable': True, 'preflight': state.get('preflight', {})}
        print(json.dumps({'ok': True, **result}, ensure_ascii=False))
        return 0
    except VideoError as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    sys.exit(main())
