from aiogram.client.session.aiohttp import AiohttpSession
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
from app.database import async_session, User, Tariff, Order, RtuSlot, ExtraService, ServiceOrder
from app.services import (
    register_user_and_car, create_order, validate_promo_code, parse_tariff_ids,
    get_app_settings, mark_order_paid_by_user, confirm_order, reject_order,
    queue_open_gate, get_finance_stats,
    list_extra_services, create_service_order, mark_service_order_paid
)

bot = Bot(token=settings.BOT_TOKEN, session=(AiohttpSession(proxy=settings.BOT_PROXY) if settings.BOT_PROXY else None))
dp = Dispatcher()
logger = logging.getLogger("uvicorn")


class RegState(StatesGroup):
    waiting_plate = State()
    waiting_model = State()
    waiting_promo = State()
    waiting_support = State()
    waiting_svc_spot = State()
    waiting_svc_phone = State()
    waiting_svc_comment = State()


class AdminState(StatesGroup):
    waiting_reply = State()


def kb_contact():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отправить номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def kb_main():
    rows = [
        [KeyboardButton(text="Мой статус"), KeyboardButton(text="Выбрать тариф / Оплатить")],
        [KeyboardButton(text="Заказать доп услугу")],
        [KeyboardButton(text="Ввести промокод"), KeyboardButton(text="Добавить машину")],
        [KeyboardButton(text="Помощь")],
    ]
    if settings.WEBAPP_URL.startswith("https://"):
        rows.insert(0, [KeyboardButton(text="Открыть личный кабинет", web_app=WebAppInfo(url=settings.WEBAPP_URL))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def kb_tariffs(discount: int = 0, promo_tariff_ids: list[int] | None = None) -> InlineKeyboardMarkup:
    async with async_session() as session:
        res = await session.execute(select(Tariff).where(Tariff.is_active == True).order_by(Tariff.id))
        buttons = []
        for t in res.scalars().all():
            d = discount if (discount and (not promo_tariff_ids or t.id in promo_tariff_ids)) else 0
            price = max(0, round(t.price * (100 - d) / 100))
            label = f"{t.name} — {price} ₽" if not d else f"{t.name} — {price} ₽ (-{d}%)"
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
    plates = order.vehicle.plate_number if getattr(order, "vehicle", None) else (", ".join([v.plate_number for v in order.user.vehicles]) or "—")
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


async def notify_user_activated(telegram_id: int | None, slot_number: int, valid_until: datetime | None, spot_label: str | None = None):
    if not telegram_id:
        return
    until = valid_until.strftime("%d.%m.%Y %H:%M") if valid_until else "—"
    spot_line = f"\nНазначенное место: {spot_label or slot_number}"
    try:
        await bot.send_message(
            telegram_id,
            f"Оплата подтверждена!\nВаш номер подключён к доступу на парковку.{spot_line}\nДоступ действует до: {until}",
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
            "Добро пожаловать на парковку!\nОтправьте номер телефона для доступа к парковке.",
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


MENU_BUTTONS = {
    "Мой статус", "Выбрать тариф / Оплатить", "Ввести промокод",
    "Добавить машину", "Помощь", "Открыть личный кабинет"
}

@dp.message(RegState.waiting_plate)
async def process_plate(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if txt in MENU_BUTTONS or txt.startswith("/"):
        await state.set_state(None)
        await message.answer("Ввод госномера отменён.", reply_markup=kb_main())
        return
    if len(txt) < 4 or len(txt) > 15:
        await message.answer("Введите корректный госномер (например, А123ВС777):")
        return
    await state.update_data(plate=txt)
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
    await message.answer("Выберите тариф для доступа к парковке:", reply_markup=await kb_tariffs(discount))


@dp.message(F.text == "Ввести промокод")
async def promo_start(message: Message, state: FSMContext):
    await state.set_state(RegState.waiting_promo)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Пропустить", callback_data="skip_promo")
    ]])
    await message.answer("Введите промокод или нажмите «Пропустить»:", reply_markup=kb)


@dp.callback_query(F.data == "skip_promo")
async def on_skip_promo(callback: CallbackQuery, state: FSMContext):
    await state.update_data(promo_code=None, promo_discount=0, promo_tariff_ids=[])
    await state.set_state(None)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("Промокод пропущен. Выберите тариф:", reply_markup=await kb_tariffs(0))
    await callback.answer()


@dp.message(RegState.waiting_promo)
async def process_promo(message: Message, state: FSMContext):
    code = message.text.strip()
    async with async_session() as session:
        u_res = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        u = u_res.scalar_one_or_none()
        try:
            promo = await validate_promo_code(session, code, user_id=u.id if u else None)
        except ValueError as e:
            await state.update_data(promo_code=None, promo_discount=0, promo_tariff_ids=[])
            await state.set_state(None)
            await message.answer(
                f"{e}. Промокод сброшен — вы можете выбрать тариф по обычной цене:",
                reply_markup=await kb_tariffs(0),
            )
            return

    t_ids = parse_tariff_ids(promo.tariff_ids)
    await state.update_data(promo_code=promo.code, promo_discount=promo.discount_percent, promo_tariff_ids=t_ids)
    await state.set_state(None)
    await message.answer(
        f"Промокод {promo.code} применён! Скидка {promo.discount_percent}%.\nВыберите тариф:",
        reply_markup=await kb_tariffs(promo.discount_percent, t_ids),
    )


@dp.message(F.text == "Выбрать тариф / Оплатить")
async def choose_tariff(message: Message, state: FSMContext):
    async with async_session() as session:
        res = await session.execute(
            select(User).options(selectinload(User.vehicles)).where(User.telegram_id == message.from_user.id)
        )
        user = res.scalar_one_or_none()
    if not user or not user.vehicles:
        await message.answer("Сначала добавьте автомобиль.", reply_markup=kb_main())
        return
    if len(user.vehicles) > 1:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"{v.plate_number}" + (f" (до {v.valid_until.strftime('%d.%m.%Y')})" if v.valid_until else ""),
                callback_data=f"car_pay:{v.id}"
            )]
            for v in user.vehicles
        ])
        await message.answer("Выберите автомобиль для оплаты / продления:", reply_markup=kb)
        return
    await state.update_data(pay_vehicle_id=user.vehicles[0].id)
    data = await state.get_data()
    discount = data.get("promo_discount", 0)
    await message.answer(
        f"Авто: {user.vehicles[0].plate_number}\nВыберите тариф:",
        reply_markup=await kb_tariffs(discount, data.get("promo_tariff_ids"))
    )


@dp.callback_query(F.data.startswith("car_pay:"))
async def on_car_pay_pick(callback: CallbackQuery, state: FSMContext):
    vid = int(callback.data.split(":")[1])
    await state.update_data(pay_vehicle_id=vid)
    data = await state.get_data()
    discount = data.get("promo_discount", 0)
    await callback.message.edit_text(
        "Выберите тариф:",
        reply_markup=await kb_tariffs(discount, data.get("promo_tariff_ids"))
    )
    await callback.answer()


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
        try:
            order = await create_order(session, user.id, tariff_id, promo_code, vehicle_id=data.get("pay_vehicle_id"))
        except ValueError as e:
            await callback.answer(str(e), show_alert=True)
            return
        tariff = await session.get(Tariff, tariff_id)
        cfg = await get_app_settings(session)

    promo_line = f"\nПромокод: {order.promo_code} (-{order.discount_percent}%)" if order.promo_code else ""
    desc_line = f"\nОписание: {tariff.description}" if getattr(tariff, "description", "") else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Я оплатил", callback_data=f"user_paid:{order.id}")
    ]])
    await callback.message.answer(
        f"Заявка #{order.id}\n"
        f"Тариф: {tariff.name}{desc_line}{promo_line}\n"
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
    await notify_user_activated(user.telegram_id, slot.slot_number, slot.valid_until, slot.spot_label)
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


STATUS_LABELS = {
    "pending_add": "Подключается",
    "pending": "Ожидает подключения",
    "active": "Активен",
    "pending_del": "Отключается",
    "free": "Не активен",
}


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
    cars = "\n".join([f"• {v.plate_number}" + (f" ({v.car_model})" if v.car_model else "") + (f" — до {v.valid_until.strftime('%d.%m.%Y %H:%M')}" if v.valid_until else " — не оплачена") for v in user.vehicles]) or "нет"
    if user.slot:
        until = user.slot.valid_until.strftime("%d.%m.%Y %H:%M") if user.slot.valid_until else "—"
        spot_txt = f" | Место: {user.slot.spot_label or user.slot.slot_number}"
        st_human = STATUS_LABELS.get(user.slot.status, user.slot.status)
        spot_line = f"Парковочное место: {user.slot.spot_label or user.slot.slot_number}\n" if (user.slot.spot_label or user.slot.slot_number) else ""
        slot_info = f"{spot_line}Статус: {st_human}\nДействует до: {until}\nНомер для звонка: {cfg['gate_phone']}"
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


async def notify_admins_support(text: str, user_tg_id: int | None = None, phone: str = "", full_name: str = ""):
    who = []
    if full_name:
        who.append(full_name)
    if phone:
        who.append(f"+{phone}")
    if user_tg_id:
        who.append(f"ID: {user_tg_id}")
    header = " · ".join(who) or "Гость с сайта"
    msg_text = f"Обращение в поддержку\nОт: {header}\n\n{text}"
    kb = None
    if user_tg_id:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Ответить", callback_data=f"sup_reply:{user_tg_id}")
        ]])
    for aid in settings.admin_ids:
        try:
            await bot.send_message(aid, msg_text, reply_markup=kb)
        except Exception as e:
            logger.error(f"Ошибка отправки обращения админу {aid}: {e}")


@dp.message(F.text == "Помощь")
async def help_start(message: Message, state: FSMContext):
    await state.set_state(RegState.waiting_support)
    await message.answer("Напишите ваш вопрос администратору (или отправьте «Отмена»):")


@dp.message(RegState.waiting_support)
async def process_support(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    await state.set_state(None)
    if not txt or txt.lower() == "отмена":
        await message.answer("Отменено.", reply_markup=kb_main())
        return
    async with async_session() as session:
        res = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = res.scalar_one_or_none()
    await notify_admins_support(
        text=txt,
        user_tg_id=message.from_user.id,
        phone=user.phone if user else "",
        full_name=message.from_user.full_name or (user.full_name if user else ""),
    )
    await message.answer("Сообщение отправлено администратору. Ожидайте ответ здесь в боте!", reply_markup=kb_main())


@dp.callback_query(F.data.startswith("sup_reply:"))
async def on_admin_support_reply(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in settings.admin_ids:
        await callback.answer("Нет доступа", show_alert=True)
        return
    target_tg_id = int(callback.data.split(":")[1])
    await state.update_data(reply_target_tg_id=target_tg_id)
    await state.set_state(AdminState.waiting_reply)
    await callback.message.answer(f"Введите ответ пользователю (ID {target_tg_id}) или «Отмена»:")
    await callback.answer()


@dp.message(AdminState.waiting_reply)
async def process_admin_reply(message: Message, state: FSMContext):
    if message.from_user.id not in settings.admin_ids:
        await state.set_state(None)
        return
    txt = (message.text or "").strip()
    data = await state.get_data()
    target_tg_id = data.get("reply_target_tg_id")
    await state.set_state(None)
    if not txt or txt.lower() == "отмена" or not target_tg_id:
        await message.answer("Ответ отменён.")
        return
    try:
        await bot.send_message(target_tg_id, f"Ответ администратора:\n\n{txt}")
        await message.answer("Ответ отправлен пользователю.")
    except Exception as e:
        await message.answer(f"Не удалось отправить ответ: {e}")


async def notify_admins_service_order(order: ServiceOrder):
    comment_line = f"\nКомментарий: {order.comment}" if order.comment else ""
    text = (
        f"Оплата доп. услуги · Заказ #{order.id}\n"
        f"Услуга: {order.service.name}\n"
        f"Стоимость: {order.amount} ₽\n"
        f"Место: {order.spot}\n"
        f"Телефон: +{order.phone}{comment_line}"
    )
    for aid in settings.admin_ids:
        try:
            await bot.send_message(aid, text)
        except Exception as e:
            logger.error(f"Ошибка уведомления админа {aid} о допуслуге: {e}")


@dp.message(F.text == "Заказать доп услугу")
async def extra_service_start(message: Message, state: FSMContext):
    await state.clear()
    async with async_session() as session:
        items = await list_extra_services(session, only_active=True)
    if not items:
        await message.answer("Дополнительные услуги сейчас недоступны.", reply_markup=kb_main())
        return
    buttons = [
        [InlineKeyboardButton(text=f"{s.name} — {s.price} ₽", callback_data=f"xsvc:{s.id}")]
        for s in items
    ]
    await message.answer("Выберите дополнительную услугу и стоимость:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("xsvc:"))
async def on_extra_service_pick(callback: CallbackQuery, state: FSMContext):
    svc_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        svc = await session.get(ExtraService, svc_id)
        res = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = res.scalar_one_or_none()
    if not svc or not svc.is_active:
        await callback.answer("Услуга недоступна", show_alert=True)
        return
    await state.update_data(xsvc_id=svc.id, xsvc_default_phone=user.phone if user else "")
    await state.set_state(RegState.waiting_svc_spot)
    await callback.message.answer(f"Выбрано: {svc.name} ({svc.price} ₽).\nУкажите номер или название места на парковке:")
    await callback.answer()


@dp.message(RegState.waiting_svc_spot)
async def process_svc_spot(message: Message, state: FSMContext):
    spot = (message.text or "").strip()
    if not spot:
        await message.answer("Пожалуйста, укажите место:")
        return
    data = await state.get_data()
    default_phone = data.get("xsvc_default_phone", "")
    await state.update_data(xsvc_spot=spot)
    await state.set_state(RegState.waiting_svc_phone)
    hint = f" (или напишите «ок», чтобы использовать +{default_phone})" if default_phone else ""
    await message.answer(f"Введите номер телефона для связи{hint}:")


@dp.message(RegState.waiting_svc_phone)
async def process_svc_phone(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    data = await state.get_data()
    phone = data.get("xsvc_default_phone", "") if raw.lower() in ("ок", "ok", "-") else raw
    if not phone:
        await message.answer("Введите номер телефона:")
        return
    await state.update_data(xsvc_phone=phone)
    await state.set_state(RegState.waiting_svc_comment)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Пропустить", callback_data="xsvc_skip_comment")
    ]])
    await message.answer("Добавьте комментарий к заказу или нажмите «Пропустить»:", reply_markup=kb)


async def _finish_service_order(message: Message, state: FSMContext, comment: str):
    data = await state.get_data()
    await state.clear()
    async with async_session() as session:
        try:
            order = await create_service_order(
                session,
                service_id=data["xsvc_id"],
                phone=data["xsvc_phone"],
                spot=data["xsvc_spot"],
                comment=comment,
            )
        except ValueError as e:
            await message.answer(f"Ошибка: {e}", reply_markup=kb_main())
            return
        cfg = await get_app_settings(session)
    comment_line = f"\nКомментарий: {order.comment}" if order.comment else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Оплатить", callback_data=f"xsvc_pay:{order.id}")
    ]])
    await message.answer(
        f"Заказ доп. услуги #{order.id}\n"
        f"Услуга: {order.service.name}\n"
        f"Место: {order.spot}\n"
        f"Телефон: +{order.phone}{comment_line}\n"
        f"К оплате: {order.amount} ₽\n\n"
        f"Реквизиты СБП:\n"
        f"Номер: {cfg['sbp_phone']}\n"
        f"Банк: {cfg['sbp_bank']}\n"
        f"Получатель: {cfg['sbp_recipient']}",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "xsvc_skip_comment")
async def on_xsvc_skip_comment(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _finish_service_order(callback.message, state, "")
    await callback.answer()


@dp.message(RegState.waiting_svc_comment)
async def process_svc_comment(message: Message, state: FSMContext):
    raw_comment = (message.text or "").strip()
    comment = "" if raw_comment == "-" else raw_comment
    await _finish_service_order(message, state, comment)

@dp.callback_query(F.data.startswith("xsvc_pay:"))
async def on_xsvc_pay_click(callback: CallbackQuery):
    order_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        try:
            order = await mark_service_order_paid(session, order_id)
        except ValueError as e:
            await callback.answer(str(e), show_alert=True)
            return
    await notify_admins_service_order(order)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(f"Оплата по заказу #{order_id} зафиксирована. Уведомление отправлено администратору!")
    await callback.answer()
