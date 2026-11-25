from datetime import datetime
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, String, Text, select, text
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from app.config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(50), primary_key=True)
    value = Column(String(255), nullable=False)


class Tariff(Base):
    __tablename__ = "tariffs"

    id = Column(Integer, primary_key=True)
    code = Column(String(20), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    price = Column(Integer, nullable=False)
    days = Column(Integer, default=0)
    is_night = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    description = Column(Text, default="", nullable=False)


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id = Column(Integer, primary_key=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    discount_percent = Column(Integer, nullable=False)
    valid_until = Column(DateTime, nullable=True)
    target_audience = Column(String(20), default="all", nullable=False)
    tariff_ids = Column(String(255), default="", nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)


class ExtraService(Base):
    __tablename__ = "extra_services"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False)
    price = Column(Integer, nullable=False, default=0)
    description = Column(Text, default="", nullable=False)
    is_active = Column(Boolean, default=True)


class ServiceOrder(Base):
    __tablename__ = "service_orders"

    id = Column(Integer, primary_key=True)
    service_id = Column(Integer, ForeignKey("extra_services.id"), nullable=False)
    phone = Column(String(20), nullable=False)
    spot = Column(String(50), default="", nullable=False)
    comment = Column(Text, default="", nullable=False)
    amount = Column(Integer, nullable=False)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime, default=datetime.now)

    service = relationship("ExtraService")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    phone = Column(String(20), unique=True, nullable=False, index=True)
    telegram_id = Column(BigInteger, unique=True, nullable=True, index=True)
    full_name = Column(String(100), nullable=True)
    is_admin = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)

    vehicles = relationship("Vehicle", back_populates="user", cascade="all, delete-orphan")
    slot = relationship("RtuSlot", back_populates="user", uselist=False)
    orders = relationship("Order", back_populates="user", cascade="all, delete-orphan")


class Vehicle(Base):
    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    plate_number = Column(String(20), nullable=False)
    car_model = Column(String(100), nullable=True)
    valid_until = Column(DateTime, nullable=True)
    spot_label = Column(String(50), nullable=True)
    overstay_status = Column(String(20), default="none", nullable=False)
    debt_since = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="vehicles")


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    tariff_id = Column(Integer, ForeignKey("tariffs.id"), nullable=False)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="SET NULL"), nullable=True)
    amount = Column(Integer, nullable=False)
    promo_code = Column(String(50), nullable=True)
    discount_percent = Column(Integer, default=0)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="orders")
    tariff = relationship("Tariff")
    vehicle = relationship("Vehicle")


class RtuSlot(Base):
    __tablename__ = "rtu_slots"

    slot_number = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    valid_until = Column(DateTime, nullable=True)
    status = Column(String(20), default="free")
    notified_expiring = Column(Boolean, default=False)
    spot_label = Column(String(50), nullable=True)

    user = relationship("User", back_populates="slot")


class SmsQueue(Base):
    __tablename__ = "sms_queue"

    id = Column(Integer, primary_key=True)
    slot_number = Column(Integer, ForeignKey("rtu_slots.slot_number"), nullable=True)
    action = Column(String(15), nullable=False)
    command_text = Column(String(100), nullable=False)
    status = Column(String(20), default="pending")
    error_msg = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


DEFAULT_TARIFFS = [
    {"code": "night", "name": "На ночь (21:00–08:00)", "price": 200, "days": 0, "is_night": True},
    {"code": "1d", "name": "1 день", "price": 300, "days": 1, "is_night": False},
    {"code": "7d", "name": "7 дней", "price": 1800, "days": 7, "is_night": False},
    {"code": "14d", "name": "14 дней", "price": 3200, "days": 14, "is_night": False},
    {"code": "30d", "name": "Месяц (30 дней)", "price": 5500, "days": 30, "is_night": False},
    {"code": "365d", "name": "Год (365 дней)", "price": 55000, "days": 365, "is_night": False},
]

DEFAULT_SETTINGS = {
    "sbp_phone": "+7 999 000-00-00",
    "sbp_bank": "Сбербанк / Т-Банк",
    "sbp_recipient": "Парковка",
    "gate_phone": settings.RTU_PHONE,
    "bot_username": "",
    "max_spots": "200",
    "description_enabled": "0",
    "description_text": "",
    "contract_text": "1. Общие положения\nНастоящий документ является публичной офертой и определяет порядок предоставления доступа на парковку и размещения транспортного средства (ТС) Пользователя на условиях выбранного тарифа.\n\n2. Предмет договора\nИсполнитель предоставляет Пользователю право проезда через шлагбаум (по номеру телефона) и размещения ТС на территории парковки в пределах оплаченного периода, а Пользователь обязуется оплатить услуги и соблюдать правила парковки.\n\n3. Порядок оформления и оплаты\nОформление заявки производится через сайт или Telegram-бота с указанием номера телефона и государственного регистрационного знака ТС. Доступ активируется после подтверждения оплаты.\n\n4. Права и обязанности сторон\nПользователю запрещается передавать доступ третьим лицам, блокировать проезд и нарушать правила внутреннего распорядка территории. При истечении оплаченного срока доступ может быть приостановлен автоматически.\n\n5. Персональные данные и реквизиты\nОтправляя данные при оформлении пропуска, Пользователь даёт согласие на их обработку для обеспечения работы сервиса и связи по вопросам обслуживания. Актуальные реквизиты получателя платежа отображаются на этапе оплаты.",
}


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        t_cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info(tariffs)"))).fetchall()]
        if "description" not in t_cols:
            await conn.execute(text("ALTER TABLE tariffs ADD COLUMN description TEXT DEFAULT ''"))
        p_cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info(promo_codes)"))).fetchall()]
        if "target_audience" not in p_cols:
            await conn.execute(text("ALTER TABLE promo_codes ADD COLUMN target_audience VARCHAR(20) DEFAULT 'all'"))
        if "tariff_ids" not in p_cols:
            await conn.execute(text("ALTER TABLE promo_codes ADD COLUMN tariff_ids VARCHAR(255) DEFAULT ''"))
        s_cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info(rtu_slots)"))).fetchall()]
        if "spot_label" not in s_cols:
            await conn.execute(text("ALTER TABLE rtu_slots ADD COLUMN spot_label VARCHAR(50)"))
        v_cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info(vehicles)"))).fetchall()]
        if "valid_until" not in v_cols:
            await conn.execute(text("ALTER TABLE vehicles ADD COLUMN valid_until DATETIME"))
        if "overstay_status" not in v_cols:
            await conn.execute(text("ALTER TABLE vehicles ADD COLUMN overstay_status VARCHAR(20) DEFAULT 'none'"))
        if "debt_since" not in v_cols:
            await conn.execute(text("ALTER TABLE vehicles ADD COLUMN debt_since DATETIME"))
        if "spot_label" not in v_cols:
            await conn.execute(text("ALTER TABLE vehicles ADD COLUMN spot_label VARCHAR(50)"))
            await conn.execute(text("UPDATE vehicles SET spot_label = (SELECT spot_label FROM rtu_slots WHERE rtu_slots.user_id = vehicles.user_id) WHERE spot_label IS NULL AND valid_until IS NOT NULL"))
        o_cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info(orders)"))).fetchall()]
        if "vehicle_id" not in o_cols:
            await conn.execute(text("ALTER TABLE orders ADD COLUMN vehicle_id INTEGER"))

    async with async_session() as session:
        if (await session.execute(select(RtuSlot.slot_number).limit(1))).scalar() is None:
            session.add_all([RtuSlot(slot_number=i, status="free") for i in range(1, 201)])

        if (await session.execute(select(Tariff.id).limit(1))).scalar() is None:
            session.add_all([Tariff(**t) for t in DEFAULT_TARIFFS])

        for k, v in DEFAULT_SETTINGS.items():
            if await session.get(AppSetting, k) is None:
                session.add(AppSetting(key=k, value=v))

        await session.commit()
