import os
import re
import sqlite3
import subprocess
import requests
from PIL import Image, ImageDraw, ImageFont

# --- الإعدادات الأساسية ---
RECITERS = {
    "minshawi": {"id": 9, "name": "محمد صديق المنشاوي"},
    "dussary": {"id": 97, "name": "ياسر الدوسري"}
}

OUT_DIR = "generated_videos"
TEMP_DIR = "temp_assets"
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

QCOM = "https://api.quran.com/api/v4"
HEADERS = {"User-Agent": "quran-video-generator/1.0"}
ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
FONT_PATH = "/usr/share/fonts/opentype/fonts-hosny-amiri/AmiriQuran.ttf"

# --- 1. إدارة قاعدة بيانات المنجزات لمنع التكرار ---
def init_db():
    conn = sqlite3.connect("quran_clips.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS completed_clips (
                    surah INTEGER,
                    start_ayah INTEGER,
                    end_ayah INTEGER,
                    reciter TEXT,
                    duration REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(surah, start_ayah, end_ayah, reciter)
                )''')
    conn.commit()
    return conn

def is_clip_done(conn, surah, start_ayah, end_ayah, reciter):
    c = conn.cursor()
    c.execute("SELECT 1 FROM completed_clips WHERE surah=? AND start_ayah=? AND end_ayah=? AND reciter=?",
              (surah, start_ayah, end_ayah, reciter))
    return c.fetchone() is not None

def mark_clip_done(conn, surah, start_ayah, end_ayah, reciter, duration):
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO completed_clips (surah, start_ayah, end_ayah, reciter, duration) VALUES (?, ?, ?, ?, ?)",
              (surah, start_ayah, end_ayah, reciter, duration))
    conn.commit()

# --- 2. جلب النصوص والتوقيتات الرسمية ---
def fetch_surah_data(surah_num, reciter_id):
    # جلب النص العثماني الرسمي
    r_text = requests.get(f"{QCOM}/quran/verses/uthmani", params={"chapter_number": surah_num}, headers=HEADERS, timeout=60)
    r_text.raise_for_status()
    verses_text = {int(v["verse_key"].split(":")[1]): v["text_uthmani"] for v in r_text.json()["verses"]}

    # جلب التوقيتات والصوت
    r_audio = requests.get(f"{QCOM}/chapter_recitations/{reciter_id}/{surah_num}", params={"segments": "true"}, headers=HEADERS, timeout=60)
    r_audio.raise_for_status()
    data = r_audio.json()["audio_file"]

    audio_url = data["audio_url"]
    timestamps = {}
    for item in data.get("timestamps", []):
        ayah = int(item["verse_key"].split(":")[1])
        timestamps[ayah] = {
            "from": item["timestamp_from"] / 1000.0,
            "to": item["timestamp_to"] / 1000.0,
            "duration": item["duration"] / 1000.0
        }

    return verses_text, timestamps, audio_url

# --- 3. تحديد المقطع التالي (30 إلى 60 ثانية بالضبط وبلا تكرار) ---
def find_next_clip(conn, surah_num, timestamps, reciter_key):
    total_ayahs = len(timestamps)
    start = 1
    
    # معرفة آخر آية تم إنتاجها لهذه السورة والقارئ
    c = conn.cursor()
    c.execute("SELECT MAX(end_ayah) FROM completed_clips WHERE surah=? AND reciter=?", (surah_num, reciter_key))
    last_ended = c.fetchone()[0]
    if last_ended and last_ended < total_ayahs:
        start = last_ended + 1
    elif last_ended and last_ended >= total_ayahs:
        return None  # السورة انتهت بالكامل

    end = start
    while end <= total_ayahs:
        dur = timestamps[end]["to"] - timestamps[start]["from"]
        if dur >= 30.0:
            if dur <= 60.0:
                return start, end, dur
            else:
                # لو آية واحدة طويلة جداً تخطت الـ 60 ثانية بمفردها
                if start == end:
                    return start, end, dur
                # لو الإضافة جعلت المقطع يتجاوز 60، نرجع للآية السابقة لو كانت فوق 25 ثانية مثلاً
                prev_dur = timestamps[end - 1]["to"] - timestamps[start]["from"]
                return start, end - 1, prev_dur
        end += 1

    # ما تبقى من السورة إن كان أقل من 30 ولكن معقول
    remaining_dur = timestamps[total_ayahs]["to"] - timestamps[start]["from"]
    if remaining_dur >= 20.0:
        return start, total_ayahs, remaining_dur
    return None

# --- 4. محرك رسم الآية وتوليد الشرائح ---
def wrap_lines(text, font, max_w):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if font.getlength(t, direction="rtl", language="ar") <= max_w:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines

def render_ayah_slide(text, ayah_num, surah_title, reciter_title, out_path):
    W, H = 1080, 1920
    # شريحة شفافة (RGBA) لتوضع فوق الفيديو
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # ترويسة الفيديو
    header_font = ImageFont.truetype(FONT_PATH, 54, layout_engine=ImageFont.Layout.RAQM)
    d.text((W // 2, 140), f"{surah_title} | {reciter_title}", font=header_font, fill=(212, 175, 55, 255), anchor="ma", direction="rtl", language="ar")

    # تنسيق نص الآية مع الرمز والرقم
    num_ar = str(ayah_num).translate(ARABIC_DIGITS)
    full_text = f"{text} \u06dd{num_ar}"

    # حساب حجم الخط تلقائياً ليناسب الشاشة تماماً
    size = 110
    while size >= 45:
        font = ImageFont.truetype(FONT_PATH, size, layout_engine=ImageFont.Layout.RAQM)
        lines = wrap_lines(full_text, font, W - 180)
        line_h = int(size * 2.1)
        if len(lines) * line_h <= 1150:
            break
        size -= 4

    # رسم الآية في المنتصف مع ظل خفيف لضمان القراءة بوضوح
    y = (H - len(lines) * line_h) // 2
    for ln in lines:
        # ظل للنص
        d.text((W // 2 + 2, y + 2), ln, font=font, fill=(0, 0, 0, 200), anchor="ma", direction="rtl", language="ar")
        # النص الأساسي بالأبيض النقي
        d.text((W // 2, y), ln, font=font, fill=(255, 255, 255, 255), anchor="ma", direction="rtl", language="ar")
        y += line_h

    img.save(out_path, "PNG")

# --- 5. التوليد النهائي للمقطع عبر FFmpeg ---
def generate_video(surah_num, reciter_key, bg_video_path=None):
    conn = init_db()
    reciter_info = RECITERS[reciter_key]
    
    print(f"[*] جلب بيانات سورة {surah_num} للقارئ {reciter_info['name']}...")
    verses_text, timestamps, audio_url = fetch_surah_data(surah_num, reciter_info["id"])

    clip_info = find_next_clip(conn, surah_num, timestamps, reciter_key)
    if not clip_info:
        print("[!] لا توجد آيات جديدة مؤهلة للإنتاج في هذه السورة.")
        return

    start_ayah, end_ayah, duration = clip_info
    print(f"[+] تم اختيار المقطع: من الآية {start_ayah} إلى {end_ayah} (المدة: {duration:.2f} ثانية)")

    # 1. تحميل وقص الصوت
    full_audio_path = os.path.join(TEMP_DIR, f"full_{surah_num}_{reciter_key}.mp3")
    if not os.path.exists(full_audio_path):
        r = requests.get(audio_url, headers=HEADERS, timeout=120)
        with open(full_audio_path, "wb") as f:
            f.write(r.content)

    t_start = timestamps[start_ayah]["from"]
    t_end = timestamps[end_ayah]["to"]
    cut_audio = os.path.join(TEMP_DIR, "cut.mp3")
    subprocess.run(["ffmpeg", "-y", "-ss", str(t_start), "-to", str(t_end), "-i", full_audio_path, "-acodec", "copy", cut_audio], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 2. توليد صور الآيات المحددة
    slide_files = []
    for ayah in range(start_ayah, end_ayah + 1):
        slide_path = os.path.join(TEMP_DIR, f"slide_{ayah}.png")
        render_ayah_slide(verses_text[ayah], ayah, f"سورة رقم {surah_num}", reciter_info["name"], slide_path)
        
        # التوقيت النسبي داخل المقطع المقصوص
        a_from = timestamps[ayah]["from"] - t_start
        a_to = timestamps[ayah]["to"] - t_start
        slide_files.append((slide_path, a_from, a_to))

    # 3. بناء فلاتر FFmpeg لظهور النص مع الصوت بدقة المللي ثانية
    filter_complex = ""
    last_label = "[0:v]"
    
    # مدخلات الصور
    inputs = []
    if bg_video_path and os.path.exists(bg_video_path):
        inputs.extend(["-stream_loop", "-1", "-i", bg_video_path])
    else:
        # خلفية متدرجة أنيقة تلقائية بدقة 1080x1920
        inputs.extend(["-f", "lavfi", "-i", "color=c=0x0a141c:s=1080x1920:d=" + str(duration)])

    idx = 1
    for path, a_from, a_to in slide_files:
        inputs.extend(["-i", path])
        next_label = f"[v{idx}]"
        filter_complex += f"{last_label}[{idx}:v]overlay=0:0:enable='between(t,{a_from},{a_to})'{next_label};"
        last_label = next_label
        idx += 1

    out_file = os.path.join(OUT_DIR, f"Surah_{surah_num}_{start_ayah}-{end_ayah}_{reciter_key}.mp4")
    
    # أمر الإنتاج النهائي
    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-i", cut_audio,
        "-filter_complex", filter_complex.rstrip(";"),
        "-map", last_label,
        "-map", f"{idx}:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-t", str(duration),
        out_file
    ]

    print("[*] جاري إنتاج الفيديو عبر FFmpeg...")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # حفظ في قاعدة البيانات لمنع التكرار نهائياً
    mark_clip_done(conn, surah_num, start_ayah, end_ayah, reciter_key, duration)
    print(f"[✓] تم بنجاح! تم حفظ الفيديو في: {out_file}")

# --- تشغيل تجريبي ---
if __name__ == "__main__":
    # مثال: توليد مقطع من سورة البقرة (رقم 2) للقارئ المنشاوي
    generate_video(surah_num=2, reciter_key="minshawi")
