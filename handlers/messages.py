import os
import logging
from datetime import datetime, timezone
from telethon import events, TelegramClient
from telethon.tl.types import User, Chat, Channel
from pathlib import Path

from config import config
from database import Database
from ai_service import AIService

logger = logging.getLogger(__name__)


def register_message_handlers(client: TelegramClient, db: Database, ai_service: AIService, bot_client: TelegramClient = None):

    async def notify_user(message_text: str, parse_mode: str = "html", file_path: str = None):
        """Akkaunt egasiga Saved Messages yoki Telegram Bot orqali bildirishnoma yuborish."""
        try:
            if file_path and os.path.exists(file_path):
                await client.send_file("me", file=file_path, caption=message_text, parse_mode=parse_mode)
            else:
                await client.send_message("me", message_text, parse_mode=parse_mode)
        except Exception as e:
            logger.warning(f"Saved Messages'ga xabar yuborishda xato: {e}")

        # Agar bot_client ham sozlangan bo'lsa, bot orqali ham jo'natish
        if bot_client and config.MY_TELEGRAM_ID:
            try:
                if file_path and os.path.exists(file_path):
                    await bot_client.send_file(config.MY_TELEGRAM_ID, file=file_path, caption=message_text, parse_mode=parse_mode)
                else:
                    await bot_client.send_message(config.MY_TELEGRAM_ID, message_text, parse_mode=parse_mode)
            except Exception as e:
                logger.debug(f"Bot orqali xabar yuborishda xato: {e}")

    @client.on(events.NewMessage)
    async def on_new_message(event: events.NewMessage.Event):
        try:
            # Saved Messages (o'zimiz bilan o'zimiz yozishganda) komandalarni commands.py ushlaydi
            me = await client.get_me()
            if event.is_private and event.chat_id == me.id:
                return

            sender = await event.get_sender()
            chat = await event.get_chat()

            sender_id = sender.id if sender else None
            sender_username = getattr(sender, "username", None)
            sender_first_name = getattr(sender, "first_name", "") or ""
            sender_last_name = getattr(sender, "last_name", "") or ""
            sender_name = f"{sender_first_name} {sender_last_name}".strip() or "Noma'lum"

            # Qora ro'yxat (Blacklist) tekshiruvi: agar bloklangan bo'lsa e'tibor bermaymiz
            if await db.is_blacklisted(sender_id, sender_username):
                return

            # Tinchlik rejimi (Silent Mode) holati
            is_silent = await db.is_silent_mode()

            # Chat ma'lumotlari
            chat_id = event.chat_id
            chat_title = getattr(chat, "title", None) or sender_name
            chat_username = getattr(chat, "username", None)

            if isinstance(chat, User):
                chat_type = "user"
            elif isinstance(chat, Channel):
                chat_type = "channel" if chat.broadcast else "supergroup"
            elif isinstance(chat, Chat):
                chat_type = "group"
            else:
                chat_type = "unknown"

            text = event.raw_text or ""
            is_outgoing = bool(event.out)

            # Yangi chat / kontaktni bazada yangilash
            if chat_id:
                await db.upsert_chat(chat_id, chat_title, chat_username, chat_type)

            # Yangi foydalanuvchi birinchi marta yozganida ogohlantirish
            if not is_outgoing and sender_id and isinstance(chat, User):
                is_first = await db.is_first_time_user(sender_id)
                if is_first:
                    await db.upsert_user(sender_id, sender_username, sender_first_name, sender_last_name)
                    if config.ENABLE_NEW_CHAT_ALERT and not is_silent:
                        alert_msg = (
                            f"👤 <b>Yangi Foydalanuvchi Yozdi:</b>\n"
                            f"├ <b>Ism:</b> {sender_name}\n"
                            f"├ <b>Username:</b> @{sender_username if sender_username else 'mavjud_emas'}\n"
                            f"├ <b>ID:</b> <code>{sender_id}</code>\n"
                            f"└ <b>Xabar:</b> {text[:200] if text else '[Fayl/Media]'}"
                        )
                        await notify_user(alert_msg)
                else:
                    await db.upsert_user(sender_id, sender_username, sender_first_name, sender_last_name)

            # Media fayllarni saqlash
            media_type = None
            media_path = None
            voice_transcript = None

            if event.media and config.ENABLE_MESSAGE_LOGGER:
                try:
                    if event.voice:
                        media_type = "voice"
                        ext = ".ogg"
                    elif event.audio:
                        media_type = "audio"
                        ext = ".mp3"
                    elif event.photo:
                        media_type = "photo"
                        ext = ".jpg"
                    elif event.video:
                        media_type = "video"
                        ext = ".mp4"
                    else:
                        media_type = "document"
                        ext = ".dat"

                    file_name = f"{abs(chat_id)}_{event.id}{ext}"
                    save_path = os.path.join(config.MEDIA_STORAGE_DIR, file_name)

                    if hasattr(event.file, "size") and event.file.size and event.file.size <= 15 * 1024 * 1024:
                        await event.download_media(file=save_path)
                        media_path = save_path

                        # Ovozli xabar bo'lsa transkripsiya qilish
                        if media_type in ("voice", "audio") and config.ENABLE_VOICE_TRANSCRIPTION:
                            voice_transcript = await ai_service.transcribe_voice(save_path)
                            if voice_transcript:
                                logger.info(f"Ovozli xabar transkripsiyasi: {voice_transcript[:60]}...")
                except Exception as e:
                    logger.warning(f"Media yuklab olishda xatolik: {e}")

            # Xabarni bazaga yozish
            msg_date = event.date or datetime.now(timezone.utc)
            await db.save_message(
                message_id=event.id,
                chat_id=chat_id,
                sender_id=sender_id,
                sender_username=sender_username,
                sender_name=sender_name,
                chat_title=chat_title,
                chat_type=chat_type,
                text=text,
                media_type=media_type,
                media_path=media_path,
                date=msg_date,
                is_outgoing=is_outgoing,
                voice_transcript=voice_transcript
            )

            # Kiruvchi xabarlar uchun aqlli shoshilinchlik va spam tekshiruvi
            if not is_outgoing and text:
                sender_info = f"@{sender_username}" if sender_username else sender_name

                # Shoshilinchlik tahlili (Urgency alert hatto silent rejimda ham keladi!)
                if config.ENABLE_URGENCY_ALERT:
                    is_urgent, reason = await ai_service.check_urgency(text, sender_info)
                    if is_urgent:
                        urgency_alert = (
                            f"🚨 <b>Shoshilinch Xabar Aniqlandi!</b>\n"
                            f"├ <b>Kimdan:</b> {sender_name} (@{sender_username or 'yoq'})\n"
                            f"├ <b>Chat:</b> {chat_title}\n"
                            f"├ <b>Sabab:</b> {reason}\n"
                            f"└ <b>Xabar:</b> {text}"
                        )
                        await notify_user(urgency_alert)

                # Spam filtri (faqat shaxsiy chatlar uchun va silent bo'lmaganda)
                if config.ENABLE_SPAM_FILTER and chat_type == "user" and not is_silent:
                    is_spam, spam_reason = await ai_service.check_spam(text, sender_info)
                    if is_spam:
                        spam_alert = (
                            f"🛡 <b>Ehtimoliy Spam / Reklama:</b>\n"
                            f"├ <b>Kimdan:</b> {sender_name} (@{sender_username or 'yoq'})\n"
                            f"├ <b>Sabab:</b> {spam_reason}\n"
                            f"└ <b>Matn:</b> {text[:300]}"
                        )
                        await notify_user(spam_alert)

        except Exception as e:
            logger.error(f"Xabar qayta ishlashda xatolik: {e}", exc_info=True)
