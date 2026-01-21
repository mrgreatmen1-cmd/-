import os
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
DB_TIMEOUT_SEC = float(os.getenv("DB_TIMEOUT_SEC", "4.0"))
EDIT_TIMEOUT_SEC = float(os.getenv("EDIT_TIMEOUT_SEC", "4.0"))
YK_TIMEOUT_SEC = float(os.getenv("YK_TIMEOUT_SEC", "8.0"))


async def safe_thread_call(fn, *args, default=None, timeout_sec: float = DB_TIMEOUT_SEC):
    """
    Вызов синхронной функции в отдельном потоке + таймаут.
    Если Supabase/сеть зависнет — бот НЕ повиснет.
    """
    try:
        return await anyio.fail_after(timeout_sec, anyio.to_thread.run_sync, fn, *args)
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

PRICE_RUB = "1000.00"
CURRENCY = "RUB"

PAYMENTS_ENABLED = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)

# Важно для стабильности: не обрабатывать апдейты параллельно
MAX_CONCURRENT_UPDATES = int(os.getenv("MAX_CONCURRENT_UPDATES", "1"))

# ✅ Секретный путь вебхука (вместо токена в URL)
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
    payload = {
        "telegram_id": telegram_id,
        "username": username,
        "started_at": now,
    }
    supabase.table("tg_users").upsert(payload, on_conflict="telegram_id").execute()


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


# ----------------------------
# YooKassa
# ----------------------------
def yk_create_payment(telegram_id: int) -> tuple[str, str]:
    idem_key = str(uuid.uuid4())
    payment_data = {
        "amount": {"value": PRICE_RUB, "currency": CURRENCY},
        "confirmation": {"type": "redirect", "return_url": "https://ai-sistems-tgcurse.ru/"},
        "capture": True,
        "description": "Доступ к курсу «Telegram-бот за вечер»",
        "metadata": {"telegram_id": str(telegram_id)},
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
    "• Email: <b>ai.sistems59@gmail.com</b>\n"
    "• Телефон: <b>8 993 197-02-11</b>"
)

PAYMENTS_DISABLED_CAPTION = (
    "⛔️ <b>Оплата временно недоступна</b>\n\n"
    "Сейчас бот запущен в тестовом режиме — ЮKassa ещё не подключена.\n"
    "Доступ к курсу пока не выдаётся.\n\n"
    "Скоро включим оплату — и всё заработает автоматически."
)

POLICIES_CAPTION = "🔐 <b>Политики</b>"


# ----------------------------
# Keyboards
# ----------------------------
def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Оплатить курс — 1000₽", callback_data="pay")],
            [InlineKeyboardButton("📚 О курсе", callback_data="about")],
            [InlineKeyboardButton("🆘 Поддержка", callback_data="support")],
            [InlineKeyboardButton("🔐 Политики", callback_data="policies")],
        ]
    )


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


# ----------------------------
# UI helper
# ----------------------------
async def edit_main_message(q, caption: str, keyboard: InlineKeyboardMarkup):
    # редактирование тоже иногда может подвиснуть на стороне Telegram — ставим таймаут
    try:
        await anyio.fail_after(
            EDIT_TIMEOUT_SEC,
            q.message.edit_caption,
            caption,
            "HTML",
            reply_markup=keyboard,
        )
        return
    except Exception as ex:
        print("[edit_caption html] error:", repr(ex))

    try:
        await anyio.fail_after(
            EDIT_TIMEOUT_SEC,
            q.message.edit_caption,
            e(caption),
            None,
            reply_markup=keyboard,
        )
        return
    except Exception as ex:
        print("[edit_caption plain] error:", repr(ex))

    try:
        await anyio.fail_after(EDIT_TIMEOUT_SEC, q.message.edit_reply_markup, reply_markup=keyboard)
    except Exception as ex:
        print("[edit_reply_markup] error:", repr(ex))


# ----------------------------
# Handlers
# ----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await safe_thread_call(db_upsert_started, user.id, user.username)

    try:
        with open(WELCOME_IMAGE_PATH, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=WELCOME_CAPTION,
                parse_mode="HTML",
                reply_markup=main_keyboard(),
            )
    except Exception as ex:
        print("Welcome image error:", repr(ex))
        await update.message.reply_text(
            WELCOME_CAPTION,
            parse_mode="HTML",
            reply_markup=main_keyboard(),
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


async def on_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await safe_answer(q)
    await edit_main_message(q, WELCOME_CAPTION, main_keyboard())


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

    # YooKassa create может зависнуть — ставим таймаут
    try:
        payment_id, pay_url = await anyio.fail_after(
            YK_TIMEOUT_SEC,
            anyio.to_thread.run_sync,
            yk_create_payment,
            telegram_id,
        )
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
        status = await anyio.fail_after(
            YK_TIMEOUT_SEC,
            anyio.to_thread.run_sync,
            yk_get_status,
            payment_id,
        )
    except Exception as ex:
        await edit_main_message(q, f"❌ Не получилось проверить платёж.\n\n{e(str(ex))}", check_keyboard())
        return

    if status == "succeeded":
        try:
            invite = await anyio.fail_after(
                EDIT_TIMEOUT_SEC,
                context.bot.create_chat_invite_link,
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
            main_keyboard(),
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
            main_keyboard(),
        )
        return

    await edit_main_message(
        q,
        f"Статус платежа: {e(status)}\nЕсли уверен(а), что оплатил(а), напиши в поддержку.",
        back_keyboard(),
    )


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
telegram_app.add_handler(CallbackQueryHandler(on_back, pattern="^(back)$"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.start()

    # ✅ self-heal webhook: если Telegram вдруг сбросил/изменил url — восстановим
    try:
        info = await telegram_app.bot.get_webhook_info()
        if (not info.url) or (info.url != WEBHOOK_URL):
            await telegram_app.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
        else:
            await telegram_app.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=False)
    except Exception as ex:
        print("[webhook setup] error:", repr(ex))

    yield

    try:
        await telegram_app.bot.delete_webhook(drop_pending_updates=False)
    except Exception as ex:
        print("[webhook delete] error:", repr(ex))

    await telegram_app.stop()
    await telegram_app.shutdown()


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
