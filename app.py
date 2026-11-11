from flask import Flask, render_template, request, redirect, url_for, flash
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, unquote
from contextlib import contextmanager
from pathlib import Path
from dotenv import load_dotenv
import pg8000
import os
import secrets

# Charger les paramètres locaux
load_dotenv(dotenv_path=Path(__file__).with_name(".env"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY")

if not app.secret_key:
    raise RuntimeError("SECRET_KEY manquante.")

TZ = ZoneInfo("Africa/Niamey")


def connect_database():
    url = urlparse(os.environ["DATABASE_URL"])

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


def init_db():
    with database() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS recipients (
                id BIGSERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                channel TEXT NOT NULL,
                mode TEXT NOT NULL,
                schedule TEXT NOT NULL,
                message TEXT NOT NULL,
                consent BOOLEAN NOT NULL DEFAULT FALSE,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                last_sent_at TEXT
            )
        """)

        con.execute("""
            ALTER TABLE recipients
            ADD COLUMN IF NOT EXISTS last_sent_at TEXT
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS surprise_links (
                id BIGSERIAL PRIMARY KEY,
                slug TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)


def check_due_messages():
    now = datetime.now(TZ)

    with database() as con:
        recipients = con.execute("""
            SELECT * FROM recipients
            WHERE active = TRUE AND consent = TRUE
        """).fetchall()

        for r in recipients:
            try:
                scheduled = datetime.fromisoformat(r["schedule"])

                if scheduled.tzinfo is None:
                    scheduled = scheduled.replace(tzinfo=TZ)
                else:
                    scheduled = scheduled.astimezone(TZ)

                if now < scheduled:
                    continue

                last = r["last_sent_at"]

                if last:
                    last_date = datetime.fromisoformat(last).date()
                else:
                    last_date = None

                if last_date == now.date():
                    continue

                print("\n" + "=" * 40)
                print("SIMULATION : MESSAGE PROGRAMMÉ")
                print("Destinataire :", r["name"])
                print("Canal :", r["channel"])
                print("Message :", r["message"])
                print("Heure :", now.strftime("%d/%m/%Y %H:%M"))
                print("AUCUN MESSAGE RÉEL ENVOYÉ")
                print("=" * 40)

                con.execute("""
                    UPDATE recipients
                    SET last_sent_at = %s
                    WHERE id = %s
                """, (now.isoformat(), r["id"]))

                if r["mode"] == "once":
                    con.execute("""
                        UPDATE recipients
                        SET active = FALSE
                        WHERE id = %s
                    """, (r["id"],))

            except (ValueError, TypeError) as error:
                print("Erreur de programmation :", error)


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        channel = request.form.get("channel", "")
        mode = request.form.get("mode", "")
        schedule = request.form.get("schedule", "")
        message = request.form.get("message", "").strip()
        consent = request.form.get("consent")

        if not all([name, phone, channel, mode, schedule, message]):
            flash("Remplis tous les champs.")

        elif channel not in ("whatsapp", "sms", "both"):
            flash("Canal invalide.")

        elif mode not in ("daily", "once"):
            flash("Mode invalide.")

        elif not consent:
            flash("Accord du destinataire obligatoire.")

        else:
            try:
                datetime.fromisoformat(schedule)

                with database() as con:
                    con.execute("""
                        INSERT INTO recipients
                        (name, phone, channel, mode, schedule,
                         message, consent)
                        VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                    """, (
                        name, phone, channel, mode,
                        schedule, message
                    ))

                flash("Surprise programmée ! ❤️")
                return redirect(url_for("index"))

            except ValueError:
                flash("Date ou heure invalide.")

    with database() as con:
        recipients = con.execute("""
            SELECT * FROM recipients ORDER BY id DESC
        """).fetchall()

    return render_template("index.html", recipients=recipients)


@app.route("/toggle/<int:recipient_id>", methods=["POST"])
def toggle(recipient_id):
    with database() as con:
        con.execute("""
            UPDATE recipients
            SET active = NOT active,
                last_sent_at = NULL
            WHERE id = %s
        """, (recipient_id,))

    return redirect(url_for("index"))


@app.route("/create-surprise", methods=["POST"])
def create_surprise():
    name = request.form.get("surprise_name", "").strip()
    message = request.form.get("surprise_message", "").strip()

    if not name or not message:
        flash("Prénom et message obligatoires.")
        return redirect(url_for("index"))

    if len(name) > 60 or len(message) > 2000:
        flash("Le prénom ou le message est trop long.")
        return redirect(url_for("index"))

    slug = secrets.token_urlsafe(12)

    with database() as con:
        con.execute("""
            INSERT INTO surprise_links
            (slug, name, message, created_at)
            VALUES (%s, %s, %s, %s)
        """, (
            slug, name, message, datetime.now(TZ).isoformat()
        ))

    link = url_for("show_surprise", slug=slug, _external=True)
    flash("Ton lien surprise est prêt : " + link)

    return redirect(url_for("index"))


@app.route("/surprise/<slug>")
def show_surprise(slug):
    with database() as con:
        surprise = con.execute("""
            SELECT name, message
            FROM surprise_links
            WHERE slug = %s
        """, (slug,)).fetchone()

    if surprise is None:
        return "Cette surprise n'existe pas.", 404

    return render_template("surprise.html", surprise=surprise)


# Initialiser la base au démarrage de l'application
init_db()

# Un seul worker Render est recommandé pour ce planificateur.
scheduler = BackgroundScheduler(timezone=TZ)
scheduler.add_job(
    check_due_messages,
    "interval",
    seconds=20,
    max_instances=1
)
scheduler.start()

print("❤️ JUSTE POUR TOI : moteur activé")


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False,
        use_reloader=False
    )
