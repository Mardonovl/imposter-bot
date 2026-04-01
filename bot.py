import asyncio
import logging
import random
import sqlite3
import time
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ChatMemberAdministrator,
    ChatMemberOwner,
)
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.client.default import DefaultBotProperties

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import os
# TOKEN = os.environ.get("BOT_TOKEN", "")
TOKEN="8674706974:AAEf7abZysSOzbPejCKCcAB4xIAd7UenZ8Y"
WORDS = [
    # Mashhur joylar va obidalar
    "Eyfel minorasi", "Misr ehromlari", "Oq uy", "Bermud uchburchagi", "Mars",
    "Antarktida", "Xitoy devori", "Vatikan", "Disney-lend", "Sahroi kabir",

    # Qiziqarli personajlar va shaxslar
    "Gitler", "King Kong", "Mario", "Pikachu", "Sherlok Xolms",
    "Betmen", "Drakula", "Tanos", "Shrek", "Garri Potter",
    "Joker", "Terminator", "Napoleon", "Eynshteyn", "Monaliza",

    # Hayvonlar va mavjudotlar (noodatiy)
    "Yagona shox (Unicorn)", "Feniks qushi", "Dinozavr", "Kenguru", "Mamont",
    "Oq akula", "Pingvin", "Chayon", "Ajdaho", "Koala",

    # Buyumlar, mevalar va brendlar
    "Banan", "Oltin tish", "iPhone", "Coca-Cola", "Uchar tarelka",
    "Dasturlash kodi", "Bitkoin", "Selfi-tayoq", "Mikroskop", "Teleskop",

    # Kutilmagan tushunchalar
    "Vaqt mashinasi", "Qora tuynuk",  "Kanditsioner", "Parashyut",
    "Kriptovalyuta", "Simsiz quloqchin", "Robot", "Kosmik kema", "Gologramma"
]

REGISTRATION_TIME = 60
TIME_EXTEND = 30
ROUND_TIME = 90
VOTING_TIME = 60
MIN_PLAYERS = 3

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

active_tasks: dict[int, asyncio.Task] = {}

BOT_USERNAME = ""

db = sqlite3.connect("imposter_game.db", check_same_thread=False)
db.row_factory = sqlite3.Row


def init_db():
    db.executescript("""
        CREATE TABLE IF NOT EXISTS games (
            chat_id     INTEGER PRIMARY KEY,
            status      TEXT    DEFAULT 'idle',
            started_by  INTEGER,
            round_num   INTEGER DEFAULT 0,
            imp_count   INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS players (
            chat_id     INTEGER,
            user_id     INTEGER,
            username    TEXT,
            first_name  TEXT,
            word        TEXT,
            is_impostor INTEGER DEFAULT 0,
            is_alive    INTEGER DEFAULT 1,
            has_written INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS votes (
            chat_id     INTEGER,
            round_num   INTEGER,
            voter_id    INTEGER,
            target_id   INTEGER,
            vote_type   TEXT DEFAULT 'elim',
            PRIMARY KEY (chat_id, round_num, voter_id, vote_type)
        );
    """)
    db.commit()


def get_game(chat_id: int) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM games WHERE chat_id=?", (chat_id,)).fetchone()


def get_alive_players(chat_id: int) -> list:
    return db.execute(
        "SELECT * FROM players WHERE chat_id=? AND is_alive=1", (chat_id,)
    ).fetchall()


def get_player(chat_id: int, user_id: int) -> Optional[sqlite3.Row]:
    return db.execute(
        "SELECT * FROM players WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()


def set_status(chat_id: int, status: str):
    db.execute("UPDATE games SET status=? WHERE chat_id=?", (status, chat_id))
    db.commit()


def mention(p) -> str:
    """Foydalanuvchi nomini mention sifatida qaytaradi."""
    name = p["first_name"] or p["username"] or str(p["user_id"])
    if p["username"]:
        return f'<a href="tg://user?id={p["user_id"]}">{name}</a> (@{p["username"]})'
    return f'<a href="tg://user?id={p["user_id"]}">{name}</a>'


def short_name(p) -> str:
    return p["first_name"] or p["username"] or str(p["user_id"])


def calc_impostors(count: int) -> int:
    if count >= 15:
        return 3
    if count >= 10:
        return 2
    return 1


def cancel_task(chat_id: int):
    task = active_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()


def cleanup_game(chat_id: int):
    cancel_task(chat_id)
    db.execute("DELETE FROM games WHERE chat_id=?", (chat_id,))
    db.execute("DELETE FROM players WHERE chat_id=?", (chat_id,))
    db.execute("DELETE FROM votes WHERE chat_id=?", (chat_id,))
    db.commit()


def kb_join(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ O'yinga qo'shilish",
            url=f"https://t.me/{BOT_USERNAME}?start=join_{chat_id}"
        )]
    ])


def kb_vote(chat_id: int) -> InlineKeyboardMarkup:
    players = get_alive_players(chat_id)
    buttons = [
        [InlineKeyboardButton(
            text=short_name(p),
            callback_data=f"vote:elim:{p['user_id']}"
        )]
        for p in players
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def check_win(chat_id: int) -> Optional[str]:
    """'crew', 'impostor' yoki None qaytaradi."""
    players = get_alive_players(chat_id)
    impostors = [p for p in players if p["is_impostor"]]
    crew = [p for p in players if not p["is_impostor"]]

    if not impostors:
        return "crew"
    if len(impostors) >= len(crew):
        return "impostor"
    return None


async def end_registration(chat_id: int):
    """Ro'yxatdan o'tish tugaganda chaqiriladi."""
    active_tasks.pop(chat_id, None)

    all_players = db.execute(
        "SELECT * FROM players WHERE chat_id=?", (chat_id,)
    ).fetchall()

    if len(all_players) < MIN_PLAYERS:
        await bot.send_message(
            chat_id,
            f"❌ Yetarli o'yinchi yo'q (minimum {MIN_PLAYERS} kishi kerak). O'yin bekor qilindi."
        )
        cleanup_game(chat_id)
        return

    valid = []
    blocked_names = []
    for p in all_players:
        try:
            await bot.send_message(p["user_id"], "⌛ O'yin boshlanmoqda, so'zingizni kutib turing...")
            valid.append(p)
        except TelegramForbiddenError:
            blocked_names.append(short_name(p))
            db.execute(
                "DELETE FROM players WHERE chat_id=? AND user_id=?",
                (chat_id, p["user_id"])
            )

    db.commit()

    if blocked_names:
        await bot.send_message(
            chat_id,
            f"⚠️ Quyidagi o'yinchilar botni shaxsiy xabarda ishga tushirmagan va o'yindan olib tashlandi:\n"
            + "\n".join(f"• {n}" for n in blocked_names)
            + "\n\n(Ishtirok etish uchun bot bilan shaxsiy xabarda /start yozing)"
        )

    if len(valid) < MIN_PLAYERS:
        await bot.send_message(chat_id, "❌ Yetarli o'yinchi qolmadi. O'yin bekor qilindi.")
        cleanup_game(chat_id)
        return

    imp_count = calc_impostors(len(valid))
    db.execute("UPDATE games SET imp_count=? WHERE chat_id=?", (imp_count, chat_id))
    impostor_ids = set(random.sample([p["user_id"] for p in valid], imp_count))

    game_word = random.choice(WORDS)

    for p in valid:
        is_imp = p["user_id"] in impostor_ids
        word = "IMPOSTER" if is_imp else game_word
        db.execute(
            "UPDATE players SET is_impostor=?, word=? WHERE chat_id=? AND user_id=?",
            (1 if is_imp else 0, word, chat_id, p["user_id"])
        )
        if is_imp:
            await bot.send_message(
                p["user_id"],
                "🔴 <b>Siz IMPOSTERSIIZ!</b>\n\n"
                "Ekipaj a'zolarini aldang. Ularning so'ziga o'xshash so'z yozing va o'yindan omon chiqing! 😈"
            )
        else:
            await bot.send_message(
                p["user_id"],
                f"🟢 <b>Sizning so'zingiz:</b> <code>{word}</code>\n\n"
                f"Guruhda shu so'zga oid bitta so'z yozing. Imposterni toping! 🕵️"
            )

    db.commit()

    new_round = 1
    db.execute(
        "UPDATE games SET round_num=?, status='playing' WHERE chat_id=?",
        (new_round, chat_id)
    )
    db.execute("UPDATE players SET has_written=0 WHERE chat_id=?", (chat_id,))
    db.commit()

    names_list = "\n".join(f"  • {short_name(p)}" for p in valid)
    await bot.send_message(
        chat_id,
        f"✅ <b>O'yin boshlandi!</b> {len(valid)} o'yinchi\n"
        f"{'🔴 Imposterlar: ' + str(imp_count) + ' ta' if imp_count > 1 else '🔴 Imposter: 1 ta'}\n\n"
        f"👥 Ishtirokchilar:\n{names_list}\n\n"
        f"📝 <b>1-aylana boshlandi!</b>\n"
        f"Har bir o'yinchi <b>bitta</b> so'z yozsin.\n"
        f"⏱ Vaqt: {ROUND_TIME} sekund"
    )

    task = asyncio.create_task(round_timer(chat_id))
    active_tasks[chat_id] = task


async def round_timer(chat_id: int):
    await asyncio.sleep(ROUND_TIME)
    active_tasks.pop(chat_id, None)
    game = get_game(chat_id)
    if game and game["status"] == "playing":
        await bot.send_message(chat_id, "⏰ Vaqt tugadi! Ovoz berish boshlanmoqda...")
        await start_voting(chat_id)


async def start_voting(chat_id: int):
    game = get_game(chat_id)
    if not game:
        return

    set_status(chat_id, "voting")
    db.execute(
        "DELETE FROM votes WHERE chat_id=? AND round_num=? AND vote_type='elim'",
        (chat_id, game["round_num"])
    )
    db.commit()

    await bot.send_message(
        chat_id,
        f"🗳 <b>Ovoz berish vaqti!</b>\n\n"
        f"Kim imposter deb o'ylaysiz? Quyidan tanlang:\n"
        f"⏱ Vaqt: {VOTING_TIME} sekund",
        reply_markup=kb_vote(chat_id)
    )

    task = asyncio.create_task(voting_timer(chat_id))
    active_tasks[chat_id] = task


async def voting_timer(chat_id: int):
    await asyncio.sleep(VOTING_TIME)
    active_tasks.pop(chat_id, None)
    game = get_game(chat_id)
    if game and game["status"] == "voting":
        await finish_voting(chat_id)


async def finish_voting(chat_id: int):
    game = get_game(chat_id)
    if not game:
        return

    round_num = game["round_num"]
    votes = db.execute(
        "SELECT target_id, COUNT(*) as cnt FROM votes "
        "WHERE chat_id=? AND round_num=? AND vote_type='elim' "
        "GROUP BY target_id ORDER BY cnt DESC",
        (chat_id, round_num)
    ).fetchall()

    if not votes:
        await bot.send_message(chat_id, "🤷 Hech kim ovoz bermadi. Keyingi aylanaga o'tildi.")
        await next_round(chat_id)
        return

    max_votes = votes[0]["cnt"]
    top = [v for v in votes if v["cnt"] == max_votes]

    if len(top) > 1:
        await bot.send_message(
            chat_id,
            "🤝 <b>Tenglik!</b> Hech kim chiqarilmadi. Keyingi aylanaga o'tildi."
        )
        await next_round(chat_id)
        return

    eliminated_id = top[0]["target_id"]
    eliminated = get_player(chat_id, eliminated_id)

    db.execute(
        "UPDATE players SET is_alive=0 WHERE chat_id=? AND user_id=?",
        (chat_id, eliminated_id)
    )
    db.commit()

    was_imp = eliminated["is_impostor"]
    await bot.send_message(
        chat_id,
        f"🗳 <b>{mention(eliminated)}</b> o'yindan chiqarildi!\n"
        + ("😱 U <b>IMPOSTER</b> edi!" if was_imp else "✅ U oddiy o'yinchi edi. Keyingi aylanaga o'tildi.")
    )

    winner = check_win(chat_id)
    if winner:
        await announce_winner(chat_id, winner)
    else:
        await next_round(chat_id)


async def next_round(chat_id: int):
    winner = check_win(chat_id)
    if winner:
        await announce_winner(chat_id, winner)
        return

    game = get_game(chat_id)
    new_round = game["round_num"] + 1
    db.execute(
        "UPDATE games SET round_num=?, status='playing' WHERE chat_id=?",
        (new_round, chat_id)
    )
    db.execute(
        "UPDATE players SET has_written=0 WHERE chat_id=? AND is_alive=1", (chat_id,)
    )
    db.commit()

    players = get_alive_players(chat_id)
    names_list = "\n".join(f"  • {short_name(p)}" for p in players)

    await bot.send_message(
        chat_id,
        f"📝 <b>{new_round}-aylana boshlandi!</b>\n\n"
        f"Qolgan o'yinchilar ({len(players)} kishi):\n{names_list}\n\n"
        f"Har biri <b>bitta</b> so'z yozsin.\n"
        f"⏱ Vaqt: {ROUND_TIME} sekund"
    )

    task = asyncio.create_task(round_timer(chat_id))
    active_tasks[chat_id] = task


async def announce_winner(chat_id: int, winner: str):
    all_players = db.execute(
        "SELECT * FROM players WHERE chat_id=?", (chat_id,)
    ).fetchall()

    impostors = [p for p in all_players if p["is_impostor"]]
    crew = [p for p in all_players if not p["is_impostor"]]

    if winner == "crew":
        title = "🎉 <b>EKIPAJ G'ALABA QILDI!</b>"
        winners, losers = crew, impostors
    else:
        title = "😈 <b>IMPOSTERLAR G'ALABA QILDI!</b>"
        winners, losers = impostors, crew

    def plist(lst):
        if not lst:
            return "  —"
        return "\n".join(f"  • {short_name(p)}" for p in lst)

    await bot.send_message(
        chat_id,
        f"{title}\n\n"
        f"🏆 <b>G'oliblar:</b>\n{plist(winners)}\n\n"
        f"💀 <b>Mag'lublar:</b>\n{plist(losers)}"
    )

    cleanup_game(chat_id)


@router.message(Command("start"))
async def cmd_start_private(message: Message):
    if message.chat.type != "private":
        return

    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].startswith("join_"):
        try:
            chat_id = int(args[1].split("_")[1])
        except (IndexError, ValueError):
            await message.answer("❌ Noto'g'ri havola.")
            return
        await _join_from_pm(message, chat_id)
        return

    await message.answer(
        "👋 Salom! Men <b>Imposter</b> o'yini botiman.\n\n"
        "Guruhga qo'shing va guruhda <b>/game</b> buyrug'ini yozing!\n\n"
        "✅ Endi o'yin boshlanishi bilan so'zingizni qabul qilishga tayyorsiz!"
    )


async def _join_from_pm(message: Message, chat_id: int):
    """Guruhdan join tugmasi bosilganda PMda chaqiriladi."""
    user_id = message.from_user.id

    game = get_game(chat_id)
    if not game or game["status"] != "registration":
        await message.answer("⚠️ Ro'yxatdan o'tish vaqti tugagan yoki bu guruhda o'yin yo'q.")
        return

    if get_player(chat_id, user_id):
        await message.answer("✅ Siz allaqachon o'yinga qo'shilgansiz!")
        return

    u = message.from_user
    db.execute(
        "INSERT INTO players (chat_id, user_id, username, first_name) VALUES (?, ?, ?, ?)",
        (chat_id, user_id, u.username, u.first_name)
    )
    db.commit()

    count = db.execute(
        "SELECT COUNT(*) FROM players WHERE chat_id=?", (chat_id,)
    ).fetchone()[0]

    await message.answer(
        "✅ <b>O'yinga muvaffaqiyatli qo'shildingiz!</b>\n\n"
        "O'yin boshlanishi bilan so'zingiz shu yerga yuboriladi. Kutib turing! 🎮"
    )
    try:
        await bot.send_message(
            chat_id,
            f"✅ <b>{u.first_name}</b> o'yinga qo'shildi! (Jami: {count} o'yinchi)"
        )
    except Exception:
        pass


@router.message(Command("game"))
async def cmd_game(message: Message):
    if message.chat.type == "private":
        await message.reply("❌ Bu buyruq faqat guruhlarda ishlaydi!")
        return

    chat_id = message.chat.id
    game = get_game(chat_id)

    if game and game["status"] != "idle":
        await message.reply("⚠️ Guruhda allaqachon o'yin ketmoqda!")
        return

    try:
        bot_member = await bot.get_chat_member(chat_id, (await bot.get_me()).id)
        if not isinstance(bot_member, (ChatMemberAdministrator, ChatMemberOwner)):
            await message.reply(
                "❌ Bot guruhda <b>admin</b> bo'lishi kerak!\n"
                "Botni admin qilib, qaytadan urinib ko'ring."
            )
            return
    except Exception:
        pass

    u = message.from_user

    try:
        await bot.send_message(
            u.id,
            "🎮 Siz o'yinni boshladingiz! O'yin boshlanishi bilan so'zingiz shu yerga yuboriladi."
        )
    except TelegramForbiddenError:
        await message.reply(
            f"❌ Avval bot bilan shaxsiy xabarda <b>/start</b> yozing, so'ng qayta urinib ko'ring.\n"
            f"👉 @{BOT_USERNAME}"
        )
        return

    db.execute(
        "INSERT OR REPLACE INTO games (chat_id, status, started_by, round_num, imp_count) "
        "VALUES (?, 'registration', ?, 0, 1)",
        (chat_id, u.id)
    )
    db.execute(
        "INSERT OR REPLACE INTO players (chat_id, user_id, username, first_name) "
        "VALUES (?, ?, ?, ?)",
        (chat_id, u.id, u.username, u.first_name)
    )
    db.commit()

    await message.reply(
        f"🎮 <b>Imposter o'yini boshlandi!</b>\n\n"
        f"Ro'yxatdan o'tish vaqti: <b>{REGISTRATION_TIME} sekund</b>\n"
        f"Minimum ishtirokchi: {MIN_PLAYERS} kishi\n\n"
        f"✅ {u.first_name} qo'shildi (1 o'yinchi)\n\n"
        f"👇 Tugmani bosib, botga o'ting va o'yinga qo'shiling!",
        reply_markup=kb_join(chat_id)
    )

    task = asyncio.create_task(_registration_countdown(chat_id))
    active_tasks[chat_id] = task


async def _registration_countdown(chat_id: int):
    await asyncio.sleep(REGISTRATION_TIME)
    active_tasks.pop(chat_id, None)
    game = get_game(chat_id)
    if not game or game["status"] != "registration":
        return
    count = db.execute(
        "SELECT COUNT(*) FROM players WHERE chat_id=?", (chat_id,)
    ).fetchone()[0]
    await bot.send_message(
        chat_id, f"⏰ Ro'yxatdan o'tish vaqti tugadi! {count} o'yinchi ro'yxatga olindi."
    )
    await end_registration(chat_id)


@router.message(Command("time"))
async def cmd_time(message: Message):
    if message.chat.type == "private":
        return

    chat_id = message.chat.id
    game = get_game(chat_id)

    if not game or game["status"] != "registration":
        await message.reply("⚠️ Hozir ro'yxatdan o'tish davri emas.")
        return

    if not await _is_authorized(message, game):
        await message.reply("❌ Bu buyruq faqat o'yinni boshlagan yoki guruh admini uchun.")
        return

    cancel_task(chat_id)

    await message.reply(f"⏱ Ro'yxatdan o'tish vaqti +{TIME_EXTEND} sekund uzaytirildi!")

    task = asyncio.create_task(_registration_extend(chat_id))
    active_tasks[chat_id] = task


async def _registration_extend(chat_id: int):
    await asyncio.sleep(TIME_EXTEND)
    active_tasks.pop(chat_id, None)
    game = get_game(chat_id)
    if not game or game["status"] != "registration":
        return
    count = db.execute(
        "SELECT COUNT(*) FROM players WHERE chat_id=?", (chat_id,)
    ).fetchone()[0]
    await bot.send_message(
        chat_id, f"⏰ Ro'yxatdan o'tish vaqti tugadi! {count} o'yinchi ro'yxatga olindi."
    )
    await end_registration(chat_id)


@router.message(Command("stop"))
async def cmd_stop(message: Message):
    if message.chat.type == "private":
        return

    chat_id = message.chat.id
    game = get_game(chat_id)

    if not game or game["status"] == "idle":
        await message.reply("Hozir faol o'yin yo'q.")
        return

    if not await _is_authorized(message, game):
        await message.reply("❌ Bu buyruq faqat o'yinni boshlagan yoki guruh admini uchun.")
        return

    cleanup_game(chat_id)
    await message.reply("🛑 O'yin to'xtatildi.")


async def _is_authorized(message: Message, game) -> bool:
    if game["started_by"] == message.from_user.id:
        return True
    try:
        member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        return isinstance(member, (ChatMemberAdministrator, ChatMemberOwner))
    except Exception:
        return False



@router.callback_query(F.data.startswith("vote:elim:"))
async def cb_vote_elim(call: CallbackQuery):
    chat_id = call.message.chat.id
    voter_id = call.from_user.id
    target_id = int(call.data.split(":")[2])

    game = get_game(chat_id)
    if not game or game["status"] != "voting":
        await call.answer("Ovoz berish vaqti tugagan.", show_alert=True)
        return

    player = get_player(chat_id, voter_id)
    if not player or not player["is_alive"]:
        await call.answer("Siz bu o'yinda emassiz.", show_alert=True)
        return

    existing = db.execute(
        "SELECT 1 FROM votes WHERE chat_id=? AND round_num=? AND voter_id=? AND vote_type='elim'",
        (chat_id, game["round_num"], voter_id)
    ).fetchone()
    if existing:
        await call.answer("Siz allaqachon ovoz bergansiz!", show_alert=True)
        return

    if voter_id == target_id:
        await call.answer("O'zingizga ovoz bera olmaysiz!", show_alert=True)
        return

    target = get_player(chat_id, target_id)
    if not target or not target["is_alive"]:
        await call.answer("Bu o'yinchi o'yinda emas.", show_alert=True)
        return

    db.execute(
        "INSERT INTO votes (chat_id, round_num, voter_id, target_id, vote_type) "
        "VALUES (?, ?, ?, ?, 'elim')",
        (chat_id, game["round_num"], voter_id, target_id)
    )
    db.commit()

    await call.answer(f"✅ {short_name(target)}ga ovoz berdingiz!")
    await bot.send_message(
        chat_id,
        f"🗳 <b>{call.from_user.first_name}</b> → <b>{short_name(target)}</b>ga ovoz berdi"
    )

    alive_count = db.execute(
        "SELECT COUNT(*) FROM players WHERE chat_id=? AND is_alive=1", (chat_id,)
    ).fetchone()[0]
    vote_count = db.execute(
        "SELECT COUNT(*) FROM votes WHERE chat_id=? AND round_num=? AND vote_type='elim'",
        (chat_id, game["round_num"])
    ).fetchone()[0]

    if vote_count >= alive_count:
        cancel_task(chat_id)
        await finish_voting(chat_id)



@router.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_group_msg(message: Message):
    if not message.text or message.text.startswith("/"):
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    game = get_game(chat_id)
    if not game or game["status"] != "playing":
        return

    player = get_player(chat_id, user_id)
    if not player or not player["is_alive"]:
        return

    if player["has_written"]:
        try:
            await message.delete()
        except (TelegramBadRequest, Exception):
            pass

        name = message.from_user.first_name
        username = message.from_user.username
        tag = f"@{username}" if username else name

        await bot.send_message(
            chat_id,
            f"⚠️ <b>{name}</b> ({tag}), siz bu aylanada allaqachon so'z yozgansiz!\n"
            f"Har bir aylanada faqat <b>bitta</b> so'z yozish mumkin."
        )
    else:
        db.execute(
            "UPDATE players SET has_written=1 WHERE chat_id=? AND user_id=?",
            (chat_id, user_id)
        )
        db.commit()

        alive = db.execute(
            "SELECT COUNT(*) FROM players WHERE chat_id=? AND is_alive=1", (chat_id,)
        ).fetchone()[0]
        written = db.execute(
            "SELECT COUNT(*) FROM players WHERE chat_id=? AND is_alive=1 AND has_written=1",
            (chat_id,)
        ).fetchone()[0]

        if written >= alive:
            cancel_task(chat_id)
            await bot.send_message(
                chat_id, "✅ Barcha o'yinchilar yozdi! Ovoz berish boshlanmoqda..."
            )
            await start_voting(chat_id)


async def main():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    init_db()
    logger.info(f"Bot ishga tushdi: @{BOT_USERNAME}")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
