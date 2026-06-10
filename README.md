# tiktok-scraper

> Proxy web para buscar, visualizar e baixar vídeos do TikTok via TikAPI.

## Stack

| Camada | Tecnologia | Versão |
|---|---|---|
| Backend | Python / Flask | 3.0.3 |
| HTTP client | requests | 2.32.3 |
| Servidor produção | Gunicorn | 22.0.0 |
| Frontend | HTML + JS vanilla | — |
| Deploy | Render | — |

## Como rodar

```bash
pip install -r requirements.txt
cp .env.example .env   # edite com seus valores
python app.py
```

Produção (Render usa `Procfile` automaticamente):
```bash
gunicorn app:app
```

## Estrutura

```
app.py                  # Flask: todas as rotas e lógica de proxy
templates/
  index.html            # SPA: busca, cards, download, timer de expiração
  login.html            # Tela de autenticação por senha
usage.json              # Contador diário de chamadas à API (auto-gerado)
Procfile                # Comando de start para Render
requirements.txt        # Dependências Python
```

## Variáveis de ambiente

| Variável | Obrigatória | Descrição |
|---|---|---|
| `TIKAPI_KEY` | Sim | Chave da API em api.tikapi.io |
| `APP_PASSWORD` | Sim | Senha de acesso à ferramenta |
| `SECRET_KEY` | Sim | Chave para assinar sessões Flask |
| `TIKAPI_DAILY_LIMIT` | Não | Limite diário de requisições (padrão: 100) |

## Fluxo principal

1. Usuário acessa a URL e faz login com `APP_PASSWORD`
2. Escolhe uma aba: Palavra-chave, Hashtag, Interseção ou Username
3. O browser envia `GET /api/search/*` para o Flask
4. Flask repassa ao TikAPI com `X-API-KEY`, incrementa `usage.json`
5. Frontend renderiza cards com thumbnail, stats e botões de ação
6. Usuário clica "Baixar": Flask faz proxy do stream do CDN do TikTok com `tt_chain_token`
7. URLs do CDN expiram via `x-expires`; contador regressivo na toolbar avisa o usuário

## Convenções

- Comentários de código em português
- Nomes de variáveis e funções em inglês
- Commits em português com prefixo `feat:` / `fix:` / `refactor:`

## Integrações externas

| Serviço | Para quê | Onde é configurado |
|---|---|---|
| TikAPI (`api.tikapi.io`) | Proxy para a API do TikTok | `TIKAPI_KEY` no `.env` / Render |
| TikTok CDN | Stream de download dos vídeos | Cookie `tt_chain_token` (vem do TikAPI) |
| Render | Hospedagem do serviço | `Procfile` + variáveis de ambiente no painel |
