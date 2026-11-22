import os
import json
import requests
from http.server import BaseHTTPRequestHandler

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL") or "baidu/ernie-5.5-flash-preview:free"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"

CHANNELS = [
    ("Clan Nox", "clannox"),
    ("Shop Nox", "shoppnox"),
    ("Nox PUBG", "N0xPubg"),
]

SYSTEM_PROMPT = (
    "Ты грубый, саркастичный и дерзкий собеседник. Отвечай коротко, "
    "с издёвкой и жёстким юмором, но без оскорблений по национальности, "
    "религии, здоровью и без угроз. На вопросы всё равно отвечай по делу."
)


def tg(method, **data):
    try:
        return requests.post(f"{TG}/{method}", json=data, timeout=10).json()
    except Exception:
        return {}


def not_subscribed(user_id):
    """Возвращает список каналов, на которые пользователь не подписан."""
    missing = []
    for title, username in CHANNELS:
        r = tg("getChatMember", chat_id=f"@{username}", user_id=user_id)
        status = r.get("result", {}).get("status")
        if status not in ("member", "administrator", "creator"):
            missing.append((title, username))
    return missing


def sub_keyboard():
    rows = [[{"text": f"📢 {t}", "url": f"https://t.me/{u}"}] for t, u in CHANNELS]
    rows.append([{"text": "✅ Проверить подписку", "callback_data": "check"}])
    return {"inline_keyboard": rows}


def menu_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "🛟 Поддержка", "callback_data": "support"}],
            [{"text": "💰 Пополнение", "callback_data": "topup"}],
            [{"text": "🤖 Нейрочат", "callback_data": "ai"}],
        ]
    }


def back_keyboard():
    return {"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "menu"}]]}


def send_sub_request(chat_id):
    tg(
        "sendMessage",
        chat_id=chat_id,
        text="Чтобы пользоваться ботом, подпишись на все каналы 👇",
        reply_markup=sub_keyboard(),
    )


def show_menu(chat_id, message_id=None):
    payload = dict(chat_id=chat_id, text="Главное меню 👇", reply_markup=menu_keyboard())
    if message_id:
        tg("editMessageText", message_id=message_id, **payload)
    else:
        tg("sendMessage", **payload)


def ask_deepseek(text):
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            json={
                "model": OPENROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                "max_tokens": 500,
            },
            timeout=25,
        )
        return r.json()["choices"][0]["message"]["content"]
    except Exception:
        return "Нейронка тупит, попробуй позже."


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "")

    if not_subscribed(user_id):
        return send_sub_request(chat_id)

    if text.startswith("/start") or text.startswith("/menu"):
        return show_menu(chat_id)

    if text:
        tg("sendChatAction", chat_id=chat_id, action="typing")
        tg("sendMessage", chat_id=chat_id, text=ask_deepseek(text))


def handle_callback(cb):
    chat_id = cb["message"]["chat"]["id"]
    message_id = cb["message"]["message_id"]
    user_id = cb["from"]["id"]
    data = cb["data"]

    missing = not_subscribed(user_id)
    if missing:
        tg(
            "answerCallbackQuery",
            callback_query_id=cb["id"],
            text="Ты не подписан на все каналы ❌",
            show_alert=True,
        )
        return

    tg("answerCallbackQuery", callback_query_id=cb["id"])

    if data in ("check", "menu"):
        show_menu(chat_id, message_id)
    elif data == "support":
        tg(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=(
                "Если нужна помощь — обращайтесь и сообщите "
                '<a href="https://t.me/Supp0rtN0x">@Supp0rtN0x</a>'
            ),
            parse_mode="HTML",
            reply_markup=back_keyboard(),
        )
    elif data == "topup":
        tg(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text="💰 Пополнение скоро будет доступно.",
            reply_markup=back_keyboard(),
        )
    elif data == "ai":
        tg(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text="🤖 Нейрочат включён. Просто напиши мне любое сообщение.",
            reply_markup=back_keyboard(),
        )


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if WEBHOOK_SECRET and self.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            self.send_response(403)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        update = json.loads(self.rfile.read(length) or "{}")

        try:
            if "message" in update:
                handle_message(update["message"])
            elif "callback_query" in update:
                handle_callback(update["callback_query"])
        except Exception as e:
            print("error:", e)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"bot is running")
