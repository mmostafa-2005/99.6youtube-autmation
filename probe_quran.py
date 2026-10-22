"""
فحص ثاني (بعد نتيجة الفحص الأول):
 أ) تصنيف فروق النص بين Quran.com و alquran.cloud: فروق شكلية ولا فروق كلمات حقيقية.
 ب) صحة التوقيت: بنقطّع كل آية من ملف السورة بالتوقيت (مفتاح timestamps)،
    ونقارن مدتها بمدة ملف الآية الجاهز. والمقاطع بتتحفظ في probe_output2/slices عشان تسمعها.
المطلوب: ffmpeg و ffprobe متثبتين، وquran_probe.py يكون في نفس الفولدر.
"""
import os
import shutil
import subprocess
import collections
import difflib
import requests

from quran_probe import (HEADERS, QCOM, get_json, normalize, same,
                         quran_com_chapter, alquran_cloud_all)

OUT = "probe_output2"
for sub in ("slices", "per_ayah", "chapters"):
    os.makedirs(os.path.join(OUT, sub), exist_ok=True)
REPORT = []

# سور الاختبار (غيّرها براحتك). فيها سورة 112 و سور قصيرة تانية
CHAPTERS = [112, 93, 108]
# أقصى فرق مقبول (ثواني) بين مدة المقطع ومدة ملف الآية الجاهز
TOL = 0.8

RECITERS = {
    "minshawi": {
        "chapter_id": 9,
        "per_ayah": {
            "qurancdn": "https://audio.qurancdn.com/Minshawi/Murattal/mp3/{c:03d}{v:03d}.mp3",
            "everyayah": "https://everyayah.com/data/Minshawy_Murattal_128kbps/{c:03d}{v:03d}.mp3",
        },
    },
    "dosari": {
        "chapter_id": 97,
        "per_ayah": {
            "everyayah": "https://everyayah.com/data/Yasser_Ad-Dussary_128kbps/{c:03d}{v:03d}.mp3",
        },
    },
}


def log(msg=""):
    print(msg, flush=True)
    REPORT.append(str(msg))


def run(name, fn):
    log("")
    log(f"===== {name} =====")
    try:
        return fn()
    except Exception as e:
        log(f"FAIL ({type(e).__name__}): {e}")
        return None


# ---------------------------------------------------------------
# أ) تصنيف الفروق
# ---------------------------------------------------------------
def test_classify_diffs():
    b = alquran_cloud_all()
    cats = collections.Counter()
    real = []
    for ch in range(1, 115):
        for k, v in quran_com_chapter(ch).items():
            na = normalize(v)
            nb = normalize(b.get(k, "").replace("\ufeff", ""))  # BOM في أول نص alquran.cloud
            if same(na, nb, k):
                cats["متطابقة"] += 1
                continue
            ha, hb = na.replace("ء", ""), nb.replace("ء", "")
            if ha == hb:
                cats["فرق في ترميز الهمزة فقط"] += 1
            elif ha.replace(" ", "") == hb.replace(" ", ""):
                cats["فرق في المسافات بين الكلمات (مع/بدون همزة)"] += 1
            else:
                cats["فرق حقيقي: لازم مراجعة"] += 1
                sm = difflib.SequenceMatcher(None, na, nb)
                d = [(op, na[i1:i2], nb[j1:j2])
                     for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]
                real.append(f"{k}: {d}\n   quran.com: {na}\n   alquran  : {nb}")
    for name, n in cats.most_common():
        log(f"  {name}: {n}")
    with open(os.path.join(OUT, "diffs_real.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(real))
    log(f"اتكتب {len(real)} فرق حقيقي في {OUT}/diffs_real.txt")
    for r in real[:30]:
        log("  " + r)


# ---------------------------------------------------------------
# ب) صحة التوقيت
# ---------------------------------------------------------------
def have_ffmpeg():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        capture_output=True, text=True)
    return float(r.stdout.strip())


def cut(src, start, dur, dst):
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", src,
         "-t", f"{dur:.3f}", "-acodec", "libmp3lame", "-q:a", "2", dst],
        check=True)


def download(url, path):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    r = requests.get(url, timeout=120, headers=HEADERS)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)


def test_timing():
    if not have_ffmpeg():
        log("FAIL: ffmpeg/ffprobe مش متثبتين (على Colab: !apt-get install -y ffmpeg)")
        return
    ok = check = 0
    for name, cfg in RECITERS.items():
        for ch in CHAPTERS:
            j = get_json(f"{QCOM}/chapter_recitations/{cfg['chapter_id']}/{ch}", segments="true")
            af = j["audio_file"]
            ts = af.get("timestamps") or []
            log(f"-- {name} سورة {ch}: {len(ts)} توقيت")
            if not ts:
                log("   WARN: مفيش توقيت للسورة دي")
                continue
            chap = os.path.join(OUT, "chapters", f"{name}_{ch:03d}.mp3")
            download(af["audio_url"], chap)
            real = duration(chap)
            log(f"   مدة الملف={real:.2f}s | آخر توقيت={ts[-1]['timestamp_to'] / 1000:.2f}s | "
                f"file_size في الـ API={af.get('file_size')} | الفعلي={os.path.getsize(chap)}")
            for t in ts:
                c, v = (int(x) for x in t["verse_key"].split(":"))
                start = t["timestamp_from"] / 1000
                slice_d = (t["timestamp_to"] - t["timestamp_from"]) / 1000
                cut(chap, start, slice_d, os.path.join(OUT, "slices", f"{name}_{c:03d}{v:03d}.mp3"))
                for src, tpl in cfg["per_ayah"].items():
                    p = os.path.join(OUT, "per_ayah", f"{name}_{src}_{c:03d}{v:03d}.mp3")
                    try:
                        download(tpl.format(c=c, v=v), p)
                        d = duration(p)
                    except Exception as e:
                        log(f"   {c}:{v} {src}: FAIL {e}")
                        continue
                    diff = d - slice_d
                    flag = "OK" if abs(diff) <= TOL else "CHECK"
                    ok += flag == "OK"
                    check += flag == "CHECK"
                    log(f"   {c}:{v} {src}: مقطع={slice_d:.2f}s | آية جاهزة={d:.2f}s | فرق={diff:+.2f} {flag}")
    log("")
    log(f"الخلاصة: OK={ok} CHECK={check} (الفروق الصغيرة طبيعية بسبب الوقفات؛ المهم الفروق بالثواني)")
    log(f"المقاطع للسماع في {OUT}/slices (اسمها: القارئ_سورة3أرقام+آية3أرقام)")


if __name__ == "__main__":
    run("أ. تصنيف فروق النص بين المصدرين", test_classify_diffs)
    run("ب. صحة التوقيت مقابل ملفات الآيات الجاهزة", test_timing)
    with open(os.path.join(OUT, "report2.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(REPORT))
    print(f"\nتم. النتيجة في {OUT}/report2.txt")
