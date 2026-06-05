#!/usr/bin/env python3
import base64
import hmac
import html
import os
import re
import sqlite3
import sys
import urllib.parse
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", ROOT)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "mex_games.sqlite3"))
STATIC_DIR = os.path.join(ROOT, "static")
APP_USER = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

PLATFORMS = ["Playstation", "Xbox"]
MEDIA_TYPES = ["Primária", "Secundária"]
STATUSES = [
    "Conta em utilização",
    "Disponível para teste de reenvio 60 dias",
    "Disponível para teste de reenvio 90 dias",
    "Não funcionou o Reenvio",
]
DATE_PATTERN = r"\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}"
DATE_RE = re.compile(rf"\b({DATE_PATTERN})\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
XBOX_ENTRY_RE = re.compile(rf"\b(?:c\s+)?(primaria|primária|secundaria|secundária)\s+({DATE_PATTERN})\b", re.IGNORECASE)
OLDEST_SENT_ORDER = "ORDER BY last_sent_at IS NULL, last_sent_at ASC, product ASC, media_type ASC, id ASC"
EMAIL_MARKER_SENT_DATE = "2026-01-31"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(seed=False):
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            product TEXT NOT NULL,
            media_type TEXT NOT NULL,
            email TEXT NOT NULL,
            status TEXT NOT NULL,
            last_sent_at TEXT,
            status_changed_at TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_accounts_filters
            ON accounts(platform, product, media_type, status);
        """
    )

    account_count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    if seed and account_count == 0:
        today = date.today()
        sample_rows = [
            ("Playstation", "FC 26 PS5", "Primária", "fc26.ps5.01@example.com", "Conta em utilização", today - timedelta(days=12), "Cliente ativo."),
            ("Playstation", "GTA V PS4", "Secundária", "gtav.ps4.02@example.com", "Conta em utilização", today - timedelta(days=32), "Cliente recebeu em maio."),
            ("Playstation", "Spider-Man 2 PS5", "Primária", "spider.ps5.03@example.com", "Conta em utilização", today - timedelta(days=114), "Será marcada automaticamente para teste."),
            ("Xbox", "Forza Horizon 5", "Primária", "forza.xbox.04@example.com", "Conta em utilização", today - timedelta(days=8), "Em uso por cliente ativo."),
            ("Playstation", "The Last of Us PS4", "Primária", "tlou.ps4.06@example.com", "Disponível para teste de reenvio 90 dias", today - timedelta(days=140), "Pronta para testar reenvio."),
        ]
        conn.executemany(
            """
            INSERT INTO accounts(platform, product, media_type, email, status, last_sent_at, status_changed_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(p, pr, mt, em, st, d.isoformat() if d else None, today.isoformat(), n) for p, pr, mt, em, st, d, n in sample_rows],
        )

    conn.commit()
    conn.close()
    migrate_statuses()


def apply_90_day_rule():
    cutoff_90 = (date.today() - timedelta(days=90)).isoformat()
    cutoff_60 = (date.today() - timedelta(days=60)).isoformat()
    failed_cutoff = (date.today() - timedelta(days=30)).isoformat()
    conn = db()
    conn.execute(
        """
        UPDATE accounts
           SET status = 'Disponível para teste de reenvio 90 dias',
               status_changed_at = ?,
               updated_at = CURRENT_TIMESTAMP
         WHERE status IN ('Conta em utilização', 'Enviada', 'Disponível para teste de reenvio', 'Disponível para teste de reenvio 60 dias')
           AND last_sent_at IS NOT NULL
           AND last_sent_at <= ?
        """,
        (date.today().isoformat(), cutoff_90),
    )
    conn.execute(
        """
        UPDATE accounts
           SET status = 'Disponível para teste de reenvio 60 dias',
               status_changed_at = ?,
               updated_at = CURRENT_TIMESTAMP
         WHERE status IN ('Conta em utilização', 'Enviada')
           AND last_sent_at IS NOT NULL
           AND last_sent_at <= ?
        """,
        (date.today().isoformat(), cutoff_60),
    )
    conn.execute(
        """
        UPDATE accounts
           SET status = 'Disponível para teste de reenvio 90 dias',
               status_changed_at = ?,
               updated_at = CURRENT_TIMESTAMP
         WHERE status = 'Não funcionou o Reenvio'
           AND status_changed_at IS NOT NULL
           AND status_changed_at != ''
           AND status_changed_at <= ?
        """,
        (date.today().isoformat(), failed_cutoff),
    )
    conn.commit()
    conn.close()


def normalize_status(value):
    value = (value or "").strip()
    if value in STATUSES:
        return value
    return "Conta em utilização"


def ensure_status_changed_column(conn):
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()]
    if "status_changed_at" not in columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN status_changed_at TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "UPDATE accounts SET status_changed_at = ? WHERE status_changed_at IS NULL OR status_changed_at = ''",
        (date.today().isoformat(),),
    )


def migrate_statuses():
    conn = db()
    ensure_status_changed_column(conn)
    conn.execute(
        """
        UPDATE accounts
           SET status = 'Disponível para teste de reenvio 90 dias',
               status_changed_at = CASE WHEN status_changed_at = '' THEN ? ELSE status_changed_at END,
               updated_at = CURRENT_TIMESTAMP
         WHERE status = 'Disponível para teste de reenvio'
        """,
        (date.today().isoformat(),),
    )
    conn.execute(
        """
        UPDATE accounts
           SET status = 'Conta em utilização',
               status_changed_at = CASE WHEN status_changed_at = '' THEN ? ELSE status_changed_at END,
               updated_at = CURRENT_TIMESTAMP
         WHERE status IN ('Disponível', 'Enviada', 'Problema/bloqueada')
        """,
        (date.today().isoformat(),),
    )
    conn.execute(
        """
        UPDATE accounts
           SET platform = 'Playstation',
               product = CASE
                   WHEN product LIKE '% PS4' OR product LIKE '% PS5' THEN product
                   ELSE product || ' ' || platform
               END,
               updated_at = CURRENT_TIMESTAMP
         WHERE platform IN ('PS4', 'PS5')
        """
    )
    conn.commit()
    conn.close()
    apply_90_day_rule()


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def normalize_media_type(platform, raw):
    value = (raw or "").strip().lower()
    platform_value = (platform or "").strip().lower()
    if "sec" in value:
        return "Secundária"
    if "prim" in value:
        return "Primária"
    if platform_value == "playstation" and (value.startswith("ps4") or value.startswith("ps5")):
        return "Primária"
    return "Primária"


def infer_platform(default_platform, line):
    upper = line.upper()
    if "XBOX" in upper:
        return "Xbox"
    if "PS4" in upper or "PS5" in upper:
        return "Playstation"
    return default_platform if default_platform in PLATFORMS else "Playstation"


def product_for_line(product, line):
    upper = line.upper()
    base = product.strip()
    if "PS5" in upper and not base.upper().endswith(" PS5"):
        return f"{base} PS5"
    if "PS4" in upper and not base.upper().endswith(" PS4"):
        return f"{base} PS4"
    return base


def parse_block_lines(product, default_platform, block_text):
    current_email = ""
    latest_by_slot = {}
    ignored = 0

    def keep_latest(row, date_priority):
        key = (row["email"], row["platform"], row["product"], row["media_type"])
        previous = latest_by_slot.get(key)
        if previous is None:
            row["date_priority"] = date_priority
            latest_by_slot[key] = row
            return
        if date_priority > previous["date_priority"]:
            row["date_priority"] = date_priority
            latest_by_slot[key] = row
            return
        if date_priority == previous["date_priority"] and row["last_sent_at"] > previous["last_sent_at"]:
            row["date_priority"] = date_priority
            latest_by_slot[key] = row

    for raw_line in block_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        email_match = EMAIL_RE.search(line)
        if email_match:
            current_email = email_match.group(0).lower()
            line = line[email_match.end():].strip()
            if not line:
                continue

        upper = line.upper()
        xbox_entries = list(XBOX_ENTRY_RE.finditer(line))
        if current_email and (default_platform == "Xbox" or "XBOX" in upper) and xbox_entries:
            for entry in xbox_entries:
                media_type = normalize_media_type("Xbox", entry.group(1))
                last_sent_at = parse_date(entry.group(2))
                keep_latest(
                    {
                        "platform": "Xbox",
                        "product": product.strip(),
                        "media_type": media_type,
                        "email": current_email,
                        "status": "Conta em utilização",
                        "last_sent_at": last_sent_at,
                        "notes": "Adicionado por bloco de contas. Senha, código e WhatsApp não foram salvos.",
                    },
                    3,
                )
            continue

        media_type = ""
        if "SECUNDARIA" in upper or "SECUNDÁRIA" in upper:
            media_type = "Secundária"
        elif "PRIMARIA" in upper or "PRIMÁRIA" in upper or "PS4" in upper or "PS5" in upper:
            media_type = "Primária"

        if not media_type:
            ignored += 1
            continue
        if not current_email:
            ignored += 1
            continue

        dates = DATE_RE.findall(line)
        has_real_date = bool(dates)
        is_playstation_email_marker = ("PS4" in upper or "PS5" in upper) and "EMAIL" in upper
        if dates:
            last_sent_at = parse_date(dates[-1])
            date_priority = 3
        elif is_playstation_email_marker:
            last_sent_at = EMAIL_MARKER_SENT_DATE
            date_priority = 2
        else:
            last_sent_at = (date.today() - timedelta(days=30)).isoformat()
            date_priority = 1
        platform = infer_platform(default_platform, line)
        row = {
            "platform": platform,
            "product": product_for_line(product, line),
            "media_type": media_type,
            "email": current_email,
            "status": "Conta em utilização",
            "last_sent_at": last_sent_at,
            "notes": "Adicionado por bloco de contas. Senha e código não foram salvos.",
        }
        keep_latest(row, date_priority)

    rows = []
    for row in latest_by_slot.values():
        clean_row = dict(row)
        clean_row.pop("date_priority", None)
        rows.append(clean_row)
    rows = sorted(
        rows,
        key=lambda row: (row["email"], row["product"], row["media_type"]),
    )
    return rows, ignored


def layout(title, body, user=None, active=""):
    links = [
        ("/", "Dashboard", "dashboard"),
        ("/accounts", "Contas", "accounts"),
        ("/reports", "Relatórios", "reports"),
        ("/blocks", "Adicionar bloco", "blocks"),
    ]
    nav_links = "".join(
        f'<a class="{"active" if active == key else ""}" href="{href}">{label}</a>'
        for href, label, key in links
    )
    nav = f"""
    <header class="topbar">
        <div>
            <strong>Mex Games</strong>
            <span>Estoque de contas</span>
        </div>
        <nav>{nav_links}</nav>
    </header>
    """
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{esc(title)} | Mex Games</title>
    <link rel="stylesheet" href="/static/style.css">
</head>
<body>
    {nav}
    <main class="page">{body}</main>
</body>
</html>"""


def options(items, selected="", blank="Todos"):
    rows = [f'<option value="">{blank}</option>'] if blank is not None else []
    for item in items:
        rows.append(f'<option value="{esc(item)}" {"selected" if item == selected else ""}>{esc(item)}</option>')
    return "".join(rows)


def account_form(row=None, errors=None):
    row = row or {}
    errors_html = ""
    if errors:
        errors_html = '<div class="alert error">' + "<br>".join(esc(e) for e in errors) + "</div>"
    return f"""
    <section class="section-head">
        <div>
            <h1>{'Editar conta' if row else 'Cadastrar conta'}</h1>
            <p>Cadastre somente os dados necessários. Senhas e códigos de segurança ficam fora do sistema.</p>
        </div>
    </section>
    {errors_html}
    <form method="post" class="form-panel">
        <label>Plataforma
            <select name="platform" required>{options(PLATFORMS, row.get('platform', ''), None)}</select>
        </label>
        <label>Produto/Jogo
            <input name="product" value="{esc(row.get('product', ''))}" required maxlength="120">
        </label>
        <label>Tipo de mídia
            <select name="media_type" required>{options(MEDIA_TYPES, row.get('media_type', ''), None)}</select>
        </label>
        <label>E-mail
            <input type="email" name="email" value="{esc(row.get('email', ''))}" required maxlength="160">
        </label>
        <label>Status
            <select name="status" required>{options(STATUSES, row.get('status', 'Conta em utilização'), None)}</select>
        </label>
        <label>Data do último envio
            <input type="date" name="last_sent_at" value="{esc(row.get('last_sent_at', '') or '')}">
        </label>
        <label class="wide">Observações
            <textarea name="notes" rows="4">{esc(row.get('notes', ''))}</textarea>
        </label>
        <div class="actions wide">
            <button class="primary" type="submit">Salvar</button>
            <a class="button" href="/accounts">Cancelar</a>
        </div>
    </form>
    """


class App(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def check_auth(self):
        if not APP_PASSWORD:
            return True
        header = self.headers.get("Authorization", "")
        prefix = "Basic "
        if not header.startswith(prefix):
            return False
        try:
            decoded = base64.b64decode(header[len(prefix):]).decode("utf-8")
        except Exception:
            return False
        username, sep, password = decoded.partition(":")
        if not sep:
            return False
        return hmac.compare_digest(username, APP_USER) and hmac.compare_digest(password, APP_PASSWORD)

    def require_auth(self):
        if self.check_auth():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Mex Games"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Acesso restrito.".encode("utf-8"))
        return False

    def send_html(self, content, status=200, extra_headers=None):
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def read_form(self):
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        return {k: v[0] if v else "" for k, v in urllib.parse.parse_qs(data).items()}

    def query(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def do_GET(self):
        if not self.require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/static/"):
            return self.serve_static(path)
        if path in ("/login", "/logout"):
            return self.redirect("/")
        user = None
        apply_90_day_rule()

        if path == "/":
            return self.dashboard(user)
        if path == "/accounts":
            return self.accounts(user)
        if path == "/accounts/new":
            return self.new_account(user)
        if path == "/accounts/edit":
            return self.edit_account(user)
        if path == "/reports":
            return self.reports(user)
        if path == "/blocks":
            return self.block_page(user)
        if path == "/import":
            return self.redirect("/blocks")
        self.send_html(layout("Não encontrado", "<h1>Página não encontrada</h1>", user), 404)

    def do_POST(self):
        if not self.require_auth():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        user = None
        apply_90_day_rule()

        if path == "/accounts/new":
            return self.save_account(user)
        if path == "/accounts/edit":
            return self.save_account(user, edit=True)
        if path == "/accounts/status":
            return self.update_account_status(user)
        if path == "/blocks":
            return self.add_block(user)
        if path == "/import":
            return self.redirect("/blocks")
        if path == "/admin/delete-product-contains":
            return self.delete_product_contains(user)
        self.send_html(layout("Não encontrado", "<h1>Página não encontrada</h1>", user), 404)

    def serve_static(self, path):
        name = os.path.basename(path)
        file_path = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(file_path):
            self.send_response(404)
            self.end_headers()
            return
        with open(file_path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/css; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def get_counts(self, filters=None):
        where, params = self.filter_sql(filters or {})
        conn = db()
        rows = conn.execute(
            f"SELECT status, COUNT(*) total FROM accounts {where} GROUP BY status",
            params,
        ).fetchall()
        total = conn.execute(f"SELECT COUNT(*) FROM accounts {where}", params).fetchone()[0]
        conn.close()
        by_status = {r["status"]: r["total"] for r in rows}
        return {
            "total": total,
            "reenvio_60": by_status.get("Disponível para teste de reenvio 60 dias", 0),
            "reenvio_90": by_status.get("Disponível para teste de reenvio 90 dias", 0),
            "uso": by_status.get("Conta em utilização", 0),
            "falhou": by_status.get("Não funcionou o Reenvio", 0),
        }

    def dashboard(self, user):
        counts = self.get_counts()
        conn = db()
        recent = conn.execute(
            f"SELECT * FROM accounts {OLDEST_SENT_ORDER} LIMIT 6"
        ).fetchall()
        conn.close()
        cards = [
            ("Em utilização", counts["uso"]),
            ("Reenvio 60 dias", counts["reenvio_60"]),
            ("Reenvio 90 dias", counts["reenvio_90"]),
            ("Não funcionou o Reenvio", counts["falhou"]),
            ("Total cadastrado", counts["total"]),
        ]
        body = f"""
        <section class="section-head">
            <div>
                <h1>Dashboard</h1>
                <p>Regra automática: contas com 60 e 90 dias entram nas filas de teste de reenvio correspondentes.</p>
            </div>
            <a class="button primary" href="/accounts/new">Cadastrar conta</a>
        </section>
        <section class="cards">
            {''.join(f'<article><span>{esc(label)}</span><strong>{value}</strong></article>' for label, value in cards)}
        </section>
        <section class="panel">
            <div class="panel-title">
                <h2>Enviadas há mais tempo</h2>
                <a href="/accounts">Ver todas</a>
            </div>
            {self.table(recent, compact=True)}
        </section>
        """
        self.send_html(layout("Dashboard", body, user, "dashboard"))

    def filter_sql(self, filters):
        clauses = []
        params = []
        fields = {
            "platform": "platform",
            "product": "product",
            "media_type": "media_type",
            "status": "status",
        }
        for key, column in fields.items():
            value = filters.get(key, "")
            if value:
                if key == "product":
                    clauses.append(f"{column} LIKE ?")
                    params.append(f"%{value}%")
                else:
                    clauses.append(f"{column} = ?")
                    params.append(value)
        return ("WHERE " + " AND ".join(clauses) if clauses else ""), params

    def filters_from_query(self):
        qs = self.query()
        return {k: qs.get(k, [""])[0] for k in ("platform", "product", "media_type", "status")}

    def filter_form(self, filters, action):
        return f"""
        <form class="filters" method="get" action="{action}">
            <label>Plataforma<select name="platform">{options(PLATFORMS, filters.get('platform', ''))}</select></label>
            <label>Produto/Jogo<input name="product" value="{esc(filters.get('product', ''))}" placeholder="Buscar produto"></label>
            <label>Tipo de mídia<select name="media_type">{options(MEDIA_TYPES, filters.get('media_type', ''))}</select></label>
            <label>Status<select name="status">{options(STATUSES, filters.get('status', ''))}</select></label>
            <button class="primary" type="submit">Filtrar</button>
            <a class="button" href="{action}">Limpar</a>
        </form>
        """

    def accounts(self, user):
        filters = self.filters_from_query()
        where, params = self.filter_sql(filters)
        conn = db()
        rows = conn.execute(
            f"SELECT * FROM accounts {where} {OLDEST_SENT_ORDER}",
            params,
        ).fetchall()
        conn.close()
        body = f"""
        <section class="section-head">
            <div>
                <h1>Contas</h1>
                <p>{len(rows)} conta(s) encontradas.</p>
            </div>
            <a class="button primary" href="/accounts/new">Cadastrar conta</a>
        </section>
        {self.filter_form(filters, '/accounts')}
        <section class="panel">{self.table(rows)}</section>
        """
        self.send_html(layout("Contas", body, user, "accounts"))

    def table(self, rows, compact=False):
        if not rows:
            return '<div class="empty">Nenhuma conta encontrada.</div>'
        head = """
        <thead><tr>
            <th>Plataforma</th><th>Produto/Jogo</th><th>Mídia</th><th>E-mail</th>
            <th>Status / Alterar</th><th>Último envio</th>
        </tr></thead>
        """
        body_rows = []
        for row in rows:
            status_cell = self.status_badge(row)
            if not compact:
                status_cell += self.status_action(row)
            body_rows.append(
                f"""
                <tr>
                    <td>{esc(row['platform'])}</td>
                    <td>{esc(row['product'])}</td>
                    <td>{esc(row['media_type'])}</td>
                    <td>{esc(row['email'])}</td>
                    <td>{status_cell}</td>
                    <td>{esc(self.format_date(row['last_sent_at']))}</td>
                </tr>
                """
            )
        return f'<div class="table-wrap"><table>{head}<tbody>{"".join(body_rows)}</tbody></table></div>'

    def status_badge(self, row):
        return f'<span class="badge {self.status_class(row["status"])}">{esc(row["status"])}</span>'

    def status_class(self, status):
        return {
            "Disponível para teste de reenvio 60 dias": "review60",
            "Disponível para teste de reenvio 90 dias": "review90",
            "Conta em utilização": "use",
            "Não funcionou o Reenvio": "failed",
        }.get(status, "")

    def status_action(self, row):
        return f"""
        <form method="post" action="/accounts/status" class="status-form">
            <input type="hidden" name="id" value="{row['id']}">
            <select name="status" aria-label="Status da conta">{options(STATUSES, row['status'], None)}</select>
            <button type="submit">Alterar</button>
        </form>
        """

    def format_date(self, value):
        if not value:
            return "-"
        try:
            return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            return value

    def new_account(self, user):
        self.send_html(layout("Cadastrar conta", account_form(), user, "accounts"))

    def edit_account(self, user):
        account_id = self.query().get("id", [""])[0]
        conn = db()
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        conn.close()
        if not row:
            return self.send_html(layout("Conta não encontrada", "<h1>Conta não encontrada</h1>", user), 404)
        self.send_html(layout("Editar conta", account_form(dict(row)), user, "accounts"))

    def validate_account(self, form):
        errors = []
        platform = form.get("platform", "").strip()
        product = form.get("product", "").strip()
        media_type = form.get("media_type", "").strip()
        email = form.get("email", "").strip().lower()
        status = form.get("status", "").strip()
        last_sent_at = parse_date(form.get("last_sent_at", ""))
        notes = form.get("notes", "").strip()
        if platform not in PLATFORMS:
            errors.append("Selecione uma plataforma válida.")
        if not product:
            errors.append("Informe o produto/jogo.")
        if media_type not in MEDIA_TYPES:
            errors.append("Selecione o tipo de mídia.")
        if "@" not in email:
            errors.append("Informe um e-mail válido.")
        if status not in STATUSES:
            errors.append("Selecione um status válido.")
        return errors, {
            "platform": platform,
            "product": product,
            "media_type": media_type,
            "email": email,
            "status": status,
            "last_sent_at": last_sent_at,
            "notes": notes,
        }

    def save_account(self, user, edit=False):
        form = self.read_form()
        errors, row = self.validate_account(form)
        account_id = self.query().get("id", [""])[0]
        if errors:
            self.send_html(layout("Corrigir conta", account_form(row, errors), user, "accounts"), 422)
            return
        conn = db()
        if edit:
            conn.execute(
                """
                UPDATE accounts
                   SET platform = ?, product = ?, media_type = ?, email = ?, status = ?,
                       last_sent_at = ?, status_changed_at = ?, notes = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (
                    row["platform"],
                    row["product"],
                    row["media_type"],
                    row["email"],
                    row["status"],
                    row["last_sent_at"],
                    date.today().isoformat(),
                    row["notes"],
                    account_id,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO accounts(platform, product, media_type, email, status, last_sent_at, status_changed_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["platform"],
                    row["product"],
                    row["media_type"],
                    row["email"],
                    row["status"],
                    row["last_sent_at"],
                    date.today().isoformat(),
                    row["notes"],
                ),
            )
        conn.commit()
        conn.close()
        self.redirect("/accounts")

    def update_account_status(self, user):
        form = self.read_form()
        account_id = form.get("id", "")
        status = form.get("status", "")
        if status not in STATUSES:
            return self.redirect("/accounts")
        conn = db()
        today = date.today().isoformat()
        if status == "Conta em utilização":
            conn.execute(
                """
                UPDATE accounts
                   SET status = ?,
                       last_sent_at = ?,
                       status_changed_at = ?,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (status, today, today, account_id),
            )
        else:
            conn.execute(
                """
                UPDATE accounts
                   SET status = ?,
                       status_changed_at = ?,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
                """,
                (status, today, account_id),
            )
        conn.commit()
        conn.close()
        self.redirect("/accounts")

    def delete_product_contains(self, user):
        form = self.read_form()
        product = form.get("product", "").strip()
        if not product:
            self.send_html("Informe o produto.", 400)
            return
        conn = db()
        cur = conn.execute(
            "DELETE FROM accounts WHERE lower(product) LIKE '%' || lower(?) || '%'",
            (product,),
        )
        conn.commit()
        conn.close()
        self.send_html(f"{cur.rowcount} conta(s) removidas contendo produto: {esc(product)}")

    def reports(self, user):
        filters = self.filters_from_query()
        counts = self.get_counts(filters)
        body = f"""
        <section class="section-head">
            <div>
                <h1>Relatórios</h1>
                <p>Filtre o estoque para ver os totais por situação.</p>
            </div>
        </section>
        {self.filter_form(filters, '/reports')}
        <section class="cards">
            <article><span>Reenvio 60 dias</span><strong>{counts['reenvio_60']}</strong></article>
            <article><span>Reenvio 90 dias</span><strong>{counts['reenvio_90']}</strong></article>
            <article><span>Em utilização</span><strong>{counts['uso']}</strong></article>
            <article><span>Não funcionou o Reenvio</span><strong>{counts['falhou']}</strong></article>
            <article><span>Total cadastrado</span><strong>{counts['total']}</strong></article>
        </section>
        """
        self.send_html(layout("Relatórios", body, user, "reports"))

    def product_options(self):
        conn = db()
        products = [
            row["product"]
            for row in conn.execute("SELECT DISTINCT product FROM accounts ORDER BY product").fetchall()
        ]
        conn.close()
        return "".join(f'<option value="{esc(product)}"></option>' for product in products)

    def block_page(self, user, message="", form=None):
        form = form or {}
        body = f"""
        <section class="section-head">
            <div>
                <h1>Adicionar bloco de contas</h1>
                <p>Cole o bloco do Google Docs. O sistema lê e-mail, tipo de vaga e última data de envio.</p>
            </div>
        </section>
        {message}
        <section class="panel">
            <form method="post" class="block-form" autocomplete="off">
                <div class="inline-fields">
                    <label>Produto/Jogo
                        <input name="product" list="products" value="{esc(form.get('product', ''))}" required maxlength="120" placeholder="Ex: FC 26" autocomplete="off">
                        <datalist id="products">{self.product_options()}</datalist>
                    </label>
                    <label>Plataforma padrão
                        <select name="default_platform">{options(PLATFORMS, form.get('default_platform', 'Playstation'), None)}</select>
                    </label>
                </div>
                <label>Bloco de contas
                    <textarea name="block_text" rows="18" required placeholder="Cole aqui o texto do Google Docs">{esc(form.get('block_text', ''))}</textarea>
                </label>
                <button class="primary" type="submit">Adicionar bloco</button>
            </form>
            <div class="hint">
                O sistema salva somente o último envio encontrado para cada vaga primária e secundária.
                Linhas com PS4 ou PS5 viram vagas primárias e adicionam PS4/PS5 ao nome do jogo. Linhas com SECUNDARIA viram vagas secundárias.
                Senhas, datas de nascimento, usuário e códigos não são salvos.
            </div>
        </section>
        """
        self.send_html(layout("Adicionar bloco", body, user, "blocks"))

    def add_block(self, user):
        form = self.read_form()
        product = form.get("product", "").strip()
        default_platform = form.get("default_platform", "Playstation").strip()
        block_text = form.get("block_text", "")
        if not product:
            return self.block_page(user, '<div class="alert error">Selecione ou informe o produto/jogo.</div>', form)
        rows, ignored = parse_block_lines(product, default_platform, block_text)
        if not rows:
            return self.block_page(
                user,
                '<div class="alert error">Nenhuma vaga primária ou secundária foi encontrada no bloco.</div>',
                form,
            )
        conn = db()
        created = 0
        updated = 0
        for row in rows:
            existing = conn.execute(
                """
                SELECT id, status
                  FROM accounts
                 WHERE email = ?
                   AND platform = ?
                   AND product = ?
                   AND media_type = ?
                 ORDER BY id
                 LIMIT 1
                """,
                (
                    row["email"],
                    row["platform"],
                    row["product"],
                    row["media_type"],
                ),
            ).fetchone()
            if existing:
                status_changed_at = date.today().isoformat() if existing["status"] != row["status"] else None
                if status_changed_at:
                    conn.execute(
                        """
                        UPDATE accounts
                           SET status = ?,
                               last_sent_at = ?,
                               status_changed_at = ?,
                               notes = ?,
                               updated_at = CURRENT_TIMESTAMP
                         WHERE id = ?
                        """,
                        (row["status"], row["last_sent_at"], status_changed_at, row["notes"], existing["id"]),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE accounts
                           SET status = ?,
                               last_sent_at = ?,
                               notes = ?,
                               updated_at = CURRENT_TIMESTAMP
                         WHERE id = ?
                        """,
                        (row["status"], row["last_sent_at"], row["notes"], existing["id"]),
                    )
                updated += 1
                continue
            conn.execute(
                """
                INSERT INTO accounts(platform, product, media_type, email, status, last_sent_at, status_changed_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["platform"],
                    row["product"],
                    row["media_type"],
                    row["email"],
                    row["status"],
                    row["last_sent_at"],
                    date.today().isoformat(),
                    row["notes"],
                ),
            )
            created += 1
        conn.commit()
        conn.close()
        apply_90_day_rule()
        primary = sum(1 for row in rows if row["media_type"] == "Primária")
        secondary = sum(1 for row in rows if row["media_type"] == "Secundária")
        message = (
            f'<div class="alert success">{len(rows)} vaga(s) processadas com o último envio de cada tipo: '
            f'{primary} primária(s) e {secondary} secundária(s). '
            f'{created} criada(s), {updated} atualizada(s). '
            f'{ignored} linha(s) ignoradas.</div>'
        )
        return self.block_page(user, message, {"product": "", "default_platform": default_platform, "block_text": ""})


def main():
    seed = "--seed" in sys.argv
    init_db(seed=seed)
    migrate_statuses()
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), App)
    print(f"Mex Games rodando em http://{host}:{port}")
    print(f"Banco de dados: {DB_PATH}")
    if APP_PASSWORD:
        print(f"Acesso protegido por senha. Usuário: {APP_USER}")
    else:
        print("Acesso sem senha. Defina APP_PASSWORD antes de publicar na internet.")
    server.serve_forever()


if __name__ == "__main__":
    main()
