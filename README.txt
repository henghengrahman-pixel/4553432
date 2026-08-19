BOT ABSENSI G-8008 - 1 SHIFT FINAL

JADWAL
- Absensi dibuka: 10:15 WIB
- Paling lambat: 11:15 WIB
- 11:15:00 sampai 11:15:59 masih tepat waktu
- 11:16:00 = telat 1 menit
- Denda: Rp50.000 per menit
- Laporan otomatis: 11:45 WIB

RAILWAY VARIABLES WAJIB
BOT_TOKEN=token_bot_telegram
ADMIN_IDS=123456789
DATA_DIR=/data

OPSIONAL
GROUP_ID=-100xxxxxxxxxx

PENTING UNTUK ERROR TELEGRAM 409
Telegram hanya mengizinkan SATU proses getUpdates untuk SATU BOT_TOKEN.
Pastikan:
1. Railway hanya memiliki 1 service aktif dengan BOT_TOKEN tersebut.
2. Replica/instance = 1.
3. Jangan jalankan main.py di PC/laptop bersamaan dengan Railway.
4. Jangan gunakan BOT_TOKEN yang sama pada project Railway lain.

Versi ini sudah menambahkan:
- delete webhook saat startup
- single-instance file lock pada DATA_DIR
- stop otomatis jika Telegram mendeteksi Conflict 409
- migrasi data 2 shift lama menjadi shift_utama
