"""app.py - the web server. Serves the website pages and the /chat endpoint that talks to Gemini."""
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, flash, g, jsonify, redirect, render_template, request, session, url_for
from google import genai
from google.genai import types
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_conn, init_db
from hotels import book_hotel as _book_hotel, search_hotels  # _book_hotel needs a user id; see book_hotel() below

load_dotenv()  # reads GEMINI_API_KEY (and optional GEMINI_MODEL) from the .env file
API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

# One Gemini client for the whole app. If there is no key we still start, and show a clear error in chat.
client = genai.Client(api_key=API_KEY) if API_KEY else None

app = Flask(__name__)
# The secret key signs the login cookie. If someone knew it they could forge a cookie for any user,
# so there is NO default any more. Put a long random SECRET_KEY in .env (see README).
_secret = os.getenv("SECRET_KEY")
if not _secret:
    _secret = os.urandom(32).hex()
    print("WARNING: SECRET_KEY is not set in .env, so everyone is logged out each time the server restarts.")
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,    # JavaScript on the page cannot read the login cookie
    SESSION_COOKIE_SAMESITE="Lax",   # the browser will not send it on cross-site POSTs (blocks CSRF)
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),  # stay logged in for 14 days
)
init_db()  # create the SQLite tables on startup

# Live Gemini "chat" objects (they hold the bot's memory of a conversation), kept in memory for speed:
#   chats[("u", user_id, conversation_id)] -> a logged-in user's chat. Its text is ALSO saved in SQLite,
#       so after a server restart we rebuild the Gemini chat from the saved messages.
#   guest_chats[token] -> a visitor who is not logged in. Not saved to the database; it only lives here.
#       If the visitor then logs in or signs up, claim_guest_chat() moves it into their account.
chats = {}
guest_chats = {}
MAX_MESSAGE = 2000  # longest message we accept and store


def system_prompt():
    """Rules for the AI. Includes today's date so "tomorrow" and "next Friday" work."""
    return f"""You are a friendly travel assistant on a travel website. Today's date is {date.today().isoformat()}.
You can chat about travel in general (destinations, itineraries, weather, food, tips) and you can search
and book hotels using your tools.

Rules:
- To find hotels, you need city, check-in date and check-out date. Ask if any is missing. Assume 2 guests if not told.
- Use search_hotels for hotel questions. Never invent hotels, prices or availability yourself. If search_hotels returns a message and no results, tell the user that message and suggest a nearby bigger city.
- Show results briefly: name, stars, price per night, total price, rooms left. Prices are in INR.
- Before booking, ask for the guest's full name, then repeat hotel, dates and total price and ask for a clear "yes".
  Only call book_hotel after that "yes".
- Booking needs a logged-in user. If book_hotel returns an error starting with NOT_LOGGED_IN, tell the user to log in or sign up using the links at the top of the page, then say "yes" here to finish the booking.
- After booking, show the booking reference and say clearly that this is a DEMO booking, not a real reservation.
- Keep answers short and friendly."""


# ------------------------------------------------------------------ users and login
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def current_user():
    """The logged-in user as a dict (id, name, email), or None. Looked up once per request."""
    if "user" not in g:
        g.user = None
        user_id = session.get("user_id")
        if user_id:
            with get_conn() as conn:
                row = conn.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,)).fetchone()
            g.user = dict(row) if row else None  # None if the account no longer exists
    return g.user


@app.context_processor
def inject_user():
    """Makes `current_user` available in every template (used by the nav bar and the booking form)."""
    return {"current_user": current_user()}


def login_required(view):
    """Put @login_required under @app.route to send logged-out visitors to the login page."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            if request.path.startswith("/api/"):
                return jsonify(error="Please log in."), 401
            flash("Please log in to continue.", "error")
            # After logging in, bring them back to the page they wanted (only possible for GET pages).
            return redirect(url_for("login", next=request.full_path.rstrip("?") if request.method == "GET" else None))
        return view(*args, **kwargs)
    return wrapper


def safe_next(target):
    """Only allow redirects to pages on THIS site. Blocks tricks like ?next=https://evil.com"""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("home")


def log_in(user_id):
    guest_token = session.get("guest_token")   # remember it: session.clear() below removes it
    session.clear()              # drop anything from before, so an old session id can never be reused
    session["user_id"] = user_id
    session.permanent = True
    claim_guest_chat(guest_token, user_id)


def book_hotel(hotel_id: int, guest_name: str, check_in: str, check_out: str, guests: int = 2) -> dict:
    """Book a hotel room (demo booking). Only call after the user confirmed the hotel, dates and guest name.

    Args:
        hotel_id: The hotel_id returned by search_hotels.
        guest_name: Full name of the guest.
        check_in: Check-in date as YYYY-MM-DD.
        check_out: Check-out date as YYYY-MM-DD.
        guests: Number of guests.

    Returns:
        A dict with the booking reference and details, or an error.
    """
    # This is the version Gemini sees. It has NO user_id parameter on purpose: the AI must never choose
    # whose account a booking goes to. We read the logged-in user from the server-side session instead.
    user = current_user()
    if user is None:
        return {"error": "NOT_LOGGED_IN: the user must log in or sign up on the website before a booking can be made."}
    return _book_hotel(user["id"], hotel_id, guest_name, check_in, check_out, guests)


# ------------------------------------------------------------------ saved chats
def _now():
    # Milliseconds, not seconds: two chats touched in the same second must still sort in the right order.
    return datetime.now().isoformat(timespec="milliseconds")


def _title_from(text):
    """A short chat title: the first question, on one line, max 40 characters."""
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= 40 else one_line[:39].rstrip() + "…"


def create_conversation(user_id, messages):
    """Save a new chat for a user. messages is a list of (role, text) pairs. Returns the new chat id."""
    first_user_text = next((text for role, text in messages if role == "user"), "New chat")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (user_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, _title_from(first_user_text), _now(), _now()),
        )
        conv_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            [(conv_id, role, text, _now()) for role, text in messages],
        )
    return conv_id


def save_turn(conv_id, user_text, bot_text):
    """Add one question + answer to an existing saved chat and move it to the top of the history."""
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            [(conv_id, "user", user_text, _now()), (conv_id, "bot", bot_text, _now())],
        )
        conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (_now(), conv_id))


def owns_conversation(user_id, conv_id):
    """True only if this chat belongs to this user (stops people opening each other's chats)."""
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM conversations WHERE id = ? AND user_id = ?", (conv_id, user_id)).fetchone() is not None


def claim_guest_chat(token, user_id):
    """The visitor chatted BEFORE logging in (e.g. the bot said 'log in to book'). Save that chat to the
    new account and keep the same Gemini chat object, so 'yes, book it' still works after logging in."""
    guest = guest_chats.pop(token, None) if token else None
    if guest and guest["messages"]:
        conv_id = create_conversation(user_id, guest["messages"])
        chats[("u", user_id, conv_id)] = guest["chat"]


def new_gemini_chat(history=None):
    """A Gemini chat that knows our system prompt and the two hotel tools, optionally pre-loaded with history."""
    return client.chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt(),
            tools=[search_hotels, book_hotel],
            temperature=0.4,
        ),
        history=history,
    )
               
               

def history_for_gemini(conv_id):
    """Rebuild Gemini's memory from the saved messages (last 30). Used after a server restart.
    Only the text of the conversation is saved, so after a restart the bot may need to search again."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 30", (conv_id,)
        ).fetchall()
    rows = list(reversed(rows))
    while rows and rows[0]["role"] != "user":  # Gemini history must start with a user turn
        rows.pop(0)
    return [
        types.Content(role="user" if r["role"] == "user" else "model", parts=[types.Part(text=r["content"])])
        for r in rows
    ]


DESTINATIONS = [
    ("Goa", "Beaches, shacks and sunsets", "linear-gradient(135deg,#f2b134,#e4572e)"),
    ("Jaipur", "Forts, palaces and bazaars", "linear-gradient(135deg,#d1495b,#edae49)"),
    ("Shimla", "Cool hills and pine forests", "linear-gradient(135deg,#3a7ca5,#81c3d7)"),
    ("Manali", "Snow, rivers and adventure", "linear-gradient(135deg,#2f6690,#16425b)"),
    ("Kerala", "Backwaters and spice hills", "linear-gradient(135deg,#2a9d8f,#264653)"),
    ("Udaipur", "Lakes and royal heritage", "linear-gradient(135deg,#6d597a,#b56576)"),
]


# ------------------------------------------------------------------ website pages
@app.get("/")
def home():
    return render_template("home.html", destinations=DESTINATIONS)


@app.get("/hotels")
def hotels_page():
    """Search form + results. Uses the same search_hotels() the chatbot uses."""
    today = date.today()
    city = request.args.get("city", "").strip()
    check_in = request.args.get("check_in") or (today + timedelta(days=7)).isoformat()
    check_out = request.args.get("check_out") or (today + timedelta(days=10)).isoformat()
    guests = request.args.get("guests", 2, type=int)
    result = search_hotels(city, check_in, check_out, guests) if city else None
    return render_template("hotels.html", city=city, check_in=check_in, check_out=check_out,
                           guests=guests, result=result, today=today.isoformat())


@app.post("/book")
@login_required
def book():
    """Booking form on the hotels page. Uses the same booking code the chatbot uses."""
    f = request.form
    result = _book_hotel(current_user()["id"], int(f["hotel_id"]), f["guest_name"].strip(), f["check_in"], f["check_out"], int(f.get("guests", 2)))
    if "error" in result:
        flash(result["error"], "error")
        return redirect(request.referrer or url_for("hotels_page"))
    flash(f"Booked! Reference {result['booking_ref']} ({result['hotel']}). {result['notice']}", "ok")
    return redirect(url_for("bookings_page"))


@app.get("/bookings")
@login_required
def bookings_page():
    rows = _user_bookings(current_user()["id"])
    # Upcoming (and in-progress) stays: soonest first. Previous stays: most recent first.
    upcoming = sorted((r for r in rows if r["status"] != "past"), key=lambda r: r["check_in"])
    previous = sorted((r for r in rows if r["status"] == "past"), key=lambda r: r["check_in"], reverse=True)
    return render_template("bookings.html", upcoming=upcoming, previous=previous)


# ------------------------------------------------------------------ sign up / log in / log out
@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("home"))
    name = email = ""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        error = None
        if len(name) < 2 or len(name) > 80:
            error = "Please enter your name."
        elif not EMAIL_RE.match(email) or len(email) > 254:
            error = "Please enter a valid email address."
        elif len(password) < 8 or len(password) > 128:
            error = "Password must be 8 to 128 characters."
        if not error:
            try:
                with get_conn() as conn:
                    cur = conn.execute(
                        "INSERT INTO users (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                        (name, email, generate_password_hash(password), datetime.now().isoformat(timespec="seconds")),
                    )
                    new_id = cur.lastrowid
            except sqlite3.IntegrityError:  # the UNIQUE rule on users.email was broken
                error = "An account with that email already exists. Try logging in."
        if error:
            flash(error, "error")
            return render_template("register.html", name=name, email=email), 400
        log_in(new_id)
        flash(f"Welcome, {name}! Your account is ready.", "ok")
        return redirect(safe_next(request.args.get("next")))
    return render_template("register.html", name=name, email=email)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("home"))
    email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        with get_conn() as conn:
            row = conn.execute("SELECT id, name, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        # Same message for "no such email" and "wrong password", so nobody can probe which emails have accounts.
        if row is None or not check_password_hash(row["password_hash"], password):
            flash("Wrong email or password.", "error")
            return render_template("login.html", email=email), 401
        log_in(row["id"])
        flash(f"Welcome back, {row['name']}!", "ok")
        return redirect(safe_next(request.args.get("next")))
    return render_template("login.html", email=email)


@app.post("/logout")  # POST (not a plain link) so another site cannot log you out by embedding an image
def logout():
    session.clear()
    flash("You are logged out.", "ok")
    return redirect(url_for("home"))


# ------------------------------------------------------------------ chatbot API
@app.post("/chat")
def chat():
    if client is None:
        return jsonify(error="GEMINI_API_KEY is missing. Add it to your .env file and restart the server."), 500

    data = request.get_json(force=True, silent=True) or {}
    message = (data.get("message") or "").strip()[:MAX_MESSAGE]
    if not message:
        return jsonify(error="Please type a message."), 400
    try:
        conv_id = int(data["conversation_id"]) if data.get("conversation_id") else None
    except (TypeError, ValueError):
        return jsonify(error="Bad chat id."), 400

    user = current_user()
    if user:
        # Logged in: continue a saved chat (conv_id) or start a new one (conv_id is None).
        if conv_id and not owns_conversation(user["id"], conv_id):
            return jsonify(error="Chat not found."), 404
        gem = chats.get(("u", user["id"], conv_id)) if conv_id else None
        if gem is None:  # a new chat, or a saved one whose live object was lost in a server restart
            gem = new_gemini_chat(history_for_gemini(conv_id) if conv_id else None)
    else:
        # Not logged in: the chat only lives in memory, tied to a random token in the visitor's cookie.
        token = session.setdefault("guest_token", uuid.uuid4().hex)
        if data.get("new_chat"):
            guest_chats.pop(token, None)
        entry = guest_chats.get(token)
        if entry is None:
            entry = guest_chats[token] = {"chat": new_gemini_chat(), "messages": []}
        gem = entry["chat"]

    try:
        response = gem.send_message(message)
        reply = response.text or "Sorry, I could not come up with a reply."
    except Exception as exc:
        text = str(exc)
        if "429" in text or "RESOURCE_EXHAUSTED" in text:
            return jsonify(error="Free Gemini limit reached. Wait a minute and try again."), 429
        if "404" in text or "NOT_FOUND" in text:
            return jsonify(error="Model not found. Set GEMINI_MODEL in .env to a model listed in Google AI Studio."), 500
        if "API key" in text or "API_KEY" in text or "403" in text:
            return jsonify(error="Gemini rejected the API key. Check GEMINI_API_KEY in .env."), 500
        return jsonify(error=f"Something went wrong: {text[:200]}"), 500

    # Only after Gemini answered successfully do we save anything (so a failed turn leaves no half-saved chat).
    if user:
        if conv_id:
            save_turn(conv_id, message, reply)
        else:
            conv_id = create_conversation(user["id"], [("user", message), ("bot", reply)])
        chats[("u", user["id"], conv_id)] = gem
    else:
        entry["messages"] += [("user", message), ("bot", reply)]
    return jsonify(reply=reply, conversation_id=conv_id)


# ------------------------------------------------------------------ saved chats API (used by chat.js)
@app.get("/api/chats")
@login_required
def chat_list():
    """The logged-in user's saved chats, newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, updated_at FROM conversations WHERE user_id = ? ORDER BY updated_at DESC, id DESC LIMIT 50",
            (current_user()["id"],),
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/chats/<int:conv_id>")
@login_required
def chat_get(conv_id):
    if not owns_conversation(current_user()["id"], conv_id):
        return jsonify(error="Chat not found."), 404
    with get_conn() as conn:
        title = conn.execute("SELECT title FROM conversations WHERE id = ?", (conv_id,)).fetchone()["title"]
        rows = conn.execute("SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id", (conv_id,)).fetchall()
    return jsonify(id=conv_id, title=title, messages=[dict(r) for r in rows])


@app.delete("/api/chats/<int:conv_id>")
@login_required
def chat_delete(conv_id):
    user_id = current_user()["id"]
    if not owns_conversation(user_id, conv_id):
        return jsonify(error="Chat not found."), 404
    with get_conn() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    chats.pop(("u", user_id, conv_id), None)
    return jsonify(ok=True)


def _user_bookings(user_id):
    """Only this user's bookings (the WHERE line is what keeps people from seeing each other's).
    Each booking gets a status: "upcoming" (not started), "now" (staying right now) or "past" (already checked out)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT b.ref, h.name AS hotel, b.guest_name, b.check_in, b.check_out, b.guests, b.total_price, b.created_at "
            "FROM bookings b JOIN hotels h ON h.id = b.hotel_id WHERE b.user_id = ? ORDER BY b.id DESC",
            (user_id,),
        ).fetchall()
    today = date.today().isoformat()  # YYYY-MM-DD strings sort in date order, so we can compare them directly
    bookings = []
    for r in rows:
        b = dict(r)
        b["nights"] = (date.fromisoformat(b["check_out"]) - date.fromisoformat(b["check_in"])).days
        b["status"] = "past" if b["check_out"] < today else ("now" if b["check_in"] <= today else "upcoming")
        bookings.append(b)
    return bookings


@app.get("/api/bookings")
@login_required
def bookings():
    return jsonify(_user_bookings(current_user()["id"]))


if __name__ == "__main__":
    app.run(debug=True, port=5000)