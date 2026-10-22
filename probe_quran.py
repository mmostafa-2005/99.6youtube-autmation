"""
اختبار التحقق الشامل لمشروع القرآن الكريم (محدّث ومصحح).
"""

import difflib
import glob
import json
import os
import re
import time
import requests
from PIL import Image, ImageDraw, ImageFont, features

OUT = "probe_output"
os.makedirs(OUT, exist_ok=True)
REPORT = []
HEADERS = {"User-Agent": "quran-probe/1.0"}
QCOM = "https://api.quran.com/api/v4"
ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")


def log(msg=""):
    print(msg, flush=True)
    REPORT.append(str(msg))


def get_json(url, max_retries=3, **params):
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, timeout=60, headers=HEADERS)
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            if attempt == max_retries - 1:
                raise e
            time.sleep(2 * (attempt + 1))


def run(name, fn):
    log("")
    log(f"===== {name} =====")
    try:
        return fn()
    except Exception as e:
        log(f"FAIL ({type(e).__name__}): {e}")
        return None


# ---------------------------------------------------------------
# 1. RAQM
# ---------------------------------------------------------------
def test_raqm():
    ok = features.check("raqm")
    log("PASS: RAQM شغال" if ok else "FAIL: RAQM مش شغال (محتاج libraqm و libfribidi)")
    return ok


# ---------------------------------------------------------------
# 2. الخط
# ---------------------------------------------------------------
FONT = None


def test_font():
    global FONT
    for pat in ("AmiriQuran*.ttf", "*AmiriQuran*.ttf"):
        found = glob.glob("/usr/share/fonts/**/" + pat, recursive=True)
        if found:
            FONT = found[0]
            log("PASS: لقيت الخط في النظام: " + FONT)
            return
    url = "https://github.com/google/fonts/raw/main/ofl/amiriquran/AmiriQuran-Regular.ttf"
    r = requests.get(url, timeout=90, headers=HEADERS)
    r.raise_for_status()
    FONT = "/tmp/AmiriQuran-Regular.ttf"
    with open(FONT, "wb") as f:
        f.write(r.content)
    log("PASS: نزّلت الخط من GitHub: " + FONT)


# ---------------------------------------------------------------
# 3. النص + مطابقة بين مصدرين
# ---------------------------------------------------------------
def normalize(s):
    # إزالة التشكيل وعلامات الرسم القرآني
    s = re.sub(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]", "", s)
    s = re.sub(r"[أإآٱ]", "ا", s)
    s = s.replace("ى", "ي").replace("ة", "ه")
    return re.sub(r"\s+", " ", s).strip()


BASMALAH_NORM = normalize("بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ")


def strip_leading_basmalah(text_norm):
    if text_norm.startswith(BASMALAH_NORM):
        return text_norm[len(BASMALAH_NORM) :].strip()
    return text_norm


def quran_com_chapter(ch):
    j = get_json(QCOM + "/quran/verses/uthmani", chapter_number=ch)
    return {v["verse_key"]: v["text_uthmani"] for v in j["verses"]}


TEXT_112 = {}
TEXT_255 = ""


def test_text():
    global TEXT_112, TEXT_255
    TEXT_112 = quran_com_chapter(112)
    log(f"PASS: Quran.com رجّع {len(TEXT_112)} آيات لسورة 112")
    for k, v in TEXT_112.items():
        log(f"  {k}: {v}")
    j = get_json(QCOM + "/quran/verses/uthmani", verse_key="2:255")
    TEXT_255 = j["verses"][0]["text_uthmani"]
    log("PASS: آية الكرسي اتحمّلت (" + str(len(TEXT_255.split())) + " كلمة)")


def test_cross_check():
    other = get_json("https://api.alquran.cloud/v1/quran/quran-uthmani")
    b = {}
    for s in other["data"]["surahs"]:
        for a in s["ayahs"]:
            b[f"{s['number']}:{a['numberInSurah']}"] = a["text"]

    total, bad = 0, []
    for ch in range(1, 115):
        a = quran_com_chapter(ch)
        for k, v in a.items():
            total += 1
            na = normalize(v)
            nb = normalize(b.get(k, ""))

            # شرط صارم: في الآية الأولى من السور (غير الفاتحة والتوبة)، نقبل الفرق إذا كان البسملة تحديداً
            surah_num, ayah_num = map(int, k.split(":"))
            if ayah_num == 1 and surah_num not in (1, 9):
                nb = strip_leading_basmalah(nb)

            if na != nb:
                bad.append(k)

    if bad:
        log(f"WARN: {len(bad)} آية من {total} مختلفة بين المصدرين. أول 15: {bad[:15]}")
    else:
        log(f"PASS: {total} آية متطابقة (هيكل الحروف) بين المصدرين")
    log("ملاحظة: المطابقة لهيكل الحروف فقط، والتشكيل معتمد على مصحف Quran.com.")


# ---------------------------------------------------------------
# 4. الصوت والتوقيت
# ---------------------------------------------------------------
def resolve_audio_url(u):
    if u.startswith("http"):
        return u
    return "https://audio.qurancdn.com/" + u.lstrip("/")


def check_download(url):
    r = requests.get(url, stream=True, timeout=60, headers=HEADERS)
    status = r.status_code
    size = r.headers.get("content-length", "?")
    ctype = r.headers.get("content-type", "?")
    first = next(r.iter_content(2048), b"")
    r.close()
    return f"status={status} type={ctype} size={size} first_bytes={len(first)}"


def test_reciters_lists():
    pat = re.compile(r"minsh|mansh|dos|dus", re.I)
    hits = []

    try:
        j = get_json(QCOM + "/resources/chapter_reciters")
        log("-- chapter_reciters المطابقة:")
        for r in j.get("reciters", []):
            if pat.search(json.dumps(r, ensure_ascii=False)):
                log("  " + json.dumps(r, ensure_ascii=False))
    except Exception as e:
        log(f"WARN chapter_reciters فشل: {e}")

    try:
        j = get_json(QCOM + "/resources/recitations")
        log("-- recitations (آية-آية) المطابقة:")
        for r in j.get("recitations", []):
            if pat.search(json.dumps(r, ensure_ascii=False)):
                log("  " + json.dumps(r, ensure_ascii=False))
                hits.append(r["id"])
    except Exception as e:
        log(f"WARN recitations فشل: {e}")

    return hits


def test_chapter_audio(reciter_id, label):
    j = get_json(f"{QCOM}/chapter_recitations/{reciter_id}/112", segments="true")
    af = j["audio_file"]
    url = resolve_audio_url(af["audio_url"])
    log(f"PASS: {label} (reciter {reciter_id}) رجّع ملف صوت: {url}")
    log("  duration: " + str(af.get("duration")))
    vt = af.get("verse_timings") or []
    log(f"  عدد توقيتات الآيات: {len(vt)}")
    for t in vt[:4]:
        log(
            "  "
            + json.dumps(
                {
                    k: t.get(k)
                    for k in (
                        "verse_key",
                        "timestamp_from",
                        "timestamp_to",
                        "duration",
                    )
                }
            )
        )
    if not vt:
        log("  WARN: مفيش verse_timings، هنحتاج مصدر توقيت تاني")
    log("  تحميل: " + check_download(url))


def test_verse_audio(recitation_ids):
    for rid in recitation_ids:
        try:
            j = get_json(f"{QCOM}/recitations/{rid}/by_ayah/112:1")
            files = j.get("audio_files", [])
            if not files:
                log(f"  recitation {rid}: مفيش ملفات")
                continue
            url = resolve_audio_url(files[0]["url"])
            log(f"PASS: recitation {rid} آية 112:1 -> {url}")
            log("  تحميل: " + check_download(url))
        except Exception as e:
            log(f"  recitation {rid}: FAIL {e}")


# ---------------------------------------------------------------
# 5. الرسم
# ---------------------------------------------------------------
def glyph_coverage(text):
    from fontTools.ttLib import TTFont

    cmap = TTFont(FONT, fontNumber=0, lazy=True).getBestCmap()
    return sorted(
        {hex(ord(c)) for c in text if not c.isspace() and ord(c) not in cmap}
    )


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


def render_ayah(text, path, title):
    W, H = 1080, 1920
    img = Image.new("RGB", (W, H), (8, 20, 28))
    d = ImageDraw.Draw(img)
    d.rectangle((40, 40, W - 40, H - 40), outline=(212, 175, 55), width=4)

    size = 120  # رفع الحجم للآيات القصيرة
    while True:
        font = ImageFont.truetype(
            FONT, size, layout_engine=ImageFont.Layout.RAQM
        )
        lines = wrap_lines(text, font, W - 200)
        line_h = int(size * 2.1)  # تباعد أسطر أكبر لتفادي تداخل الحركات
        if len(lines) * line_h <= 1100 or size <= 40:
            break
        size -= 4

    small = ImageFont.truetype(FONT, 56, layout_engine=ImageFont.Layout.RAQM)
    d.text(
        (W // 2, 110),
        title,
        font=small,
        fill=(212, 175, 55),
        anchor="ma",
        direction="rtl",
        language="ar",
    )

    y = (H - len(lines) * line_h) // 2
    for ln in lines:
        d.text(
            (W // 2, y),
            ln,
            font=font,
            fill=(255, 255, 255),
            anchor="ma",
            direction="rtl",
            language="ar",
        )
        y += line_h

    img.save(path)
    return size, len(lines)


def test_render():
    samples = [
        ("112:1", TEXT_112.get("112:1", ""), "سورة الإخلاص", "probe_112_1.png"),
        ("112:4", TEXT_112.get("112:4", ""), "سورة الإخلاص", "probe_112_4.png"),
        ("2:255", TEXT_255, "سورة البقرة", "probe_2_255.png"),
    ]
    for key, text, title, fname in samples:
        if not text:
            log(f"SKIP {key}: النص مش متاح")
            continue
        num = key.split(":")[1].translate(ARABIC_DIGITS)
        full = f"{text} \u06dd{num}"
        missing = glyph_coverage(full)
        if missing:
            log(f"FAIL {key}: رموز ناقصة في الخط: {missing}")
        else:
            log(f"PASS {key}: الخط فيه كل رموز الآية")
        size, n = render_ayah(full, os.path.join(OUT, fname), title)
        log(f"  اترسمت: {fname} (خط {size}، {n} سطر)")


# ---------------------------------------------------------------
# 6. JSON الخام للتوقيت
# ---------------------------------------------------------------
def test_raw_timing():
    for rid in (9, 97):
        for params in ({"segments": "true"}, {}):
            try:
                j = get_json(f"{QCOM}/chapter_recitations/{rid}/112", **params)
                log(f"-- reciter {rid} params={params}")
                log(json.dumps(j, ensure_ascii=False)[:1500])
            except Exception as e:
                log(f"-- reciter {rid} params={params}: FAIL {e}")

    for rid in (9, 97):
        try:
            j = get_json(f"{QCOM}/quran/recitations/{rid}", chapter_number=112)
            log(
                f"-- quran/recitations/{rid}: "
                + json.dumps(j, ensure_ascii=False)[:800]
            )
        except Exception as e:
            log(f"-- quran/recitations/{rid}: FAIL {e}")


# ---------------------------------------------------------------
# 7. everyayah
# ---------------------------------------------------------------
def test_everyayah():
    base = "https://everyayah.com/data/"
    candidates = [
        "Minshawy_Murattal_128kbps",
        "Minshawy_Mujawwad_192kbps",
        "Yasser_Ad-Dussary_128kbps",
    ]
    for c in candidates:
        url = f"{base}{c}/001001.mp3"
        try:
            r = requests.head(
                url, timeout=30, headers=HEADERS, allow_redirects=True
            )
            log(
                f"{c}: status={r.status_code} size={r.headers.get('content-length')}"
            )
        except Exception as e:
            log(f"{c}: FAIL {e}")

    # محاولة فحص قائمة القراء الفعلية من الملف
    try:
        r = requests.get(
            "https://everyayah.com/data/recitations.js",
            timeout=30,
            headers=HEADERS,
        )
        if r.status_code == 200:
            lines = r.text.splitlines()
            log("-- تطابقات من everyayah recitations.js:")
            for line in lines:
                if re.search(r"minsh|duss|doss|yasser", line, re.I):
                    log("  " + line.strip()[:180])
    except Exception as e:
        log(f"recitations.js check FAIL: {e}")


# ---------------------------------------------------------------
# 8. فروق الآيات (Diffs)
# ---------------------------------------------------------------
def test_diff_276():
    other = get_json("https://api.alquran.cloud/v1/quran/quran-uthmani")
    b = {}
    for s in other["data"]["surahs"]:
        for a in s["ayahs"]:
            b[f"{s['number']}:{a['numberInSurah']}"] = a["text"]

    lines = []
    for ch in range(1, 115):
        for k, v in quran_com_chapter(ch).items():
            na = normalize(v)
            nb = normalize(b.get(k, ""))

            surah_num, ayah_num = map(int, k.split(":"))
            if ayah_num == 1 and surah_num not in (1, 9):
                nb = strip_leading_basmalah(nb)

            if na != nb:
                sm = difflib.SequenceMatcher(None, na, nb)
                diffs = [
                    (op, na[i1:i2], nb[j1:j2])
                    for op, i1, i2, j1, j2 in sm.get_opcodes()
                    if op != "equal"
                ]
                lines.append(f"{k}: {diffs}")

    with open(os.path.join(OUT, "diffs.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(f"اتكتب {len(lines)} فرق في probe_output/diffs.txt")
    for l in lines[:25]:
        log("  " + l)


# ---------------------------------------------------------------
# تشغيل الاختبارات بالترتيب
# ---------------------------------------------------------------
if __name__ == "__main__":
    run("1. محرك الرسم RAQM", test_raqm)
    run("2. الخط", test_font)
    run("3. نص الآيات", test_text)
    run("3b. مطابقة المصدرين (كل القرآن)", test_cross_check)
    rec_ids = run("4a. قوائم القراء", test_reciters_lists) or []
    run(
        "4b. صوت السورة كاملة + التوقيت: المنشاوي (9)",
        lambda: test_chapter_audio(9, "المنشاوي"),
    )
    run(
        "4c. صوت السورة كاملة + التوقيت: الدوسري (97)",
        lambda: test_chapter_audio(97, "الدوسري"),
    )
    run("4d. صوت آية-آية للقراء المطابقين", lambda: test_verse_audio(rec_ids))
    run("5. رسم الآيات بالخط", test_render)
    run("6. JSON الخام للتوقيت", test_raw_timing)
    run("7. everyayah", test_everyayah)
    run("8. تفاصيل الفروق الدقيقة", test_diff_276)

    with open(os.path.join(OUT, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT))

    print(
        "\nتم الانتهاء بالكامل. النتيجة في probe_output/report.txt و probe_output/diffs.txt"
    )
