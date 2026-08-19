from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import Conflict
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from datetime import datetime, timedelta
import fcntl
import json
import os
import pytz
import signal
import sys
import traceback

TIMEZONE = pytz.timezone("Asia/Jakarta")
TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
]


def env_int(name, default=0):
    value = os.getenv(name, str(default)).strip()
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


GROUP_ID = env_int("GROUP_ID", 0)
DATA_DIR = os.getenv("DATA_DIR", "/data").strip() or "/data"
os.makedirs(DATA_DIR, exist_ok=True)

MEMBER_FILE = os.path.join(DATA_DIR, "members.json")
ABSEN_FILE = os.path.join(DATA_DIR, "absensi.json")
GROUP_FILE = os.path.join(DATA_DIR, "groups.json")
SHIFT_FILE = os.path.join(DATA_DIR, "shift_history.json")
NOTIF_FILE = os.path.join(DATA_DIR, "notification_history.json")
LOCK_FILE = os.path.join(DATA_DIR, "bot_instance.lock")

# Satu shift saja.
# Absensi dibuka 1 jam sebelum batas normal 11:15 WIB.
# Setelah 11:15:59 tetap boleh absen sebagai MISTAKE/TELAT sampai 13:14:59.
# Tepat 13:15:00 absensi ditutup dan laporan dikirim.
SHIFT_KEY = "shift_utama"
SHIFT_CONFIG = {
    SHIFT_KEY: {
        "label": "SHIFT UTAMA",
        "button": "📝 ABSEN",
        "mulai_jam": 10,
        "mulai_menit": 15,
        "batas_jam": 11,
        "batas_menit": 15,
        "notif_jam": 13,
        "notif_menit": 15,
    }
}

DENDA_PER_MENIT = 50000

members = {}
absensi = {}
allowed_groups = {}
shift_history = {}
notification_history = {}
_instance_lock_handle = None


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception as exc:
        print(f"PERINGATAN: gagal membaca {path}: {exc}")
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp_path, path)


def save_members():
    save_json(MEMBER_FILE, members)


def save_absensi():
    save_json(ABSEN_FILE, absensi)


def save_groups():
    save_json(GROUP_FILE, allowed_groups)


def save_shift_history():
    save_json(SHIFT_FILE, shift_history)


def save_notification_history():
    save_json(NOTIF_FILE, notification_history)


def load_data():
    global members, absensi, allowed_groups, shift_history, notification_history

    members = load_json(MEMBER_FILE, {})
    absensi = load_json(ABSEN_FILE, {})
    allowed_groups = load_json(GROUP_FILE, {})
    shift_history = load_json(SHIFT_FILE, {})
    notification_history = load_json(NOTIF_FILE, {})

    if not isinstance(members, dict):
        members = {}
    if not isinstance(absensi, dict):
        absensi = {}
    if not isinstance(allowed_groups, dict):
        allowed_groups = {}
    if not isinstance(shift_history, dict):
        shift_history = {}
    if not isinstance(notification_history, dict):
        notification_history = {}

    # Migrasi seluruh data shift lama menjadi satu shift utama.
    new_shift_history = {}
    for uid in set(list(shift_history.keys()) + list(members.keys())):
        new_shift_history[str(uid)] = SHIFT_KEY
    if new_shift_history != shift_history:
        shift_history = new_shift_history
        save_shift_history()

    # Migrasi absensi lama. Jika seorang staff ada di beberapa shift pada tanggal
    # yang sama, gunakan record paling awal agar tidak terjadi double absensi.
    changed_absen = False
    for tanggal, day_data in list(absensi.items()):
        if not isinstance(day_data, dict):
            absensi[tanggal] = {SHIFT_KEY: {}}
            changed_absen = True
            continue

        merged = {}
        for old_shift, records in list(day_data.items()):
            if not isinstance(records, dict):
                continue
            for uid, record in records.items():
                if uid not in merged:
                    merged[uid] = record
                    continue
                old_jam = str(merged[uid].get("jam", "99:99:99")) if isinstance(merged[uid], dict) else "99:99:99"
                new_jam = str(record.get("jam", "99:99:99")) if isinstance(record, dict) else "99:99:99"
                if new_jam < old_jam:
                    merged[uid] = record

        normalized = {}
        for uid, record in merged.items():
            if not isinstance(record, dict):
                record = {}
            fixed = dict(record)
            fixed["shift"] = SHIFT_KEY
            normalized[str(uid)] = fixed

        if day_data != {SHIFT_KEY: normalized}:
            absensi[tanggal] = {SHIFT_KEY: normalized}
            changed_absen = True

    if changed_absen:
        save_absensi()


def get_today_key():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def ensure_today():
    today = get_today_key()
    changed = False

    if today not in absensi or not isinstance(absensi.get(today), dict):
        absensi[today] = {}
        changed = True
    if SHIFT_KEY not in absensi[today] or not isinstance(absensi[today].get(SHIFT_KEY), dict):
        absensi[today][SHIFT_KEY] = {}
        changed = True
    if changed:
        save_absensi()

    if today not in notification_history or not isinstance(notification_history.get(today), dict):
        notification_history[today] = {}
        save_notification_history()

    return today


def rupiah(nominal):
    return f"Rp{int(nominal):,}".replace(",", ".")


def is_owner_admin(user_id):
    return int(user_id) in ADMIN_IDS


def is_group_allowed(chat_id):
    if GROUP_ID and int(chat_id) == GROUP_ID:
        return True
    return str(chat_id) in allowed_groups


def shift_time_text():
    config = SHIFT_CONFIG[SHIFT_KEY]
    return (
        f"Buka {config['mulai_jam']:02d}:{config['mulai_menit']:02d} WIB | "
        f"Paling lambat {config['batas_jam']:02d}:{config['batas_menit']:02d} WIB"
    )


def register_member(user, chat):
    uid = str(user.id)
    members[uid] = {
        "id": user.id,
        "nama": user.full_name,
        "username": user.username or "",
        "group_id": chat.id,
        "group_name": chat.title or "",
        "last_seen": datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
    }
    shift_history[uid] = SHIFT_KEY
    save_members()
    save_shift_history()


async def kirim_admin(context, pesan):
    safe_pesan = str(pesan).replace("*", "").replace("`", "")
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=safe_pesan)
        except Exception as exc:
            print(f"GAGAL KIRIM ADMIN {admin_id}: {exc}")


async def post_init(application: Application):
    # Pastikan mode polling tidak bentrok dengan webhook lama.
    await application.bot.delete_webhook(drop_pending_updates=True)
    me = await application.bot.get_me()
    print(f"LOGIN BOT OK: @{me.username or me.id}")


async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    event = update.my_chat_member
    if not event:
        return

    chat = event.chat
    from_user = event.from_user
    if chat.type not in ["group", "supergroup"]:
        return

    new_status = event.new_chat_member.status
    if new_status not in ["member", "administrator"]:
        return

    if is_owner_admin(from_user.id):
        allowed_groups[str(chat.id)] = {
            "id": chat.id,
            "title": chat.title or "",
            "added_by_id": from_user.id,
            "added_by_name": from_user.full_name,
            "created_at": datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_groups()
        await kirim_admin(
            context,
            "✅ BOT ABSENSI BERHASIL DIAKTIFKAN\n\n"
            f"👥 Grup: {chat.title or '-'}\n"
            f"🆔 Group ID: {chat.id}\n"
            f"👤 Ditambahkan oleh: {from_user.full_name}",
        )
    else:
        await kirim_admin(
            context,
            "🚨 BOT DITAMBAHKAN OLEH NON ADMIN UTAMA\n\n"
            f"👥 Grup: {chat.title or '-'}\n"
            f"🆔 Group ID: {chat.id}\n"
            f"👤 Oleh: {from_user.full_name}\n"
            f"🆔 User ID: {from_user.id}\n\n"
            "Bot otomatis keluar dari grup.",
        )
        try:
            await context.bot.leave_chat(chat.id)
        except Exception:
            pass


async def track_member(update: Update, context: ContextTypes.DEFAULT_TYPE = None):
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user or user.is_bot:
        return
    if chat.type not in ["group", "supergroup"] or not is_group_allowed(chat.id):
        return
    register_member(user, chat)


async def start_absensi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or not update.message:
        return

    if chat.type not in ["group", "supergroup"]:
        await update.message.reply_text("❌ Bot absensi hanya bisa digunakan di grup.")
        return

    if not is_group_allowed(chat.id):
        await kirim_admin(
            context,
            "🚨 AKSES GRUP DITOLAK\n\n"
            f"👥 Grup: {chat.title or '-'}\n"
            f"🆔 Group ID: {chat.id}\n"
            f"👤 User: {user.full_name}",
        )
        await update.message.reply_text("❌ Grup belum terdaftar dalam sistem.")
        return

    register_member(user, chat)
    now = datetime.now(TIMEZONE)
    config = SHIFT_CONFIG[SHIFT_KEY]
    mulai = now.replace(hour=config["mulai_jam"], minute=config["mulai_menit"], second=0, microsecond=0)
    batas = now.replace(hour=config["batas_jam"], minute=config["batas_menit"], second=0, microsecond=0)
    tutup = now.replace(hour=config["notif_jam"], minute=config["notif_menit"], second=0, microsecond=0)
    keyboard = []
    if mulai <= now < tutup:
        keyboard = [[InlineKeyboardButton(SHIFT_CONFIG[SHIFT_KEY]["button"], callback_data="absen_shift_utama")]]
    text = (
        "📋 ABSENSI STAFF CRM / TELE G-8008 POIPET\n\n"
        "🕘 JADWAL ABSENSI\n"
        f"• {shift_time_text()}\n\n"
        "✅ Tepat waktu sampai 11:15:59 WIB.\n"
        "⚠️ Mulai 11:16:00 WIB dihitung TELAT 1 menit.\n"
        "⚠️ Jangan absen sebelum masuk kantor ! .\n"
        f"💸 Denda keterlambatan: {rupiah(DENDA_PER_MENIT)} per menit.\n"
        "⚠️ Setelah 11:15:59 WIB absen tetap diterima sebagai MISTAKE/TELAT per menit.\n"
    )
    if now < mulai:
        text += "\n\n⏳ STATUS: Absensi belum dibuka."
    elif now >= tutup:
        text += "\n\n🔒 STATUS: Absensi hari ini sudah ditutup."
    elif now >= batas + timedelta(minutes=1):
        text += "\n\n⚠️ STATUS: Absensi masih dibuka, tetapi sudah masuk MISTAKE/TELAT per menit."
    else:
        text += "\n\n🟢 STATUS: Absensi sedang dibuka dan masih tepat waktu."

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await update.message.reply_text(text, reply_markup=reply_markup)


async def handle_absen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.message:
        return
    await query.answer()

    user = query.from_user
    chat = query.message.chat
    if chat.type not in ["group", "supergroup"]:
        await query.message.reply_text("❌ Absensi hanya bisa dilakukan di grup.")
        return
    if not is_group_allowed(chat.id):
        await query.message.reply_text("❌ Grup belum terdaftar dalam sistem.")
        return
    if query.data != "absen_shift_utama":
        await query.message.reply_text("❌ Tombol absensi tidak valid. Silakan /start ulang.")
        return

    register_member(user, chat)
    now = datetime.now(TIMEZONE)
    config = SHIFT_CONFIG[SHIFT_KEY]
    mulai = now.replace(hour=config["mulai_jam"], minute=config["mulai_menit"], second=0, microsecond=0)
    batas = now.replace(hour=config["batas_jam"], minute=config["batas_menit"], second=0, microsecond=0)

    if now < mulai:
        await query.message.reply_text(
            f"❌ Absensi belum dibuka.\n\n🕘 {shift_time_text()}"
        )
        return

    # Batas NORMAL adalah 11:15:59 WIB.
    # Mulai 11:16:00 tetap boleh absen tetapi dihitung MISTAKE/TELAT per menit.
    # Tepat 13:15:00 absensi ditutup total sampai window hari berikutnya.
    tutup = now.replace(hour=config["notif_jam"], minute=config["notif_menit"], second=0, microsecond=0)
    if now >= tutup:
        await query.message.reply_text(
            "🔒 ABSENSI SUDAH DITUTUP\n\n"
            "Absensi ditutup pukul 13:15:00 WIB.\n"
            "Silakan menunggu absensi berikutnya dibuka pukul 10:15 WIB."
        )
        return

    telat_menit = 0
    denda = 0
    mulai_telat = batas + timedelta(minutes=1)  # 11:16:00
    if now >= mulai_telat:
        # 11:16:00-11:16:59 = 1 menit, 11:17:00-11:17:59 = 2 menit, dst.
        telat_menit = int((now - mulai_telat).total_seconds() // 60) + 1
        denda = telat_menit * DENDA_PER_MENIT

    today = ensure_today()
    uid = str(user.id)
    if uid in absensi[today][SHIFT_KEY]:
        data_lama = absensi[today][SHIFT_KEY][uid]
        await query.message.reply_text(
            "✅ Kamu sudah melakukan absensi hari ini.\n\n"
            f"👤 Staff: {user.full_name}\n"
            f"🕘 Jam Absensi: {data_lama.get('jam', '-')} WIB"
        )
        return

    absensi[today][SHIFT_KEY][uid] = {
        "id": user.id,
        "nama": user.full_name,
        "username": user.username or "",
        "jam": now.strftime("%H:%M:%S"),
        "telat_menit": telat_menit,
        "denda": denda,
        "group_id": chat.id,
        "group_name": chat.title or "",
        "shift": SHIFT_KEY,
    }
    save_absensi()

    pesan = (
        "✅ ABSENSI BERHASIL\n\n"
        f"👤 Staff: {user.full_name}\n"
        f"🕘 Jam Absensi: {now.strftime('%H:%M:%S')} WIB"
    )
    if telat_menit > 0:
        pesan += f"\n\n⚠️ MISTAKE/TELAT: {telat_menit} menit\n💸 Denda: {rupiah(denda)}"
        await kirim_admin(
            context,
            "🚨 STAFF MISTAKE/TELAT ABSENSI\n\n"
            f"👥 Grup: {chat.title or '-'}\n"
            f"👤 Staff: {user.full_name}\n"
            f"🕘 Jam Absensi: {now.strftime('%H:%M:%S')} WIB\n"
            f"⚠️ Mistake/Telat: {telat_menit} menit\n"
            f"💸 Denda: {rupiah(denda)}",
        )
    await query.message.reply_text(pesan)


async def reset_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner_admin(user.id) or not update.message:
        return
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "Reply pesan staff lalu kirim /resetshift. Karena sekarang hanya 1 shift, staff akan langsung ditetapkan ke SHIFT UTAMA."
        )
        return

    target = update.message.reply_to_message.from_user
    register_member(target, update.effective_chat)
    await update.message.reply_text(
        "✅ DATA SHIFT STAFF BERHASIL DISET\n\n"
        f"👤 Staff: {target.full_name}\n"
        "📌 Shift: SHIFT UTAMA — batas tepat waktu 11.15 WIB"
    )


async def reset_shift_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner_admin(user.id) or not update.message:
        return
    for uid in list(members.keys()):
        shift_history[str(uid)] = SHIFT_KEY
    save_shift_history()
    await update.message.reply_text(
        f"✅ Semua staff ({len(members)}) sudah ditetapkan ke SHIFT UTAMA — batas tepat waktu 11.15 WIB, tutup 13.15 WIB."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner_admin(user.id) or not update.message:
        return
    today = ensure_today()
    data = absensi[today].get(SHIFT_KEY, {})
    lines = ["📋 STATUS ABSENSI HARI INI", "", "🕘 Shift utama — batas tepat waktu 11.15 WIB | tutup 13.15 WIB", ""]
    if not data:
        lines.append("Belum ada absensi.")
    else:
        for item in data.values():
            lines.append(f"👤 {item.get('nama', '-')}")
            lines.append(f"🕘 {item.get('jam', '-')} WIB")
            telat = int(item.get("telat_menit", 0) or 0)
            denda = int(item.get("denda", 0) or 0)
            if telat > 0:
                lines.append(f"⚠️ Telat {telat} menit")
                lines.append(f"💸 {rupiah(denda)}")
            lines.append("")
    await update.message.reply_text("\n".join(lines))


async def list_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner_admin(user.id) or not update.message:
        return
    lines = ["📋 DATA STAFF — SHIFT UTAMA | BATAS TEPAT WAKTU 11.15 WIB", ""]
    if not members:
        lines.append("Belum ada data staff.")
    else:
        for i, (uid, data) in enumerate(sorted(members.items(), key=lambda x: str(x[1].get("nama", "")).lower()), 1):
            lines.append(f"{i}. {data.get('nama', uid)}")
    await update.message.reply_text("\n".join(lines))


async def id_grup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or not is_owner_admin(user.id) or not update.message:
        return
    await update.message.reply_text(f"👥 Nama Grup: {chat.title or '-'}\n🆔 Group ID: {chat.id}")


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("✅ BOT ONLINE — 1 SHIFT 11.15 WIB")


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    text = (
        "📋 MENU BOT ABSENSI\n\n"
        "👥 Staff:\n"
        "/start - Buka tombol absensi\n\n"
        "👨‍💼 Admin:\n"
        "/status - Lihat absensi hari ini\n"
        "/listshift - Lihat daftar staff shift utama\n"
        "/idgrup - Lihat ID grup\n"
        "/ping - Cek bot online\n"
        "/resetshift - Reply staff, set ke shift utama\n"
        "/resetshiftall - Set semua staff ke shift utama\n\n"
        "🕘 Absensi buka 10.15 WIB; tepat waktu sampai 11.15:59 WIB.\n"
        "⚠️ Mulai 11.16 WIB tetap bisa absen, tetapi masuk MISTAKE/TELAT per menit.\n"
        "🔒 Pukul 13.15 WIB absensi ditutup dan laporan langsung dikirim."
    )
    await update.message.reply_text(text)


async def cek_absensi(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE)
    today = ensure_today()
    config = SHIFT_CONFIG[SHIFT_KEY]
    notif_key = f"{today}-{SHIFT_KEY}-laporan-2-jam"

    if notification_history.get(today, {}).get(notif_key):
        return

    waktu_laporan = now.replace(hour=config["notif_jam"], minute=config["notif_menit"], second=0, microsecond=0)
    if now < waktu_laporan:
        return

    data_shift = absensi[today].get(SHIFT_KEY, {})

    # Hanya staff pada grup yang diizinkan yang dihitung. Jika GROUP_ID diisi,
    # prioritaskan roster grup tersebut agar data grup lama tidak ikut laporan.
    staff = []
    for uid, member in members.items():
        try:
            member_group_id = int(member.get("group_id", 0) or 0)
        except (TypeError, ValueError):
            member_group_id = 0
        if GROUP_ID and member_group_id != GROUP_ID:
            continue
        if not GROUP_ID and not is_group_allowed(member_group_id):
            continue
        staff.append((str(uid), member.get("nama", uid)))

    tidak_absen = []
    telat = []
    tepat_waktu = []
    total_denda = 0

    for uid, nama in staff:
        item = data_shift.get(uid)
        if not item:
            tidak_absen.append(nama)
            continue
        telat_menit = int(item.get("telat_menit", 0) or 0)
        denda = int(item.get("denda", 0) or 0)
        jam = item.get("jam", "-")
        if telat_menit > 0:
            telat.append((nama, jam, telat_menit, denda))
            total_denda += denda
        else:
            tepat_waktu.append((nama, jam))

    lines = [
        "📋 LAPORAN LENGKAP ABSENSI",
        "",
        f"📅 Tanggal: {now.strftime('%d-%m-%Y')}",
        "📌 Shift: SHIFT UTAMA",
        "🕘 Batas tepat waktu: 11.15:59 WIB",
        "⚠️ Mulai mistake/telat: 11.16:00 WIB",
        "🔒 Tutup absensi + laporan: 13.15 WIB",
        f"👥 Total staff: {len(staff)}",
        "",
        f"❌ TIDAK ABSEN ({len(tidak_absen)})",
    ]
    if tidak_absen:
        lines.extend(f"{i}. {nama}" for i, nama in enumerate(tidak_absen, 1))
    else:
        lines.append("- Tidak ada")

    lines.extend(["", f"⚠️ MISTAKE/TELAT ({len(telat)})"])
    if telat:
        for i, (nama, jam, menit, denda) in enumerate(telat, 1):
            lines.append(f"{i}. {nama} — {jam} WIB — Mistake/Telat {menit} menit — Denda {rupiah(denda)}")
    else:
        lines.append("- Tidak ada")

    lines.extend(["", f"✅ TEPAT WAKTU ({len(tepat_waktu)})"])
    if tepat_waktu:
        for i, (nama, jam) in enumerate(tepat_waktu, 1):
            lines.append(f"{i}. {nama} — {jam} WIB")
    else:
        lines.append("- Tidak ada")

    lines.extend(["", f"💰 TOTAL DENDA KETERLAMBATAN: {rupiah(total_denda)}"])
    await kirim_admin(context, "\n".join(lines))

    notification_history.setdefault(today, {})[notif_key] = True
    save_notification_history()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        print("FATAL TELEGRAM 409: token yang sama sedang dipakai instance getUpdates lain.")
        print("Matikan deployment/service lain atau pastikan Railway hanya 1 replica.")
        try:
            context.application.stop_running()
        except Exception:
            pass
        return

    print("ERROR SAAT BOT BERJALAN:")
    if err:
        traceback.print_exception(type(err), err, err.__traceback__)


def acquire_instance_lock():
    global _instance_lock_handle
    os.makedirs(DATA_DIR, exist_ok=True)
    handle = open(LOCK_FILE, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError(
            "BOT INSTANCE GANDA TERDETEKSI: proses lain sudah memakai DATA_DIR ini. "
            "Pastikan hanya 1 replica/deployment bot yang aktif."
        )

    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\nstarted={datetime.now(TIMEZONE).isoformat()}\n")
    handle.flush()
    _instance_lock_handle = handle


def build_app():
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_error_handler(error_handler)
    app.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", start_absensi))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", menu))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("listshift", list_shift))
    app.add_handler(CommandHandler("idgrup", id_grup))
    app.add_handler(CommandHandler("resetshift", reset_shift))
    app.add_handler(CommandHandler("resetshiftall", reset_shift_all))
    app.add_handler(CallbackQueryHandler(handle_absen, pattern=r"^absen_shift_utama$"))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), track_member))
    app.job_queue.run_repeating(cek_absensi, interval=60, first=15)
    return app


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN belum diisi")
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS belum diisi")

    acquire_instance_lock()
    load_data()
    app = build_app()

    print("BOT ABSENSI AKTIF")
    print("MODE: 1 SHIFT | BUKA 10:15 | NORMAL s/d 11:15:59 | MISTAKE 11:16-13:14:59 | TUTUP+LAPORAN 13:15")
    print("SINGLE-INSTANCE LOCK: AKTIF")

    # allowed_updates=Update.ALL_TYPES diperlukan agar bot tetap menerima event
    # my_chat_member dan pesan grup. drop_pending_updates mencegah update lama diproses ulang.
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )


if __name__ == "__main__":
    try:
        print("STARTING BOT...")
        main()
    except Conflict:
        print("FATAL TELEGRAM 409: ada instance getUpdates lain dengan BOT_TOKEN yang sama.")
        print("Solusi wajib: sisakan SATU Railway service/replica saja untuk token tersebut.")
        sys.exit(2)
    except Exception as exc:
        print("ERROR START BOT:")
        print(str(exc))
        traceback.print_exc()
        sys.exit(1)
