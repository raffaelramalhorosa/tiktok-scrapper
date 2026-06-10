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

    return jsonify({
        'type': 'search',
        'items': items,
        'videoHeaders': video_headers,
        'cursor': data.get('nextCursor', ''),
        'hasMore': data.get('has_more', False),
        'quota': get_quota()
    })


@app.route('/api/search/multi')
@login_required
def search_multi():
    raw = request.args.get('q', '')
    keywords = [k.strip() for k in raw.split(',') if k.strip()]

    if not keywords:
        return jsonify({'error': 'Pelo menos uma keyword é obrigatória'}), 400
    if len(keywords) > 5:
        return jsonify({'error': 'Máximo 5 palavras-chave por busca'}), 400

    def fetch_keyword(kw):
        status, data, _ = tikapi_get('/public/search/general', {
            'query': kw, 'count': 30, 'type': 'general'
        })
        if status != 200:
            return kw, [], {}
        raw_entries = data.get('data', [])
        items = [entry['item'] for entry in raw_entries if 'item' in entry]
        headers = data.get('$other', {}).get('videoLinkHeaders', {})
        return kw, items, headers

    all_items = []
    seen_ids = set()
    video_headers = {}
    keyword_counts = {}

    # Dispara todas as buscas em paralelo
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_keyword, kw): kw for kw in keywords}
        for future in as_completed(futures):
            kw, items, headers = future.result()
            keyword_counts[kw] = len(items)
            if headers:
                video_headers = headers
            for item in items:
                vid_id = item.get('id', '')
                if vid_id and vid_id not in seen_ids:
                    seen_ids.add(vid_id)
                    item['_keyword'] = kw  # indica qual keyword achou este vídeo
                    all_items.append(item)

    return jsonify({
        'type': 'multi',
        'items': all_items,
        'videoHeaders': video_headers,
        'keywordCounts': keyword_counts,
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
