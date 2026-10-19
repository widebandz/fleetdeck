"""Find cited repository references without fetching, cloning or running them."""
import re
import urllib.parse

HOSTS = ('github.com', 'gitlab.com', 'codeberg.org')
LINK = re.compile(r'(?<![\w.@/-])(?:https?://)?(?:www\.)?'
    r'((?:github\.com|gitlab\.com|codeberg\.org)/[A-Za-z0-9_.~/%+-]+(?:/[A-Za-z0-9_.~/%+-]+)+)', re.I)
MENTION = re.compile(r'\brepo(?:sitory)?\b|\bgithub\b|\bgit\s+hub\b|\bgitlab\b|\bcodeberg\b', re.I)
RESERVED = {'about', 'apps', 'collections', 'contact', 'enterprise', 'explore',
    'features', 'groups', 'help', 'issues', 'login', 'marketplace', 'orgs', 'pricing',
    'pulls', 'search', 'security', 'settings', 'signup', 'sponsors', 'topics', 'users'}


def canonical_repository(value):
    try:
        parsed = urllib.parse.urlsplit('https://' + value)
        if parsed.hostname not in HOSTS or parsed.username or parsed.password:
            return None
        parts = parsed.path.strip('/').split('/')
        if parsed.hostname == 'gitlab.com' and '-' in parts:
            parts = parts[:parts.index('-')]
        elif parsed.hostname != 'gitlab.com':
            parts = parts[:2]
        if len(parts) < 2 or parts[0].lower() in RESERVED:
            return None
        parts[-1] = parts[-1].rstrip('.,;:!')
        if parts[-1].endswith('.git'):
            parts[-1] = parts[-1][:-4]
        if any(not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}', part)
               or part in ('.', '..') for part in parts):
            return None
        return 'https://' + parsed.hostname + '/' + '/'.join(parts)
    except ValueError:
        return None


def repository_references(source, segments):
    repositories, mentions = {}, []
    truncated = False
    metadata = source.get('metadata') or {}
    description = metadata.get('description') if isinstance(metadata, dict) else ''
    if not isinstance(description, str):
        description = ''
    if len(description) > 262144:
        truncated = True
        description = description[:262144]
    sources = [('description', description, None)] if description else []
    sources.extend(('transcript', str(segment.get('text') or ''), segment) for segment in segments)
    for kind, text, segment in sources:
        # Spoken URLs remain candidates. Keep the verbatim segment as evidence.
        spoken = re.sub(r'\b(github|gitlab|codeberg)\s+dot\s+(com|org)\b', r'\1.\2', text, flags=re.I)
        spoken = re.sub(r'\s+slash\s+', '/', spoken, flags=re.I)
        matched = False
        for method, examined in [('written_url', text), ('spoken_url', spoken)] if spoken != text else [('written_url', text)]:
            for match in LINK.finditer(examined):
                url = canonical_repository(match.group(1))
                if not url:
                    continue
                matched = True
                if url not in repositories and len(repositories) >= 64:
                    truncated = True
                    continue
                reference = repositories.setdefault(url, {'url': url, 'status': 'unverified', 'evidence': []})
                start = max(0, match.start() - 120) if method == 'written_url' else 0
                quote = text[start:start + 600]
                evidence = {'kind': kind, 'quote': quote, 'method': method}
                if segment:
                    evidence.update(segment_id=segment['id'], start_ms=segment['start_ms'], end_ms=segment['end_ms'])
                else:
                    evidence['source_url'] = source.get('public_url')
                if evidence not in reference['evidence']:
                    if len(reference['evidence']) < 32:
                        reference['evidence'].append(evidence)
                    else:
                        truncated = True
        if not matched and kind == 'transcript' and MENTION.search(text):
            if len(mentions) < 128:
                mentions.append({'status': 'needs_identity', 'segment_id': segment['id'],
                    'start_ms': segment['start_ms'], 'end_ms': segment['end_ms'], 'quote': text[:600]})
            else:
                truncated = True
    return {'repositories': list(repositories.values()), 'unresolved_mentions': mentions,
        'status': 'unverified', 'truncated': truncated,
        'transcript_segments_scanned': len(segments), 'description_scanned': bool(description),
        'note': 'References are source claims. Existence, ownership and code have not been verified; nothing was cloned or executed.'}
