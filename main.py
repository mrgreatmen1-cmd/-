import os
import re
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from html import escape

import anyio
from fastapi import FastAPI, Request, Response

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from yookassa import Configuration, Payment
from supabase import create_client


# ----------------------------
# helpers
# ----------------------------
def e(s: str) -> str:
    """Escape for HTML parse_mode."""
    return escape(s or "", quote=False)


def normalize_url(url: str) -> str:
    """Make URL Telegram-valid. Returns '' if can't be normalized."""
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith(("http://", "https://")):
        return u
    if u.startswith(("telegra.ph/", "www.")):
        return "https://" + u
    if "." in u and " " not in u:
        return "https://" + u
    return ""


def _require(name: str, value: str) -> None:
    if not value:
        raise RuntimeError(f"Missing env var: {name}")


# Таймауты (важно для стабильности)
DB_TIMEOUT_SEC = float(os.getenv("DB_TIMEOUT_SEC", "6.0"))
EDIT_TIMEOUT_SEC = float(os.getenv("EDIT_TIMEOUT_SEC", "6.0"))
YK_TIMEOUT_SEC = float(os.getenv("YK_TIMEOUT_SEC", "12.0"))

# Не обрабатывать апдейты параллельно (важно для стабильности)
MAX_CONCURRENT_UPDATES = int(os.getenv("MAX_CONCURRENT_UPDATES", "1"))

# Админ(ы)
ADMIN_TELEGRAM_ID_RAW = os.getenv("ADMIN_TELEGRAM_ID", "").strip()  # "123" or "123,456"
ADMIN_IDS: set[int] = set()
if ADMIN_TELEGRAM_ID_RAW:
    for part in ADMIN_TELEGRAM_ID_RAW.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ADMIN_IDS.add(int(part))


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def safe_thread_call(fn, *args, default=None, timeout_sec: float = DB_TIMEOUT_SEC):
    """
    Вызов синхронной функции в отдельном потоке + таймаут.
    AnyIO v4: fail_after is a context manager.
    """
    try:
        with anyio.fail_after(timeout_sec):
            return await anyio.to_thread.run_sync(fn, *args)
    except TimeoutError:
        print(f"[safe_thread_call] {fn.__name__} timeout after {timeout_sec}s")
        return default
    except Exception as ex:
        print(f"[safe_thread_call] {fn.__name__} error:", repr(ex))
        return default


async def safe_answer(q):
    """Всегда пытаемся быстро закрыть 'loading' у кнопки."""
    try:
        await q.answer()
    except Exception as ex:
        print("[callback answer] error:", repr(ex))


# ----------------------------
# ENV
# ----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or ""
).strip().rstrip("/")

COURSE_GROUP_CHAT_ID = os.getenv("COURSE_GROUP_CHAT_ID", "").strip()

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

PRIVACY_URL = os.getenv("PRIVACY_URL", "https://ai-sistems-tgcurse.ru/privacy").strip()
DATA_POLICY_URL = os.getenv("DATA_POLICY_URL", "https://ai-sistems-tgcurse.ru/privacy").strip()

SUPPORT_TEXT_EXTRA = os.getenv("SUPPORT_TEXT_EXTRA", "").strip()

WELCOME_IMAGE_PATH = os.getenv("WELCOME_IMAGE_PATH", "assets/welcome.png").strip()
OFFERTA_FILE_PATH = os.getenv("OFFERTA_FILE_PATH", "assets/offerta.pdf").strip()

PRICE_RUB = "1000.00"
CURRENCY = "RUB"

PAYMENTS_ENABLED = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)

# ✅ Секретный путь вебхука
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

_require("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
_require("PUBLIC_BASE_URL (or RENDER_EXTERNAL_URL)", PUBLIC_BASE_URL)
_require("COURSE_GROUP_CHAT_ID", COURSE_GROUP_CHAT_ID)
_require("SUPABASE_URL", SUPABASE_URL)
_require("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_SERVICE_ROLE_KEY)
_require("WEBHOOK_SECRET", WEBHOOK_SECRET)

if PAYMENTS_ENABLED:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY


# ----------------------------
# Supabase
# ----------------------------
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def db_upsert_started(telegram_id: int, username: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    payload = {"telegram_id": telegram_id, "username": username, "started_at": now}
    supabase.table("tg_users").upsert(payload, on_conflict="telegram_id").execute()


def db_set_customer_email(telegram_id: int, email: str) -> None:
    supabase.table("tg_users").upsert(
        {"telegram_id": telegram_id, "customer_email": email},
        on_conflict="telegram_id",
    ).execute()


def db_set_last_payment(telegram_id: int, payment_id: str) -> None:
    supabase.table("tg_users").upsert(
        {"telegram_id": telegram_id, "last_payment_id": payment_id},
        on_conflict="telegram_id",
    ).execute()


def db_mark_paid(telegram_id: int, payment_id: str, invite_link: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "telegram_id": telegram_id,
        "paid": True,
        "paid_at": now,
        "last_payment_id": payment_id,
    }
    if invite_link:
        payload["invite_link"] = invite_link
    supabase.table("tg_users").upsert(payload, on_conflict="telegram_id").execute()


def db_get_user(telegram_id: int) -> dict | None:
    res = (
        supabase.table("tg_users")
        .select("*")
        .eq("telegram_id", telegram_id)
        .limit(1)
        .execute()
    )
    data = res.data or []
    return data[0] if data else None


def db_list_paid_user_ids() -> list[int]:
    res = supabase.table("tg_users").select("telegram_id").eq("paid", True).execute()
    rows = res.data or []
    out: list[int] = []
    for r in rows:
        try:
            out.append(int(r.get("telegram_id")))
        except Exception:
            pass
    return out


def db_list_unpaid_user_ids() -> list[int]:
    # unpaid = paid is NULL or paid = false
    try:
        res = (
            supabase.table("tg_users")
            .select("telegram_id")
            .or_("paid.is.null,paid.eq.false")
            .execute()
        )
    except Exception:
        # fallback
        res = supabase.table("tg_users").select("telegram_id").execute()

    rows = res.data or []
    out: list[int] = []
    for r in rows:
        try:
            out.append(int(r.get("telegram_id")))
        except Exception:
            pass
    return out


# ----------------------------
# YooKassa (with receipt)
# ----------------------------
def yk_create_payment(telegram_id: int, customer_email: str) -> tuple[str, str]:
    idem_key = str(uuid.uuid4())
    payment_data = {
        "amount": {"value": PRICE_RUB, "currency": CURRENCY},
        "confirmation": {"type": "redirect", "return_url": "https://ai-sistems-tgcurse.ru/"},
        "capture": True,
        "description": "Доступ к курсу «Telegram-бот за вечер»",
        "metadata": {"telegram_id": str(telegram_id)},

        # ✅ Чеки от ЮKassa (54-ФЗ)
        "receipt": {
            "customer": {"email": customer_email},
            "tax_system_code": 2,  # ✅ УСН доходы
            "items": [
                {
                    "description": "Доступ к курсу «Telegram-бот за вечер»",
                    "quantity": "1.00",
                    "amount": {"value": PRICE_RUB, "currency": CURRENCY},
                    "vat_code": 1,  # ✅ без НДС
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }
            ],
        },
    }

    payment = Payment.create(payment_data, idem_key)
    payment_id = getattr(payment, "id", None) or payment.get("id")
    confirmation = getattr(payment, "confirmation", None) or payment.get("confirmation")

    confirmation_url = None
    if hasattr(confirmation, "confirmation_url"):
        confirmation_url = confirmation.confirmation_url
    elif isinstance(confirmation, dict):
        confirmation_url = confirmation.get("confirmation_url")

    if not payment_id or not confirmation_url:
        raise RuntimeError("YooKassa: failed to create payment / no confirmation_url")

    return payment_id, confirmation_url


def yk_get_status(payment_id: str) -> str:
    payment = Payment.find_one(payment_id)
    status = getattr(payment, "status", None) or payment.get("status")
    return str(status or "").lower().strip()


# ----------------------------
# Texts (HTML)
# ----------------------------
WELCOME_CAPTION = (
    "👋 Привет! Добро пожаловать в курс <b>«Telegram-бот за вечер»</b>.\n\n"
    "🚀 Соберёшь бота с нуля и запустишь 24/7.\n"
    "Python → BotFather → Supabase → GitHub → Render → UptimeRobot + GPT.\n\n"
    "💳 Цена: <b>1000₽</b> (доступ навсегда после оплаты)."
)

ABOUT_CAPTION = (
    "📚 <b>О курсе</b>\n\n"
    "Курс из 4 видео: введение + 3 урока.\n"
    "Собираем бота, подключаем базу, деплоим в облако и (опционально) добавляем ИИ.\n\n"
    "🔎 Подробности — на сайте."
)

SUPPORT_CAPTION = (
    "🆘 <b>Поддержка</b>\n\n"
    "• Telegram: <b>@ai_sistems</b>\n"
    "• Email: <b>ai.sistems59@gmail.com</b>"
)

PAYMENTS_DISABLED_CAPTION = (
    "⛔️ <b>Оплата временно недоступна</b>\n\n"
    "Сейчас бот запущен в тестовом режиме — ЮKassa ещё не подключена.\n"
    "Доступ к курсу пока не выдаётся.\n\n"
    "Скоро включим оплату — и всё заработает автоматически."
)

POLICIES_CAPTION = "🔐 <b>Политики</b>"

NEED_EMAIL_CAPTION = (
    "📧 <b>Нужен email для чека</b>\n\n"
    "Отправь, пожалуйста, свой email одним сообщением (пример: name@gmail.com)."
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ----------------------------
# Keyboards
# ----------------------------
def main_keyboard(is_admin_user: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("💳 Оплатить курс — 1000₽", callback_data="pay")],
        [InlineKeyboardButton("📚 О курсе", callback_data="about")],
        [InlineKeyboardButton("🆘 Поддержка", callback_data="support")],
        [InlineKeyboardButton("🔐 Политики", callback_data="policies")],
        [InlineKeyboardButton("📄 Оферта", callback_data="offer")],
    ]
    if is_admin_user:
        rows.append([InlineKeyboardButton("📣 Рассылка (админ)", callback_data="admin_broadcast")])
    return InlineKeyboardMarkup(rows)


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back")]])


def about_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Подробнее на сайте", url="https://ai-sistems-tgcurse.ru/")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
        ]
    )


def support_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back")]])


def policies_keyboard() -> InlineKeyboardMarkup:
    p1 = normalize_url(PRIVACY_URL)
    p2 = normalize_url(DATA_POLICY_URL)
    rows = []
    if p1:
        rows.append([InlineKeyboardButton("Политика конфиденциальности", url=p1)])
    if p2:
        rows.append([InlineKeyboardButton("Политика обработки данных", url=p2)])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(rows)


def pay_keyboard_disabled() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back")]])


def pay_keyboard_enabled(pay_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 Перейти к оплате", url=pay_url)],
            [InlineKeyboardButton("✅ Проверить оплату", callback_data="check")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
        ]
    )


def check_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Проверить оплату", callback_data="check")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
        ]
    )


def admin_broadcast_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Оплатили", callback_data="broadcast_paid")],
            [InlineKeyboardButton("❌ Не оплатили", callback_data="broadcast_unpaid")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
        ]
    )


def admin_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="broadcast_cancel")]])


# ----------------------------
# UI helper (caption OR text)
# ----------------------------
async def edit_main_message(q, text: str, keyboard: InlineKeyboardMarkup):
    msg = q.message

    # 1) Try edit caption if this message has caption (photo)
    try:
        if getattr(msg, "caption", None) is not None:
            with anyio.fail_after(EDIT_TIMEOUT_SEC):
                await msg.edit_caption(caption=text, parse_mode="HTML", reply_markup=keyboard)
            return
    except Exception as ex:
        print("[edit_caption html] error:", repr(ex))

    # 2) Try edit text if it's a text message
    try:
        if getattr(msg, "text", None) is not None:
            with anyio.fail_after(EDIT_TIMEOUT_SEC):
                await msg.edit_text(
                    text=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            return
    except Exception as ex:
        print("[edit_text html] error:", repr(ex))

    # 3) Fallback without HTML
    try:
        if getattr(msg, "caption", None) is not None:
            with anyio.fail_after(EDIT_TIMEOUT_SEC):
                await msg.edit_caption(caption=e(text), reply_markup=keyboard)
            return
    except Exception as ex:
        print("[edit_caption plain] error:", repr(ex))

    try:
        if getattr(msg, "text", None) is not None:
            with anyio.fail_after(EDIT_TIMEOUT_SEC):
                await msg.edit_text(text=e(text), reply_markup=keyboard, disable_web_page_preview=True)
            return
    except Exception as ex:
        print("[edit_text plain] error:", repr(ex))

    # 4) Last resort — just change keyboard
    try:
        with anyio.fail_after(EDIT_TIMEOUT_SEC):
            await msg.edit_reply_markup(reply_markup=keyboard)
    except Exception as ex:
        print("[edit_reply_markup] error:", repr(ex))


# ----------------------------
# Handlers
# ----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await safe_thread_call(db_upsert_started, user.id, user.username)

    kb = main_keyboard(is_admin_user=is_admin(user.id))
    try:
        with open(WELCOME_IMAGE_PATH, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=WELCOME_CAPTION,
                parse_mode="HTML",
                reply_markup=kb,
            )
    except Exception as ex:
        print("Welcome image error:", repr(ex))
        await update.message.reply_text(
            WELCOME_CAPTION,
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )


async def on_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await safe_answer(q)
    await edit_main_message(q, ABOUT_CAPTION, about_keyboard())


async def on_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await safe_answer(q)
    caption = SUPPORT_CAPTION
    if SUPPORT_TEXT_EXTRA:
        caption += "\n\n" + e(SUPPORT_TEXT_EXTRA)
    await edit_main_message(q, caption, support_keyboard())


async def on_policies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await safe_answer(q)
    await edit_main_message(q, POLICIES_CAPTION, policies_keyboard())


async def on_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await safe_answer(q)

    try:
        with open(OFFERTA_FILE_PATH, "rb") as f:
            with anyio.fail_after(EDIT_TIMEOUT_SEC):
                await context.bot.send_document(
                    chat_id=q.message.chat_id,
                    document=f,
                    filename=os.path.basename(OFFERTA_FILE_PATH),
                    caption="📄 Публичная оферта (PDF)",
                )
        await edit_main_message(q, "📄 Оферта отправлена файлом ниже.", back_keyboard())
    except Exception as ex:
        print("[offer send] error:", repr(ex))
        await edit_main_message(
            q,
            "❌ Не смог отправить оферту.\nПроверь, что файл есть в репозитории и путь OFFERTA_FILE_PATH верный.",
            back_keyboard(),
        )


async def on_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await safe_answer(q)
    await edit_main_message(q, WELCOME_CAPTION, main_keyboard(is_admin_user=is_admin(q.from_user.id)))


async def on_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await safe_answer(q)

    if not PAYMENTS_ENABLED:
        await edit_main_message(q, PAYMENTS_DISABLED_CAPTION, pay_keyboard_disabled())
        return

    telegram_id = q.from_user.id
    user_row = await safe_thread_call(db_get_user, telegram_id, default=None)

    if user_row and user_row.get("paid"):
        invite_link = user_row.get("invite_link") or ""
        caption = "✅ <b>У тебя уже открыт доступ.</b>"
        if invite_link:
            caption += f"\n\nВход в группу с курсом:\n{e(invite_link)}"
        else:
            caption += "\n\nЕсли нужна ссылка — напиши в поддержку."
        await edit_main_message(q, caption, back_keyboard())
        return

    customer_email = (user_row or {}).get("customer_email") if user_row else None
    if not customer_email:
        context.user_data["awaiting_email_for_payment"] = True
        await edit_main_message(q, NEED_EMAIL_CAPTION, back_keyboard())
        return

    try:
        with anyio.fail_after(YK_TIMEOUT_SEC):
            payment_id, pay_url = await anyio.to_thread.run_sync(yk_create_payment, telegram_id, customer_email)
        await safe_thread_call(db_set_last_payment, telegram_id, payment_id)
    except Exception as ex:
        await edit_main_message(q, f"❌ Не получилось создать платёж.\n\n{e(str(ex))}", back_keyboard())
        return

    caption = (
        "💳 <b>Оплата курса</b>\n\n"
        "1) Нажми «Перейти к оплате» и оплати 1000₽.\n"
        "2) Вернись сюда и нажми «Проверить оплату».\n\n"
        "После успешной оплаты я дам ссылку на вход в группу (доступ навсегда)."
    )
    await edit_main_message(q, caption, pay_keyboard_enabled(pay_url))


async def on_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await safe_answer(q)

    if not PAYMENTS_ENABLED:
        await edit_main_message(q, PAYMENTS_DISABLED_CAPTION, pay_keyboard_disabled())
        return

    telegram_id = q.from_user.id
    user_row = await safe_thread_call(db_get_user, telegram_id, default=None)

    if not user_row or not user_row.get("last_payment_id"):
        await edit_main_message(
            q,
            "Пока не вижу созданного платежа.\nНажми «Оплатить курс — 1000₽» и создай ссылку на оплату.",
            back_keyboard(),
        )
        return

    if user_row.get("paid"):
        invite_link = user_row.get("invite_link") or ""
        caption = "✅ <b>Доступ уже открыт.</b>"
        if invite_link:
            caption += f"\n\nВход в группу:\n{e(invite_link)}"
        else:
            caption += "\n\nЕсли нужна ссылка — напиши в поддержку."
        await edit_main_message(q, caption, back_keyboard())
        return

    payment_id = user_row["last_payment_id"]

    try:
        with anyio.fail_after(YK_TIMEOUT_SEC):
            status = await anyio.to_thread.run_sync(yk_get_status, payment_id)
    except Exception as ex:
        await edit_main_message(q, f"❌ Не получилось проверить платёж.\n\n{e(str(ex))}", check_keyboard())
        return

    if status == "succeeded":
        try:
            with anyio.fail_after(EDIT_TIMEOUT_SEC):
                invite = await context.bot.create_chat_invite_link(
                    chat_id=int(COURSE_GROUP_CHAT_ID),
                    member_limit=1,
                )
            invite_link = invite.invite_link
        except Exception as ex:
            await safe_thread_call(db_mark_paid, telegram_id, payment_id, None)
            await edit_main_message(
                q,
                "✅ Оплата прошла!\n\n"
                "Но я не смог создать инвайт-ссылку автоматически.\n"
                "Напиши в поддержку — вручную дадим доступ.\n\n"
                f"{e(str(ex))}",
                back_keyboard(),
            )
            return

        await safe_thread_call(db_mark_paid, telegram_id, payment_id, invite_link)

        await edit_main_message(
            q,
            "✅ <b>Оплата прошла!</b>\n\n"
            "Вот вход в группу с курсом (доступ навсегда):\n"
            f"{e(invite_link)}",
            main_keyboard(is_admin_user=is_admin(telegram_id)),
        )
        return

    if status in ("pending", "waiting_for_capture"):
        await edit_main_message(
            q,
            "⏳ Платёж ещё не завершён.\n"
            "Если ты уже оплатил(а), подожди 10–30 секунд и нажми «Проверить оплату» ещё раз.",
            check_keyboard(),
        )
        return

    if status == "canceled":
        await edit_main_message(
            q,
            "❌ Платёж отменён.\nНажми «Оплатить курс — 1000₽», чтобы создать новую ссылку.",
            main_keyboard(is_admin_user=is_admin(telegram_id)),
        )
        return

    await edit_main_message(
        q,
        f"Статус платежа: {e(status)}\nЕсли уверен(а), что оплатил(а), напиши в поддержку.",
        back_keyboard(),
    )


# --- email capture (after bot asks for email) ---
async def on_text_for_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("awaiting_email_for_payment"):
        return

    email = (update.message.text or "").strip()
    if not EMAIL_RE.match(email):
        await update.message.reply_text("❌ Это не похоже на email. Пришли в формате name@example.com")
        return

    context.user_data["awaiting_email_for_payment"] = False

    telegram_id = update.effective_user.id
    await safe_thread_call(db_set_customer_email, telegram_id, email)

    if not PAYMENTS_ENABLED:
        await update.message.reply_text(PAYMENTS_DISABLED_CAPTION, parse_mode="HTML")
        return

    try:
        with anyio.fail_after(YK_TIMEOUT_SEC):
            payment_id, pay_url = await anyio.to_thread.run_sync(yk_create_payment, telegram_id, email)
        await safe_thread_call(db_set_last_payment, telegram_id, payment_id)
    except Exception as ex:
        await update.message.reply_text(f"❌ Не получилось создать платёж.\n\n{e(str(ex))}")
        return

    caption = (
        "💳 <b>Оплата курса</b>\n\n"
        "1) Нажми «Перейти к оплате» и оплати 1000₽.\n"
        "2) Вернись сюда и нажми «Проверить оплату».\n\n"
        "После успешной оплаты я дам ссылку на вход в группу (доступ навсегда)."
    )
    await update.message.reply_text(caption, parse_mode="HTML", reply_markup=pay_keyboard_enabled(pay_url))


# ----------------------------
# Admin broadcast flow
# ----------------------------
BCAST_CHOOSE_AUDIENCE, BCAST_ENTER_TEXT, BCAST_CONFIRM = range(3)


async def on_admin_broadcast_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await safe_answer(q)

    if not is_admin(q.from_user.id):
        await edit_main_message(q, "⛔️ Нет доступа.", back_keyboard())
        return ConversationHandler.END

    await edit_main_message(q, "📣 <b>Рассылка</b>\n\nКому отправляем?", admin_broadcast_keyboard())
    return BCAST_CHOOSE_AUDIENCE


async def on_broadcast_choose_paid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await safe_answer(q)
    if not is_admin(q.from_user.id):
        return ConversationHandler.END

    context.user_data["bcast_paid"] = True
    await edit_main_message(q, "✍️ Пришли текст рассылки одним сообщением.", admin_cancel_keyboard())
    return BCAST_ENTER_TEXT


async def on_broadcast_choose_unpaid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await safe_answer(q)
    if not is_admin(q.from_user.id):
        return ConversationHandler.END

    context.user_data["bcast_paid"] = False
    await edit_main_message(q, "✍️ Пришли текст рассылки одним сообщением.", admin_cancel_keyboard())
    return BCAST_ENTER_TEXT


async def on_broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await safe_answer(q)

    context.user_data.pop("bcast_paid", None)
    context.user_data.pop("bcast_text", None)

    await edit_main_message(q, WELCOME_CAPTION, main_keyboard(is_admin_user=is_admin(q.from_user.id)))
    return ConversationHandler.END


async def on_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not is_admin(update.effective_user.id):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Пришли текст одним сообщением.")
        return BCAST_ENTER_TEXT

    context.user_data["bcast_text"] = text
    paid = bool(context.user_data.get("bcast_paid", False))
    audience_name = "✅ оплатившим" if paid else "❌ не оплатившим"

    preview = (
        f"📣 <b>Подтверждение рассылки</b>\n\n"
        f"Кому: <b>{audience_name}</b>\n\n"
        f"Текст:\n\n{text}\n\n"
        f"Отправить?"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Отправить", callback_data="broadcast_send")],
            [InlineKeyboardButton("❌ Отмена", callback_data="broadcast_cancel")],
        ]
    )
    await update.message.reply_text(preview, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    return BCAST_CONFIRM


async def on_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await safe_answer(q)
    if not is_admin(q.from_user.id):
        return ConversationHandler.END

    paid = bool(context.user_data.get("bcast_paid", False))
    text = context.user_data.get("bcast_text", "")
    if not text:
        await edit_main_message(q, "Текст рассылки не найден. Начни заново.", back_keyboard())
        return ConversationHandler.END

    user_ids = await safe_thread_call(db_list_paid_user_ids if paid else db_list_unpaid_user_ids, default=[])
    total = len(user_ids)

    await edit_main_message(q, f"⏳ Отправляю... получателей: <b>{total}</b>", back_keyboard())

    sent = 0
    failed = 0

    for uid in user_ids:
        try:
            with anyio.fail_after(10):
                await context.bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            sent += 1
        except Exception:
            failed += 1
        await anyio.sleep(0.05)

    summary = (
        "✅ <b>Рассылка завершена</b>\n\n"
        f"Получателей: <b>{total}</b>\n"
        f"Отправлено: <b>{sent}</b>\n"
        f"Ошибок: <b>{failed}</b>"
    )
    await edit_main_message(q, summary, main_keyboard(is_admin_user=True))

    context.user_data.pop("bcast_paid", None)
    context.user_data.pop("bcast_text", None)
    return ConversationHandler.END


# ----------------------------
# FastAPI + webhook glue
# ----------------------------
WEBHOOK_PATH = f"/bot/webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}"

telegram_app = (
    Application.builder()
    .token(TELEGRAM_BOT_TOKEN)
    .concurrent_updates(MAX_CONCURRENT_UPDATES)
    .build()
)

telegram_app.add_handler(CommandHandler("start", cmd_start))
telegram_app.add_handler(CallbackQueryHandler(on_pay, pattern="^pay$"))
telegram_app.add_handler(CallbackQueryHandler(on_check, pattern="^check$"))
telegram_app.add_handler(CallbackQueryHandler(on_about, pattern="^about$"))
telegram_app.add_handler(CallbackQueryHandler(on_support, pattern="^support$"))
telegram_app.add_handler(CallbackQueryHandler(on_policies, pattern="^policies$"))
telegram_app.add_handler(CallbackQueryHandler(on_offer, pattern="^offer$"))
telegram_app.add_handler(CallbackQueryHandler(on_back, pattern="^(back)$"))

# Admin broadcast conversation (group 0)
broadcast_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(on_admin_broadcast_menu, pattern="^admin_broadcast$")],
    states={
        BCAST_CHOOSE_AUDIENCE: [
            CallbackQueryHandler(on_broadcast_choose_paid, pattern="^broadcast_paid$"),
            CallbackQueryHandler(on_broadcast_choose_unpaid, pattern="^broadcast_unpaid$"),
            CallbackQueryHandler(on_broadcast_cancel, pattern="^broadcast_cancel$"),
        ],
        BCAST_ENTER_TEXT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_broadcast_text),
            CallbackQueryHandler(on_broadcast_cancel, pattern="^broadcast_cancel$"),
        ],
        BCAST_CONFIRM: [
            CallbackQueryHandler(on_broadcast_send, pattern="^broadcast_send$"),
            CallbackQueryHandler(on_broadcast_cancel, pattern="^broadcast_cancel$"),
        ],
    },
    fallbacks=[CallbackQueryHandler(on_broadcast_cancel, pattern="^broadcast_cancel$")],
    per_user=True,
    per_chat=True,
)
telegram_app.add_handler(broadcast_conv, group=0)

# Email capture (group 1) — чтобы не мешать рассылке
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_for_email), group=1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()

    # ✅ self-heal webhook
    try:
        info = await telegram_app.bot.get_webhook_info()
        if (not info.url) or (info.url != WEBHOOK_URL):
            await telegram_app.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
        else:
            await telegram_app.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=False)
    except Exception as ex:
        print("[webhook setup] error:", repr(ex))

    yield

    # ВАЖНО: НЕ delete_webhook() на Render — иначе после рестартов Telegram может "дергаться"
    try:
        await telegram_app.stop()
        await telegram_app.shutdown()
    except Exception as ex:
        print("[app stop/shutdown] error:", repr(ex))


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return {"ok": True, "service": "tg-payment-bot", "payments_enabled": PAYMENTS_ENABLED}


@app.get("/health")
async def health():
    return {"ok": True}


@app.head("/")
async def root_head():
    return Response(status_code=200)


@app.head("/health")
async def health_head():
    return Response(status_code=200)


@app.get("/debug/webhook")
async def debug_webhook():
    info = await telegram_app.bot.get_webhook_info()
    return {
        "expected": WEBHOOK_URL,
        "current_url": info.url,
        "pending_update_count": info.pending_update_count,
        "last_error_date": info.last_error_date,
        "last_error_message": info.last_error_message,
    }


@app.get("/debug/reset-webhook")
async def debug_reset_webhook():
    await telegram_app.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
    return {"ok": True, "set_to": WEBHOOK_URL}


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Response:
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return Response(status_code=200)

