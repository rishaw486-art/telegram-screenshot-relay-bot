from aiogram.methods.base import TelegramMethod
from aiogram.types import Message


class SendRecurringStarsInvoice(TelegramMethod[Message]):
    """sendInvoice with subscription_period, omitted by some aiogram releases."""

    __returning__ = Message
    __api_method__ = "sendInvoice"

    chat_id: int
    title: str
    description: str
    payload: str
    currency: str
    prices: list[dict[str, int | str]]
    provider_token: str = ""
    subscription_period: int
