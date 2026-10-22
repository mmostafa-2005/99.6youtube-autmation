import os
import re
import random
import sqlite3
import subprocess
import requests
from PIL import Image, ImageDraw, ImageFont

# --- الإعدادات الأساسية ومتغيرات البيئة ---
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

MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024

# متغيرات GitHub و Make
MAKE_WEBHOOK_URL = os.environ.get("MAKE_WEBHOOK_URL")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")

# البحث التلقائي عن مسار خط المصحف
FONT_PATH = "/usr/share/fonts/opentype/fonts-hosny-amiri/AmiriQuran.ttf"
if not os.path.exists(FONT_PATH):
    import glob
    matches = glob.glob("/usr/share/fonts/**/AmiriQuran*.ttf", recursive=True)
    if matches:
        FONT_PATH = matches[0]

# --- 1. إدارة قاعدة البيانات ---
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

# --- 2. جلب نصوص الآيات والتوقيتات ---
def fetch_surah_data(surah_num, reciter_id):
    r_info = requests.get(f"{QCOM}/chapters/{surah_num}", headers=HEADERS, timeout=60)
    r_info.raise_for_status()
    surah_name_ar = r_info.json()["chapter"]["name_arabic"]

    r_text = requests.get(f"{QCOM}/quran/verses/uthmani", params={"chapter_number": surah_num}, headers=HEADERS, timeout=60)
    r_text.raise_for_status()
    verses_text = {int(v["verse_key"].split(":")[1]): v["text_uthmani"] for v in r_text.json()["verses"]}

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

# --- 3. اختيار المقطع غير المكرر ---
def find_next_clip(conn, surah_num, timestamps, reciter_key):
    total_ayahs = len(timestamps)
    c = conn.cursor()
    c.execute("SELECT MAX(end_ayah) FROM completed_clips WHERE surah=? AND reciter=?", (surah_num, reciter_key))
    row = c.fetchone()
    last_ended = row[0] if row and row[0] is not None else 0
    
    if last_ended >= total_ayahs:
        return None

    start = last_ended + 1
    end = start

    while end <= total_ayahs:
        dur = timestamps[end]["to"] - timestamps[start]["from"]
        if 25.0 <= dur <= 65.0:
            return start, end, dur
        if dur > 65.0:
            if start == end:
                return start, end, dur
            prev_dur = timestamps[end - 1]["to"] - timestamps[start]["from"]
            return start, end - 1, prev_dur
        end += 1

    rem_dur = timestamps[total_ayahs]["to"] - timestamps[start]["from"]
    if rem_dur >= 10.0:
        return start, total_ayahs, rem_dur

    return None

# --- 4. جلب فيديو الخلفية من Pixabay ---
def get_pixabay_video(min_duration):
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key:
        return None

    queries = ["waterfall", "sea ocean", "clouds sky", "mountains", "forest trees"]
    query = random.choice(queries)
    url = "https://pixabay.com/api/videos/"
    params = {"key": key, "q": query, "video_type": "film", "per_page": 50}

    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        hits = r.json().get("hits", [])
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
        dl_headers = {"User-Agent": "Mozilla/5.0"}
        with requests.get(video_url, headers=dl_headers, stream=True, timeout=90) as stream_res:
            stream_res.raise_for_status()
            with open(bg_path, "wb") as f:
                for chunk in stream_res.iter_content(chunk_size=1024 * 512):
                    f.write(chunk)

        if os.path.exists(bg_path) and os.path.getsize(bg_path) > 100000:
            return bg_path
    except Exception as e:
        print(f"[!] خطأ Pixabay: {e}")
    return None

# --- 5. تقسيم النصوص ورسم الشرائح بحجم 3 إلى 4 كلمات ---
def chunk_text(words, min_w=3, max_w=4):
    """تقسيم قائمة الكلمات إلى مقاطع متوازنة تتراوح بين 3 و 4 كلمات"""
    chunks = []
    i = 0
    total = len(words)
    while i < total:
        rem = total - i
        if rem <= max_w:
            take = rem
        elif rem == 5:
            take = 3
        else:
            take = max_w
        chunks.append(" ".join(words[i:i+take]))
        i += take
    return chunks

def render_phrase_slide(phrase_text, surah_name_ar, reciter_title, out_path):
    """رسم مقطع الكلمات في منتصف الشاشة بخط كبير وواضح"""
    W, H = 1080, 1920
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # عنوان السورة والقارئ بالأعلى
    header_font = ImageFont.truetype(FONT_PATH, 50, layout_engine=ImageFont.Layout.RAQM)
    header_text = f"سورة {surah_name_ar} | {reciter_title}"
    d.text((W // 2, 160), header_text, font=header_font, fill=(235, 195, 80, 255), anchor="ma", direction="rtl", language="ar")

    # تحديد حجم خط ملائم لعدد الكلمات
    font_size = 95
    font = ImageFont.truetype(FONT_PATH, font_size, layout_engine=ImageFont.Layout.RAQM)
    
    while font.getlength(phrase_text, direction="rtl", language="ar") > (W - 200) and font_size > 50:
        font_size -= 5
        font = ImageFont.truetype(FONT_PATH, font_size, layout_engine=ImageFont.Layout.RAQM)

    y = H // 2 - 40
    # رسم الظل لزيادة الوضوح
    for dx, dy in [(-3, -3), (3, -3), (-3, 3), (3, 3), (0, 4)]:
        d.text((W // 2 + dx, y + dy), phrase_text, font=font, fill=(0, 0, 0, 240), anchor="ma", direction="rtl", language="ar")
    # رسم النص الأبيض
    d.text((W // 2, y), phrase_text, font=font, fill=(255, 255, 255, 255), anchor="ma", direction="rtl", language="ar")

    img.save(out_path, "PNG")

# --- 6. الإنتاج النهائي عبر FFmpeg ---
def generate_video(surah_num=2, reciter_key="minshawi"):
    conn = init_db()
    reciter_info = RECITERS[reciter_key]
    
    print(f"[*] جلب بيانات سورة {surah_num} للقارئ {reciter_info['name']}...")
    surah_name_ar, verses_text, timestamps, audio_url = fetch_surah_data(surah_num, reciter_info["id"])

    clip_info = find_next_clip(conn, surah_num, timestamps, reciter_key)
    if not clip_info:
        print("[!] لا توجد آيات جديدة مؤهلة للإنتاج في هذه السورة.")
        return None, None

    start_ayah, end_ayah, duration = clip_info
    print(f"[+] سورة {surah_name_ar}: الآيات {start_ayah}-{end_ayah} (المدة: {duration:.2f} ثانية)")

    # 1. تحميل وتجهيز الصوت
    full_audio_path = os.path.join(TEMP_DIR, f"full_{surah_num}_{reciter_key}.mp3")
    if not os.path.exists(full_audio_path):
        r = requests.get(audio_url, headers=HEADERS, timeout=120)
        with open(full_audio_path, "wb") as f:
            f.write(r.content)

    t_start = timestamps[start_ayah]["from"]
    t_end = timestamps[end_ayah]["to"]
    cut_audio = os.path.join(TEMP_DIR, "cut.mp3")
    subprocess.run(["ffmpeg", "-y", "-ss", str(t_start), "-to", str(t_end), "-i", full_audio_path, "-acodec", "copy", cut_audio], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 2. توليد شرائح الكلمات
    slide_files = []
    slide_counter = 0

    for ayah in range(start_ayah, end_ayah + 1):
        num_ar = str(ayah).translate(ARABIC_DIGITS)
        ayah_words = verses_text[ayah].split()
        
        if ayah_words:
            ayah_words[-1] = f"{ayah_words[-1]} \u06dd{num_ar}"
            
        chunks = chunk_text(ayah_words, min_w=3, max_w=4)
        num_chunks = len(chunks)
        
        a_from = timestamps[ayah]["from"] - t_start
        a_to = timestamps[ayah]["to"] - t_start
        ayah_duration = max(0.1, a_to - a_from)
        chunk_duration = ayah_duration / num_chunks

        for i, chunk in enumerate(chunks):
            slide_path = os.path.join(TEMP_DIR, f"phrase_{slide_counter}.png")
            render_phrase_slide(chunk, surah_name_ar, reciter_info["name"], slide_path)
            
            c_start = a_from + (i * chunk_duration)
            c_end = a_from + ((i + 1) * chunk_duration)
            slide_files.append((slide_path, c_start, c_end))
            slide_counter += 1

    # 3. تجهيز الخلفية
    bg_video = get_pixabay_video(duration)
    inputs = []

    if bg_video and os.path.exists(bg_video):
        inputs.extend(["-stream_loop", "-1", "-an", "-i", bg_video])
        base_filter = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,drawbox=t=fill:c=black@0.45[bg];"
        last_label = "[bg]"
    else:
        inputs.extend(["-f", "lavfi", "-i", f"color=c=0x08131d:s=1080x1920:d={duration}"])
        base_filter = ""
        last_label = "[0:v]"

    # 4. دمج الشرائح مع حركة Fade تدريجية
    fade_d = 0.2
    filter_chains = base_filter
    idx = 1

    for path, c_from, c_to in slide_files:
        inputs.extend(["-loop", "1", "-t", str(duration), "-i", path])
        f_in = c_from
        f_out = max(c_from, c_to - fade_d)

        slide_faded = f"[slide_faded_{idx}]"
        filter_chains += (
            f"[{idx}:v]format=yuva420p,"
            f"fade=t=in:st={f_in}:d={fade_d}:alpha=1,"
            f"fade=t=out:st={f_out}:d={fade_d}:alpha=1{slide_faded};"
        )
        
        next_label = f"[v_mix_{idx}]"
        filter_chains += f"{last_label}{slide_faded}overlay=0:0:enable='between(t,{c_from},{c_to})'{next_label};"
        last_label = next_label
        idx += 1

    out_file = os.path.join(OUT_DIR, f"Surah_{surah_num}_{start_ayah}-{end_ayah}_{reciter_key}.mp4")

    # إعدادات الضغط والتصدير النهائي
    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-i", cut_audio,
        "-filter_complex", filter_chains.rstrip(";"),
        "-map", last_label,
        "-map", f"{idx}:a",
        "-c:v", "libx264",
        "-crf", "28",
        "-preset", "veryfast",
        "-b:v", "1500k",
        "-maxrate", "2000k",
        "-bufsize", "4000k",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "96k",
        "-t", str(duration),
        out_file
    ]

    print("[*] جاري المعالجة وتصدير الفيديو النهائي عبر FFmpeg...")
    subprocess.run(cmd, check=True)

    actual_size = os.path.getsize(out_file)
    size_mb = actual_size / (1024 * 1024)
    print(f"[*] حجم الفيديو النهائي: {size_mb:.2f} ميجابايت")

    if actual_size > MAX_FILE_SIZE_BYTES:
        raise ValueError(f"حجم الفيديو تجاوز 100 ميجا: {size_mb:.2f} MB")

    mark_clip_done(conn, surah_num, start_ayah, end_ayah, reciter_key, duration)
    print(f"[✓] تم تصدير الفيديو بنجاح: {out_file}")

    clip_title = f"سورة {surah_name_ar} (الآيات {start_ayah}-{end_ayah}) | القارئ {reciter_info['name']} #قرآن #shorts"
    return out_file, clip_title

# --- 7. رفع الفيديو إلى GitHub Releases وإشعار Make.com ---
def upload_to_github_release(video_path, tag_name="latest"):
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        print("[!] بيانات GitHub Token غير متوفرة.")
        return None

    file_name = os.path.basename(video_path)
    print(f"[*] جاري رفع الفيديو {file_name} إلى GitHub Releases...")

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

    release_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/tags/{tag_name}"
    res = requests.get(release_url, headers=headers)
    
    if res.status_code == 200:
        release_id = res.json()["id"]
        for asset in res.json().get("assets", []):
            if asset["name"] == file_name:
                requests.delete(asset["url"], headers=headers)
    else:
        create_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases"
        data = {"tag_name": tag_name, "name": "Latest Quran Clip", "draft": False, "prerelease": False}
        res_create = requests.post(create_url, headers=headers, json=data)
        res_create.raise_for_status()
        release_id = res_create.json()["id"]

    upload_url = f"https://uploads.github.com/repos/{GITHUB_REPOSITORY}/releases/{release_id}/assets?name={file_name}"
    upload_headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "video/mp4"
    }
    
    with open(video_path, "rb") as f:
        upload_res = requests.post(upload_url, headers=upload_headers, data=f)
        upload_res.raise_for_status()
        
    direct_url = upload_res.json()["browser_download_url"]
    print(f"[✓] تم رفع الفيديو بنجاح: {direct_url}")
    return direct_url

def notify_make(video_url, video_title):
    if not MAKE_WEBHOOK_URL:
        print("[!] تنبيه: MAKE_WEBHOOK_URL غير موجود.")
        return

    payload = {"video_url": video_url, "title": video_title}
    print("[*] إرسال بيانات النشر إلى Make.com...")
    res = requests.post(MAKE_WEBHOOK_URL, json=payload)
    if res.status_code in [200, 204]:
        print("[✓] استلم Make.com الطلب بنجاح وسيبدأ النشر الآن!")
    else:
        print(f"[!] خطأ أثناء إرسال البيانات إلى Make: {res.status_code} - {res.text}")

if __name__ == "__main__":
    video_path, title = generate_video(surah_num=2, reciter_key="minshawi")
    if video_path and title:
        direct_link = upload_to_github_release(video_path)
        if direct_link:
            notify_make(direct_link, title)
