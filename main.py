from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ChatMemberHandler,
    MessageHandler,
    filters
)
from datetime import datetime, timedelta
import pytz
import json
import os
import traceback

TIMEZONE = pytz.timezone("Asia/Jakarta")

TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

def env_int(name, default=0):
    value = os.getenv(name, str(default)).strip()
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


GROUP_ID = env_int("GROUP_ID", 0)

DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

MEMBER_FILE = os.path.join(DATA_DIR, "members.json")
ABSEN_FILE = os.path.join(DATA_DIR, "absensi.json")
GROUP_FILE = os.path.join(DATA_DIR, "groups.json")
SHIFT_FILE = os.path.join(DATA_DIR, "shift_history.json")
NOTIF_FILE = os.path.join(DATA_DIR, "notification_history.json")

SHIFT_CONFIG = {
    "shift_utama": {
        "label": "SHIFT UTAMA",
        "button": "📝 ABSEN",
        "mulai_jam": 10,
        "mulai_menit": 15,
        "batas_jam": 11,
        "batas_menit": 15,
        "notif_jam": 11,
        "notif_menit": 45
    }
}

# Semua data shift lama otomatis dipindahkan ke dua shift baru.
OLD_SHIFT_MAP = {
    "siang":"shift_pagi",
    "pagi":"shift_pagi",
    "jam_11_siang":"shift_pagi",
    "malam":"shift_malam",
    "jam_11_malam":"shift_malam",
    "shift_utama": None
}

DENDA_PER_MENIT = 50000

members = {}
absensi = {}
allowed_groups = {}
shift_history = {}
notification_history = {}


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as file:
                return json.load(file)
        except Exception:
            return default
    return default


def save_json(path, data):
    temp_path = f"{path}.tmp"

    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)

    os.replace(temp_path, path)


def normalize_shift_key(shift):
    if not shift:
        return None

    shift = str(shift).strip().lower()

    if shift in OLD_SHIFT_MAP:
        return OLD_SHIFT_MAP[shift]

    if shift in SHIFT_CONFIG:
        return shift

    return None


def load_data():
    global members
    global absensi
    global allowed_groups
    global shift_history
    global notification_history

    members = load_json(MEMBER_FILE, {})
    absensi = load_json(ABSEN_FILE, {})
    allowed_groups = load_json(GROUP_FILE, {})
    shift_history = load_json(SHIFT_FILE, {})
    notification_history = load_json(NOTIF_FILE, {})

    # Jangan biarkan file JSON rusak / format lama membuat bot crash.
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

    changed_shift = False

    for uid, shift in list(shift_history.items()):
        fixed_shift = normalize_shift_key(shift)

        if fixed_shift:
            if fixed_shift != shift:
                shift_history[uid] = fixed_shift
                changed_shift = True
        else:
            # Data SHIFT UTAMA lama tidak boleh otomatis dianggap SHIFT PAGI.
            # Staff harus memilih PAGI/MALAM sekali lagi agar laporan tidak salah.
            shift_history.pop(uid, None)
            changed_shift = True

    # Normalisasi struktur absensi agar data lama / sebagian tidak membuat KeyError.
    changed_absen = False
    for tanggal, day_data in list(absensi.items()):
        if not isinstance(day_data, dict):
            absensi[tanggal] = {}
            changed_absen = True
            continue

        for shift_key in ("shift_utama"):
            if shift_key in day_data and not isinstance(day_data[shift_key], dict):
                day_data[shift_key] = {}
                changed_absen = True

        # Migrasi hanya shift lama yang identitasnya jelas.
        for old_key, new_key in OLD_SHIFT_MAP.items():
            if not new_key or old_key not in day_data or old_key == new_key:
                continue
            old_records = day_data.get(old_key)
            if isinstance(old_records, dict):
                target = day_data.setdefault(new_key, {})
                if not isinstance(target, dict):
                    target = {}
                    day_data[new_key] = target
                for uid, record in old_records.items():
                    target.setdefault(uid, record)
            day_data.pop(old_key, None)
            changed_absen = True

    if changed_shift:
        save_shift_history()
    if changed_absen:
        save_absensi()


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


def get_today_key():
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")


def ensure_today():

    today = get_today_key()

    if today not in absensi:
        absensi[today] = {}

    for shift_key in SHIFT_CONFIG.keys():

        if shift_key not in absensi[today]:
            absensi[today][shift_key] = {}

    save_absensi()

    if today not in notification_history:
        notification_history[today] = {}
        save_notification_history()

    return today


def rupiah(nominal):
    return f"Rp{int(nominal):,}".replace(",", ".")


def is_owner_admin(user_id):
    return user_id in ADMIN_IDS


def is_group_allowed(chat_id):

    if GROUP_ID != 0 and chat_id == GROUP_ID:
        return True

    return str(chat_id) in allowed_groups


def shift_time_text(shift):

    config = SHIFT_CONFIG[shift]

    return (
        f"Buka {config['mulai_jam']:02d}:{config['mulai_menit']:02d} WIB | "
        f"Jadwal {config['batas_jam']:02d}:{config['batas_menit']:02d} WIB"
    )


async def kirim_admin(context, pesan):
    # Pesan admin memakai plain text agar nama staff/grup dengan karakter khusus
    # tidak membuat Telegram menolak laporan.
    safe_pesan = str(pesan).replace("*", "").replace("`", "")

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=safe_pesan
            )
        except Exception as exc:
            print(f"GAGAL KIRIM ADMIN {admin_id}: {exc}")


async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):

    event = update.my_chat_member

    if not event:
        return

    chat = event.chat
    from_user = event.from_user

    if chat.type not in ["group", "supergroup"]:
        return

    new_status = event.new_chat_member.status

    if new_status in ["member", "administrator"]:

        if is_owner_admin(from_user.id):

            allowed_groups[str(chat.id)] = {
                "id": chat.id,
                "title": chat.title or "",
                "added_by_id": from_user.id,
                "added_by_name": from_user.full_name,
                "created_at": datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
            }

            save_groups()

            await kirim_admin(
                context,
                (
                    "✅ *BOT ABSENSI BERHASIL DIAKTIFKAN*\n\n"
                    f"👥 Grup: {chat.title or '-'}\n"
                    f"🆔 Group ID: `{chat.id}`\n"
                    f"👤 Ditambahkan oleh: {from_user.full_name}"
                )
            )

        else:

            await kirim_admin(
                context,
                (
                    "🚨 *BOT DITAMBAHKAN OLEH NON ADMIN UTAMA*\n\n"
                    f"👥 Grup: {chat.title or '-'}\n"
                    f"🆔 Group ID: `{chat.id}`\n"
                    f"👤 Oleh: {from_user.full_name}\n"
                    f"🆔 User ID: `{from_user.id}`\n\n"
                    "Bot otomatis keluar dari grup."
                )
            )

            try:
                await context.bot.leave_chat(chat.id)
            except Exception:
                pass


async def track_member(update: Update, context: ContextTypes.DEFAULT_TYPE = None):

    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return

    if chat.type not in ["group", "supergroup"]:
        return

    if not is_group_allowed(chat.id):
        return

    members[str(user.id)] = {
        "id": user.id,
        "nama": user.full_name,
        "username": user.username or "",
        "group_id": chat.id,
        "group_name": chat.title or "",
        "last_seen": datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    }

    save_members()


async def start_absensi(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return

    if chat.type not in ["group", "supergroup"]:
        return await update.message.reply_text(
            "❌ Bot absensi hanya bisa digunakan di grup."
        )

    if not is_group_allowed(chat.id):

        await kirim_admin(
            context,
            (
                "🚨 *AKSES GRUP DITOLAK*\n\n"
                f"👥 Grup: {chat.title or '-'}\n"
                f"🆔 Group ID: `{chat.id}`\n"
                f"👤 User: {user.full_name}"
            )
        )

        return await update.message.reply_text(
            "❌ Grup belum terdaftar dalam sistem."
        )

    await track_member(update, context)

    saved_shift = normalize_shift_key(shift_history.get(str(user.id)))

    if saved_shift:
        keyboard = [[
            InlineKeyboardButton(
                SHIFT_CONFIG[saved_shift]["button"],
                callback_data=f"absen_{saved_shift}"
            )
        ]]
        info_shift = f"\n📌 Shift terdaftar: *{SHIFT_CONFIG[saved_shift]['label']}*\n"
    else:
        keyboard = [
            [InlineKeyboardButton(
                SHIFT_CONFIG["shift_pagi"]["button"],
                callback_data="absen_shift_pagi"
            )],
            [InlineKeyboardButton(
                SHIFT_CONFIG["shift_malam"]["button"],
                callback_data="absen_shift_malam"
            )]
        ]
        info_shift = "\n📌 Pilih shift sesuai jadwal kerja. Shift pertama yang dipilih akan tersimpan.\n"

    text = (
        "📋 *ABSENSI STAFF G-8008 POIPET*\n\n"
        "🕘 *JADWAL ABSENSI*\n"
        f"• SHIFT PAGI: {shift_time_text('shift_pagi')}\n"
        f"• SHIFT MALAM: {shift_time_text('shift_malam')}\n"
        f"{info_shift}\n"
        "✅ Tepat waktu sampai 11:15:59 / 23:15:59 WIB.\n"
        "⚠️ Mulai 11:16 / 23:16 dihitung TELAT 1 menit.\n"
        "⚠️ JANGAN ABSEN SEBELUM MASUK KANTOR! ADA SANKSI TEGAS JIKA DILANGGAR.\n"
        f"💸 Denda keterlambatan: {rupiah(DENDA_PER_MENIT)} per menit."
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_absen(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    if not query or not query.message:
        return

    await query.answer()

    user = query.from_user
    chat = query.message.chat

    if chat.type not in ["group", "supergroup"]:
        return await query.message.reply_text(
            "❌ Absensi hanya bisa dilakukan di grup."
        )

    if not is_group_allowed(chat.id):
        return await query.message.reply_text(
            "❌ Grup belum terdaftar dalam sistem."
        )

    shift = normalize_shift_key(
        query.data.replace("absen_", "")
    )

    if not shift:
        return await query.message.reply_text(
            "❌ Shift tidak valid."
        )

    await track_member(update, context)

    saved_shift = normalize_shift_key(
        shift_history.get(str(user.id))
    )

    if saved_shift:
        shift_history[str(user.id)] = saved_shift
        save_shift_history()

    if saved_shift and saved_shift != shift:
        return await query.message.reply_text(
            (
                f"❌ Kamu sudah terdaftar pada {SHIFT_CONFIG[saved_shift]['label']}.\n"
                "Hubungi admin apabila ingin melakukan pergantian shift."
            )
        )

    if not saved_shift:
        shift_history[str(user.id)] = shift
        save_shift_history()

    now = datetime.now(TIMEZONE)
    config = SHIFT_CONFIG[shift]

    mulai = now.replace(
        hour=config["mulai_jam"],
        minute=config["mulai_menit"],
        second=0,
        microsecond=0
    )

    batas = now.replace(
        hour=config["batas_jam"],
        minute=config["batas_menit"],
        second=0,
        microsecond=0
    )

    if now < mulai:
        return await query.message.reply_text(
            (
                f"❌ Absensi {config['label']} belum dibuka.\n\n"
                f"🕘 Jadwal absensi: {shift_time_text(shift)}"
            )
        )

    telat_menit = 0

    # Batas aman sampai detik ke-59 pada menit jadwal. Denda mulai tepat 1 menit setelah jadwal.
    mulai_denda = batas + timedelta(minutes=1)

    if now >= mulai_denda:
        telat_menit = int(
            (now - mulai_denda).total_seconds() // 60
        ) + 1

    denda = telat_menit * DENDA_PER_MENIT

    today = ensure_today()

    if str(user.id) in absensi[today][shift]:

        data_lama = absensi[today][shift][str(user.id)]

        return await query.message.reply_text(
            (
                "✅ Kamu sudah melakukan absensi hari ini.\n\n"
                f"👤 Staff: {user.full_name}\n"
                f"📌 Shift: {config['label']}\n"
                f"🕘 Jam Absensi: {data_lama.get('jam', '-')}"
            )
        )

    absensi[today][shift][str(user.id)] = {
        "id": user.id,
        "nama": user.full_name,
        "username": user.username or "",
        "jam": now.strftime("%H:%M:%S"),
        "telat_menit": telat_menit,
        "denda": denda,
        "group_id": chat.id,
        "group_name": chat.title or "",
        "shift": shift
    }

    save_absensi()

    pesan = (
        "✅ *ABSENSI BERHASIL*\n\n"
        f"👤 Staff: {user.full_name}\n"
        f"📌 Shift: {config['label']}\n"
        f"🕘 Jam Absensi: {now.strftime('%H:%M:%S')} WIB"
    )

    if telat_menit > 0:

        pesan += (
            f"\n\n⚠️ Keterlambatan: {telat_menit} menit"
            f"\n💸 Denda: {rupiah(denda)}"
        )

        await kirim_admin(
            context,
            (
                "🚨 *STAFF TELAT ABSENSI*\n\n"
                f"👥 Grup: {chat.title or '-'}\n"
                f"👤 Staff: {user.full_name}\n"
                f"📌 Shift: {config['label']}\n"
                f"🕘 Jam Absensi: {now.strftime('%H:%M:%S')} WIB\n"
                f"⚠️ Telat: {telat_menit} menit\n"
                f"💸 Denda: {rupiah(denda)}"
            )
        )

    await query.message.reply_text(
        pesan,
        parse_mode="Markdown"
    )


async def reset_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user or not is_owner_admin(user.id):
        return await update.message.reply_text(
            "❌ Hanya admin utama yang bisa mengatur shift staff."
        )

    if not update.message.reply_to_message:
        return await update.message.reply_text(
            "Reply pesan staff lalu kirim /resetshift. Staff kemudian pilih SHIFT PAGI atau SHIFT MALAM saat /start."
        )

    target = update.message.reply_to_message.from_user
    shift_history.pop(str(target.id), None)
    save_shift_history()

    members[str(target.id)] = {
        "id": target.id,
        "nama": target.full_name,
        "username": target.username or "",
        "group_id": update.effective_chat.id,
        "group_name": update.effective_chat.title or "",
        "last_seen": datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    }
    save_members()

    await update.message.reply_text(
        (
            "✅ *SHIFT STAFF BERHASIL DIRESET*\n\n"
            f"👤 Staff: {target.full_name}\n"
            "📌 Staff dapat memilih SHIFT PAGI atau SHIFT MALAM saat membuka /start."
        ),
        parse_mode="Markdown"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user or not is_owner_admin(user.id):
        return

    today = ensure_today()

    text = "📋 *STATUS ABSENSI HARI INI*\n\n"

    for shift, config in SHIFT_CONFIG.items():

        text += f"{config['button']}\n"

        data = absensi[today].get(shift, {})

        if not data:
            text += "Belum ada absensi.\n\n"
            continue

        for item in data.values():

            text += (
                f"👤 {item.get('nama', '-')}\n"
                f"🕘 {item.get('jam', '-')}\n"
            )

            telat = int(item.get("telat_menit", 0))
            denda = int(item.get("denda", 0))

            if telat > 0:
                text += (
                    f"⚠️ Telat {telat} menit\n"
                    f"💸 {rupiah(denda)}\n"
                )

            text += "\n"

    await update.message.reply_text(
        text,
        parse_mode="Markdown"
    )


async def list_shift(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user or not is_owner_admin(user.id):
        return

    if not shift_history:
        return await update.message.reply_text(
            "Belum ada data shift staff."
        )

    hasil = "📋 *DATA SHIFT STAFF*\n\n"

    for shift_key, config in SHIFT_CONFIG.items():

        hasil += f"{config['button']}\n"

        daftar = []

        for uid, shift in shift_history.items():

            fixed_shift = normalize_shift_key(shift)

            if fixed_shift == shift_key:
                nama = members.get(uid, {}).get("nama", uid)
                daftar.append(f"• {nama}")

        hasil += "\n".join(daftar) if daftar else "-"
        hasil += "\n\n"

    await update.message.reply_text(
        hasil,
        parse_mode="Markdown"
    )


async def id_grup(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    chat = update.effective_chat

    if not user or not is_owner_admin(user.id):
        return

    await update.message.reply_text(
        (
            f"👥 Nama Grup: {chat.title or '-'}\n"
            f"🆔 Group ID: `{chat.id}`"
        ),
        parse_mode="Markdown"
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ BOT ONLINE")


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = (
        "📋 *MENU BOT ABSENSI*\n\n"
        "👥 *Staff:*\n"
        "/start - Buka menu absensi\n\n"
        "👨‍💼 *Admin:*\n"
        "/status - Lihat absensi hari ini\n"
        "/listshift - Lihat daftar staff\n"
        "/idgrup - Lihat ID grup\n"
        "/ping - Cek bot online\n\n"
        "🔁 *Tetapkan Staff:*\n"
        "Reply pesan staff lalu kirim /resetshift"
    )

    await update.message.reply_text(
        text,
        parse_mode="Markdown"
    )


async def cek_absensi(context: ContextTypes.DEFAULT_TYPE):

    now = datetime.now(TIMEZONE)
    today = ensure_today()

    for shift, config in SHIFT_CONFIG.items():

        notif_key = f"{today}-{shift}-laporan-30-menit"

        if notification_history.get(today, {}).get(notif_key):
            continue

        # Laporan dikirim tepat 30 menit setelah jadwal absensi.
        jadwal = now.replace(
            hour=config["batas_jam"],
            minute=config["batas_menit"],
            second=0,
            microsecond=0
        )
        waktu_laporan = jadwal + timedelta(minutes=30)

        # Job berjalan setiap 60 detik. Gunakan >= agar laporan tetap terkirim
        # walaupun Railway/job terlambat beberapa detik atau sempat restart.
        if now < waktu_laporan:
            continue

        data_shift = absensi[today].get(shift, {})

        staff_shift = []
        for uid, member_shift in shift_history.items():
            fixed_shift = normalize_shift_key(member_shift)
            if fixed_shift == shift:
                staff_shift.append((uid, members.get(uid, {}).get("nama", uid)))

        tidak_absen = []
        telat = []
        tepat_waktu = []
        total_denda = 0

        for uid, nama in staff_shift:
            item = data_shift.get(uid)

            if not item:
                tidak_absen.append(nama)
                continue

            telat_menit = int(item.get("telat_menit", 0) or 0)
            denda = int(item.get("denda", 0) or 0)
            jam = item.get("jam", "-")

            if telat_menit > 0:
                telat.append({
                    "nama": nama,
                    "jam": jam,
                    "telat_menit": telat_menit,
                    "denda": denda
                })
                total_denda += denda
            else:
                tepat_waktu.append({"nama": nama, "jam": jam})

        lines = [
            "📋 *LAPORAN LENGKAP ABSENSI*",
            "",
            f"📅 Tanggal: {now.strftime('%d-%m-%Y')}",
            f"📌 Shift: {config['label']}",
            f"🕘 Jadwal: {config['batas_jam']:02d}:{config['batas_menit']:02d} WIB",
            f"📢 Laporan: {waktu_laporan.strftime('%H:%M')} WIB (30 menit setelah jadwal)",
            f"👥 Total staff shift: {len(staff_shift)}",
            "",
            f"❌ *TIDAK ABSEN ({len(tidak_absen)})*"
        ]

        if tidak_absen:
            for i, nama in enumerate(tidak_absen, 1):
                lines.append(f"{i}. {nama}")
        else:
            lines.append("- Tidak ada")

        lines.extend([
            "",
            f"⚠️ *TERLAMBAT ({len(telat)})*"
        ])

        if telat:
            for i, item in enumerate(telat, 1):
                lines.append(
                    f"{i}. {item['nama']} — {item['jam']} WIB — "
                    f"Telat {item['telat_menit']} menit — Denda {rupiah(item['denda'])}"
                )
        else:
            lines.append("- Tidak ada")

        lines.extend([
            "",
            f"✅ *TEPAT WAKTU ({len(tepat_waktu)})*"
        ])

        if tepat_waktu:
            for i, item in enumerate(tepat_waktu, 1):
                lines.append(f"{i}. {item['nama']} — {item['jam']} WIB")
        else:
            lines.append("- Tidak ada")

        lines.extend([
            "",
            f"💰 *TOTAL DENDA KETERLAMBATAN: {rupiah(total_denda)}*"
        ])

        await kirim_admin(context, "\n".join(lines))

        notification_history.setdefault(today, {})
        notification_history[today][notif_key] = True
        save_notification_history()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("ERROR SAAT BOT BERJALAN:")
    if context.error:
        traceback.print_exception(
            type(context.error),
            context.error,
            context.error.__traceback__
        )


def main():

    if not TOKEN:
        raise RuntimeError("BOT_TOKEN belum diisi")

    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS belum diisi")

    load_data()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_error_handler(error_handler)

    app.add_handler(
        ChatMemberHandler(
            my_chat_member,
            ChatMemberHandler.MY_CHAT_MEMBER
        )
    )

    app.add_handler(CommandHandler("start", start_absensi))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("help", menu))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("listshift", list_shift))
    app.add_handler(CommandHandler("idgrup", id_grup))
    app.add_handler(CommandHandler("resetshift", reset_shift))

    app.add_handler(
        CallbackQueryHandler(
            handle_absen,
            pattern=r"^absen_(shift_pagi|shift_malam)$"
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            track_member
        )
    )

    app.job_queue.run_repeating(
        cek_absensi,
        interval=60,
        first=15
    )

    print("BOT ABSENSI AKTIF")

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":

    try:

        print("STARTING BOT...")

        main()

    except Exception as e:

        print("ERROR START BOT:")
        print(str(e))
        traceback.print_exc()
