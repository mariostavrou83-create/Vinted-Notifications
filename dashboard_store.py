"""Private dashboard storage, backed by the existing persistent SQLite database."""
from contextlib import closing
import hashlib
import io
import json
import re
import time
import warnings
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from search_settings import connection, parse_exclusions


def normalize_url(value):
    value = value.strip()
    if len(value) > 6000:
        raise ValueError('That Vinted link is too long.')
    parsed = urlparse(value)
    if (parsed.scheme != 'https' or parsed.netloc not in ('www.vinted.co.uk', 'vinted.co.uk')
            or parsed.username or parsed.password):
        raise ValueError('Paste a UK Vinted search link starting with https://www.vinted.co.uk/catalog.')
    params = parse_qs(parsed.query)
    if re.fullmatch(r'/brand/\d+(?:-[\w-]+)?/?', parsed.path):
        params['brand_ids[]'] = [parsed.path.split('/')[2].split('-')[0]]
    elif parsed.path.rstrip('/') != '/catalog':
        raise ValueError('Use a Vinted search results link, not an individual item link.')
    params['order'] = ['newest_first']
    for key in ('time', 'search_id', 'disabled_personalization', 'page'):
        params.pop(key, None)
    return urlunparse(('https', 'www.vinted.co.uk', '/catalog', '', urlencode(params, doseq=True), ''))


def normalize_photo(stream):
    from PIL import Image, ImageOps, UnidentifiedImageError
    raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('Choose a photo smaller than 8 MB.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as original:
                if original.format not in ('JPEG', 'PNG', 'WEBP'):
                    raise ValueError('Choose a JPG, PNG or WebP photo.')
                if original.width * original.height > 24_000_000:
                    raise ValueError('Choose a photo smaller than 24 megapixels.')
                original.load()
                photo = ImageOps.exif_transpose(original).convert('RGBA')
                photo.thumbnail((1280, 1280))
                background = Image.new('RGB', photo.size, 'white')
                background.paste(photo, mask=photo.getchannel('A'))
                result = io.BytesIO()
                background.save(result, 'JPEG', quality=85, optimize=True)
                return result.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError('That file could not be read as a photo. Choose a JPG, PNG or WebP image.') from None


def save_search(query_id, form, photo=None):
    name, reminder = form.get('query_name', '').strip(), form.get('reminder', '').strip()
    if not name or len(name) > 100 or len(reminder) > 800:
        raise ValueError('Give your search a name (up to 100 characters) and a reminder of up to 800 characters.')
    url = normalize_url(form.get('query', ''))
    exclusions = json.dumps(parse_exclusions(form.get('exclusions', '')), ensure_ascii=False)
    with closing(connection()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        old = conn.execute('''SELECT q.*, COALESCE(d.revision,0) revision FROM queries q
            LEFT JOIN search_dashboard d ON d.query_id=q.id WHERE q.id=?''', (query_id,)).fetchone()
        if query_id is not None:
            if not old:
                raise ValueError('This search no longer exists.')
            if str(old['revision']) != form.get('revision'):
                raise ValueError('This search changed in another tab. Reload it before saving again.')
            # Preserve byte-for-byte URLs when their meaning is unchanged.
            if normalize_url(old['query']) == url:
                url = old['query']
        duplicate = conn.execute('SELECT id FROM queries WHERE query=? AND id!=?', (url, query_id or -1)).fetchone()
        if duplicate:
            raise ValueError(f'This link is already saved as search #{duplicate[0]}.')
        if query_id is None:
            query_id = conn.execute('INSERT INTO queries(query,query_name) VALUES (?,?)', (url, name)).lastrowid
        else:
            conn.execute('UPDATE queries SET query=?, query_name=? WHERE id=?', (url, name, query_id))
        conn.execute('INSERT OR IGNORE INTO search_dashboard(query_id) VALUES (?)', (query_id,))
        conn.execute('''INSERT INTO search_preferences VALUES (?,?,?) ON CONFLICT(query_id)
            DO UPDATE SET reminder=excluded.reminder, exclusions=excluded.exclusions''', (query_id, reminder, exclusions))
        conn.execute('''UPDATE search_dashboard SET revision=revision+1,
            rebaseline=CASE WHEN ? THEN 1 ELSE rebaseline END WHERE query_id=?''',
            (bool(old and old['query'] != url), query_id))
        if photo:
            digest = hashlib.sha256(photo).hexdigest()
            # Keep recently detached photos for alerts already in the delivery queue.
            conn.execute('''DELETE FROM dashboard_media WHERE created<? AND id NOT IN
                (SELECT reference_id FROM search_dashboard WHERE reference_id IS NOT NULL)''', (time.time()-86400,))
            size = conn.execute('SELECT COALESCE(SUM(length(image)),0) FROM dashboard_media').fetchone()[0]
            if size + len(photo) > 50 * 1024 * 1024:
                raise ValueError('Photo storage is full. Remove unused example photos and try again tomorrow.')
            conn.execute('INSERT OR IGNORE INTO dashboard_media(id,image,created) VALUES (?,?,?)', (digest, photo, time.time()))
            conn.execute('UPDATE search_dashboard SET reference_id=? WHERE query_id=?', (digest, query_id))
        elif form.get('remove_photo') == 'yes':
            conn.execute('UPDATE search_dashboard SET reference_id=NULL WHERE query_id=?', (query_id,))
        return query_id


def list_searches(archived=False):
    with closing(connection()) as conn:
        return [dict(row) for row in conn.execute('''SELECT q.*,
            COALESCE(p.reminder,'') reminder, COALESCE(p.exclusions,'[]') exclusions,
            COALESCE(d.paused,0) paused, COALESCE(d.archived,0) archived, d.reference_id,
            COALESCE(d.revision,0) revision, h.last_success, h.actual_interval, h.failures
            FROM queries q LEFT JOIN search_preferences p ON p.query_id=q.id
            LEFT JOIN search_dashboard d ON d.query_id=q.id
            LEFT JOIN search_health h ON h.query_id=q.id
            WHERE COALESCE(d.archived,0)=? ORDER BY q.id DESC''', (int(archived),))]


def change_state(query_id, action, revision):
    if action not in ('pause', 'resume', 'archive', 'restore'):
        raise ValueError('Unknown action.')
    with closing(connection()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM queries WHERE id=?', (query_id,)).fetchone():
            raise ValueError('Search not found.')
        conn.execute('INSERT OR IGNORE INTO search_dashboard(query_id) VALUES (?)', (query_id,))
        row = conn.execute('SELECT revision FROM search_dashboard WHERE query_id=?', (query_id,)).fetchone()
        if str(row[0]) != str(revision):
            raise ValueError('This search changed in another tab. Reload and try again.')
        changes = {'pause':'paused=1', 'resume':'paused=0, rebaseline=1',
                   'archive':'archived=1, paused=1', 'restore':'archived=0, paused=1'}
        conn.execute(f'UPDATE search_dashboard SET {changes[action]}, revision=revision+1 WHERE query_id=?', (query_id,))


def get_media(media_id):
    with closing(connection()) as conn:
        row = conn.execute('SELECT * FROM dashboard_media WHERE id=?', (media_id,)).fetchone()
        return dict(row) if row else None


def cache_telegram_photo(media_id, file_id):
    with closing(connection()) as conn, conn:
        conn.execute('UPDATE dashboard_media SET telegram_file_id=? WHERE id=?', (file_id, media_id))
