import os
import re
import json
import time
import html
import requests
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL") or "baidu/ernie-5.5-flash-preview:free"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# ID администраторов через запятую, например: 123456789,987654321
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}

# База данных Upstash Redis (подключается через Vercel Storage)
def _find_env(suffixes, exclude=("READ_ONLY",)):
    """Ищет переменную по окончанию имени (подходит при любом префиксе Vercel)."""
    for key, value in os.environ.items():
        if value and key.endswith(suffixes) and not any(x in key for x in exclude):
            return value
    return None


REDIS_URL = (
    os.environ.get("UPSTASH_REDIS_REST_URL")
    or os.environ.get("KV_REST_API_URL")
    or _find_env(("REST_API_URL", "REST_URL"))
)
REDIS_TOKEN = (
    os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    or os.environ.get("KV_REST_API_TOKEN")
    or _find_env(("REST_API_TOKEN", "REST_TOKEN"))
)

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"
BOT_ID = int(BOT_TOKEN.split(":")[0])
MAX_CHANNELS = 5
SUPPORT_USERNAME = "Supp0rtN0x"

# Каналы, на которые нужно быть подписанным
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

STATUS_TEXT = {
    "member": "✅ подписан",
    "administrator": "👑 админ",
    "creator": "👑 владелец",
}

FIELD_NAMES = {"name": "название", "price": "цену", "desc": "описание", "photo": "фото"}


# ---------- Вспомогательные функции ----------

def tg(method, **data):
    try:
        return requests.post(f"{TG}/{method}", json=data, timeout=10).json()
    except Exception:
        return {}


def redis(*cmd):
    if not REDIS_URL or not REDIS_TOKEN:
        return None
    try:
        r = requests.post(
            REDIS_URL,
            headers={"Authorization": f"Bearer {REDIS_TOKEN}"},
            json=[str(c) for c in cmd],
            timeout=8,
        )
        return r.json().get("result")
    except Exception:
        return None


def load_json(key, default):
    raw = redis("GET", key)
    try:
        return json.loads(raw) if raw else default
    except Exception:
        return default


def save_json(key, value):
    redis("SET", key, json.dumps(value, ensure_ascii=False))


def is_admin(user_id):
    return user_id in ADMIN_IDS


def get_channels(user_id):
    return load_json(f"ch:{user_id}", [])


def save_channels(user_id, channels):
    save_json(f"ch:{user_id}", channels)


def get_catalog():
    return load_json("catalog", [])


def save_catalog(items):
    save_json("catalog", items)


def get_buttons():
    return load_json("buttons", [])


def save_buttons(items):
    save_json("buttons", items)


def track_user(user_id):
    if REDIS_URL:
        redis("SADD", "users", user_id)


def broadcast(from_chat, mid):
    """Копирует сообщение админа всем пользователям. Возвращает (отправлено, ошибок, пропущено)."""
    users = redis("SMEMBERS", "users") or []
    deadline = time.time() + 50

    def one(uid):
        if time.time() > deadline:
            return None
        for _ in range(2):
            r = tg("copyMessage", chat_id=int(uid), from_chat_id=from_chat, message_id=mid)
            if r.get("ok"):
                return True
            if r.get("error_code") == 429:
                time.sleep(min(r.get("parameters", {}).get("retry_after", 1), 5))
                continue
            if r.get("error_code") in (400, 403):
                redis("SREM", "users", uid)  # пользователь заблокировал бота
            return False
        return False

    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(one, users))
    return results.count(True), results.count(False), results.count(None)


def find_product(pid):
    for p in get_catalog():
        if str(p["id"]) == str(pid):
            return p
    return None


def set_state(user_id, value, ttl=600):
    redis("SET", f"st:{user_id}", value, "EX", ttl)


def get_state(user_id):
    return redis("GET", f"st:{user_id}")


def clear_state(user_id):
    redis("DEL", f"st:{user_id}")
    redis("DEL", f"dr:{user_id}")


def fmt_num(n):
    return f"{n:,}".replace(",", " ") if isinstance(n, int) else "н/д"


def esc(s):
    return html.escape(str(s))


# ---------- Проверка подписки ----------

def not_subscribed(user_id):
    if is_admin(user_id):
        return []
    missing = []
    for title, username in CHANNELS:
        r = tg("getChatMember", chat_id=f"@{username}", user_id=user_id)
        status = r.get("result", {}).get("status")
        if status not in ("member", "administrator", "creator"):
            missing.append((title, username))
    return missing


# ---------- Клавиатуры ----------

def sub_keyboard():
    rows = [[{"text": f"📢 {t}", "url": f"https://t.me/{u}"}] for t, u in CHANNELS]
    rows.append([{"text": "✅ Проверить подписку", "callback_data": "check"}])
    return {"inline_keyboard": rows}


def menu_keyboard(user_id):
    rows = [
        [{"text": "🛍 Каталог", "callback_data": "cat"}],
        [{"text": "📊 Мои каналы", "callback_data": "mych"}],
        [{"text": "👤 Мой аккаунт", "callback_data": "account"}],
        [{"text": "🛟 Поддержка", "callback_data": "support"}],
        [{"text": "💰 Пополнение", "callback_data": "topup"}],
        [{"text": "🤖 Нейрочат", "callback_data": "ai"}],
    ]
    for b in get_buttons():
        if b["kind"] == "url":
            rows.append([{"text": b["name"], "url": b["value"]}])
        else:
            rows.append([{"text": b["name"], "callback_data": f"b:{b['id']}"}])
    if is_admin(user_id):
        rows.append([{"text": "⚙️ Админка", "callback_data": "adm"}])
    return {"inline_keyboard": rows}


def back_keyboard(target="menu", label="⬅️ Назад"):
    return {"inline_keyboard": [[{"text": label, "callback_data": target}]]}


# ---------- Отправка / редактирование ----------

def send(chat_id, text, kb=None, html_mode=False, photo=None):
    if photo:
        payload = dict(chat_id=chat_id, photo=photo, caption=text[:1000])
        if html_mode:
            payload["parse_mode"] = "HTML"
        if kb:
            payload["reply_markup"] = kb
        return tg("sendPhoto", **payload)
    payload = dict(chat_id=chat_id, text=text, disable_web_page_preview=True)
    if html_mode:
        payload["parse_mode"] = "HTML"
    if kb:
        payload["reply_markup"] = kb
    return tg("sendMessage", **payload)


def edit(cbm, text, kb=None, html_mode=False, photo=None):
    """Меняет сообщение с кнопкой. Если нужно фото или старое сообщение с фото,
    удаляет его и отправляет новое."""
    chat_id = cbm["chat"]["id"]
    message_id = cbm["message_id"]
    if photo or "photo" in cbm:
        tg("deleteMessage", chat_id=chat_id, message_id=message_id)
        return send(chat_id, text, kb, html_mode, photo)
    payload = dict(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        disable_web_page_preview=True,
    )
    if html_mode:
        payload["parse_mode"] = "HTML"
    if kb:
        payload["reply_markup"] = kb
    return tg("editMessageText", **payload)


def show_menu(chat_id, user_id, cbm=None):
    text = "Главное меню 👇"
    kb = menu_keyboard(user_id)
    if cbm:
        edit(cbm, text, kb)
    else:
        send(chat_id, text, kb)


# ---------- Каталог ----------

def catalog_screen():
    items = get_catalog()
    if not REDIS_URL:
        return "⚠️ База данных не подключена.", back_keyboard()
    if not items:
        return "🛍 <b>Каталог</b>\n\nПока пусто, загляни позже.", back_keyboard()
    rows = [
        [{"text": f"{p['name']} — {p['price']}"[:60], "callback_data": f"p:{p['id']}"}]
        for p in items
    ]
    rows.append([{"text": "⬅️ Назад", "callback_data": "menu"}])
    return "🛍 <b>Каталог</b>\n\nВыбери товар:", {"inline_keyboard": rows}


def product_screen(p):
    text = f"<b>{esc(p['name'])}</b>\n💰 {esc(p['price'])}"
    if p.get("desc"):
        text += f"\n\n{esc(p['desc'])}"
    kb = {
        "inline_keyboard": [
            [{"text": "💳 Купить", "url": f"https://t.me/{SUPPORT_USERNAME}"}],
            [{"text": "⬅️ К каталогу", "callback_data": "cat"}],
        ]
    }
    return text, kb


# ---------- Админка ----------

def admin_menu():
    kb = {
        "inline_keyboard": [
            [{"text": "➕ Добавить товар", "callback_data": "adm_add"}],
            [{"text": "✏️ Изменить товар", "callback_data": "adm_edit"}],
            [{"text": "🗑 Удалить товар", "callback_data": "adm_del"}],
            [{"text": "📢 Рассылка всем", "callback_data": "adm_bc"}],
            [{"text": "🔘 Кнопки меню", "callback_data": "adm_btn"}],
            [{"text": "📈 Статистика бота", "callback_data": "adm_stats"}],
            [{"text": "⬅️ Назад", "callback_data": "menu"}],
        ]
    }
    return "⚙️ <b>Админка</b>\n\nВыбери действие:", kb


def product_picker(prefix, title):
    items = get_catalog()
    if not items:
        return "Каталог пуст.", back_keyboard("adm")
    rows = [
        [{"text": f"{p['name']} — {p['price']}"[:60], "callback_data": f"{prefix}:{p['id']}"}]
        for p in items
    ]
    rows.append([{"text": "⬅️ Назад", "callback_data": "adm"}])
    return title, {"inline_keyboard": rows}


def admin_state_step(user_id, chat_id, msg, state):
    text = (msg.get("text") or "").strip()
    photo = msg["photo"][-1]["file_id"] if msg.get("photo") else None
    done_kb = {
        "inline_keyboard": [
            [{"text": "⚙️ Админка", "callback_data": "adm"}],
            [{"text": "🛍 Каталог", "callback_data": "cat"}],
        ]
    }
    cancel_kb = back_keyboard("adm", "⬅️ Отмена")

    if state == "bc_wait":
        draft = {"chat": chat_id, "mid": msg["message_id"]}
        save_json(f"dr:{user_id}", draft)
        redis("DEL", f"st:{user_id}")
        total = redis("SCARD", "users") or 0
        tg("copyMessage", chat_id=chat_id, from_chat_id=chat_id, message_id=msg["message_id"])
        return send(
            chat_id,
            f"👆 Так будет выглядеть сообщение.\nОтправить его {total} пользователям?",
            {
                "inline_keyboard": [
                    [{"text": "✅ Отправить всем", "callback_data": "bc_go"}],
                    [{"text": "❌ Отмена", "callback_data": "adm"}],
                ]
            },
        )

    if state == "btn_name":
        if not text:
            return send(chat_id, "Отправь название кнопки текстом.", cancel_kb)
        save_json(f"dr:{user_id}", {"name": text[:40]})
        set_state(user_id, "btn_value", 1800)
        return send(
            chat_id,
            "Теперь отправь:\n• ссылку (https://... или @username), и кнопка её откроет\n"
            "• или любой текст, и кнопка покажет его пользователю.",
            cancel_kb,
        )

    if state == "btn_value":
        if not text:
            return send(chat_id, "Отправь ссылку или текст.", cancel_kb)
        draft = load_json(f"dr:{user_id}", {})
        if re.match(r"^https?://\S+$", text):
            kind, value = "url", text
        elif re.match(r"^t\.me/\S+$", text):
            kind, value = "url", "https://" + text
        elif re.match(r"^@[A-Za-z0-9_]{4,32}$", text):
            kind, value = "url", "https://t.me/" + text[1:]
        else:
            kind, value = "text", text
        bid = redis("INCR", "buttons:next") or int(time.time())
        items = get_buttons()
        items.append({"id": bid, "name": draft.get("name", "Кнопка"), "kind": kind, "value": value})
        save_buttons(items)
        clear_state(user_id)
        return send(chat_id, f"✅ Кнопка «{draft.get('name')}» добавлена в меню.", done_kb)

    if state == "add_name":
        if not text:
            return send(chat_id, "Отправь название текстом.", cancel_kb)
        save_json(f"dr:{user_id}", {"name": text})
        set_state(user_id, "add_price", 1800)
        return send(chat_id, "💰 Отправь цену, например: 500 ₽", cancel_kb)

    if state == "add_price":
        if not text:
            return send(chat_id, "Отправь цену текстом.", cancel_kb)
        draft = load_json(f"dr:{user_id}", {})
        draft["price"] = text
        save_json(f"dr:{user_id}", draft)
        set_state(user_id, "add_desc", 1800)
        return send(chat_id, "📝 Отправь описание товара (или «-», чтобы пропустить).", cancel_kb)

    if state == "add_desc":
        if not text:
            return send(chat_id, "Отправь описание текстом или «-».", cancel_kb)
        draft = load_json(f"dr:{user_id}", {})
        draft["desc"] = "" if text == "-" else text
        save_json(f"dr:{user_id}", draft)
        set_state(user_id, "add_photo", 1800)
        return send(chat_id, "🖼 Отправь фото товара (или «-», чтобы пропустить).", cancel_kb)

    if state == "add_photo":
        if not photo and text != "-":
            return send(chat_id, "Отправь фото или «-».", cancel_kb)
        draft = load_json(f"dr:{user_id}", {})
        pid = redis("INCR", "catalog:next") or int(time.time())
        draft.update({"id": pid, "photo": photo or ""})
        items = get_catalog()
        items.append(draft)
        save_catalog(items)
        clear_state(user_id)
        return send(chat_id, f"✅ Товар «{draft['name']}» добавлен в каталог.", done_kb)

    if state.startswith("edit:"):
        _, pid, field = state.split(":")
        items = get_catalog()
        for p in items:
            if str(p["id"]) == pid:
                if field == "photo":
                    if photo:
                        p["photo"] = photo
                    elif text == "-":
                        p["photo"] = ""
                    else:
                        return send(chat_id, "Отправь фото или «-», чтобы убрать.", cancel_kb)
                else:
                    if not text:
                        return send(chat_id, "Отправь новое значение текстом.", cancel_kb)
                    p[field] = "" if (field == "desc" and text == "-") else text
                save_catalog(items)
                clear_state(user_id)
                return send(chat_id, "✅ Изменено.", done_kb)
        clear_state(user_id)
        return send(chat_id, "Товар не найден.", done_kb)


# ---------- Каналы пользователя ----------

def channels_screen(user_id):
    if not REDIS_URL:
        return "⚠️ База данных не подключена. Напиши в поддержку.", back_keyboard()
    chans = get_channels(user_id)
    keyboard = [[{"text": "➕ Добавить канал", "callback_data": "addch"}]]
    if not chans:
        text = "📊 <b>Мои каналы</b>\n\nУ тебя пока нет добавленных каналов. Нажми «Добавить канал»."
    else:
        lines = ["📊 <b>Мои каналы</b>\n"]
        for c in chans:
            count = tg("getChatMemberCount", chat_id=c["id"]).get("result")
            link = f'<a href="https://t.me/{c["username"]}">{esc(c["title"])}</a>'
            line = f"• {link}\n   👥 Подписчиков: {fmt_num(count)}"
            if isinstance(count, int) and isinstance(c.get("start"), int):
                diff = count - c["start"]
                sign = "+" if diff > 0 else ""
                line += f"\n   📈 С момента добавления: {sign}{diff}"
            lines.append(line)
            keyboard.append(
                [{"text": f"🗑 Удалить {c['title'][:25]}", "callback_data": f"del:{c['id']}"}]
            )
        text = "\n".join(lines)
    keyboard.append([{"text": "⬅️ Назад", "callback_data": "menu"}])
    return text, {"inline_keyboard": keyboard}


def add_channel(user_id, text):
    m = re.search(r"(?:https?://)?(?:t\.me/)?@?([A-Za-z][A-Za-z0-9_]{4,31})\s*$", text.strip())
    if not m:
        return "❌ Не понял. Отправь @username канала, например @mychannel"
    username = m.group(1)
    info = tg("getChat", chat_id=f"@{username}").get("result")
    if not info or info.get("type") != "channel":
        return "❌ Канал не найден. Он должен быть публичным (с @username)."
    chans = get_channels(user_id)
    if any(c["id"] == info["id"] for c in chans):
        return "ℹ️ Этот канал уже добавлен."
    if len(chans) >= MAX_CHANNELS:
        return f"❌ Можно добавить не больше {MAX_CHANNELS} каналов. Удали ненужный."
    bot_member = tg("getChatMember", chat_id=info["id"], user_id=BOT_ID).get("result", {})
    if bot_member.get("status") != "administrator":
        return "❌ Сначала добавь этого бота в администраторы канала, потом отправь @username ещё раз."
    user_member = tg("getChatMember", chat_id=info["id"], user_id=user_id).get("result", {})
    if user_member.get("status") not in ("administrator", "creator"):
        return "❌ Ты не администратор этого канала, добавить его нельзя."
    count = tg("getChatMemberCount", chat_id=info["id"]).get("result")
    chans.append(
        {
            "id": info["id"],
            "title": info.get("title", username),
            "username": info.get("username", username),
            "start": count if isinstance(count, int) else None,
            "added": int(time.time()),
        }
    )
    save_channels(user_id, chans)
    return f"✅ Канал «{info.get('title', username)}» добавлен!"


def account_stats_text(user):
    name = esc(" ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or "—")
    username = f"@{esc(user['username'])}" if user.get("username") else "нет"
    premium = "да ⭐" if user.get("is_premium") else "нет"
    lang = esc(user.get("language_code", "н/д"))
    lines = [
        "👤 <b>Твой аккаунт</b>\n",
        f"Имя: {name}",
        f"Username: {username}",
        f"ID: <code>{user['id']}</code>",
        f"Telegram Premium: {premium}",
        f"Язык: {lang}",
        "\n<b>Подписки:</b>",
    ]
    for title, uname in CHANNELS:
        r = tg("getChatMember", chat_id=f"@{uname}", user_id=user["id"])
        status = r.get("result", {}).get("status")
        lines.append(f"• {esc(title)}: {STATUS_TEXT.get(status, '❌ не подписан')}")
    return "\n".join(lines)


# ---------- Нейрочат ----------

def ask_ai(text):
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


# ---------- Обработчики ----------

def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "")
    track_user(user_id)

    if not_subscribed(user_id):
        return send_sub_request(chat_id)

    if text.startswith("/start") or text.startswith("/menu"):
        clear_state(user_id)
        return show_menu(chat_id, user_id)

    state = get_state(user_id)

    if state == "add_channel" and text:
        answer = add_channel(user_id, text)
        if answer.startswith("✅"):
            clear_state(user_id)
        return send(
            chat_id,
            answer,
            {
                "inline_keyboard": [
                    [{"text": "📊 Мои каналы", "callback_data": "mych"}],
                    [{"text": "⬅️ Назад", "callback_data": "menu"}],
                ]
            },
        )

    if state and is_admin(user_id) and state.startswith(("add_", "edit:", "bc_", "btn_")):
        return admin_state_step(user_id, chat_id, msg, state)

    if not text:
        return

    tg("sendChatAction", chat_id=chat_id, action="typing")
    send(chat_id, ask_ai(text))


def send_sub_request(chat_id):
    send(chat_id, "Чтобы пользоваться ботом, подпишись на все каналы 👇", sub_keyboard())


def handle_callback(cb):
    cbm = cb["message"]
    chat_id = cbm["chat"]["id"]
    user_id = cb["from"]["id"]
    data = cb["data"]
    track_user(user_id)

    if not_subscribed(user_id):
        tg(
            "answerCallbackQuery",
            callback_query_id=cb["id"],
            text="Ты не подписан на все каналы ❌",
            show_alert=True,
        )
        return

    tg("answerCallbackQuery", callback_query_id=cb["id"])

    # --- общее меню ---
    if data in ("check", "menu"):
        clear_state(user_id)
        return show_menu(chat_id, user_id, cbm)

    # --- каталог ---
    if data == "cat":
        text, kb = catalog_screen()
        return edit(cbm, text, kb, html_mode=True)
    if data.startswith("p:"):
        p = find_product(data[2:])
        if not p:
            text, kb = catalog_screen()
            return edit(cbm, "Товар не найден.\n\n" + text, kb, html_mode=True)
        text, kb = product_screen(p)
        return edit(cbm, text, kb, html_mode=True, photo=p.get("photo") or None)

    # --- пользовательские кнопки ---
    if data.startswith("b:"):
        for b in get_buttons():
            if str(b["id"]) == data[2:]:
                return edit(cbm, b["value"], back_keyboard())
        return show_menu(chat_id, user_id, cbm)

    # --- админка ---
    if data.startswith(("adm", "e:", "ef:", "d:", "dy:", "bc_", "bd:")):
        if not is_admin(user_id):
            return
        if data == "adm":
            clear_state(user_id)
            text, kb = admin_menu()
            return edit(cbm, text, kb, html_mode=True)
        if data == "adm_bc":
            set_state(user_id, "bc_wait", 1800)
            return edit(
                cbm,
                "📢 <b>Рассылка</b>\n\nОтправь сообщение, которое получат все пользователи бота. "
                "Подойдёт текст, фото или видео с подписью.",
                back_keyboard("adm", "⬅️ Отмена"),
                html_mode=True,
            )
        if data == "bc_go":
            draft = load_json(f"dr:{user_id}", {})
            if not draft:
                return edit(cbm, "Сообщение не найдено, начни заново.", back_keyboard("adm"))
            edit(cbm, "⏳ Отправляю, подожди...")
            ok, fail, skipped = broadcast(draft["chat"], draft["mid"])
            clear_state(user_id)
            text = f"✅ Рассылка завершена\n\nДоставлено: {ok}\nНе доставлено: {fail}"
            if skipped:
                text += f"\nНе успели отправить: {skipped} (нажми рассылку ещё раз позже)"
            return send(chat_id, text, back_keyboard("adm"))
        if data == "adm_stats":
            users = redis("SCARD", "users") or 0
            text = (
                "📈 <b>Статистика бота</b>\n\n"
                f"👥 Пользователей: {users}\n"
                f"🛍 Товаров в каталоге: {len(get_catalog())}\n"
                f"🔘 Своих кнопок в меню: {len(get_buttons())}"
            )
            return edit(cbm, text, back_keyboard("adm"), html_mode=True)
        if data == "adm_btn":
            items = get_buttons()
            rows = [[{"text": "➕ Добавить кнопку", "callback_data": "adm_btn_add"}]]
            for b in items:
                rows.append([{"text": f"🗑 {b['name']}"[:50], "callback_data": f"bd:{b['id']}"}])
            rows.append([{"text": "⬅️ Назад", "callback_data": "adm"}])
            text = "🔘 Кнопки главного меню. Нажми на кнопку, чтобы удалить её." if items else \
                "🔘 Своих кнопок пока нет."
            return edit(cbm, text, {"inline_keyboard": rows})
        if data == "adm_btn_add":
            set_state(user_id, "btn_name", 1800)
            return edit(cbm, "Отправь название новой кнопки.", back_keyboard("adm", "⬅️ Отмена"))
        if data.startswith("bd:"):
            save_buttons([b for b in get_buttons() if str(b["id"]) != data[3:]])
            return edit(cbm, "✅ Кнопка удалена.", back_keyboard("adm_btn"))
        if data == "adm_add":
            set_state(user_id, "add_name", 1800)
            return edit(
                cbm,
                "➕ <b>Новый товар</b>\n\nОтправь название товара.",
                back_keyboard("adm", "⬅️ Отмена"),
                html_mode=True,
            )
        if data == "adm_edit":
            text, kb = product_picker("e", "✏️ Какой товар изменить?")
            return edit(cbm, text, kb)
        if data == "adm_del":
            text, kb = product_picker("d", "🗑 Какой товар удалить?")
            return edit(cbm, text, kb)
        if data.startswith("e:"):
            p = find_product(data[2:])
            if not p:
                return
            pid = p["id"]
            kb = {
                "inline_keyboard": [
                    [{"text": "Название", "callback_data": f"ef:{pid}:name"}],
                    [{"text": "Цена", "callback_data": f"ef:{pid}:price"}],
                    [{"text": "Описание", "callback_data": f"ef:{pid}:desc"}],
                    [{"text": "Фото", "callback_data": f"ef:{pid}:photo"}],
                    [{"text": "⬅️ Назад", "callback_data": "adm_edit"}],
                ]
            }
            return edit(cbm, f"Что изменить у «{p['name']}»?", kb)
        if data.startswith("ef:"):
            _, pid, field = data.split(":")
            set_state(user_id, f"edit:{pid}:{field}", 1800)
            hint = " (или «-», чтобы убрать)" if field in ("photo", "desc") else ""
            return edit(
                cbm,
                f"Отправь новое значение: {FIELD_NAMES[field]}{hint}.",
                back_keyboard("adm", "⬅️ Отмена"),
            )
        if data.startswith("d:"):
            p = find_product(data[2:])
            if not p:
                return
            kb = {
                "inline_keyboard": [
                    [{"text": "✅ Да, удалить", "callback_data": f"dy:{p['id']}"}],
                    [{"text": "⬅️ Отмена", "callback_data": "adm"}],
                ]
            }
            return edit(cbm, f"Удалить «{p['name']}»?", kb)
        if data.startswith("dy:"):
            items = [p for p in get_catalog() if str(p["id"]) != data[3:]]
            save_catalog(items)
            text, kb = admin_menu()
            return edit(cbm, "✅ Удалено.\n\n" + text, kb, html_mode=True)
        return

    # --- каналы пользователя ---
    if data == "mych":
        clear_state(user_id)
        text, kb = channels_screen(user_id)
        return edit(cbm, text, kb, html_mode=True)
    if data == "addch":
        if not REDIS_URL:
            return edit(cbm, "⚠️ База данных не подключена.", back_keyboard())
        set_state(user_id, "add_channel")
        return edit(
            cbm,
            "➕ <b>Добавление канала</b>\n\n"
            "1. Добавь этого бота в администраторы своего канала.\n"
            "2. Отправь сюда @username канала (канал должен быть публичным).\n\n"
            "Добавить канал может только его администратор.",
            back_keyboard("mych", "⬅️ Отмена"),
            html_mode=True,
        )
    if data.startswith("del:"):
        chans = [c for c in get_channels(user_id) if str(c["id"]) != data[4:]]
        save_channels(user_id, chans)
        text, kb = channels_screen(user_id)
        return edit(cbm, text, kb, html_mode=True)

    # --- остальное ---
    if data == "account":
        return edit(cbm, account_stats_text(cb["from"]), back_keyboard(), html_mode=True)
    if data == "support":
        return edit(
            cbm,
            "Если нужна помощь — обращайтесь и сообщите "
            f'<a href="https://t.me/{SUPPORT_USERNAME}">@{SUPPORT_USERNAME}</a>',
            back_keyboard(),
            html_mode=True,
        )
    if data == "topup":
        return edit(cbm, "💰 Пополнение скоро будет доступно.", back_keyboard())
    if data == "ai":
        return edit(cbm, "🤖 Нейрочат включён. Просто напиши мне любое сообщение.", back_keyboard())


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
