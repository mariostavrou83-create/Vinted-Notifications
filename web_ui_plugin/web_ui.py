"""Single-owner dashboard. Every search/photo route requires authentication."""
from contextlib import closing
from datetime import timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import time

from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
import db
import dashboard_store as store
import search_settings


def auth_row():
    with closing(search_settings.connection()) as conn:
        return dict(conn.execute('SELECT * FROM dashboard_auth WHERE id=1').fetchone())


def initialize_auth():
    with closing(search_settings.connection()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT 1 FROM dashboard_auth WHERE id=1').fetchone():
            code = secrets.token_urlsafe(24)
            path = Path(db.DB_PATH).resolve().parent / 'dashboard-setup-code.txt'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(code + '\n')
            conn.execute('INSERT INTO dashboard_auth(id,setup_hash,session_key) VALUES (1,?,?)',
                         (hashlib.sha256(code.encode()).hexdigest(), secrets.token_hex(32)))


def create_app(test_config=None):
    initialize_auth()
    app = Flask(__name__)
    app.config.update(SECRET_KEY=auth_row()['session_key'], MAX_CONTENT_LENGTH=9*1024*1024,
        MAX_FORM_MEMORY_SIZE=100_000, MAX_FORM_PARTS=30,
        SESSION_COOKIE_NAME='msj_session', SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(days=7))
    if test_config:
        app.config.update(test_config)

    @app.before_request
    def protect():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        if request.method == 'POST' and not secrets.compare_digest(
                session['csrf'], request.form.get('csrf', '')):
            abort(400, 'This form expired. Reload the page and try again.')
        public = ('login', 'setup', 'static', 'health')
        if request.endpoint not in public and not session.get('owner'):
            return redirect(url_for('login'))
        if request.endpoint not in public and not auth_row()['password_hash']:
            session.clear()
            return redirect(url_for('setup'))

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
            'X-Frame-Options':'DENY', 'Referrer-Policy':'no-referrer',
            'Content-Security-Policy':"default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            'Strict-Transport-Security':'max-age=31536000'})
        return response

    @app.context_processor
    def common():
        return {'csrf':session.get('csrf'), 'loads':json.loads}

    def count_attempt():
        # A persistent global limit cannot be bypassed by changing IPs/cookies or restarting.
        with closing(search_settings.connection()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT attempts,window_start FROM dashboard_auth WHERE id=1').fetchone()
            now = time.time()
            attempts, start = row
            if now-start > 900:
                attempts, start = 0, now
            if attempts >= 15:
                abort(429, 'Too many sign-in attempts. Please try again in 15 minutes.')
            conn.execute('UPDATE dashboard_auth SET attempts=?,window_start=? WHERE id=1', (attempts+1,start))

    def signed_in():
        session.clear()
        session['owner'] = True
        session['csrf'] = secrets.token_urlsafe(32)
        session.permanent = True
        with closing(search_settings.connection()) as conn, conn:
            conn.execute('UPDATE dashboard_auth SET attempts=0,window_start=0 WHERE id=1')
        return redirect(url_for('dashboard'))

    @app.route('/setup', methods=['GET','POST'])
    def setup():
        if auth_row()['password_hash']:
            return redirect(url_for('login'))
        if request.method == 'POST':
            count_attempt()
            code = hashlib.sha256(request.form.get('code','').strip().encode()).hexdigest()
            password = request.form.get('password','')
            if not secrets.compare_digest(auth_row()['setup_hash'],code):
                flash('The setup code is incorrect.', 'error')
            elif not 12 <= len(password) <= 128 or password != request.form.get('confirm'):
                flash('Use 12–128 characters and enter the same password twice.', 'error')
            else:
                with closing(search_settings.connection()) as conn, conn:
                    updated = conn.execute('''UPDATE dashboard_auth SET password_hash=?,setup_hash=''
                        WHERE id=1 AND password_hash IS NULL''', (generate_password_hash(password),)).rowcount
                if not updated:
                    return redirect(url_for('login'))
                (Path(db.DB_PATH).resolve().parent/'dashboard-setup-code.txt').unlink(missing_ok=True)
                return signed_in()
        return render_template('msj_auth.html', setup=True)

    @app.route('/login', methods=['GET','POST'])
    def login():
        if not auth_row()['password_hash']:
            return redirect(url_for('setup'))
        if request.method == 'POST':
            count_attempt()
            password = request.form.get('password','')
            if len(password) <= 128 and check_password_hash(auth_row()['password_hash'], password):
                return signed_in()
            flash('Incorrect password. Please try again.', 'error')
        return render_template('msj_auth.html', setup=False)

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.get('/healthz')
    def health():
        return {'status':'ok'}

    @app.get('/')
    def dashboard():
        archived = request.args.get('view') == 'archive'
        rows = store.list_searches(archived)
        now = time.time()
        for row in rows:
            age = now-(row['last_success'] or 0)
            row['status'] = ('Archived' if archived else 'Paused' if row['paused'] else
                'Checking' if row['last_success'] and age < 90 and not row['failures'] else
                'Waiting' if not row['last_success'] else 'Retrying')
            row['ago'] = ('Not checked yet' if not row['last_success'] else
                'Checked just now' if age < 60 else f'Checked {int(age/60)} min ago')
        return render_template('msj_dashboard.html', rows=rows, archived=archived,
            active=sum(not r['paused'] for r in rows), photos=sum(bool(r['reference_id']) for r in rows),
            interval=db.get_parameter('query_refresh_delay'))

    @app.route('/search/new', methods=['GET','POST'])
    @app.route('/search/<int:query_id>', methods=['GET','POST'])
    def edit(query_id=None):
        original = search_settings.get_search(query_id) if query_id is not None else None
        if query_id is not None and original is None:
            abort(404)
        row = dict(original) if original else {'id':None, 'query_name':'', 'query':'',
            'reminder':'', 'exclusions':[], 'reference_id':None, 'revision':0}
        if request.method == 'POST':
            try:
                upload = request.files.get('photo')
                photo = store.normalize_photo(upload.stream) if upload and upload.filename else None
                store.save_search(query_id, request.form, photo)
                flash('Search saved. Changes apply automatically.', 'success')
                return redirect(url_for('dashboard'))
            except ValueError as exc:
                flash(str(exc), 'error')
                for key in ('query_name','query','reminder','revision'):
                    row[key] = request.form.get(key,'')
                row['exclusions'] = request.form.get('exclusions','').splitlines()
        return render_template('msj_edit.html', row=row)

    @app.post('/search/<int:query_id>/<action>')
    def state(query_id, action):
        try:
            store.change_state(query_id, action, request.form.get('revision'))
            flash({'pause':'Search paused.', 'resume':'Search resumed. New listings will alert after the first check.',
                'archive':'Search archived. Its history is preserved.', 'restore':'Search restored, ready to edit or resume.'}[action], 'success')
        except ValueError as exc:
            flash(str(exc), 'error')
        return redirect(url_for('dashboard'))

    @app.get('/reference/<media_id>')
    def reference(media_id):
        media = store.get_media(media_id)
        if not media:
            abort(404)
        return send_file(io.BytesIO(media['image']), mimetype='image/jpeg')

    @app.errorhandler(413)
    def too_large(error):
        return render_template('msj_error.html', message='That upload is too large. Choose a photo smaller than 8 MB.'), 413

    @app.errorhandler(400)
    @app.errorhandler(429)
    def request_error(error):
        return render_template('msj_error.html', message=error.description), error.code

    return app


def web_ui_process():
    from waitress import serve
    serve(create_app(), host='0.0.0.0', port=8000, threads=4, max_request_body_size=9*1024*1024)
