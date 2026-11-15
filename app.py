from flask import Flask, render_template, request, redirect, url_for, flash
from urllib.parse import urlparse, unquote
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import pg8000
import os
import secrets
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Configuration
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")

if not app.secret_key:
    raise RuntimeError("SECRET_KEY manquante.")

# Protection CSRF des formulaires
csrf = CSRFProtect(app)

# Adresse IP réelle du visiteur sur Render
def get_client_ip():
    return request.headers.get("CF-Connecting-IP") or get_remote_address()

# Limitation des créations de liens
limiter = Limiter(
    key_func=get_client_ip,
    app=app,
    default_limits=[],
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
)

TZ = ZoneInfo("Africa/Niamey")


# Connexion PostgreSQL
def connect_database():
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL manquante.")

    url = urlparse(database_url)

    return pg8000.connect(
        user=unquote(url.username),
        password=unquote(url.password),
        host=url.hostname,
        port=url.port or 5432,
        database=url.path.lstrip("/"),
        ssl_context=True,
        timeout=20
    )


class QueryResult:
    def __init__(self, cursor):
        self.cursor = cursor
        self.columns = (
            [column[0] for column in cursor.description]
            if cursor.description else []
        )

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(zip(self.columns, row)) if row else None

    def fetchall(self):
        return [
            dict(zip(self.columns, row))
            for row in self.cursor.fetchall()
        ]


@contextmanager
def database():
    con = connect_database()

    try:
        class DatabaseWrapper:
            def execute(self, sql, params=()):
                cursor = con.cursor()
                cursor.execute(sql, params)
                return QueryResult(cursor)

        yield DatabaseWrapper()
        con.commit()

    except Exception:
        con.rollback()
        raise

    finally:
        con.close()


# Initialisation de la table des surprises
def init_db():
    with database() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS surprise_links (
                id BIGSERIAL PRIMARY KEY,
                slug TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        con.execute("""
            ALTER TABLE surprise_links
            ADD COLUMN IF NOT EXISTS orbit_text TEXT NOT NULL DEFAULT ''
        """)


# Page d'accueil
@app.route("/")
def index():
    return render_template("index.html")


# Création d'un lien surprise
@app.route("/create-surprise", methods=["POST"])
@limiter.limit("5 per hour")
def create_surprise():
    name = request.form.get("surprise_name", "").strip()
    message = request.form.get("surprise_message", "").strip()
    orbit_text = request.form.get("orbit_text", "").strip()

    if not name or not message:
        flash("Le prénom et le message sont obligatoires.")
        return redirect(url_for("index"))

    if len(name) > 60 or len(message) > 2000 or len(orbit_text) > 300:
        flash("Un des champs dépasse la longueur autorisée.")
        return redirect(url_for("index"))

    slug = secrets.token_urlsafe(12)

    with database() as con:
        con.execute("""
            INSERT INTO surprise_links
            (slug, name, message, orbit_text, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            slug,
            name,
            message,
            orbit_text,
            datetime.now(TZ).isoformat()
        ))

    link = url_for(
        "show_surprise",
        slug=slug,
        _external=True
    )

    flash("Ton lien surprise est prêt : " + link)

    return redirect(url_for("index"))


# Affichage de la surprise
@app.route("/surprise/<slug>")
def show_surprise(slug):
    with database() as con:
        surprise = con.execute("""
            SELECT name, message, orbit_text
            FROM surprise_links
            WHERE slug = %s
        """, (slug,)).fetchone()

    if surprise is None:
        return "Cette surprise n'existe pas.", 404

    return render_template(
        "surprise.html",
        surprise=surprise
    )


# Démarrage
init_db()

print("❤️ JUSTE POUR TOI V1 : application activée")

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False
    )
