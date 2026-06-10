from flask import Flask, render_template, jsonify, request, Response
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import requests
import socket
import json
import os

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-key-troque-em-producao')
app.config['TEMPLATES_AUTO_RELOAD'] = True

# Necessário para o Flask gerar URLs corretas (https) quando está atrás do proxy do Render
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

TIKAPI_KEY    = os.getenv('TIKAPI_KEY', '')
APP_PASSWORD  = os.getenv('APP_PASSWORD', '')
DAILY_LIMIT   = int(os.getenv('TIKAPI_DAILY_LIMIT', 100))
BASE_URL      = 'https://api.tikapi.io'
USAGE_FILE    = os.path.join(os.path.dirname(__file__), 'usage.json')


# ── Contador de uso diário ───────────────────────────────────────────────────

def _load_usage():
    today = str(date.today())
    try:
        with open(USAGE_FILE, 'r') as f:
            data = json.load(f)
        # Reseta se for um novo dia
        if data.get('date') != today:
            return {'date': today, 'count': 0}
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {'date': today, 'count': 0}


def _save_usage(usage):
    with open(USAGE_FILE, 'w') as f:
        json.dump(usage, f)


def increment_usage(n=1):
    """Incrementa o contador e salva. Retorna o estado atual."""
    usage = _load_usage()
    usage['count'] += n
    _save_usage(usage)
    return usage


def get_quota():
    usage = _load_usage()
    used = usage['count']
    return {
        'used': used,
        'limit': DAILY_LIMIT,
        'remaining': max(0, DAILY_LIMIT - used),
        'resetAt': str(date.today()) + ' (meia-noite)'
    }


# ── TikAPI ───────────────────────────────────────────────────────────────────

def tikapi_get(endpoint, params=None):
    """Faz GET autenticado ao TikAPI, incrementa contador e retorna (status, json, headers)."""
    resp = requests.get(
        f'{BASE_URL}{endpoint}',
        headers={'X-API-KEY': TIKAPI_KEY},
        params=params,
        timeout=15
    )
    increment_usage()
    try:
        return resp.status_code, resp.json(), dict(resp.headers)
    except Exception:
        return resp.status_code, {'error': 'Resposta inválida da API'}, {}


# ── Rotas ────────────────────────────────────────────────────────────────────

def login_required(f):
    """Decorator: redireciona para /login se APP_PASSWORD estiver definido e usuário não autenticado."""
    from functools import wraps
    from flask import session, redirect, url_for
    @wraps(f)
    def wrapper(*args, **kwargs):
        if APP_PASSWORD and not session.get('authenticated'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


@app.route('/login', methods=['GET', 'POST'])
def login():
    from flask import session, redirect, url_for
    if request.method == 'POST':
        if request.form.get('password') == APP_PASSWORD:
            session['authenticated'] = True
            return redirect(url_for('index'))
        return render_template('login.html', error=True)
    return render_template('login.html', error=False)


@app.route('/logout')
def logout():
    from flask import session, redirect, url_for
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    return render_template('index.html')


@app.route('/api/quota')
@login_required
def quota():
    """Retorna uso diário da API."""
    return jsonify(get_quota())


@app.route('/api/search/user')
@login_required
def search_user():
    username = request.args.get('username', '').strip().lstrip('@')
    if not username:
        return jsonify({'error': 'Username obrigatório'}), 400

    status, user_data, _ = tikapi_get('/public/user/info', {'username': username})
    if status != 200:
        return jsonify({'error': 'Usuário não encontrado', 'status_tikapi': status, 'detail': user_data}), 400

    user_info = user_data.get('userInfo', {})
    user_id = user_info.get('user', {}).get('id', '')
    sec_uid = user_info.get('user', {}).get('secUid', '')

    _, posts_data, _ = tikapi_get('/public/user/posts', {
        'id': user_id,
        'secUid': sec_uid,
        'count': 30
    })

    return jsonify({
        'type': 'user',
        'user': user_info,
        'posts': posts_data.get('itemList', []),
        'quota': get_quota()
    })


@app.route('/api/search/keyword')
@login_required
def search_keyword():
    query = request.args.get('q', '').strip()
    cursor = request.args.get('cursor', '')
    if not query:
        return jsonify({'error': 'Query obrigatória'}), 400

    params = {'query': query, 'count': 30, 'type': 'general'}
    if cursor:
        params['cursor'] = cursor

    status, data, _ = tikapi_get('/public/search/general', params)
    if status != 200:
        return jsonify({'error': 'Erro na busca', 'status_tikapi': status, 'detail': data}), 400

    # Cada entry vem como {'common': {...}, 'item': {...}} — extrai só o item
    raw = data.get('data', [])
    items = [entry['item'] for entry in raw if 'item' in entry]

    video_headers = data.get('$other', {}).get('videoLinkHeaders', {})
    next_cursor = data.get('nextCursor', '')
    has_more = data.get('has_more', False)

    return jsonify({
        'type': 'search',
        'items': items,
        'videoHeaders': video_headers,
        'cursor': next_cursor,
        'hasMore': has_more,
        'quota': get_quota()
    })


@app.route('/api/search/hashtag')
@login_required
def search_hashtag():
    names_raw  = request.args.get('names', '').strip() or request.args.get('name', '').strip()
    cursors_raw = request.args.get('cursors', '').strip()

    if not names_raw and not cursors_raw:
        return jsonify({'error': 'Nome da hashtag obrigatório'}), 400

    def resolve_name(name):
        """Retorna (hashtag_id, name) ou (None, name) se não encontrado."""
        status, info_data, _ = tikapi_get('/public/hashtag', {'name': name.lstrip('#')})
        if status != 200:
            return None, name
        ht_id = info_data.get('challengeInfo', {}).get('challenge', {}).get('id', '')
        return (ht_id or None), name

    def fetch_posts(ht_id, cursor=None):
        """Retorna (items, video_headers, next_cursor, has_more)."""
        params = {'id': ht_id}
        if cursor:
            params['cursor'] = cursor
        status, data, _ = tikapi_get('/public/hashtag', params)
        if status != 200:
            return [], {}, None, False
        items = data.get('itemList', [])
        headers = data.get('$other', {}).get('videoLinkHeaders', {})
        next_cursor = str(data.get('cursor', '')) or None
        has_more = bool(data.get('hasMore', False))
        return items, headers, next_cursor, has_more

    all_items    = []
    seen_ids     = set()
    video_headers = {}
    hashtag_states = {}  # {id: {name, cursor, hasMore}}

    if names_raw:
        # Primeira busca: resolve nomes → IDs e busca primeira página em paralelo
        names = [n.strip() for n in names_raw.split(',') if n.strip()][:5]

        def resolve_and_fetch(name):
            ht_id, name = resolve_name(name)
            if not ht_id:
                return name, None, [], {}, None, False
            items, headers, next_cursor, has_more = fetch_posts(ht_id)
            return name, ht_id, items, headers, next_cursor, has_more

        with ThreadPoolExecutor(max_workers=5) as executor:
            for name, ht_id, items, headers, next_cursor, has_more in executor.map(resolve_and_fetch, names):
                if not ht_id:
                    continue
                hashtag_states[ht_id] = {'name': name, 'cursor': next_cursor, 'hasMore': has_more}
                if headers:
                    video_headers = headers
                for item in items:
                    vid_id = item.get('id', '')
                    if vid_id and vid_id not in seen_ids:
                        seen_ids.add(vid_id)
                        item['_hashtag'] = name
                        all_items.append(item)

    else:
        # Load more: formato "id1:cursor1,id2:cursor2"
        pairs = []
        for part in cursors_raw.split(','):
            if ':' in part:
                ht_id, cursor = part.split(':', 1)
                pairs.append((ht_id.strip(), cursor.strip()))

        def fetch_more(pair):
            ht_id, cursor = pair
            items, headers, next_cursor, has_more = fetch_posts(ht_id, cursor)
            return ht_id, items, headers, next_cursor, has_more

        with ThreadPoolExecutor(max_workers=5) as executor:
            for ht_id, items, headers, next_cursor, has_more in executor.map(fetch_more, pairs):
                hashtag_states[ht_id] = {'cursor': next_cursor, 'hasMore': has_more}
                if headers:
                    video_headers = headers
                for item in items:
                    vid_id = item.get('id', '')
                    if vid_id and vid_id not in seen_ids:
                        seen_ids.add(vid_id)
                        all_items.append(item)

    return jsonify({
        'type': 'hashtag',
        'items': all_items,
        'hashtagStates': hashtag_states,
        'videoHeaders': video_headers,
        'quota': get_quota()
    })


@app.route('/api/search/intersect/keyword')
@login_required
def intersect_keyword():
    raw = request.args.get('q', '').strip()
    keywords = [k.strip() for k in raw.split(',') if k.strip()]
    if len(keywords) < 2:
        return jsonify({'error': 'Mínimo 2 palavras-chave para interseção'}), 400
    if len(keywords) > 5:
        return jsonify({'error': 'Máximo 5 palavras-chave'}), 400

    def fetch_kw(kw):
        status, data, _ = tikapi_get('/public/search/general', {'query': kw, 'count': 30, 'type': 'general'})
        if status != 200:
            return kw, {}, {}
        raw_entries = data.get('data', [])
        items_list = [entry['item'] for entry in raw_entries if 'item' in entry]
        headers = data.get('$other', {}).get('videoLinkHeaders', {})
        return kw, {item['id']: item for item in items_list if item.get('id')}, headers

    kw_maps = {}
    video_headers = {}

    with ThreadPoolExecutor(max_workers=5) as executor:
        for kw, id_map, headers in executor.map(fetch_kw, keywords):
            kw_maps[kw] = id_map
            if headers:
                video_headers = headers

    id_sets = [set(m.keys()) for m in kw_maps.values() if m]
    if len(id_sets) < 2:
        return jsonify({'type': 'intersect_keyword', 'items': [], 'videoHeaders': video_headers, 'quota': get_quota()})

    common_ids = id_sets[0].intersection(*id_sets[1:])

    all_items_map = {}
    for id_map in kw_maps.values():
        for vid_id, item in id_map.items():
            if vid_id not in all_items_map:
                all_items_map[vid_id] = item

    items = [all_items_map[vid_id] for vid_id in common_ids if vid_id in all_items_map]

    return jsonify({
        'type': 'intersect_keyword',
        'items': items,
        'videoHeaders': video_headers,
        'quota': get_quota()
    })


@app.route('/api/search/intersect/hashtag')
@login_required
def intersect_hashtag():
    names_raw = request.args.get('names', '').strip()
    names = [n.strip().lstrip('#') for n in names_raw.split(',') if n.strip()]
    if len(names) < 2:
        return jsonify({'error': 'Mínimo 2 hashtags para interseção'}), 400
    if len(names) > 5:
        return jsonify({'error': 'Máximo 5 hashtags'}), 400

    def resolve_and_fetch(name):
        status, info_data, _ = tikapi_get('/public/hashtag', {'name': name})
        if status != 200:
            return name, {}, {}
        ht_id = info_data.get('challengeInfo', {}).get('challenge', {}).get('id', '')
        if not ht_id:
            return name, {}, {}
        status, data, _ = tikapi_get('/public/hashtag', {'id': ht_id})
        if status != 200:
            return name, {}, {}
        items_list = data.get('itemList', [])
        headers = data.get('$other', {}).get('videoLinkHeaders', {})
        return name, {item['id']: item for item in items_list if item.get('id')}, headers

    ht_maps = {}
    video_headers = {}

    with ThreadPoolExecutor(max_workers=5) as executor:
        for name, id_map, headers in executor.map(resolve_and_fetch, names):
            ht_maps[name] = id_map
            if headers:
                video_headers = headers

    id_sets = [set(m.keys()) for m in ht_maps.values() if m]
    if len(id_sets) < 2:
        return jsonify({'type': 'intersect_hashtag', 'items': [], 'videoHeaders': video_headers, 'quota': get_quota()})

    common_ids = id_sets[0].intersection(*id_sets[1:])

    all_items_map = {}
    for id_map in ht_maps.values():
        for vid_id, item in id_map.items():
            if vid_id not in all_items_map:
                all_items_map[vid_id] = item

    items = [all_items_map[vid_id] for vid_id in common_ids if vid_id in all_items_map]

    return jsonify({
        'type': 'intersect_hashtag',
        'items': items,
        'videoHeaders': video_headers,
        'quota': get_quota()
    })


@app.route('/api/download')
@login_required
def proxy_download():
    url = request.args.get('url', '')
    filename = request.args.get('filename', 'tiktok_video.mp4')
    cookie = request.args.get('cookie', '')

    if not url:
        return jsonify({'error': 'URL obrigatória'}), 400

    try:
        headers = {
            'Referer': 'https://www.tiktok.com/',
            'Origin': 'https://www.tiktok.com',
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
            )
        }
        if cookie:
            headers['Cookie'] = cookie

        resp = requests.get(url, stream=True, headers=headers, timeout=60)

        content_type = resp.headers.get('Content-Type', '')
        # Se o TikTok retornou HTML/JSON em vez de vídeo, a URL expirou ou o cookie é inválido
        if resp.status_code != 200 or 'video' not in content_type:
            return jsonify({
                'error': 'URL do vídeo expirada ou inválida. Refaça a busca e tente novamente.',
                'status': resp.status_code,
                'content_type': content_type
            }), 400

        def stream_chunks():
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        return Response(
            stream_chunks(),
            content_type='video/mp4',
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
    except Exception:
        local_ip = '127.0.0.1'
    finally:
        s.close()

    port = 5000
    print(f'\n  TikAPI Scraper iniciado!')
    print(f'  Local:   http://localhost:{port}')
    print(f'  Rede:    http://{local_ip}:{port}')
    print(f'\n  Limite diário configurado: {DAILY_LIMIT} requests')
    print(f'  Compartilhe o endereço "Rede" com quem estiver no mesmo Wi-Fi.\n')

    app.run(host='0.0.0.0', port=port)
