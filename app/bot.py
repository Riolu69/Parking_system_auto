import os
import logging
from datetime import datetime
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, FSInputFile
)
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import async_session, User, Tariff, Order, RtuSlot
from app.services import (
    register_user_and_car, create_order, validate_promo_code,
    get_app_settings, mark_order_paid_by_user, confirm_order, reject_order,
    queue_open_gate, get_finance_stats
)

bot = Bot(token=settings.BOT_TOKEN)
dp = Dispatcher()
logger = logging.getLogger("uvicorn")


class RegState(StatesGroup):
    waiting_plate = State()
    waiting_model = State()
    waiting_promo = State()


def kb_contact():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def kb_main():
    rows = [
        [KeyboardButton(text="Мой статус"), KeyboardButton(text="Выбрать тариф / Оплатить")],
        [KeyboardButton(text="Ввести промокод"), KeyboardButton(text="Добавить машину")],
    ]
    if settings.WEBAPP_URL.startswith("https://"):
        rows.insert(0, [KeyboardButton(text="Открыть личный кабинет", web_app=WebAppInfo(url=settings.WEBAPP_URL))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def kb_tariffs(discount: int = 0) -> InlineKeyboardMarkup:
    async with async_session() as session:
        res = await session.execute(select(Tariff).where(Tariff.is_active == True).order_by(Tariff.id))
        buttons = []
        for t in res.scalars().all():
            price = max(0, round(t.price * (100 - discount) / 100))
            label = f"{t.name} — {price} ₽" if not discount else f"{t.name} — {price} ₽ (-{discount}%)"
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"tariff:{t.id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def notify_admins_text(text: str):
    for aid in settings.admin_ids:
        try:
            await bot.send_message(aid, text)
        except Exception as e:
            logger.error(f"Ошибка отправки админу {aid}: {e}")


async def send_db_backup_to_admins():
    db_path = "parking.db"
    if not os.path.exists(db_path):
        return
    caption = f"Автобэкап БД парковки ({datetime.now().strftime('%d.%m.%Y %H:%M')})"
    for aid in settings.admin_ids:
        try:
            await bot.send_document(aid, FSInputFile(db_path), caption=caption)
        except Exception as e:
            logger.error(f"Ошибка отправки бэкапа админу {aid}: {e}")


async def broadcast_to_users(text: str) -> int:
    async with async_session() as session:
        res = await session.execute(select(User.telegram_id).where(User.telegram_id != None))
        tg_ids = [r[0] for r in res.all()]
    sent = 0
    for tid in tg_ids:
        try:
            await bot.send_message(tid, f"Объявление парковки:\n\n{text}")
            sent += 1
        except Exception:
            pass
    return sent


async def notify_admins_order(order: Order):
    plates = ", ".join([v.plate_number for v in order.user.vehicles]) or "—"
    promo = f"\nПромокод: {order.promo_code} (-{order.discount_percent}%)" if order.promo_code else ""
    text = (
        f"Новая оплата · Заявка #{order.id}\n"
        f"Телефон: +{order.user.phone}\n"
        f"Авто: {plates}\n"
        f"Тариф: {order.tariff.name}{promo}\n"
        f"Сумма: {order.amount} ₽"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Подтвердить", callback_data=f"adm_ok:{order.id}"),
        InlineKeyboardButton(text="Отклонить", callback_data=f"adm_rej:{order.id}"),
    ]])
    for aid in settings.admin_ids:
        try:
            await bot.send_message(aid, text, reply_markup=kb)
        except Exception as e:
            logger.error(f"Ошибка уведомления админа {aid}: {e}")


async def notify_user_activated(telegram_id: int | None, slot_number: int, valid_until: datetime | None):
    if not telegram_id:
        return
    until = valid_until.strftime("%d.%m.%Y %H:%M") if valid_until else "—"
    try:
        await bot.send_message(
            telegram_id,
            f"Оплата подтверждена!\nВаш номер добавлен в шлагбаум (ячейка #{slot_number}).\nДоступ действует до: {until}",
        )
    except Exception:
        pass


async def notify_user_expiring(telegram_id: int, valid_until: datetime):
    until = valid_until.strftime("%d.%m.%Y %H:%M")
    try:
        await bot.send_message(
            telegram_id,
            f"Напоминание: срок вашей парковки истекает {until}.\nНажмите «Выбрать тариф / Оплатить», чтобы продлить доступ.",
            reply_markup=kb_main(),
        )
    except Exception:
        pass


@dp.message(CommandStart())
async def cmd_start(message: Message):
    async with async_session() as session:
        res = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = res.scalar_one_or_none()

    if user:
        await message.answer(f"С возвращением, {user.full_name or user.phone}!", reply_markup=kb_main())
    else:
        await message.answer(
            "Добро пожаловать на парковку!\nОтправьте номер телефона для доступа к шлагбауму.",
            reply_markup=kb_contact(),
        )


@dp.message(F.contact)
async def handle_contact(message: Message, state: FSMContext):
    async with async_session() as session:
        user = await register_user_and_car(
            session,
            phone=message.contact.phone_number,
            full_name=message.from_user.full_name,
            telegram_id=message.from_user.id,
        )
    if user.vehicles:
        await message.answer("Номер привязан! Выберите тариф:", reply_markup=await kb_tariffs())
    else:
        await state.update_data(phone=user.phone)
        await state.set_state(RegState.waiting_plate)
        await message.answer("Номер сохранён. Введите госномер машины (например, А123ВС777):")


@dp.message(F.text == "Добавить машину")
async def add_car_start(message: Message, state: FSMContext):
    async with async_session() as session:
        res = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = res.scalar_one_or_none()
    if not user:
        await message.answer("Сначала отправьте номер телефона.", reply_markup=kb_contact())
        return
    await state.update_data(phone=user.phone)
    await state.set_state(RegState.waiting_plate)
    await message.answer("Введите госномер машины:")


@dp.message(RegState.waiting_plate)
async def process_plate(message: Message, state: FSMContext):
    await state.update_data(plate=message.text.strip())
    await state.set_state(RegState.waiting_model)
    await message.answer("Введите марку/модель авто (или «-» чтобы пропустить):")


@dp.message(RegState.waiting_model)
async def process_model(message: Message, state: FSMContext):
    data = await state.get_data()
    model = None if message.text.strip() == "-" else message.text.strip()
    async with async_session() as session:
        await register_user_and_car(
            session,
            phone=data["phone"],
            plate_number=data["plate"],
            car_model=model,
            telegram_id=message.from_user.id,
        )
    await state.set_state(None)
    discount = data.get("promo_discount", 0)
    await message.answer("Машина добавлена!", reply_markup=kb_main())
    await message.answer("Выберите тариф для доступа к шлагбауму:", reply_markup=await kb_tariffs(discount))


@dp.message(F.text == "Ввести промокод")
async def promo_start(message: Message, state: FSMContext):
    await state.set_state(RegState.waiting_promo)
    await message.answer("Введите промокод:")


@dp.message(RegState.waiting_promo)
async def process_promo(message: Message, state: FSMContext):
    code = message.text.strip()
    async with async_session() as session:
        try:
            promo = await validate_promo_code(session, code)
        except ValueError as e:
            await state.update_data(promo_code=None, promo_discount=0)
            await state.set_state(None)
            await message.answer(
                f"{e}. Промокод сброшен — вы можете выбрать тариф по обычной цене:",
                reply_markup=await kb_tariffs(0),
            )
            return

    await state.update_data(promo_code=promo.code, promo_discount=promo.discount_percent)
    await state.set_state(None)
    await message.answer(
        f"Промокод {promo.code} применён! Скидка {promo.discount_percent}%.\nВыберите тариф:",
        reply_markup=await kb_tariffs(promo.discount_percent),
    )


@dp.message(F.text == "Выбрать тариф / Оплатить")
async def choose_tariff(message: Message, state: FSMContext):
    data = await state.get_data()
    discount = data.get("promo_discount", 0)
    await message.answer("Выберите тариф:", reply_markup=await kb_tariffs(discount))


@dp.callback_query(F.data.startswith("tariff:"))
async def on_tariff_click(callback: CallbackQuery, state: FSMContext):
    tariff_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    promo_code = data.get("promo_code")

    async with async_session() as session:
        res = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = res.scalar_one_or_none()
        if not user:
            await callback.answer("Сначала зарегистрируйтесь", show_alert=True)
            return
        order = await create_order(session, user.id, tariff_id, promo_code)
        tariff = await session.get(Tariff, tariff_id)
        cfg = await get_app_settings(session)

    promo_line = f"\nПромокод: {order.promo_code} (-{order.discount_percent}%)" if order.promo_code else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Я оплатил", callback_data=f"user_paid:{order.id}")
    ]])
    await callback.message.answer(
        f"Заявка #{order.id}\n"
        f"Тариф: {tariff.name}{promo_line}\n"
        f"К оплате: {order.amount} ₽\n\n"
        f"Перевод по СБП:\n"
        f"Номер: {cfg['sbp_phone']}\n"
        f"Банк: {cfg['sbp_bank']}\n"
        f"Получатель: {cfg['sbp_recipient']}\n\n"
        f"После перевода нажмите кнопку «Я оплатил» ниже.",
        reply_markup=kb,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("user_paid:"))
async def on_user_paid(callback: CallbackQuery):
    order_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        try:
            order = await mark_order_paid_by_user(session, order_id)
        except ValueError as e:
            await callback.answer(str(e), show_alert=True)
            return
    await notify_admins_order(order)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"Заявка #{order_id} отправлена администратору. Ожидайте подтверждения!")
    await callback.answer()


@dp.callback_query(F.data.startswith("adm_ok:"))
async def on_admin_confirm(callback: CallbackQuery):
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        try:
            slot, user = await confirm_order(session, order_id)
        except ValueError as e:
            await callback.answer(str(e), show_alert=True)
            return
    await notify_user_activated(user.telegram_id, slot.slot_number, slot.valid_until)
    await callback.message.edit_text(callback.message.text + f"\n\nПодтверждено (ячейка #{slot.slot_number})")
    await callback.answer("Подтверждено")


@dp.callback_query(F.data.startswith("adm_rej:"))
async def on_admin_reject(callback: CallbackQuery):
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Нет доступа", show_alert=True)
        return
    order_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        try:
            order = await reject_order(session, order_id)
        except ValueError as e:
            await callback.answer(str(e), show_alert=True)
            return
    if order.user and order.user.telegram_id:
        try:
            await bot.send_message(order.user.telegram_id, f"Заявка #{order_id} отклонена администратором.")
        except Exception:
            pass
    await callback.message.edit_text(callback.message.text + "\n\nОтклонено")
    await callback.answer("Отклонено")


@dp.message(F.text == "Мой статус")
async def my_status(message: Message):
    async with async_session() as session:
        res = await session.execute(
            select(User)
            .options(selectinload(User.vehicles), selectinload(User.slot))
            .where(User.telegram_id == message.from_user.id)
        )
        user = res.scalar_one_or_none()
        cfg = await get_app_settings(session)
    if not user:
        await message.answer("Вы не зарегистрированы.", reply_markup=kb_contact())
        return
    cars = "\n".join([f"• {v.plate_number} ({v.car_model or 'без модели'})" for v in user.vehicles]) or "нет"
    if user.slot:
        until = user.slot.valid_until.strftime("%d.%m.%Y %H:%M") if user.slot.valid_until else "—"
        slot_info = f"Ячейка #{user.slot.slot_number} | Статус: {user.slot.status} | До: {until}\nНомер шлагбаума: {cfg['gate_phone']}"
    else:
        slot_info = "Доступ не активирован"
    await message.answer(f"Телефон: +{user.phone}\nМашины:\n{cars}\n\nШлагбаум: {slot_info}")


# --- Админ-команды в боте ---
@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id not in settings.admin_ids:
        return
    async with async_session() as session:
        used = (await session.execute(select(func.count(RtuSlot.slot_number)).where(RtuSlot.status != "free"))).scalar() or 0
        fin = await get_finance_stats(session)
    await message.answer(
        f"Статистика парковки:\n"
        f"Ячейки RTU5024: {used} / 200\n"
        f"Выручка сегодня: {fin['today']} ₽\n"
        f"За 7 дней: {fin['week']} ₽\n"
        f"За 30 дней: {fin['month']} ₽"
    )


@dp.message(Command("open"))
async def cmd_open(message: Message):
    if message.from_user.id not in settings.admin_ids:
        return
    async with async_session() as session:
        await queue_open_gate(session)
    await message.answer("Команда открытия шлагбаума (CC) поставлена в очередь на модем RX100-R4.")


@dp.message(Command("find"))
async def cmd_find(message: Message):
    if message.from_user.id not in settings.admin_ids:
        return
    q = message.text.replace("/find", "", 1).strip().upper()
    if not q:
        await message.answer("Использование: /find <госномер или цифры телефона>")
        return
    async with async_session() as session:
        res = await session.execute(select(User).options(selectinload(User.vehicles), selectinload(User.slot)))
        found = []
        for u in res.scalars().all():
            plates = [v.plate_number for v in u.vehicles]
            if q in u.phone or any(q in p for p in plates):
                slot_txt = f"ячейка #{u.slot.slot_number} ({u.slot.status})" if u.slot else "нет доступа"
                found.append(f"+{u.phone} | {', '.join(plates) or '—'} | {slot_txt}")
    await message.answer("\n".join(found[:15]) if found else "Ничего не найдено.")


@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id not in settings.admin_ids:
        return
    text = message.text.replace("/broadcast", "", 1).strip()
    if not text:
        await message.answer("Использование: /broadcast <текст объявления>")
        return
    count = await broadcast_to_users(text)
    await message.answer(f"Рассылка отправлена {count} пользователям.")
