from flask import Flask, render_template, request, send_file, redirect, url_for, flash
from werkzeug.utils import secure_filename
import pandas as pd
import re
import io

app = Flask(__name__)
app.secret_key = "dev-key"
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB

ALLOWED_EXT = {"xls", "xlsx"}

# ==== Sabit genişlikler (1–7 | 8–16 | 17–22) ====
FRONT_W, MID_W, LAST_W = 7, 9, 6
TOTAL_W = FRONT_W + MID_W + LAST_W

# Ön ve orta sabit kolona doldurulur, SON ASLA KESİLMEZ
def ford_pack(front: str, middle: str, last: str, *, strict: bool = False) -> str:
    if strict and (len(front) > FRONT_W or len(middle) > MID_W):
        raise ValueError("Ön/Orta alanlarından biri kendi alanına sığmıyor.")
    front  = (front or "")
    middle = (middle or "")
    last   = (last or "")
    return front.ljust(FRONT_W) + middle.ljust(MID_W) + last  # son olduğu gibi

# =========================
#  VD52 — Kurallar
# =========================
# Orta kod BAŞTAN eşleşecek desenler ve öncelik (rank büyük = daha iyi)
_PATTERNS = [
    (re.compile(r"^\d{2}C\d{3}"), 9),           # 14C022, 10C714, 15C868 (en yüksek)
    (re.compile(r"^[A-Z]\d{3}[A-Z]\d{2}"), 8),  # A405A02 (birleşik)
    (re.compile(r"^\d{2}[A-Z]\d{3}"), 7),       # 14F680, 17K747
    (re.compile(r"^\d[A-Z]\d{3}"), 6),          # 8C436
    (re.compile(r"^[A-Z]\d{5}"), 5),            # R17757
    (re.compile(r"^\d{5}"), 4),                 # 17757
    (re.compile(r"^\d{3}[A-Z]\d{2}"), 3),       # 405C54
    (re.compile(r"^[A-Z]\d{3}"), 2),            # A405
]

# Sağdan fallback için birleşik (overlap arama)
_FALLBACK_UNION = re.compile(
    r"(?=("
    r"\d{2}C\d{3}"
    r"|[A-Z]\d{3}[A-Z]\d{2}"
    r"|\d{2}[A-Z]\d{3}"
    r"|\d[A-Z]\d{3}"
    r"|[A-Z]\d{5}"
    r"|\d{5}"
    r"|\d{3}[A-Z]\d{2}"
    r"|[A-Z]\d{3}"
    r"))"
)

def _score_candidate(pattern_rank: int, son: str, mid: str, prefix_len: int):
    """
    Skor yüksek olan tercih edilir.
    - pattern_rank: büyük daha iyi
    - son_penalty: son rakamla başlıyorsa güçlü ceza, '^\d[A-Z]' ise ekstra ceza
    - mid_len: uzun olan hafifçe tercih edilir
    - prefix_len: eşitlikte 4 prefiksi 5'e göre çok küçük avantaj
    """
    son_penalty = 0
    if re.match(r"^\d", son):       # rakamla başlıyorsa güçlü ceza
        son_penalty += 3
    if re.match(r"^\d[A-Z]", son):  # 1R..., 7F... vb. ekstra ceza
        son_penalty += 2
    return (-son_penalty, pattern_rank, len(mid), 1 if prefix_len == 4 else 0)

def _try_with_prefix_and_pick(s: str, L: int):
    """
    Ön kodu L uzunluğunda alır, en iyi orta kodu seçer (skor ile).
    Özel C-kaydırma kuralını SADECE PS741 için uygular.
    """
    if len(s) < L:
        return None
    on = s[:L]
    rest = s[L:]

    # ÖZEL DURUM (C kaydırma) — SADECE PS741 için:
    # PS7418C436... -> PS741 | 8C436 | ...
    if L == 4 and s.startswith("PS741"):
        m_c6 = re.match(r"^(\d{2})([A-Z]\d{3})", rest)
        if m_c6 and m_c6.group(2).startswith("C") and len(s) >= 5:
            on5 = s[:5]
            rest5 = s[5:]
            m_5 = re.match(r"^\d[A-Z]\d{3}", rest5)
            if m_5:
                mid = m_5.group(0)
                son = rest5[m_5.end():]
                sc = _score_candidate(6, son, mid, 5)  # 8C436 rank=6
                return on5, mid, son, sc

    best = None  # (score_tuple, on, mid, son)
    for pat, rank in _PATTERNS:
        m = pat.match(rest)
        if not m:
            continue
        mid = m.group(0)
        son = rest[m.end():]
        sc = _score_candidate(rank, son, mid, L)
        if (best is None) or (sc > best[0]):
            best = (sc, on, mid, son)

    if best:
        _, on_b, mid_b, son_b = best
        return on_b, mid_b, son_b, best[0]
    return None

def vd52_format(raw):
    if pd.isna(raw):
        return ""

    # Temizle + baştaki 'M' varsa at
    s = re.sub(r"[\s\-_/\.,]", "", str(raw).upper())
    if s.startswith("M"):
        s = s[1:]

    cand4 = _try_with_prefix_and_pick(s, 4)
    cand5 = _try_with_prefix_and_pick(s, 5)

    chosen = None
    if cand4 and cand5:
        chosen = cand4 if cand4[3] > cand5[3] else cand5
    else:
        chosen = cand4 or cand5

    if chosen:
        on, mid, son, _ = chosen
    else:
        # Fallback: sağdan son uygun aday
        last = None
        for m in _FALLBACK_UNION.finditer(s):
            last = m
        if last:
            mid = last.group(1)
            on  = s[: last.start()]
            son = s[last.start() + len(mid):]
        else:
            # tamamen kurala uymayan değerler için kaba bölme
            if len(s) <= 6:
                on, mid, son = s, "", ""
            elif len(s) <= 12:
                on, mid, son = s[:-6], "", s[-6:]
            else:
                on, mid, son = s[:-12], s[-12:-6], s[-6:]

    return ford_pack(on, mid, son)

# =========================
#  Ana sayfa: VD52 üret
# =========================
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Lütfen bir Excel dosyası seçin.")
            return redirect(url_for("index"))

        ext = file.filename.rsplit(".", 1)[-1].lower()
        if ext not in ALLOWED_EXT:
            flash("Sadece .xls veya .xlsx dosyaları yükleyin.")
            return redirect(url_for("index"))

        try:
            df = pd.read_excel(file)
        except Exception as e:
            flash(f"Excel okunamadı: {e}")
            return redirect(url_for("index"))

        if "Oldmaterialnumber" not in df.columns:
            flash("Excel'de 'Oldmaterialnumber' sütunu bulunamadı.")
            return redirect(url_for("index"))

        df["VD52"] = df["Oldmaterialnumber"].apply(vd52_format)

        # ÇIKIŞ (xlsxwriter)
        out = io.BytesIO()
        with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
            sheet_name = "Sheet1"
            df.to_excel(writer, index=False, sheet_name=sheet_name)
            wb, ws = writer.book, writer.sheets[sheet_name]

            header_fmt = wb.add_format({
                "bold": True, "bg_color": "#EEF2FF", "border": 0, "bottom": 1, "align": "left"
            })
            for c, name in enumerate(df.columns):
                ws.write(0, c, name, header_fmt)

            vd52_col_idx = int(df.columns.get_loc("VD52"))
            vd52_fmt = wb.add_format({"font_name": "Consolas", "align": "left"})

            ws.autofilter(0, 0, len(df), len(df.columns)-1)
            ws.freeze_panes(1, 0)

            for c, name in enumerate(df.columns):
                series = df[name].astype(str)
                max_len = max(series.map(len).max(), len(name))
                width = min(max(max_len + 2, 10), 60)
                if c == vd52_col_idx:
                    width = max(width, 26)
                    ws.set_column(c, c, width, vd52_fmt)
                else:
                    ws.set_column(c, c, width)

        out.seek(0)
        return send_file(
            out,
            as_attachment=True,
            download_name="with_vd52.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    return render_template("index.html")

# =========================
#  /compare — iki Excel'in VD52 farklarını ver
# =========================
@app.route("/compare", methods=["GET", "POST"])
def compare():
    if request.method == "POST":
        f1 = request.files.get("file1")
        f2 = request.files.get("file2")

        if not f1 or not f2 or not f1.filename or not f2.filename:
            flash("Lütfen iki Excel dosyası da seçin.")
            return redirect(url_for("compare"))

        try:
            df1 = pd.read_excel(f1)
            df2 = pd.read_excel(f2)
        except Exception as e:
            flash(f"Excel okunamadı: {e}")
            return redirect(url_for("compare"))

        def _norm(df):
            if "VD52" not in df.columns:
                raise ValueError("Excel'de 'VD52' sütunu bulunamadı.")
            # trailing space'leri kırp; iç boşluklar korunur
            return df["VD52"].astype(str).str.rstrip()

        try:
            s1 = _norm(df1)
            s2 = _norm(df2)
        except ValueError as e:
            flash(str(e))
            return redirect(url_for("compare"))

        n = min(len(s1), len(s2))
        diffs = []
        for i in range(n):
            v1, v2 = s1.iloc[i], s2.iloc[i]
            if v1 != v2:
                diffs.append({"Satır": i + 1, "Dosya1_VD52": v1, "Dosya2_VD52": v2})

        if len(s1) > n:
            for i in range(n, len(s1)):
                diffs.append({"Satır": i + 1, "Dosya1_VD52": s1.iloc[i], "Dosya2_VD52": "<YOK>"})
        if len(s2) > n:
            for i in range(n, len(s2)):
                diffs.append({"Satır": i + 1, "Dosya1_VD52": "<YOK>", "Dosya2_VD52": s2.iloc[i]})

        if not diffs:
            flash("İki dosyanın VD52 sütunları tamamen aynı. Fark bulunamadı.")
            return redirect(url_for("compare"))

        out = io.BytesIO()
        diff_df = pd.DataFrame(diffs, columns=["Satır", "Dosya1_VD52", "Dosya2_VD52"])
        with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
            sh = "Farklar"
            diff_df.to_excel(writer, index=False, sheet_name=sh)
            wb, ws = writer.book, writer.sheets[sh]

            header_fmt = wb.add_format({"bold": True, "bg_color": "#EEF2FF", "bottom": 1})
            for c, name in enumerate(diff_df.columns):
                ws.write(0, c, name, header_fmt)

            ws.autofilter(0, 0, len(diff_df), len(diff_df.columns)-1)
            ws.freeze_panes(1, 0)

            for c, name in enumerate(diff_df.columns):
                max_len = max(len(name), diff_df[name].astype(str).map(len).max())
                width = min(max(max_len + 2, 10), 60)
                if name.endswith("_VD52"):
                    mono = wb.add_format({"font_name": "Consolas", "align": "left"})
                    ws.set_column(c, c, max(width, 26), mono)
                else:
                    ws.set_column(c, c, width)

        out.seek(0)
        return send_file(
            out,
            as_attachment=True,
            download_name="vd52_diff.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    return render_template("compare.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
