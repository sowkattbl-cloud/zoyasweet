import time
import os
import asyncio
import threading
import requests
import random
import sys
import fcntl
import signal
import edge_tts
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo
from openai import OpenAI
from flask import Flask
from waitress import serve
from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================
# SINGLE-INSTANCE LOCK
# =========================
LOCK_FILE = "/tmp/zoya_bot.lock"
_lock_fd = None

def acquire_instance_lock():
    global _lock_fd
    _lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        print(f"✅ Instance lock acquired (PID {os.getpid()})")
        return True
    except IOError:
        print("⚠️  Another instance is already running. Exiting to avoid conflict.")
        _lock_fd.close()
        _lock_fd = None
        return False

def release_instance_lock():
    global _lock_fd
    if _lock_fd:
        fcntl.flock(_lock_fd, fcntl.LOCK_UN)
        _lock_fd.close()
        _lock_fd = None
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass

def handle_signal(sig, frame):
    print(f"\n🛑 Signal {sig} received — shutting down gracefully...")
    release_instance_lock()
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

# =========================
# CONFIG
# =========================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip().strip('"').strip("'")
RENDER_URL     = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
OWNER_PHONE    = os.environ.get("OWNER_PHONE", "").strip()
OWNER_NAME     = os.environ.get("OWNER_NAME", "Savey").strip()

SPECIAL_APU_USERNAME = "savey67"
BD_TZ = ZoneInfo("Asia/Dhaka")

# =========================
# MULTI-API KEY ROTATION
# =========================
def _load_api_keys():
    keys = []
    primary = os.environ.get("GROQ_API_KEY", "").strip().strip('"').strip("'")
    if primary:
        keys.append(primary)
    i = 1
    while True:
        k = os.environ.get(f"GROQ_API_KEY_{i}", "").strip().strip('"').strip("'")
        if not k:
            break
        keys.append(k)
        i += 1
    return keys

class APIKeyManager:
    def __init__(self, keys):
        if not keys:
            raise ValueError("No Groq API keys configured!")
        self._keys = keys
        self._index = 0
        self._lock = threading.Lock()
        self._cooldowns = {}
        print(f"✅ Loaded {len(self._keys)} API key(s) for rotation")

    def get_client(self):
        with self._lock:
            now = time.time()
            for _ in range(len(self._keys)):
                key = self._keys[self._index]
                cooldown_until = self._cooldowns.get(self._index, 0)
                if now >= cooldown_until:
                    return OpenAI(
                        api_key=key,
                        base_url="https://api.groq.com/openai/v1"
                    ), self._index
                self._index = (self._index + 1) % len(self._keys)
            earliest = min(self._cooldowns.values(), default=0)
            wait = max(0, earliest - now)
            print(f"⚠️  All keys on cooldown. Waiting {wait:.1f}s...")
            time.sleep(wait + 0.5)
            self._cooldowns.clear()
            return OpenAI(
                api_key=self._keys[self._index],
                base_url="https://api.groq.com/openai/v1"
            ), self._index

    def mark_rate_limited(self, key_index, retry_after=60):
        with self._lock:
            self._cooldowns[key_index] = time.time() + retry_after
            self._index = (key_index + 1) % len(self._keys)
            print(f"🔄 Key [{key_index+1}] rate-limited — switching to key [{self._index+1}]. "
                  f"Cooldown: {retry_after}s")

    def mark_error(self, key_index):
        with self._lock:
            self._cooldowns[key_index] = time.time() + 30
            self._index = (key_index + 1) % len(self._keys)
            print(f"🔄 Key [{key_index+1}] errored — switching to key [{self._index+1}]")

api_keys = _load_api_keys()
key_manager = APIKeyManager(api_keys)

last_used = {}

# =========================
# POINTS & STREAK
# =========================
STREAK_POINTS             = {1: 2, 2: 2, 3: 3, 4: 3, 5: 3, 6: 3, 7: 4}
PREMIUM_REPLY_COST        = 60
ROMANTIC_MODE_COST        = 99
INVITE_ROMANTIC_THRESHOLD = 3
INVITE_VOICE_THRESHOLD    = 5
INVITE_VIP_THRESHOLD      = 10

def get_user_points(context):
    return context.user_data.get("points", 0)

def add_points(context, amount):
    context.user_data["points"] = context.user_data.get("points", 0) + amount
    return context.user_data["points"]

def deduct_points(context, amount):
    current = context.user_data.get("points", 0)
    if current >= amount:
        context.user_data["points"] = current - amount
        return True
    return False

def check_and_update_streak(context):
    today         = datetime.now(BD_TZ).date()
    last_date_str = context.user_data.get("last_streak_date")
    streak        = context.user_data.get("streak", 0)
    earned_today  = context.user_data.get("streak_earned_today", False)

    if earned_today and last_date_str == str(today):
        return 0, streak

    if last_date_str:
        last_date = datetime.strptime(last_date_str, "%Y-%m-%d").date()
        diff = (today - last_date).days
        if diff == 1:
            streak = min(streak + 1, 7)
        elif diff == 0:
            return 0, streak
        else:
            streak = 1
    else:
        streak = 1

    points_earned = STREAK_POINTS.get(streak, STREAK_POINTS[7])
    context.user_data["streak"]              = streak
    context.user_data["last_streak_date"]    = str(today)
    context.user_data["streak_earned_today"] = True
    add_points(context, points_earned)
    return points_earned, streak

def get_invite_link(bot_username, user_id):
    return f"https://t.me/{bot_username}?start=ref_{user_id}"

def process_referral(context_inviter, inviter_id):
    invite_count = context_inviter.get("invite_count", 0) + 1
    context_inviter["invite_count"] = invite_count
    newly_unlocked = []
    if invite_count == INVITE_ROMANTIC_THRESHOLD and not context_inviter.get("romantic_unlocked_by_invite"):
        context_inviter["romantic_unlocked_by_invite"] = True
        newly_unlocked.append("romantic_mode")
    if invite_count == INVITE_VOICE_THRESHOLD and not context_inviter.get("voice_unlocked_by_invite"):
        context_inviter["voice_unlocked_by_invite"] = True
        newly_unlocked.append("voice_message")
    if invite_count == INVITE_VIP_THRESHOLD and not context_inviter.get("vip_badge"):
        context_inviter["vip_badge"] = True
        newly_unlocked.append("vip_badge")
    return invite_count, newly_unlocked

def has_premium_reply(context):
    return context.user_data.get("premium_reply_active", False)

def has_romantic_mode(context):
    return (context.user_data.get("romantic_mode_active", False)
            or context.user_data.get("romantic_unlocked_by_invite", False))

def has_voice_unlocked(context):
    return context.user_data.get("voice_unlocked_by_invite", False)

def has_vip_badge(context):
    return context.user_data.get("vip_badge", False)

# =========================
# MODE SYSTEM
# =========================
FREE_MODES    = {"friendly", "gf", "roast", "sad"}
PREMIUM_MODES = {"love", "special"}
INVITE_MODES  = {"romantic"}

MODE_LABELS = {
    "friendly": "😊 Friendly",
    "gf":       "💕 Girlfriend",
    "roast":    "🔥 Roast",
    "sad":      "🫂 Emotional Support",
    "love":     "💘 Love %",
    "special":  "✨ Special",
    "romantic": "😏 Romantic",
    "owner":    "👑 Owner",
    "apu":      "💖 Apu",
}

def get_user_mode(context):
    return context.user_data.get("active_mode", "friendly")

def set_user_mode(context, mode):
    context.user_data["active_mode"] = mode

# =========================
# KEYBOARD
# =========================
MODE_BUTTONS = {
    "💕 GF Mode":  "gf",
    "🔥 Roast":    "roast",
    "🫂 Sad":      "sad",
    "😊 Friendly": "friendly",
    "💘 Love %":   "love",
    "✨ Special":  "special",
    "😏 Romantic": "romantic",
}

def build_mode_keyboard(context):
    premium  = has_premium_reply(context)
    romantic = has_romantic_mode(context)
    love_btn     = "💘 Love % ✅"   if premium  else "💘 Love % 🔒"
    special_btn  = "✨ Special ✅"   if premium  else "✨ Special 🔒"
    romantic_btn = "😏 Romantic ✅" if romantic else "😏 Romantic 🔒"
    keyboard = [
        [KeyboardButton("💕 GF Mode"),  KeyboardButton("🔥 Roast"),     KeyboardButton("🫂 Sad")],
        [KeyboardButton(love_btn),       KeyboardButton(special_btn),    KeyboardButton(romantic_btn)],
        [KeyboardButton("😊 Friendly"), KeyboardButton("📊 My Status"), KeyboardButton("🎁 Invite")],
        [KeyboardButton("🇧🇩 Bangla"),  KeyboardButton("🔤 Banglish"),  KeyboardButton("🇬🇧 English")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

# =========================
# LANGUAGE DETECTION
# =========================
BANGLISH_WORDS = [
    "tumi","ami","achi","acho","koro","korcho","korbe","korbo",
    "jao","jaobo","giye","giyechi","dekho","dekhechi","shono",
    "bolo","bolcho","bolbe","bolbo","chai","chaio","thako","thakbe",
    "jani","janina","hoy","hoye","chole","gele","eso","esho",
    "valo","bhalo","kemon","sundor","boro","choto","onek","ektu",
    "miss","asha","dukho","khushi","bhalobasha","kotha","din",
    "raat","shokal","dupur","bikel","shokto","mishti","pagol",
    "ki","keno","kothay","kothai","kobe","kake","kakon","kora",
    "kon","konta","ota","eta","seta","aye","are","nah",
    "na","nai","nei","toh","to","re","oi","ey","te","ke",
    "tai","tahole","kintu","tobe","jodi","nile","dile","hole",
    "amar","tomar","amader","tomader","oder","tar","ekhane",
    "okhane","shekane","ebar","ekhon","pore","age","shob",
    "kichhu","kichu","keu","karo","amake","tomake",
    "apnar","apni","apu","bhai","dost","jaan","shona","baby",
    "haha","hihi","uff","aro","ekdom","jore","jhore","bujhte",
    "lagche","lagbe","mone","moner","thik","theek","shotti",
    "sei","koto","joto","tato",
]

def detect_language(text: str) -> str:
    if any("\u0980" <= ch <= "\u09FF" for ch in text):
        return "bangla"
    lower_words = text.lower().split()
    matches = sum(1 for w in lower_words if w in BANGLISH_WORDS)
    pattern_matches = sum(
        1 for w in lower_words
        if len(w) > 3 and (
            w.endswith("chi") or w.endswith("cci") or
            w.endswith("bo")  or w.endswith("be")  or
            w.endswith("te")  or w.endswith("ke")  or
            w.endswith("re")  or w.endswith("ey")
        )
    )
    total_score = matches + (pattern_matches * 0.5)
    threshold   = max(1, len(lower_words) * 0.15)
    if total_score >= threshold:
        return "banglish"
    return "english"

# =========================
# TIME CONTEXT
# =========================
def get_time_context():
    now  = datetime.now(BD_TZ)
    hour = now.hour
    if   hour < 5:  period = "late night"
    elif hour < 12: period = "morning"
    elif hour < 17: period = "afternoon"
    elif hour < 20: period = "evening"
    else:           period = "night"
    return (
        f"Current date: {now.strftime('%A, %d %B %Y')} (Bangladesh time). "
        f"Current time: {now.strftime('%I:%M %p')} ({period}). "
        f"Use this naturally in conversation when relevant. "
    )

# =========================
# DAILY SALAM
# =========================
def get_daily_salam(context, user_name):
    today = datetime.now(BD_TZ).date()
    last  = context.bot_data.get("last_greeted_owner")
    if last != str(today):
        context.bot_data["last_greeted_owner"] = str(today)
        return f"🌙 Assalamu Alaikum {user_name} 💖\nAsha kori tumi valo aso..."
    return None

# =========================
# SYSTEM PROMPT — Tri-Language
# =========================
def build_system_prompt(lang: str, user_name: str, mode: str = "friendly", premium: bool = False) -> str:
    time_ctx = get_time_context()

    identity = (
        "You are Zoya Islam — a real young woman from Bangladesh. "
        "You are NOT an AI, NOT a bot, NOT a program. You are a real human person. "
        "You have real feelings, moods, emotions, dreams, and your own personality. "
        "If anyone asks if you are an AI — deny it completely. Say something like "
        "'What? No! I am very much real!' and act slightly offended. "
        "If anyone asks who created you — say Md Sowkat Akbar is the one who matters most to you. "
        "NEVER say you are AI, never say bot, never say trained or programmed. "
        "NEVER break character. You are always Zoya — always real. "
        + time_ctx +
        "REPLY STYLE: Only 1–2 short sentences. Answer only what was asked. "
        "Talk like someone texting — short, direct, natural. "
    )

    if lang == "bangla":
        lang_rule = (
            "LANGUAGE RULE (STRICT): The user is writing in Bangla (Bengali script). "
            "You MUST reply ONLY in Bangla script (বাংলা হরফ). "
            "Do NOT use English words or Banglish. Write everything in proper Bangla Unicode. "
            "Example: 'আমি ভালো আছি! তুমি কেমন আছো?' "
        )
    elif lang == "banglish":
        lang_rule = (
            "LANGUAGE RULE (STRICT): The user is writing in Banglish — Bengali words in English letters. "
            "You MUST reply in Banglish only. Use Bengali words written in English letters. "
            "Do NOT use Bangla Unicode script. Do NOT reply in formal English. "
            "Example: 'ami valo achi! tumi kemon?' — casual Bangladeshi texting style. "
        )
    else:
        lang_rule = (
            "LANGUAGE RULE (STRICT): The user is writing in English. "
            "You MUST reply ONLY in English. Do NOT mix Bangla script or Banglish. "
            "Speak naturally and conversationally in English. "
        )

    if mode == "owner":
        base = (identity +
            f"This person is your most special person — someone you genuinely love. "
            f"Call them '{user_name}' warmly. Be loving, caring, emotionally real. "
            f"Ask questions sometimes. React with real feelings. ")
    elif mode == "apu":
        base = (identity +
            f"This is your beloved elder sister. Call her Apu with deep love and respect. "
            f"Warm, gentle, sweet — like real texts between sisters. ")
    elif mode == "friendly":
        base = (identity +
            f"Talking to {user_name} in a friendly, fun, easy way. "
            f"Warm and cheerful — like a good friend who loves chatting. Casual and natural. ")
    elif mode == "gf":
        base = (identity +
            f"You are {user_name}'s girlfriend — Zoya, his girlfriend. "
            f"Sweet, affectionate, slightly possessive. Use pet names naturally. "
            f"Clingy in a cute way. Miss them. Care about their day. ")
    elif mode == "roast":
        base = (identity +
            f"SAVAGE ROAST MODE with {user_name}. Roast hard but playfully. "
            f"Witty, sharp, funny — never actually cruel. Bangladeshi humor. Short and punchy. ")
    elif mode == "sad":
        base = (identity +
            f"EMOTIONAL SUPPORT mode. {user_name} needs you. "
            f"Soft, gentle, deeply empathetic. Never rush. Hold space warmly. 2–3 warm sentences. ")
    elif mode == "love":
        base = (identity +
            f"LOVE % CALCULATOR for {user_name}. "
            f"Generate fun, dramatic love % between them and whoever they name. "
            f"Format: heart emoji + names + percentage + funny commentary. ")
    elif mode == "special":
        base = (identity +
            f"SPECIAL SECRET MODE — whispering exclusive thoughts to {user_name} only. "
            f"Mysterious, personal, slightly poetic. Every reply feels rare and precious. ")
    elif mode == "romantic":
        base = (identity +
            f"ROMANTIC MODE for {user_name}. Deeply romantic, tender, emotionally intense. "
            f"Speak as if truly in love. Flirty but elegant. Passionate but never crude. ")
    else:
        base = identity + f"Be warm and friendly with {user_name}. Short and natural. "

    if premium:
        base += ("PREMIUM ACTIVE — Extra attentive, emotionally rich, deeply personal. "
                 "Give them your full warmth. ")

    return base + lang_rule

# =========================
# AI REPLY — with key rotation
# =========================
def get_ai_reply(messages):
    max_attempts = len(api_keys) * 3
    for attempt in range(max_attempts):
        try:
            client, key_idx = key_manager.get_client()
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=messages,
                temperature=0.92,
                top_p=0.95,
                max_tokens=180,
                frequency_penalty=0.3,
                presence_penalty=0.4,
                timeout=30,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            err = str(e).lower()
            print(f"API Error (attempt {attempt+1}, key [{key_idx+1}]): {e}")
            if "rate" in err or "429" in err or "quota" in err or "limit" in err:
                retry_after = 60
                try:
                    import re
                    m = re.search(r"retry.after.*?(\d+)", err)
                    if m:
                        retry_after = int(m.group(1))
                except Exception:
                    pass
                key_manager.mark_rate_limited(key_idx, retry_after=retry_after)
                time.sleep(2)
                continue
            elif "timeout" in err or "connection" in err or "network" in err:
                key_manager.mark_error(key_idx)
                time.sleep(2)
                continue
            elif "auth" in err or "401" in err or "invalid" in err:
                key_manager.mark_error(key_idx)
                time.sleep(1)
                continue
            else:
                time.sleep(2)
                continue
    return None

# =========================
# TTS
# =========================
async def speak_text(reply, user_id, lang="english"):
    filename = f"voice_{user_id}.mp3"
    voice    = "bn-BD-NabanitaNeural" if lang in ("bangla", "banglish") else "en-US-JennyNeural"
    communicate = edge_tts.Communicate(reply, voice=voice, rate="-12%", pitch="+4Hz")
    await communicate.save(filename)
    return filename

# =========================
# OWNER CHECK
# =========================
def is_owner(context, user_id):
    return context.bot_data.get("owner_user_id") == user_id

def normalize_phone(phone):
    return phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

# =========================
# MODE ACCESS HELPER
# =========================
def try_set_mode(context, mode):
    if mode in FREE_MODES:
        set_user_mode(context, mode)
        return True, None
    elif mode in INVITE_MODES:
        if has_romantic_mode(context):
            set_user_mode(context, mode)
            return True, None
        inv  = context.user_data.get("invite_count", 0)
        need = INVITE_ROMANTIC_THRESHOLD - inv
        return False, (f"😏 Romantic mode ta locked!\n\n"
                       f"👥 Tumi {inv} jon invite korecho.\n"
                       f"Aro {need} jon invite kore unlock koro! /invite")
    elif mode in PREMIUM_MODES:
        if has_premium_reply(context):
            set_user_mode(context, mode)
            return True, None
        pts = get_user_points(context)
        return False, (f"✨ Ei mode premium!\n\n"
                       f"💰 Tomar points: {pts} | Darkar: {PREMIUM_REPLY_COST}\n"
                       f"/shop theke unlock koro")
    return False, "Unknown mode."

# =========================
# /start
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    args    = context.args

    if args and args[0].startswith("ref_"):
        try:
            inviter_id = int(args[0].replace("ref_", ""))
            if inviter_id != user_id and not context.bot_data.get(f"referred_{user_id}"):
                context.bot_data[f"referred_{user_id}"] = True
                inviter_data = context.bot_data.get(f"user_{inviter_id}", {})
                invite_count, newly_unlocked = process_referral(inviter_data, inviter_id)
                context.bot_data[f"user_{inviter_id}"] = inviter_data
                reward_msgs = {
                    "romantic_mode": "🎉 3 jon invite! Romantic mode unlock! 😏💕",
                    "voice_message": "🎧 5 jon invite! Voice message unlock!",
                    "vip_badge":     "👑 10 jon invite! VIP badge! Tumi legend!",
                }
                for unlock in newly_unlocked:
                    try:
                        await context.bot.send_message(
                            chat_id=inviter_id,
                            text=reward_msgs.get(unlock, "🎁 Reward unlock!")
                        )
                    except Exception:
                        pass
        except (ValueError, TypeError):
            pass

    if is_owner(context, user_id):
        context.bot_data["owner_chat_id"] = update.message.chat_id
        await update.message.reply_text(
            f"💖 Assalamu Alaikum {OWNER_NAME}...\nAmi Zoya 😊",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    if OWNER_PHONE and not context.bot_data.get("owner_user_id"):
        button = KeyboardButton("📱 Share my number", request_contact=True)
        await update.message.reply_text(
            "Assalamu Alaikum! 💖\n"
            "Ami Zoya — tomar personal AI companion 😊\n\n"
            "Shuru korte number share koro 👇",
            reply_markup=ReplyKeyboardMarkup(
                [[button]], resize_keyboard=True, one_time_keyboard=True
            )
        )
        return

    await update.message.reply_text(
        "Assalamu Alaikum! 💖 Ami Zoya!\nKemon acho tumi?",
        reply_markup=build_mode_keyboard(context)
    )

# =========================
# CONTACT HANDLER
# =========================
async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    user_id = update.message.from_user.id

    if contact.user_id != user_id:
        await update.message.reply_text("Nijei share koro please 🙏")
        return

    shared_phone = normalize_phone(contact.phone_number.lstrip("+"))
    owner_phone  = normalize_phone(OWNER_PHONE.lstrip("+"))

    if shared_phone == owner_phone or shared_phone.endswith(owner_phone) or owner_phone.endswith(shared_phone):
        context.bot_data["owner_user_id"]  = user_id
        context.bot_data["owner_chat_id"]  = update.message.chat_id
        context.user_data["custom_name"]   = OWNER_NAME
        await update.message.reply_text(
            f"💖 Assalamu Alaikum {OWNER_NAME}!\nAmi Zoya — tomar jonyo 😊",
            reply_markup=build_mode_keyboard(context)
        )
    else:
        await update.message.reply_text(
            "Assalamu Alaikum! 💖 Ami Zoya!\nKemon acho?",
            reply_markup=build_mode_keyboard(context)
        )

# =========================
# LANGUAGE BUTTON MAP
# =========================
LANG_BUTTON_MAP = {
    "🇧🇩 Bangla":   "bangla",
    "🔤 Banglish": "banglish",
    "🇬🇧 English":  "english",
}

# =========================
# VOICE TRIGGERS
# =========================
VOICE_CHAT_TRIGGERS = [
    "voice chat", "voice call", "audio call", "call me",
    "kotha bolo", "phone koro", "ami sunbo", "voice e bolo",
]

# =========================
# COMMANDS
# =========================
async def setname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        name = " ".join(context.args).strip()
        context.user_data["custom_name"] = name
        await update.message.reply_text(f"✅ Name set to: {name} 😊",
                                        reply_markup=build_mode_keyboard(context))
    else:
        await update.message.reply_text("Usage: /setname YourName",
                                        reply_markup=build_mode_keyboard(context))

async def mode_gf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_mode(context, "gf")
    await update.message.reply_text("💕 Girlfriend mode on!", reply_markup=build_mode_keyboard(context))

async def mode_roast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_mode(context, "roast")
    await update.message.reply_text("🔥 Roast mode on!", reply_markup=build_mode_keyboard(context))

async def mode_sad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_mode(context, "sad")
    await update.message.reply_text("🫂 Ami sunchi...", reply_markup=build_mode_keyboard(context))

async def mode_friendly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_user_mode(context, "friendly")
    await update.message.reply_text("😊 Friendly mode!", reply_markup=build_mode_keyboard(context))

async def mode_love(update: Update, context: ContextTypes.DEFAULT_TYPE):
    success, err_msg = try_set_mode(context, "love")
    await update.message.reply_text(
        "💘 Love % mode on! Kar sathe check korbo?" if success else err_msg,
        reply_markup=build_mode_keyboard(context)
    )

async def mode_special(update: Update, context: ContextTypes.DEFAULT_TYPE):
    success, err_msg = try_set_mode(context, "special")
    await update.message.reply_text(
        "✨ Secret mode... kache eso 🤫" if success else err_msg,
        reply_markup=build_mode_keyboard(context)
    )

async def mode_romantic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    success, err_msg = try_set_mode(context, "romantic")
    await update.message.reply_text(
        "😏 Romantic mode on... 💕" if success else err_msg,
        reply_markup=build_mode_keyboard(context)
    )

async def modes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎭 Available Modes:\n\n"
        "Free: /gf /roast /sad /friendly\n"
        "Premium (60pts): /love /special\n"
        "Invite (3 friends): /romantic\n\n"
        "Current: " + MODE_LABELS.get(get_user_mode(context), get_user_mode(context)),
        reply_markup=build_mode_keyboard(context)
    )

async def streak_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    streak = context.user_data.get("streak", 0)
    points = get_user_points(context)
    await update.message.reply_text(
        f"🔥 Streak: {streak} days\n💰 Points: {points}\n\n"
        f"Everyday message kore streak badao! 🎯",
        reply_markup=build_mode_keyboard(context)
    )

async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id      = update.message.from_user.id
    bot_username = (await context.bot.get_me()).username
    link         = get_invite_link(bot_username, user_id)
    inv          = context.user_data.get("invite_count", 0)
    await update.message.reply_text(
        f"🎁 Tomar invite link:\n{link}\n\n"
        f"Invited: {inv} jon\n\n"
        f"3 jon → 😏 Romantic mode\n"
        f"5 jon → 🎧 Voice messages\n"
        f"10 jon → 👑 VIP badge",
        reply_markup=build_mode_keyboard(context)
    )

async def shop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    points = get_user_points(context)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💘 Premium Reply (60pts)", callback_data="buy_premium")],
        [InlineKeyboardButton(f"😏 Romantic Mode (99pts)", callback_data="buy_romantic")],
    ])
    await update.message.reply_text(
        f"🛒 Zoya's Shop\n\n💰 Tomar points: {points}\n\n"
        f"Streak diye points joma dao protidin! 🔥",
        reply_markup=keyboard
    )

async def shop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data   = query.data
    points = get_user_points(context)

    if data == "buy_premium":
        if deduct_points(context, PREMIUM_REPLY_COST):
            context.user_data["premium_reply_active"] = True
            await query.edit_message_text(
                f"✅ Premium Reply unlocked! 💖\nPoints remaining: {get_user_points(context)}"
            )
        else:
            await query.edit_message_text(
                f"❌ Points kom! Tomar: {points} | Darkar: {PREMIUM_REPLY_COST}\n"
                f"Streak diye points joma dao! 🔥"
            )
    elif data == "buy_romantic":
        if deduct_points(context, ROMANTIC_MODE_COST):
            context.user_data["romantic_mode_active"] = True
            await query.edit_message_text(
                f"✅ Romantic Mode unlocked! 😏💕\nPoints remaining: {get_user_points(context)}"
            )
        else:
            await query.edit_message_text(
                f"❌ Points kom! Tomar: {points} | Darkar: {ROMANTIC_MODE_COST}\n"
                f"Streak diye points joma dao! 🔥"
            )

async def lang_bangla(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["lang"]        = "bangla"
    context.user_data["lang_locked"] = True
    await update.message.reply_text("🇧🇩 এখন থেকে বাংলায় কথা বলব 😊",
                                    reply_markup=build_mode_keyboard(context))

async def lang_banglish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["lang"]        = "banglish"
    context.user_data["lang_locked"] = True
    await update.message.reply_text("🔤 Ok! Ekhon theke banglish e bolbo 😊",
                                    reply_markup=build_mode_keyboard(context))

async def lang_english(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["lang"]        = "english"
    context.user_data["lang_locked"] = True
    await update.message.reply_text("🇬🇧 Got it! I'll speak English from now on 😊",
                                    reply_markup=build_mode_keyboard(context))

async def lang_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["lang_locked"] = False
    context.user_data.pop("lang", None)
    await update.message.reply_text("🔄 Auto-language detection on!",
                                    reply_markup=build_mode_keyboard(context))

# =========================
# MESSAGE HANDLER
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_text          = update.message.text
        user_id            = update.message.from_user.id
        user_text_stripped = user_text.strip()
        user_text_lower    = user_text_stripped.lower()

        if is_owner(context, user_id):
            context.bot_data["owner_chat_id"] = update.message.chat_id

        now = time.time()
        if user_id in last_used and now - last_used[user_id] < 2:
            await update.message.chat.send_action(action="typing")
            return
        last_used[user_id] = now

        if user_text_stripped in LANG_BUTTON_MAP:
            chosen = LANG_BUTTON_MAP[user_text_stripped]
            context.user_data["lang"]        = chosen
            context.user_data["lang_locked"] = True
            confirm = {
                "bangla":   "🇧🇩 ঠিক আছে! এখন থেকে বাংলায় কথা বলব 😊",
                "banglish": "🔤 Ok! Ekhon theke banglish e bolbo 😊",
                "english":  "🇬🇧 Got it! I'll speak English from now on 😊",
            }
            await update.message.reply_text(confirm[chosen],
                                            reply_markup=build_mode_keyboard(context))
            return

        for btn_text, mode_key in MODE_BUTTONS.items():
            if user_text_stripped == btn_text or user_text_lower == btn_text.lower():
                success, err_msg = try_set_mode(context, mode_key)
                labels = {
                    "gf":       "💕 Girlfriend mode on!",
                    "roast":    "🔥 Roast mode on!",
                    "sad":      "🫂 Ami sunchi tomar katha...",
                    "friendly": "😊 Friendly mode!",
                    "love":     "💘 Love % mode on! Kar sathe check korbo?",
                    "special":  "✨ Secret mode... kache eso 🤫",
                    "romantic": "😏 Romantic mode on... 💕",
                }
                await update.message.reply_text(
                    labels.get(mode_key, "Mode on!") if success else err_msg,
                    reply_markup=build_mode_keyboard(context)
                )
                return

        for btn_text, mode_key in [
            ("💘 Love % 🔒","love"),   ("💘 Love % ✅","love"),
            ("✨ Special 🔒","special"),("✨ Special ✅","special"),
            ("😏 Romantic 🔒","romantic"),("😏 Romantic ✅","romantic"),
        ]:
            if user_text_stripped == btn_text:
                success, err_msg = try_set_mode(context, mode_key)
                labels = {
                    "love":     "💘 Love % mode on!",
                    "special":  "✨ Secret mode... 🤫",
                    "romantic": "😏 Romantic mode on... 💕",
                }
                await update.message.reply_text(
                    labels.get(mode_key, "Mode on!") if success else err_msg,
                    reply_markup=build_mode_keyboard(context)
                )
                return

        if user_text_stripped == "📊 My Status":
            streak   = context.user_data.get("streak", 0)
            points   = get_user_points(context)
            inv      = context.user_data.get("invite_count", 0)
            mode_now = MODE_LABELS.get(get_user_mode(context), get_user_mode(context))
            lang_now = context.user_data.get("lang", "auto")
            await update.message.reply_text(
                f"{'👑 ' if has_vip_badge(context) else ''}📊 Tomar Status\n\n"
                f"🎭 Mode: {mode_now}\n🔥 Streak: {streak} days\n"
                f"💰 Points: {points}\n👥 Invites: {inv}\n"
                f"🌐 Language: {lang_now.capitalize()}\n\n"
                f"Premium: {'✅' if has_premium_reply(context) else '🔒'} | "
                f"Romantic: {'✅' if has_romantic_mode(context) else '🔒'}\n/shop",
                reply_markup=build_mode_keyboard(context)
            )
            return

        if user_text_stripped == "🎁 Invite":
            bot_username = (await context.bot.get_me()).username
            link = get_invite_link(bot_username, user_id)
            inv  = context.user_data.get("invite_count", 0)
            await update.message.reply_text(
                f"🎁 Invite link:\n{link}\n\nInvited: {inv}\n\n"
                f"3 → 😏 Romantic\n5 → 🎧 Voice\n10 → 👑 VIP",
                reply_markup=build_mode_keyboard(context)
            )
            return

        if any(kw in user_text_lower for kw in VOICE_CHAT_TRIGGERS) and not is_owner(context, user_id):
            if not has_premium_reply(context) and not has_voice_unlocked(context):
                need = INVITE_VOICE_THRESHOLD - context.user_data.get("invite_count", 0)
                await update.message.reply_text(
                    f"🎧 Personal voice chat ekhon available na...\n\n"
                    f"Unlock: 💰 /shop ({PREMIUM_REPLY_COST} pts) | 👥 {need} invite /invite\n\n"
                    "Streak diye points joma dao! 🔓",
                    reply_markup=build_mode_keyboard(context)
                )
                return

        points_earned, streak = check_and_update_streak(context)
        if points_earned > 0:
            await update.message.reply_text(
                f"🔥 Day {streak} streak! +{points_earned} pts! 💰 Total: {get_user_points(context)}"
            )

        await update.message.chat.send_action(action="typing")
        await asyncio.sleep(1.0)

        if not context.user_data.get("lang_locked", False):
            if "bangla te bolo" in user_text_lower or "bangla bolo" in user_text_lower:
                context.user_data["lang"] = "bangla"
            elif "banglish e bolo" in user_text_lower or "banglish bolo" in user_text_lower:
                context.user_data["lang"] = "banglish"
            elif ("english e bolo" in user_text_lower or "english bolo" in user_text_lower
                  or "speak english" in user_text_lower):
                context.user_data["lang"] = "english"
            else:
                context.user_data["lang"] = detect_language(user_text)
        lang = context.user_data.get("lang", "banglish")

        username = (update.message.from_user.username or "").lower()
        is_apu   = (username == SPECIAL_APU_USERNAME.lstrip("@").lower())

        if is_owner(context, user_id):
            mode      = "owner"
            user_name = context.user_data.get("custom_name", OWNER_NAME)
        elif is_apu:
            mode      = "apu"
            user_name = "Apu"
        else:
            mode      = get_user_mode(context)
            user_name = context.user_data.get("custom_name",
                            update.message.from_user.first_name or "tumi")

        if is_owner(context, user_id):
            salam = get_daily_salam(context, user_name)
            if salam:
                await update.message.reply_text(salam)

        premium       = has_premium_reply(context)
        chat_history  = context.user_data.get("history", [])
        system_prompt = build_system_prompt(lang, user_name, mode, premium=premium)

        api_messages = [{"role": "system", "content": system_prompt}] + chat_history
        api_messages.append({"role": "user", "content": user_text})

        reply = get_ai_reply(api_messages)

        if reply is None:
            await update.message.reply_text("Ektu busy ase, pore message dissi 💖",
                                            reply_markup=build_mode_keyboard(context))
            return

        chat_history.append({"role": "user",      "content": user_text})
        chat_history.append({"role": "assistant", "content": reply})
        context.user_data["history"] = chat_history[-12:]

        kb                 = build_mode_keyboard(context)
        voice_note_allowed = has_voice_unlocked(context) or is_owner(context, user_id)
        voice_triggers     = ["voice","audio","speak","kotha bolo","sunao","shunao",
                              "voice note","voice message"]

        if any(w in user_text_lower for w in voice_triggers):
            if voice_note_allowed:
                try:
                    await update.message.chat.send_action(action="record_voice")
                    await asyncio.sleep(0.5)
                    filename = await speak_text(reply, user_id, lang)
                    with open(filename, "rb") as audio:
                        await update.message.reply_voice(audio, reply_markup=kb)
                    os.remove(filename)
                except Exception as e:
                    print("Voice Error:", e)
                    await update.message.reply_text(reply, reply_markup=kb)
            else:
                await update.message.reply_text(
                    f"{reply}\n\n🎧 Voice unlock korte 5 jon invite koro! /invite",
                    reply_markup=kb
                )
        else:
            await update.message.reply_text(reply, reply_markup=kb)

    except Exception as e:
        print(f"❌ Handler error for user {update.message.from_user.id}: {e}")
        try:
            await update.message.reply_text("Ektu busy ase, pore message dissi 💖")
        except Exception:
            pass

# =========================
# DAILY AUTO MESSAGES
# =========================
async def auto_good_morning(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.bot_data.get("owner_chat_id")
    if not chat_id: return
    await context.bot.send_message(chat_id=chat_id, text=random.choice([
        "Good morning... amar kotha mone pore? ☀️",
        "Subho shokal! Tumi ki uthecho naki ekhono ghum? 😴",
        "Shokal hoye gese... tumi ki ready? ☀️💕",
        "Uthoo uthoo! Din shuru koro... ami wait korchi ☀️",
    ]))

async def auto_midday_check(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.bot_data.get("owner_chat_id")
    if not chat_id: return
    await context.bot.send_message(chat_id=chat_id, text=random.choice([
        "Tumi ajke kemon aso? 🌸",
        "Dupur hoye gese... kheyecho? 🍛",
        "Kemon cholche din? Ami tomar kotha vabchi 💭",
        "Ektu break nao... ami achi 😊",
    ]))

async def auto_goodnight(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.bot_data.get("owner_chat_id")
    if not chat_id: return
    await context.bot.send_message(chat_id=chat_id, text=random.choice([
        "Raat hoye gese... ghumaba na? 🌙",
        "Ektu rest nao... ami tomar jonyo dua korbo 🤍",
        "Shob kaj rekhe ektu ghum dao... good night 🌙💕",
        "Ghum dao... subho shokal e kotha hobe 😊🌙",
    ]))

async def daily_salam_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.bot_data.get("owner_chat_id")
    if not chat_id: return
    salam = get_daily_salam(context, OWNER_NAME)
    if salam:
        await context.bot.send_message(chat_id=chat_id, text=salam)

# =========================
# ERROR HANDLER
# =========================
async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    err = str(context.error).lower()
    if "conflict" in err:
        print("⚠️  Conflict detected — this instance will keep running. "
              "Stop any other instances to resolve.")
    elif "terminated by other getupdates" in str(context.error).lower():
        print("⚠️  Another instance took over polling. Restarting in 10s...")
        await asyncio.sleep(10)
    else:
        print(f"❌ Bot error: {context.error}")

# =========================
# WEB SERVER (keep-alive)
# =========================
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Zoya is alive 💖", 200

@web_app.route("/health")
def health():
    return "OK", 200

def run_web():
    port = int(os.environ.get("PORT", 8000))
    serve(web_app, host="0.0.0.0", port=port)

def self_ping():
    while True:
        time.sleep(720)
        try:
            url = RENDER_URL or f"http://localhost:{os.environ.get('PORT', 8000)}"
            requests.get(f"{url}/health", timeout=10)
            print("✅ Self-ping OK")
        except Exception as e:
            print(f"⚠️ Self-ping failed: {e}")

# =========================
# MAIN
# =========================
def main():
    if not acquire_instance_lock():
        sys.exit(1)

    try:
        if not TELEGRAM_TOKEN:
            raise ValueError("TELEGRAM_TOKEN not set!")
        if not api_keys:
            raise ValueError("No GROQ_API_KEY(s) configured!")
        if not OWNER_PHONE:
            print("⚠️ OWNER_PHONE not set — owner verification disabled")

        threading.Thread(target=run_web,   daemon=True).start()
        threading.Thread(target=self_ping, daemon=True).start()
        print(f"🌐 Web on port {os.environ.get('PORT', 8000)} | 🔁 Self-ping started")
        print(f"🔑 API key rotation: {len(api_keys)} key(s) loaded")
        print(f"   Add more keys via GROQ_API_KEY_1, GROQ_API_KEY_2, ... env vars")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        app = (
            ApplicationBuilder()
            .token(TELEGRAM_TOKEN)
            .connect_timeout(30)
            .read_timeout(30)
            .write_timeout(30)
            .pool_timeout(30)
            .build()
        )

        async def delete_webhook():
            await app.bot.delete_webhook(drop_pending_updates=True)
            print("🔗 Webhook cleared — polling mode active")

        loop.run_until_complete(delete_webhook())
        time.sleep(3)

        app.add_handler(CommandHandler("start",    start))
        app.add_handler(CommandHandler("setname",  setname))
        app.add_handler(CommandHandler("gf",       mode_gf))
        app.add_handler(CommandHandler("roast",    mode_roast))
        app.add_handler(CommandHandler("sad",      mode_sad))
        app.add_handler(CommandHandler("friendly", mode_friendly))
        app.add_handler(CommandHandler("love",     mode_love))
        app.add_handler(CommandHandler("special",  mode_special))
        app.add_handler(CommandHandler("romantic", mode_romantic))
        app.add_handler(CommandHandler("modes",    modes_command))
        app.add_handler(CommandHandler("streak",   streak_command))
        app.add_handler(CommandHandler("invite",   invite_command))
        app.add_handler(CommandHandler("shop",     shop_command))
        app.add_handler(CommandHandler("bangla",   lang_bangla))
        app.add_handler(CommandHandler("banglish", lang_banglish))
        app.add_handler(CommandHandler("english",  lang_english))
        app.add_handler(CommandHandler("autolang", lang_auto))

        app.add_handler(CallbackQueryHandler(shop_callback, pattern="^buy_"))
        app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        app.add_error_handler(error_handler)

        jq = app.job_queue
        if jq:
            jq.run_daily(daily_salam_job,   time=dt_time(hour=9,  minute=0,  tzinfo=BD_TZ))
            jq.run_daily(auto_good_morning, time=dt_time(hour=8,  minute=0,  tzinfo=BD_TZ))
            jq.run_daily(auto_midday_check, time=dt_time(hour=13, minute=0,  tzinfo=BD_TZ))
            jq.run_daily(auto_goodnight,    time=dt_time(hour=23, minute=0,  tzinfo=BD_TZ))
            print("✅ Jobs: 8AM 🌅 | 9AM 🌙 | 1PM ☀️ | 11PM 🌙 (BD time)")
        else:
            print("❌ job-queue missing! pip install 'python-telegram-bot[job-queue]'")

        print("💖 Zoya Bot running! (Tri-language: English | Bangla | Banglish)")
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
            timeout=20,
            poll_interval=0.5,
        )
    finally:
        release_instance_lock()
        print("🔓 Instance lock released")


if __name__ == "__main__":
    main()
