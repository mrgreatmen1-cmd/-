import os
import uuid
from datetime import datetime, timezone
from contextlib import asynccontextmanager

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

# YooKassa оставляем импорт, но использовать будем только когда появятся ключи
from yookassa import Configuration, Payment

from supabase import create_client


# ----------------------------
# ENV
# ----------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Render часто даёт RENDER_EXTERNAL_URL автоматически
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or ""
).strip().rstrip("/")

COURSE_GROUP_CHAT_ID = os.getenv("COURSE_GROUP_CHAT_ID", "").strip()    # e.g. -1001234567890

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

# Политики (Telegraph)
PRIVACY_URL = os.getenv("PRIVACY_URL", "https://ai-sistems-tgcurse.ru/privacy").strip()
DATA_POLICY_URL = os.getenv("DATA_POLICY_URL", "https://ai-sistems-tgcurse.ru/privacy").strip()

# Поддержка
SUPPORT_TEXT_EXTRA = os.getenv("SUPPORT_TEXT_EXTRA", "").strip()

# Приветственная картинка
WELCOME_IMAGE_PATH = os.getenv("WELCOME_IMAGE_PATH", "assets/welcome.png").strip()

# Цена
PRICE_RUB = "1000.00"
CURRENCY = "RUB"

# Флаг: включать оплату только когда есть ключи
PAYMENTS_ENABLED = bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)


def _require(name: str, value: str) -> None:
    if not value:
        raise RuntimeError(f"Missing env var: {name}")


# Минимальные обязательные env для тестового запуска без ЮKassa:
_require("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
_require("PUBLIC_BASE_URL (or RENDER_EXTERNAL_URL)", PUBLIC_BASE_URL)
_require("COURSE_GROUP_CHAT_ID", COURSE_GROUP_CHAT_ID)
_require("SUPABASE_URL", SUPABASE_URL)
_require("SUPABASE_SERVICE_ROLE_KEY", SUPABASE_SERVICE_ROLE_KEY)

# YooKassa конфигурируем только если реально включена
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
# YooKassa (реальные функции — будут использоваться позже)
# ----------------------------
def yk_create_payment(telegram_id: int) -> tuple[str, str]:
    idem_key = str(uuid.uuid4())

    payment_data = {
        "amount": {"value": PRICE_RUB, "currency": CURRENCY},
        "confirmation": {
            "type": "redirect",
            "return_url": "https://ai-sistems-tgcurse.ru/",
        },
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
# Bot texts
# ----------------------------
WELCOME_TEXT = (
    "👋 Привет!\n\n"
    "🚀 *Собери Telegram-бота за вечер своими руками — и запусти его так, чтобы он работал 24/7*\n\n"
    "Для новичков · копи-паст · запуск в облаке.\n"
    "Маршрут: Python и VS Code → BotFather → база в Supabase → GitHub → Render → UptimeRobot (пингер) + внедрение ИИ через GPT API.\n\n"
    "💳 *Цена доступа: 1000₽*\n\n"
    "Курс находится в Telegram-канале. Доступ открывается *после оплаты*."
)

ABOUT_TEXT = (
    "📚 *О курсе*\n\n"
    "Ты получаешь рабочий проект: бот отвечает в Telegram, хранит данные, разворачивается в облаке и не «засыпает».\n\n"
    "🧩 *Программа*\n"
    "• Введение — подготовка без сюрпризов (чек-лист перед стартом)\n"
    "• Урок 1 — аккаунты и инструменты (GitHub, Render, UptimeRobot, Supabase, GPT API) + установка Python/VS Code\n"
    "• Урок 2 — сборка проекта и ИИ (BotFather, таблица в Supabase, структура проекта, внедрение GPT-логики, локальный тест)\n"
    "• Урок 3 — финал: GitHub → Render → UptimeRobot (переменные окружения, запуск 24/7, план «если упало»)\n\n"
    "Доступ выдаётся после оплаты — навсегда."
)

SUPPORT_TEXT = (
    "🆘 *Поддержка*\n\n"
    "Если что-то не получилось — напиши:\n"
    "• Email: ai.sistems59@gmail.com\n"
    "• Телефон: 8 993 197-02-11\n"
)

PAYMENTS_DISABLED_TEXT = (
    "⛔️ *Оплата временно недоступна*\n\n"
    "Сейчас бот запущен в тестовом режиме, ЮKassa ещё не подключена.\n"
    "Доступ к курсу пока не выдаётся.\n\n"
    "Скоро включим оплату — и всё заработает автоматически."
)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💳 Оплатить курс — 1000₽", callback_data="pay")],
            [InlineKeyboardButton("📚 О курсе", callback_data="about")],
            [InlineKeyboardButton("🆘 Поддержка", callback_data="support")],
            [InlineKeyboardButton("🔐 Политики", callback_data="policies")],
        ]
    )


def policies_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Политика конфиденциальности", url=PRIVACY_URL)],
            [InlineKeyboardButton("Политика обработки данных", url=DATA_POLICY_URL)],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back")],
        ]
    )


# ----------------------------
# Telegram handlers
# ----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db_upsert_started(user.id, user.username)

    # 1) Фото + кнопки (короткая подпись, чтобы не упереться в лимит caption)
    try:
        with open(WELCOME_IMAGE_PATH, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption="👋 Добро пожаловать в курс «Telegram-бот за вечер»",
                reply_markup=main_keyboard(),
            )
    except Exception:
        # Если файла нет/путь неверный — просто пропускаем картинку
        pass

    # 2) Полный текст вторым сообщением (без кнопок, чтобы не дублировать)
    await update.message.reply_text(
        WELCOME_TEXT,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def on_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        ABOUT_TEXT,
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
        disable_web_page_preview=True,
    )


async def on_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    text = SUPPORT_TEXT
    if SUPPORT_TEXT_EXTRA:
        text += "\n" + SUPPORT_TEXT_EXTRA
    await update.callback_query.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
        disable_web_page_preview=True,
    )


async def on_policies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "🔐 Политики:",
        reply_markup=policies_keyboard(),
        disable_web_page_preview=True,
    )


async def on_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await update.callback_query.message.reply_text(
        "Меню:",
        reply_markup=main_keyboard(),
        disable_web_page_preview=True,
    )


async def on_pay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    # Заглушка: пока нет ключей — не даём оплату и не создаём платежи
    if not PAYMENTS_ENABLED:
        await q.message.reply_text(
            PAYMENTS_DISABLED_TEXT,
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
            disable_web_page_preview=True,
        )
        return

    # ----- Ниже будет рабочая логика, когда подключишь ЮKassa -----
    telegram_id = q.from_user.id
    user_row = db_get_user(telegram_id)

    if user_row and user_row.get("paid"):
        invite_link = user_row.get("invite_link")
        if invite_link:
            await q.message.reply_text(
                "✅ У тебя уже открыт доступ.\n\n"
                f"Вход в группу с курсом: {invite_link}",
                reply_markup=main_keyboard(),
                disable_web_page_preview=True,
            )
        else:
            await q.message.reply_text(
                "✅ Оплата уже была. Если нужна ссылка — напиши в поддержку.",
                reply_markup=main_keyboard(),
            )
        return

    try:
        payment_id, pay_url = await anyio.to_thread.run_sync(yk_create_payment, telegram_id)
        db_set_last_payment(telegram_id, payment_id)
    except Exception as e:
        await q.message.reply_text(
            f"❌ Не получилось создать платёж.\n\nОшибка: {e}",
            reply_markup=main_keyboard(),
        )
        return

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 Перейти к оплате", url=pay_url)],
            [InlineKeyboardButton("✅ Проверить оплату", callback_data="check")],
            [InlineKeyboardButton("⬅️ Меню", callback_data="back")],
        ]
    )

    await q.message.reply_text(
        "💳 *Оплата курса*\n\n"
        "1) Нажми «Перейти к оплате» и оплати 1000₽.\n"
        "2) Вернись сюда и нажми «Проверить оплату».\n\n"
        "После успешной оплаты я дам ссылку на вход в группу с курсом (доступ навсегда).",
        parse_mode="Markdown",
        reply_markup=kb,
        disable_web_page_preview=True,
    )


async def on_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    # Заглушка: пока нет ключей — никакой проверки и доступа
    if not PAYMENTS_ENABLED:
        await q.message.reply_text(
            PAYMENTS_DISABLED_TEXT,
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
            disable_web_page_preview=True,
        )
        return

    # ----- Ниже будет рабочая логика, когда подключишь ЮKassa -----
    telegram_id = q.from_user.id
    user_row = db_get_user(telegram_id)

    if not user_row or not user_row.get("last_payment_id"):
        await q.message.reply_text(
            "Пока не вижу созданного платежа.\nНажми «Оплатить курс — 1000₽» и создай ссылку на оплату.",
            reply_markup=main_keyboard(),
        )
        return

    if user_row.get("paid"):
        invite_link = user_row.get("invite_link")
        if invite_link:
            await q.message.reply_text(
                "✅ Доступ уже открыт.\n\n"
                f"Вход в группу с курсом: {invite_link}",
                reply_markup=main_keyboard(),
                disable_web_page_preview=True,
            )
        else:
            await q.message.reply_text(
                "✅ Оплата уже была. Если нужна ссылка — напиши в поддержку.",
                reply_markup=main_keyboard(),
            )
        return

    payment_id = user_row["last_payment_id"]

    try:
        status = await anyio.to_thread.run_sync(yk_get_status, payment_id)
    except Exception as e:
        await q.message.reply_text(
            f"❌ Не получилось проверить платёж.\n\nОшибка: {e}",
            reply_markup=main_keyboard(),
        )
        return

    if status == "succeeded":
        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=int(COURSE_GROUP_CHAT_ID),
                member_limit=1,
            )
            invite_link = invite.invite_link
        except Exception as e:
            db_mark_paid(telegram_id, payment_id, invite_link=None)
            await q.message.reply_text(
                "✅ Оплата прошла!\n\n"
                "Но я не смог создать инвайт-ссылку автоматически.\n"
                "Напиши в поддержку — вручную дадим доступ.\n\n"
                f"Ошибка: {e}",
                reply_markup=main_keyboard(),
            )
            return

        db_mark_paid(telegram_id, payment_id, invite_link=invite_link)

        await q.message.reply_text(
            "✅ *Оплата прошла!*\n\n"
            "Вот вход в группу с курсом (доступ навсегда):\n"
            f"{invite_link}\n\n"
            "Если ссылка не открывается — просто напиши в поддержку.",
            parse_mode="Markdown",
            reply_markup=main_keyboard(),
            disable_web_page_preview=True,
        )
        return

    if status in ("pending", "waiting_for_capture"):
        await q.message.reply_text(
            "⏳ Платёж ещё не завершён.\n"
            "Если ты уже оплатил(а), подожди 10–30 секунд и нажми «Проверить оплату» ещё раз.",
            reply_markup=main_keyboard(),
        )
        return

    if status == "canceled":
        await q.message.reply_text(
            "❌ Платёж отменён.\nНажми «Оплатить курс — 1000₽», чтобы создать новую ссылку.",
            reply_markup=main_keyboard(),
        )
        return

    await q.message.reply_text(
        f"Статус платежа: {status}\n"
        "Если уверен(а), что оплатил(а), напиши в поддержку.",
        reply_markup=main_keyboard(),
    )


# ----------------------------
# FastAPI + webhook glue
# ----------------------------
WEBHOOK_PATH = f"/bot/{TELEGRAM_BOT_TOKEN}"
WEBHOOK_URL = f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}"

telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

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

    await telegram_app.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)

    yield

    await telegram_app.bot.delete_webhook(drop_pending_updates=False)
    await telegram_app.stop()
    await telegram_app.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return {"ok": True, "service": "tg-payment-bot", "webhook": WEBHOOK_PATH, "payments_enabled": PAYMENTS_ENABLED}


@app.get("/health")
async def health():
    return {"ok": True}


@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request) -> Response:
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return Response(status_code=200)
