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


def _find_env(suffixes, exclude=("READ_ONLY",)):
    """Ищет переменную по окончанию имени (подходит при любом префиксе Vercel)."""
    for key, value in os.environ.items():
        if value and key.endswith(suffixes) and not any(x in key for x in exclude):
            return value
    return None


# База данных Upstash Redis (подключается через Vercel Storage)
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

# Тексты кнопок возле клавиатуры
BTN_CH = "📊 Мои каналы"
BTN_REF = "🎁 Рефералы и награды"
BTN_ACC = "👤 Мой аккаунт"
BTN_SUP = "🛟 Поддержка"
BTN_TOP = "💰 Пополнение"
BTN_AI = "🤖 Нейрочат"
BTN_ADM = "⚙️ Админка"
BUILTIN_BUTTONS = {BTN_CH, BTN_REF, BTN_ACC, BTN_SUP, BTN_TOP, BTN_AI, BTN_ADM}

# (сколько рефералов нужно, награда)
REWARDS = [
    (10, "Фул 6 с МК ВК"),
    (50, "Золотой фул 6 с МК ВК"),
    (100, "Сопровождение на 8 карту"),
]


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


def get_buttons():
    return load_json("buttons", [])


def save_buttons(items):
    save_json("buttons", items)


def track_user(user_id):
    """Запоминает пользователя. Возвращает True, если он зашёл в бота впервые."""
    if REDIS_URL:
        return redis("SADD", "users", user_id) == 1
    return False


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
        if not r.get("ok") and "chat not found" in str(r.get("description", "")).lower():
            continue  # канал удалён: не блокируем людей из-за него
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
    """Постоянная клавиатура с кнопками возле поля ввода."""
    labels = [BTN_CH, BTN_REF, BTN_ACC, BTN_SUP, BTN_TOP, BTN_AI]
    labels += [b["name"] for b in get_buttons()]
    rows = [[{"text": t} for t in labels[i:i + 2]] for i in range(0, len(labels), 2)]
    if is_admin(user_id):
        rows.append([{"text": BTN_ADM}])
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Выбери кнопку или напиши вопрос",
    }


def back_keyboard(target="menu", label="⬅️ Назад"):
    return {"inline_keyboard": [[{"text": label, "callback_data": target}]]}


# ---------- Отправка / редактирование ----------

def send(chat_id, text, kb=None, html_mode=False):
    payload = dict(chat_id=chat_id, text=text, disable_web_page_preview=True)
    if html_mode:
        payload["parse_mode"] = "HTML"
    if kb:
        payload["reply_markup"] = kb
    return tg("sendMessage", **payload)


def edit(cbm, text, kb=None, html_mode=False):
    payload = dict(
        chat_id=cbm["chat"]["id"],
        message_id=cbm["message_id"],
        text=text,
        disable_web_page_preview=True,
    )
    if html_mode:
        payload["parse_mode"] = "HTML"
    if kb:
        payload["reply_markup"] = kb
    return tg("editMessageText", **payload)


def show_menu(chat_id, user_id):
    send(chat_id, "Главное меню 👇", menu_keyboard(user_id))


def send_sub_request(chat_id):
    send(chat_id, "Чтобы пользоваться ботом, подпишись на все каналы 👇", sub_keyboard())


# ---------- Админка ----------

def admin_menu():
    kb = {
        "inline_keyboard": [
            [{"text": "📢 Рассылка всем", "callback_data": "adm_bc"}],
            [{"text": "🔘 Кнопки меню", "callback_data": "adm_btn"}],
            [{"text": "📈 Статистика бота", "callback_data": "adm_stats"}],
            [{"text": "⬅️ Назад", "callback_data": "menu"}],
        ]
    }
    return "⚙️ <b>Админка</b>\n\nВыбери действие:", kb


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


def admin_state_step(user_id, chat_id, msg, state):
    text = (msg.get("text") or "").strip()
    done_kb = {"inline_keyboard": [[{"text": "⚙️ Админка", "callback_data": "adm"}]]}
    cancel_kb = back_keyboard("adm", "⬅️ Отмена")

    if state == "bc_wait":
        save_json(f"dr:{user_id}", {"chat": chat_id, "mid": msg["message_id"]})
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
        if text in BUILTIN_BUTTONS:
            return send(chat_id, "Такое название уже занято, придумай другое.", cancel_kb)
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
        send(chat_id, f"✅ Кнопка «{draft.get('name')}» добавлена.", done_kb)
        return show_menu(chat_id, user_id)


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


# ---------- Рефералы ----------

_bot_username = None


def bot_username():
    global _bot_username
    if not _bot_username:
        _bot_username = os.environ.get("BOT_USERNAME") or tg("getMe").get("result", {}).get("username")
    return _bot_username


def notify_admins(text):
    for admin in ADMIN_IDS:
        send(admin, text, html_mode=True)


def user_label(u):
    name = esc(" ".join(filter(None, [u.get("first_name"), u.get("last_name")])) or "—")
    uname = f"@{esc(u['username'])}" if u.get("username") else "без username"
    return f"{name} ({uname}), ID <code>{u['id']}</code>"


def ref_count(uid):
    return redis("SCARD", f"refs:{uid}") or 0


def claimed_set(uid):
    return {int(x) for x in (redis("SMEMBERS", f"claimed:{uid}") or [])}


def capture_ref(user_id, text):
    """Запоминает, кто пригласил нового пользователя (до проверки подписки)."""
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].startswith("ref_"):
        return
    try:
        ref = int(parts[1][4:])
    except ValueError:
        return
    if ref != user_id:
        redis("SET", f"refp:{user_id}", ref, "EX", 2592000)


def finalize_referral(user):
    """Засчитывает реферала, когда человек прошёл подписку на каналы."""
    if not REDIS_URL:
        return
    uid = user["id"]
    ref = redis("GET", f"refp:{uid}")
    if not ref:
        return
    redis("DEL", f"refp:{uid}")
    ref = int(ref)
    if redis("GET", f"ref_by:{uid}"):
        return
    redis("SET", f"ref_by:{uid}", ref)
    redis("SADD", f"refs:{ref}", uid)
    count = ref_count(ref)
    ref_chat = tg("getChat", chat_id=ref).get("result") or {"id": ref}
    ref_chat["id"] = ref

    notify_admins(
        "✅ <b>Реферал выполнил действия</b> (подписался на все каналы)\n\n"
        f"Новый: {user_label(user)}\n"
        f"Пригласил: {user_label(ref_chat)}\n"
        f"Всего рефералов у пригласившего: <b>{count}</b>"
    )
    send(ref, f"👥 По твоей ссылке пришёл новый человек! Всего рефералов: {count}")
    for need, reward in REWARDS:
        if count == need:
            send(
                ref,
                f"🎉 У тебя {need} рефералов!\nНаграда: {reward}\n\nХочешь забрать награду?",
                {
                    "inline_keyboard": [
                        [{"text": "🎁 Забрать награду", "callback_data": f"claim:{need}"}],
                        [{"text": "Позже", "callback_data": "ref"}],
                    ]
                },
            )


def referral_screen(uid):
    if not REDIS_URL:
        return "⚠️ База данных не подключена.", back_keyboard()
    count = ref_count(uid)
    done = claimed_set(uid)
    link = f"https://t.me/{bot_username()}?start=ref_{uid}"
    lines = [
        "🎁 <b>Реферальная программа</b>\n",
        "Приглашай друзей по своей ссылке. Человек засчитывается, когда подпишется на все каналы.\n",
        f"🔗 Твоя ссылка:\n<code>{link}</code>\n",
        f"👥 Приглашено: <b>{count}</b>\n",
        "<b>Награды:</b>",
    ]
    kb = [[{"text": "📤 Поделиться ссылкой", "url": "https://t.me/share/url?url=" + link}]]
    for need, reward in REWARDS:
        if need in done:
            status = "✅ получено"
        elif count >= need:
            status = "🎁 можно забрать"
            kb.append([{"text": f"🎁 Забрать: {reward}"[:60], "callback_data": f"claim:{need}"}])
        else:
            status = f"🔒 ещё {need - count}"
        lines.append(f"• {need} чел. — {esc(reward)}: {status}")
    return "\n".join(lines), {"inline_keyboard": kb}


def process_claim(user, chat_id, state, text):
    uid = user["id"]
    try:
        need = int(state.split(":")[1])
    except (IndexError, ValueError):
        need = 0
    reward = dict(REWARDS).get(need)
    game_id = text.strip()
    if not reward or ref_count(uid) < need or need in claimed_set(uid):
        clear_state(uid)
        return send(chat_id, "Эта награда недоступна.")
    if len(game_id) > 40:
        return send(chat_id, "Слишком длинно. Отправь только свой игровой ID.")
    redis("SADD", f"claimed:{uid}", need)
    clear_state(uid)
    notify_admins(
        "🎁 <b>Заявка на награду</b>\n\n"
        f"Человек: {user_label(user)}\n"
        f"Решил забрать награду за <b>{need}</b> рефералов\n"
        f"Награда: {esc(reward)}\n"
        f"Игровой ID: <code>{esc(game_id)}</code>"
    )
    send(chat_id, f"✅ Заявка отправлена! Админ свяжется с тобой.\nНаграда: {reward}\nИгровой ID: {game_id}")


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

def handle_menu_button(user, chat_id, text):
    """Нажатие кнопки возле клавиатуры. Возвращает True, если текст был кнопкой."""
    uid = user["id"]
    custom = {b["name"]: b for b in get_buttons()}
    if text not in BUILTIN_BUTTONS and text not in custom:
        return False
    if text == BTN_ADM and not is_admin(uid):
        return False

    clear_state(uid)
    if text == BTN_CH:
        t, kb = channels_screen(uid)
        send(chat_id, t, kb, html_mode=True)
    elif text == BTN_REF:
        t, kb = referral_screen(uid)
        send(chat_id, t, kb, html_mode=True)
    elif text == BTN_ACC:
        send(chat_id, account_stats_text(user), html_mode=True)
    elif text == BTN_SUP:
        send(
            chat_id,
            "Если нужна помощь — обращайтесь и сообщите "
            f'<a href="https://t.me/{SUPPORT_USERNAME}">@{SUPPORT_USERNAME}</a>',
            html_mode=True,
        )
    elif text == BTN_TOP:
        send(chat_id, "💰 Пополнение скоро будет доступно.")
    elif text == BTN_AI:
        send(chat_id, "🤖 Нейрочат включён. Просто напиши мне любое сообщение.")
    elif text == BTN_ADM:
        t, kb = admin_menu()
        send(chat_id, t, kb, html_mode=True)
    else:
        b = custom[text]
        if b["kind"] == "url":
            send(chat_id, f"Открыть: {b['name']}", {"inline_keyboard": [[{"text": b["name"], "url": b["value"]}]]})
        else:
            send(chat_id, b["value"])
    return True


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user = msg["from"]
    user_id = user["id"]
    text = msg.get("text", "")
    is_new = track_user(user_id)
    if text.startswith("/start") and is_new:
        capture_ref(user_id, text)

    if not_subscribed(user_id):
        return send_sub_request(chat_id)
    finalize_referral(user)

    if text.startswith("/start") or text.startswith("/menu"):
        clear_state(user_id)
        return show_menu(chat_id, user_id)

    if text and handle_menu_button(user, chat_id, text):
        return

    state = get_state(user_id)

    if state == "add_channel" and text:
        answer = add_channel(user_id, text)
        if answer.startswith("✅"):
            clear_state(user_id)
        return send(
            chat_id,
            answer,
            {"inline_keyboard": [[{"text": "📊 Мои каналы", "callback_data": "mych"}]]},
        )

    if state and state.startswith("claim:") and text:
        return process_claim(user, chat_id, state, text)

    if state and is_admin(user_id) and state.startswith(("bc_", "btn_")):
        return admin_state_step(user_id, chat_id, msg, state)

    if not text:
        return

    tg("sendChatAction", chat_id=chat_id, action="typing")
    send(chat_id, ask_ai(text))


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
    finalize_referral(cb["from"])

    # --- общее меню ---
    if data in ("check", "menu"):
        clear_state(user_id)
        tg("deleteMessage", chat_id=chat_id, message_id=cbm["message_id"])
        return show_menu(chat_id, user_id)

    # --- рефералы ---
    if data == "ref":
        clear_state(user_id)
        text, kb = referral_screen(user_id)
        return edit(cbm, text, kb, html_mode=True)
    if data.startswith("claim:"):
        try:
            need = int(data[6:])
        except ValueError:
            return
        reward = dict(REWARDS).get(need)
        if not reward or ref_count(user_id) < need or need in claimed_set(user_id):
            return edit(cbm, "Эта награда недоступна.")
        set_state(user_id, f"claim:{need}", 1800)
        return edit(
            cbm,
            f"🎁 Награда: {reward}\n\nНапиши свой игровой ID одним сообщением.",
            back_keyboard("ref", "⬅️ Отмена"),
        )

    # --- админка ---
    if data.startswith(("adm", "bc_", "bd:")):
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
                f"🔘 Своих кнопок в меню: {len(get_buttons())}"
            )
            return edit(cbm, text, back_keyboard("adm"), html_mode=True)
        if data == "adm_btn":
            items = get_buttons()
            rows = [[{"text": "➕ Добавить кнопку", "callback_data": "adm_btn_add"}]]
            for b in items:
                rows.append([{"text": f"🗑 {b['name']}"[:50], "callback_data": f"bd:{b['id']}"}])
            rows.append([{"text": "⬅️ Назад", "callback_data": "adm"}])
            text = (
                "🔘 Кнопки возле клавиатуры. Нажми на кнопку, чтобы удалить её."
                if items
                else "🔘 Своих кнопок пока нет."
            )
            return edit(cbm, text, {"inline_keyboard": rows})
        if data == "adm_btn_add":
            set_state(user_id, "btn_name", 1800)
            return edit(cbm, "Отправь название новой кнопки.", back_keyboard("adm", "⬅️ Отмена"))
        if data.startswith("bd:"):
            save_buttons([b for b in get_buttons() if str(b["id"]) != data[3:]])
            send(chat_id, "✅ Кнопка удалена.", back_keyboard("adm_btn"))
            return show_menu(chat_id, user_id)
        return

    # --- каналы пользователя ---
    if data == "mych":
        clear_state(user_id)
        text, kb = channels_screen(user_id)
        return edit(cbm, text, kb, html_mode=True)
    if data == "addch":
        if not REDIS_URL:
            return edit(cbm, "⚠️ База данных не подключена.")
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

    # Старые кнопки из прошлых версий: просто показываем меню
    tg("deleteMessage", chat_id=chat_id, message_id=cbm["message_id"])
    show_menu(chat_id, user_id)


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
