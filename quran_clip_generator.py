import os
import re
import random
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

# البحث عن خط المصحف تلقائياً
FONT_PATH = "/usr/share/fonts/opentype/fonts-hosny-amiri/AmiriQuran.ttf"
if not os.path.exists(FONT_PATH):
    import glob
    matches = glob.glob("/usr/share/fonts/**/AmiriQuran*.ttf", recursive=True)
    if matches:
        FONT_PATH = matches[0]

PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")

# --- 1. إدارة قاعدة البيانات لمنع التكرار ---
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

def mark_clip_done(conn, surah, start_ayah, end_ayah, reciter, duration):
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO completed_clips (surah, start_ayah, end_ayah, reciter, duration) VALUES (?, ?, ?, ?, ?)",
              (surah, start_ayah, end_ayah, reciter, duration))
    conn.commit()

# --- 2. جلب النصوص والتوقيتات واسم السورة ---
def fetch_surah_data(surah_num, reciter_id):
    # اسم السورة بالعربي
    r_info = requests.get(f"{QCOM}/chapters/{surah_num}", headers=HEADERS, timeout=60)
    r_info.raise_for_status()
    surah_name_ar = r_info.json()["chapter"]["name_arabic"]

    # نصوص الآيات العثمانية
    r_text = requests.get(f"{QCOM}/quran/verses/uthmani", params={"chapter_number": surah_num}, headers=HEADERS, timeout=60)
    r_text.raise_for_status()
    verses_text = {int(v["verse_key"].split(":")[1]): v["text_uthmani"] for v in r_text.json()["verses"]}

    # التوقيتات والصوت
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

    return surah_name_ar, verses_text, timestamps, audio_url

# --- 3. اختيار المقطع غير المكرر (30-60 ثانية) ---
def find_next_clip(conn, surah_num, timestamps, reciter_key):
    total_ayahs = len(timestamps)
    start = 1
    
    c = conn.cursor()
    c.execute("SELECT MAX(end_ayah) FROM completed_clips WHERE surah=? AND reciter=?", (surah_num, reciter_key))
    last_ended = c.fetchone()[0]
    if last_ended and last_ended < total_ayahs:
        start = last_ended + 1
    elif last_ended and last_ended >= total_ayahs:
        return None

    end = start
    while end <= total_ayahs:
        dur = timestamps[end]["to"] - timestamps[start]["from"]
        if dur >= 30.0:
            if dur <= 60.0:
                return start, end, dur
            else:
                if start == end:
                    return start, end, dur
                prev_dur = timestamps[end - 1]["to"] - timestamps[start]["from"]
                return start, end - 1, prev_dur
        end += 1

    remaining_dur = timestamps[total_ayahs]["to"] - timestamps[start]["from"]
    if remaining_dur >= 20.0:
        return start, total_ayahs, remaining_dur
    return None

# --- 4. جلب فيديو طبيعي من Pixabay ---
def get_pixabay_video(min_duration):
    if not PIXABAY_API_KEY:
        print("[!] لم يتم العثور على PIXABAY_API_KEY، سيتم استخدام خلفية رقمية.")
        return None

    queries = ["nature", "waterfall", "sea waves", "mountains clouds", "forest aerial", "desert dunes"]
    query = random.choice(queries)
    url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={requests.utils.quote(query)}&per_page=30"

    try:
        r = requests.get(url, timeout=30)
        data = r.json()
        hits = data.get("hits", [])
        
        valid_hits = [h for h in hits if h.get("duration", 0) >= 10]
        if not valid_hits:
            return None

        chosen = random.choice(valid_hits)
        videos = chosen.get("videos", {})
        target = videos.get("large") or videos.get("medium") or videos.get("small")
        if not target or not target.get("url"):
            return None

        video_url = target["url"]
        bg_path = os.path.join(TEMP_DIR, "pixabay_bg.mp4")
        print(f"[*] جاري تحميل فيديو طبيعة ({query}) من Pixabay...")
        
        with requests.get(video_url, stream=True, timeout=90) as stream_res:
            stream_res.raise_for_status()
            with open(bg_path, "wb") as f:
                for chunk in stream_res.iter_content(chunk_size=1024 * 512):
                    f.write(chunk)
                    
        return bg_path
    except Exception as e:
        print(f"[!] خطأ أثناء سحب فيديو Pixabay: {e}")
        return None

# --- 5. محرك رسم الآيات بشفافية وتنسيق موزون ---
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

def render_ayah_slide(text, ayah_num, surah_name_ar, reciter_title, out_path):
    W, H = 1080, 1920
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # الترويسة: اسم السورة + القارئ
    header_font = ImageFont.truetype(FONT_PATH, 50, layout_engine=ImageFont.Layout.RAQM)
    header_text = f"سورة {surah_name_ar} | {reciter_title}"
    d.text((W // 2, 160), header_text, font=header_font, fill=(235, 195, 80, 255), anchor="ma", direction="rtl", language="ar")

    num_ar = str(ayah_num).translate(ARABIC_DIGITS)
    full_text = f"{text} \u06dd{num_ar}"

    size = 85
    while size >= 40:
        font = ImageFont.truetype(FONT_PATH, size, layout_engine=ImageFont.Layout.RAQM)
        lines = wrap_lines(full_text, font, W - 220)
        line_h = int(size * 2.0)
        if len(lines) * line_h <= 1100:
            break
        size -= 3

    y = (H - len(lines) * line_h) // 2
    for ln in lines:
        for dx, dy in [(-2, -2), (2, -2), (-2, 2), (2, 2), (0, 3)]:
            d.text((W // 2 + dx, y + dy), ln, font=font, fill=(0, 0, 0, 240), anchor="ma", direction="rtl", language="ar")
        d.text((W // 2, y), ln, font=font, fill=(255, 255, 255, 255), anchor="ma", direction="rtl", language="ar")
        y += line_h

    img.save(out_path, "PNG")

# --- 6. دمج المقطع مع Fade In / Out وعزل صوت الخلفية تماماً ---
def generate_video(surah_num=2, reciter_key="minshawi"):
    conn = init_db()
    reciter_info = RECITERS[reciter_key]
    
    print(f"[*] جلب بيانات سورة {surah_num} للقارئ {reciter_info['name']}...")
    surah_name_ar, verses_text, timestamps, audio_url = fetch_surah_data(surah_num, reciter_info["id"])

    clip_info = find_next_clip(conn, surah_num, timestamps, reciter_key)
    if not clip_info:
        print("[!] لا توجد آيات جديدة مؤهلة للإنتاج في هذه السورة.")
        return

    start_ayah, end_ayah, duration = clip_info
    print(f"[+] سورة {surah_name_ar}: من الآية {start_ayah} إلى {end_ayah} (المدة: {duration:.2f} ثانية)")

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

    # 2. توليد صور الآيات وتحديد أوقات الظهور
    slide_files = []
    for ayah in range(start_ayah, end_ayah + 1):
        slide_path = os.path.join(TEMP_DIR, f"slide_{ayah}.png")
        render_ayah_slide(verses_text[ayah], ayah, surah_name_ar, reciter_info["name"], slide_path)
        a_from = timestamps[ayah]["from"] - t_start
        a_to = timestamps[ayah]["to"] - t_start
        slide_files.append((slide_path, a_from, a_to))

    # 3. إعداد الخلفية وعزل أي صوت منها (تجاهل مسار الصوت بالكامل)
    bg_video = get_pixabay_video(duration)
    inputs = []

    if bg_video and os.path.exists(bg_video):
        # -an تضمن عدم قراءة أي صوت من فيديو Pixabay إطلاقاً
        inputs.extend(["-stream_loop", "-1", "-an", "-i", bg_video])
        base_filter = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,drawbox=t=fill:c=black@0.45[bg];"
        last_label = "[bg]"
