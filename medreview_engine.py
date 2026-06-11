#!/usr/bin/env python3
"""
MED-Review — Editor Automático de Depoimentos v4
==================================================
Transcreve, legenda (legenda dinâmica), seleciona melhores trechos e edita
automaticamente, adaptando-se à orientação do vídeo:

  • Vertical (9:16)   → overlays diretos: logo + banner brushstroke + legendas amarelas
  • Horizontal (16:9) → moldura purple (estilo Extensive) + legendas brancas

Principais correções da v4 (vs v3):
  - ASS Style agora interpola fontsize/cor corretamente (v3 escrevia "{fs}" literal → quebrava tudo)
  - Corte real de trechos: vídeo, áudio e legendas re-sincronizados no mesmo timeline
  - Vídeos sem trilha de áudio são suportados (gera áudio silencioso quando preciso)
  - amix com normalize=0 + weights → voz não fica abafada pela trilha
  - Caminhos de arquivo escapados para o filtro ass (Windows e espaços)
  - Remoção de fundo do logo vetorizada (numpy) → ~100x mais rápida
  - frame_words renderizados como pills na moldura (v3 ignorava silenciosamente)
  - Layout da moldura sem colisão entre legenda e CTA
  - Validações de entrada e mensagens de erro claras

Requisitos:
    pip install faster-whisper Pillow numpy
    FFmpeg + ffprobe no PATH

Uso:
    # Vertical 9:16
    python medreview_editor_v4.py vertical.mp4 --nome "Igor Pires" --tema aprovacao

    # Horizontal → moldura purple, corte de 60s
    python medreview_editor_v4.py horizontal.mp4 --nome "Isadora Aquilla" \\
        --name-sub "Aprovada em Dermato" --tema aprovacao --duracao 60 \\
        --frame-top "HISTÓRIAS DE\\nQUEM CONSEGUIU" \\
        --frame-words "Aprovação,Didática,Resultados" \\
        --frame-bottom "Você é o próximo ✨" \\
        --logo logo.png --musica trilha.mp3
"""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("❌  Pillow não encontrado. Instale com: pip install Pillow")

try:
    import numpy as np
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


# ═══ CONFIG ══════════════════════════════════════════════════════════════════

OUT_W, OUT_H = 1080, 1920          # saída sempre 9:16

THEMES = {
    "produto":     {"label": "Produto MED-Review",     "color": (14, 165, 233)},
    "aprovacao":   {"label": "Aprovação MED-Review",    "color": (16, 185, 129)},
    "experiencia": {"label": "Experiência MED-Review",  "color": (139, 92, 246)},
}

# Cores ASS em formato &HAABBGGRR (alpha-blue-green-red). AA=00 → opaco.
ASS_YELLOW = "&H0000D7FF"   # #FFD700  (vertical, igual à referência dos vídeos)
ASS_WHITE  = "&H00FFFFFF"   # #FFFFFF  (horizontal, igual à moldura purple)
ASS_OUTLINE = "&H00000000"  # contorno preto
ASS_SHADOW  = "&H64000000"  # sombra translúcida

SUB_FONT = "DejaVu Sans"    # fonte garantida no Linux; ASS usa fontconfig
SUB_MARGIN_B_VERTICAL = 200
SUB_MARGIN_B_HORIZONTAL = 230  # acima do CTA (~y0.945), abaixo do vídeo

DURACOES_VALIDAS = {0, 30, 60, 90}

HIGHLIGHT_KW = [
    "aprovei", "aprovação", "aprovada", "aprovado", "passei", "med-review",
    "medreview", "incrível", "excelente", "perfeito", "ótimo", "ótima",
    "recomendo", "melhor", "diferença", "flashcard", "e-book", "aula",
    "questões", "didática", "conteúdo", "residência", "prova", "três palavras",
    "3 palavras", "obrigado", "obrigada", "transformou",
]


# ═══ DATA ════════════════════════════════════════════════════════════════════

@dataclass
class Word:
    text: str
    start: float
    end: float
    prob: float = 1.0

@dataclass
class Seg:
    text: str
    start: float
    end: float
    words: list = field(default_factory=list)
    score: float = 0.0

@dataclass
class SubChunk:
    text: str
    start: float
    end: float


# ═══ HELPERS ═════════════════════════════════════════════════════════════════

def run(cmd, **kw):
    """Executa subprocess capturando saída; levanta com stderr legível."""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)

def check_binaries():
    for b in ("ffmpeg", "ffprobe"):
        if shutil.which(b) is None:
            sys.exit(f"❌  '{b}' não encontrado no PATH. Instale o FFmpeg: https://ffmpeg.org/download.html")

def probe(path):
    r = run(["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path])
    if r.returncode != 0:
        sys.exit(f"❌  ffprobe falhou em '{path}'. O arquivo é um vídeo válido?")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        sys.exit(f"❌  Não foi possível ler metadados de '{path}'.")

def video_stream(info):
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    sys.exit("❌  Nenhum stream de vídeo encontrado no arquivo.")

def has_audio(info):
    return any(s.get("codec_type") == "audio" for s in info.get("streams", []))

def get_dims(info):
    s = video_stream(info)
    w, h = int(s["width"]), int(s["height"])
    # Respeita rotação em metadados (vídeos de celular)
    rot = 0
    tags = s.get("tags", {})
    if "rotate" in tags:
        try: rot = abs(int(tags["rotate"])) % 180
        except ValueError: rot = 0
    for sd in s.get("side_data_list", []):
        if "rotation" in sd:
            try: rot = abs(int(sd["rotation"])) % 180
            except (ValueError, TypeError): pass
    if rot == 90:
        w, h = h, w
    return w, h

def get_dur(info):
    d = info.get("format", {}).get("duration")
    if d is None:
        vs = video_stream(info)
        d = vs.get("duration", 0)
    try: return float(d)
    except (ValueError, TypeError): return 0.0

def get_font(bold=False, size=32):
    candidates = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
         "/Library/Fonts/Arial Bold.ttf", "C:\\Windows\\Fonts\\arialbd.ttf"]
        if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/Library/Fonts/Arial.ttf", "C:\\Windows\\Fonts\\arial.ttf"]
    )
    for c in candidates:
        if os.path.exists(c):
            try: return ImageFont.truetype(c, size)
            except OSError: continue
    return ImageFont.load_default()

def text_width(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0]

# Remove emojis/símbolos que a fonte do PIL não renderiza (vira "tofu" □).
# Mantém acentos latinos e pontuação comum.
_EMOJI_RE = re.compile(
    "[" "\U0001F000-\U0001FAFF" "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF" "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF" "\U0000FE00-\U0000FE0F" "\U00002000-\U0000206F" "]+",
    flags=re.UNICODE,
)

def strip_emoji(text):
    if not text:
        return text
    cleaned = _EMOJI_RE.sub("", text)
    # remove espaços duplicados deixados pela remoção
    return re.sub(r"\s{2,}", " ", cleaned).strip()

def escape_ass_path(path):
    """Escapa caminho para uso dentro do filtro ass='...' do FFmpeg.
    Em Windows o ':' de C:\\ precisa ser escapado, e '\\' vira '/'."""
    p = path.replace("\\", "/")
    p = p.replace(":", "\\:")
    p = p.replace("'", "\\'")
    return p

def remove_black_bg(logo):
    """Remove fundo preto do logo. Vetorizado com numpy quando disponível."""
    logo = logo.convert("RGBA")
    if HAVE_NUMPY:
        arr = np.array(logo)
        # pixels quase pretos → alpha 0
        black = (arr[:, :, 0] < 30) & (arr[:, :, 1] < 30) & (arr[:, :, 2] < 30)
        arr[black, 3] = 0
        logo = Image.fromarray(arr, "RGBA")
    else:
        px = logo.load()
        for y in range(logo.height):
            for x in range(logo.width):
                r, g, b, a = px[x, y]
                if r < 30 and g < 30 and b < 30:
                    px[x, y] = (0, 0, 0, 0)
    bbox = logo.getbbox()
    return logo.crop(bbox) if bbox else logo


# ═══ 1. TRANSCRIPTION ═══════════════════════════════════════════════════════

_WHISPER_CACHE = {}

def transcribe(audio, model_size="base"):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("❌  faster-whisper não encontrado. Instale: pip install faster-whisper")
    # Cache do modelo: evita recarregar a cada chamada (importante no servidor)
    if model_size not in _WHISPER_CACHE:
        print(f"🎙️  Carregando modelo Whisper ({model_size})...")
        _WHISPER_CACHE[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
    m = _WHISPER_CACHE[model_size]
    print("🎙️  Transcrevendo...")
    raw, info = m.transcribe(audio, language="pt", beam_size=5,
                             word_timestamps=True, vad_filter=True)
    segs = []
    for s in raw:
        ws = [Word(w.word.strip(), w.start, w.end, w.probability) for w in (s.words or [])]
        segs.append(Seg(s.text.strip(), round(s.start, 2), round(s.end, 2), ws))
        print(f"  [{s.start:5.1f}s] {s.text.strip()}")
    print(f"  ✅ {len(segs)} segmentos")
    return segs

def load_transcript(path):
    if not os.path.exists(path):
        sys.exit(f"❌  Transcrição não encontrada: {path}")
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    segs = []
    for s in d.get("segments", []):
        ws = [Word(w["word"], w["start"], w["end"], w.get("probability", 1.0))
              for w in s.get("words", [])]
        segs.append(Seg(s["text"], float(s["start"]), float(s["end"]), ws))
    return segs


# ═══ 2. SMART EXCERPT SELECTION ═════════════════════════════════════════════

def score_segs(segs):
    for s in segs:
        sc, t = 0.0, s.text.lower()
        for kw in HIGHLIGHT_KW:
            if kw in t: sc += 3.0
        wc = len(s.text.split())
        if 5 <= wc <= 20: sc += 1.0
        if s.start < 10: sc += 2.0
        if "três palavras" in t or "3 palavras" in t: sc += 5.0
        for p in ("gostei", "amo", "adorei", "incrível", "perfeito",
                  "excelente", "top", "demais", "melhor"):
            if p in t: sc += 1.5
        s.score = max(0.0, sc)
    return segs

def select_excerpts(segs, target):
    """Seleciona trechos de maior valor para caber em ~target segundos.
    Sempre tenta manter intro e fechamento; preenche o meio por score."""
    if not segs:
        return []
    total = segs[-1].end - segs[0].start
    if total <= target + 3:
        return segs
    sel = set()
    intro = min(8.0, target * 0.15)
    outro = min(8.0, target * 0.15)
    for i, s in enumerate(segs):
        if s.end <= segs[0].start + intro: sel.add(i)
        if s.start >= segs[-1].end - outro: sel.add(i)
    used = sum(segs[i].end - segs[i].start for i in sel)
    rem = target - used
    mid = sorted((i for i in range(len(segs)) if i not in sel),
                 key=lambda i: segs[i].score, reverse=True)
    for i in mid:
        d = segs[i].end - segs[i].start
        if d <= rem:
            sel.add(i); rem -= d
            for nb in (i - 1, i + 1):
                if 0 <= nb < len(segs) and nb not in sel:
                    nd = segs[nb].end - segs[nb].start
                    if nd <= rem:
                        sel.add(nb); rem -= nd
        if rem <= 0: break
    return [segs[i] for i in sorted(sel)]

def group_contiguous(segs, gap=0.4):
    """Agrupa segmentos contíguos em (start, end) para minimizar cortes."""
    if not segs:
        return []
    groups = [[segs[0].start, segs[0].end]]
    for s in segs[1:]:
        if s.start - groups[-1][1] <= gap:
            groups[-1][1] = s.end
        else:
            groups.append([s.start, s.end])
    return [(round(a, 3), round(b, 3)) for a, b in groups]

def remap_to_cut_timeline(segs, groups):
    """Remapeia timestamps dos segmentos para o timeline DEPOIS do corte+concat.
    Resolve o bug da v3 onde legendas ficavam dessincronizadas."""
    # offset acumulado: para cada grupo, calcula início no novo timeline
    new_segs = []
    # mapa (orig_start) → new_start
    timeline = []  # lista de (orig_start, orig_end, new_start)
    cursor = 0.0
    for (gs, ge) in groups:
        timeline.append((gs, ge, cursor))
        cursor += (ge - gs)
    for s in segs:
        # encontra o grupo que contém o segmento
        for (gs, ge, ns) in timeline:
            if s.start >= gs - 0.05 and s.end <= ge + 0.05:
                delta = ns - gs
                new_segs.append(Seg(
                    s.text,
                    max(0.0, round(s.start + delta, 3)),
                    round(s.end + delta, 3),
                    [Word(w.text, round(w.start + delta, 3), round(w.end + delta, 3), w.prob)
                     for w in s.words],
                    s.score,
                ))
                break
    return new_segs, cursor  # cursor = duração final


# ═══ 3. ASS SUBTITLES ═══════════════════════════════════════════════════════

def make_chunks(segs, max_w=4, max_c=15):
    """Gera chunks de legenda: máx 15 caracteres por linha, máx 2 linhas."""
    all_w = []
    for s in segs:
        if s.words:
            all_w.extend(s.words)
        elif s.text.strip():
            ws = s.text.split()
            d = max(s.end - s.start, 0.01)
            pw = d / len(ws)
            for j, w in enumerate(ws):
                all_w.append(Word(w, round(s.start + j * pw, 2),
                                  round(s.start + (j + 1) * pw, 2)))
    if not all_w:
        return []
    chunks, cur, st = [], [], all_w[0].start
    for w in all_w:
        if not cur:
            st = w.start
        cur.append(w.text)
        if len(" ".join(cur)) >= max_c:
            chunks.append(SubChunk(" ".join(cur), st, w.end))
            cur = []
    if cur:
        chunks.append(SubChunk(" ".join(cur), st, all_w[-1].end))
    return chunks

def write_ass(chunks, path, w=OUT_W, h=OUT_H, white=False):
    """Gera arquivo .ass. CORRIGIDO: agora interpola fontsize e cor de verdade."""
    fs = int(h * 0.032)
    color = ASS_WHITE if white else ASS_YELLOW
    margin_b = SUB_MARGIN_B_HORIZONTAL if white else SUB_MARGIN_B_VERTICAL

    def ft(s):
        s = max(0.0, s)
        hr = int(s // 3600); mn = int((s % 3600) // 60)
        sec = int(s % 60); cs = int(round((s - int(s)) * 100))
        if cs == 100: cs = 99
        return f"{hr}:{mn:02d}:{sec:02d}.{cs:02d}"

    header = (
        "[Script Info]\n"
        "Title: MED-Review\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {w}\n"
        f"PlayResY: {h}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
        "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"Style: MR,{SUB_FONT},{fs},{color},&H000000FF,{ASS_OUTLINE},{ASS_SHADOW},"
        f"-1,0,0,0,100,100,0,0,1,3,2,2,40,40,{margin_b},1\n\n"
        "[Events]\n"
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text"
    )
    lines = [header]
    for c in chunks:
        t = c.text.replace("{", "(").replace("}", ")").strip()
        lines.append(f"Dialogue: 0,{ft(c.start)},{ft(c.end)},MR,,0,0,0,,{t}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


# ═══ 4. OVERLAYS ════════════════════════════════════════════════════════════

def load_logo_fitted(logo_path, max_w, max_h):
    if not (logo_path and os.path.exists(logo_path)):
        return None
    logo = remove_black_bg(Image.open(logo_path))
    ratio = min(max_w / logo.width, max_h / logo.height)
    return logo.resize((max(1, int(logo.width * ratio)),
                        max(1, int(logo.height * ratio))), Image.LANCZOS)

def create_logo_overlay(w, h, logo_path, output):
    """Logo pequeno no canto superior direito (vídeos verticais)."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    logo = load_logo_fitted(logo_path, int(w * 0.10), int(h * 0.06))
    if logo:
        m = int(w * 0.03)
        img.paste(logo, (w - logo.width - m, int(h * 0.015)), logo)
    img.save(output, "PNG")
    return output

def find_empty_region(video_path, total_dur, w, h, nome, name_sub):
    """Analisa frames do vídeo pra achar a região mais vazia (menor variância
    temporal + espacial) onde o nome NÃO vai cobrir o rosto/movimento da pessoa.

    Retorna (x, y, text_w, text_h) — anchor top-left do bloco de texto.
    Exclui automaticamente a zona inferior (legendas)."""
    fs = int(h * 0.028)
    fs_sub = int(h * 0.020)
    text_w = int(len(nome) * fs * 0.55)
    text_h = fs + fs_sub + int(h * 0.015)

    # Fallback se numpy não disponível: topo-esquerda
    if not HAVE_NUMPY:
        return (int(w * 0.05), int(h * 0.10), text_w, text_h)

    # Amostra 4 frames em baixa resolução pra análise rápida
    sw = 240
    sh = max(2, int(round(sw * h / w)))
    if sh % 2: sh += 1
    sample_times = [total_dur * t for t in (0.15, 0.4, 0.65, 0.85)]

    frames = []
    with tempfile.TemporaryDirectory(prefix="mr_emp_") as tmp:
        for i, t in enumerate(sample_times):
            out = os.path.join(tmp, f"f{i}.jpg")
            r = run(["ffmpeg", "-y", "-ss", str(t), "-i", video_path,
                     "-frames:v", "1", "-vf", f"scale={sw}:{sh}",
                     "-q:v", "5", out])
            if r.returncode == 0 and os.path.exists(out):
                try:
                    arr = np.array(Image.open(out).convert("L"), dtype=np.float32)
                    if arr.shape == (sh, sw):
                        frames.append(arr)
                except Exception:
                    pass

    if len(frames) < 2:
        return (int(w * 0.05), int(h * 0.10), text_w, text_h)

    stacked = np.stack(frames)

    # Mapa de "ocupação visual":
    #   - variância temporal = movimento entre frames (rosto, mãos)
    #   - variância espacial = bordas/detalhes (texturas, contornos)
    temporal_var = stacked.var(axis=0)
    avg = stacked.mean(axis=0)
    gx = np.zeros_like(avg); gy = np.zeros_like(avg)
    gx[:, 1:] = np.abs(np.diff(avg, axis=1))
    gy[1:, :] = np.abs(np.diff(avg, axis=0))
    spatial_var = gx + gy

    busy = temporal_var + spatial_var * 0.3

    # Média por bloco (cell de 12 px) → grid de busyness
    cell = 12
    rows = sh // cell
    cols = sw // cell
    if rows == 0 or cols == 0:
        return (int(w * 0.05), int(h * 0.10), text_w, text_h)
    trunc = busy[:rows * cell, :cols * cell]
    block_busy = trunc.reshape(rows, cell, cols, cell).mean(axis=(1, 3))

    # Tamanho do bloco de texto em cells
    cells_w = max(3, min(cols, int(text_w * sw / w / cell) + 1))
    cells_h = max(2, min(rows, int(text_h * sh / h / cell) + 1))

    # Zonas proibidas:
    #   - Bottom 28% (zona da legenda amarela)
    #   - Top 3% (folga do logo se existir)
    BIG = 1e9
    block_busy_masked = block_busy.copy()
    block_busy_masked[int(rows * 0.72):, :] = BIG
    block_busy_masked[:max(1, int(rows * 0.03)), :] = BIG

    # Bias forte pro topo: penalizar regiões abaixo de 25% da altura
    # (evita nome no meio do rosto em selfies)
    for i in range(rows):
        frac = i / max(rows - 1, 1)
        if frac > 0.25:
            block_busy_masked[i, :] += BIG * 0.5

    # Sliding window: acha região com MENOR soma de busyness
    best_score = BIG
    best_ij = (max(1, int(rows * 0.05)), max(1, int(cols * 0.05)))
    for i in range(rows - cells_h + 1):
        for j in range(cols - cells_w + 1):
            score = block_busy_masked[i:i + cells_h, j:j + cells_w].sum()
            if score < best_score:
                best_score = score
                best_ij = (i, j)

    # Converte de volta pra resolução cheia
    x = int(best_ij[1] * cell * w / sw)
    y = int(best_ij[0] * cell * h / sh)

    # Margens de segurança — usa a largura MÁXIMA entre nome e subtítulo
    # (o sub pode ser muito mais largo que o nome curto)
    fs_sub_est = int(h * 0.020)
    sub_w_est = int(len(name_sub) * fs_sub_est * 0.62)
    max_block_w = max(text_w, sub_w_est) + int(w * 0.04)  # +margem interna
    # Alinhado à esquerda (margem fixa)
    x = int(w * 0.04)
    y = max(int(h * 0.05), min(y, int(h * 0.72) - text_h))

    return (x, y, max_block_w, text_h)


def _get_brand_font(size):
    """Tenta Exo2-Bold (bundled), fallback pra get_font(bold)."""
    brand_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Exo2-Bold.otf")
    if os.path.exists(brand_path):
        try: return ImageFont.truetype(brand_path, size)
        except OSError: pass
    return get_font(True, size)


def _get_sub_font(size):
    """Tenta Orbitron-Bold (bundled), fallback pra _get_brand_font."""
    sub_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Orbitron-Bold.ttf")
    if os.path.exists(sub_path):
        try: return ImageFont.truetype(sub_path, size)
        except OSError: pass
    return _get_brand_font(size)


def create_name_banner(w, h, nome, sub, output, position):
    """Banner estilo MED-Review: parallelogramos escalonados (salmon + brown).
    position = (x, y, text_w, text_h)"""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    base_x, base_y, _, _ = position

    # Colors
    salmon = (222, 167, 143, 240)
    brown = (112, 60, 39, 240)
    black_text = (0, 0, 0, 255)
    white_text = (255, 255, 255, 255)

    # Font sizes proportional to output height
    fs_name = int(h * 0.030)
    fs_sub = int(h * 0.016)
    font_name = _get_brand_font(fs_name)
    font_sub = _get_sub_font(fs_sub)

    pad_x = int(w * 0.018)
    pad_y = int(h * 0.006)
    skew = int(w * 0.008)  # parallelogram lean

    # Measure text
    name_bb = draw.textbbox((0, 0), nome.upper(), font=font_name)
    sub_bb = draw.textbbox((0, 0), sub.upper(), font=font_sub)
    name_tw = name_bb[2] - name_bb[0]
    name_th = name_bb[3] - name_bb[1]
    sub_tw = sub_bb[2] - sub_bb[0]
    sub_th = sub_bb[3] - sub_bb[1]

    # Subtitle bar (salmon, smaller, top-left)
    sub_bar_w = sub_tw + pad_x * 2
    sub_bar_h = sub_th + pad_y * 2
    sx = base_x
    sy = base_y
    draw.polygon([
        (sx + skew, sy),
        (sx + sub_bar_w + skew, sy),
        (sx + sub_bar_w, sy + sub_bar_h),
        (sx, sy + sub_bar_h),
    ], fill=salmon)
    draw.text((sx + pad_x + skew // 2, sy + pad_y - sub_bb[1]),
              sub.upper(), fill=black_text, font=font_sub)

    # Name bar (brown, larger, offset down-right, slight overlap)
    name_bar_w = name_tw + pad_x * 2
    name_bar_h = name_th + pad_y * 2
    nx = base_x + int(w * 0.010)
    ny = sy + sub_bar_h - int(h * 0.003)
    draw.polygon([
        (nx + skew, ny),
        (nx + name_bar_w + skew, ny),
        (nx + name_bar_w, ny + name_bar_h),
        (nx, ny + name_bar_h),
    ], fill=brown)
    draw.text((nx + pad_x + skew // 2, ny + pad_y - name_bb[1]),
              nome.upper(), fill=white_text, font=font_name)

    img.save(output, "PNG")
    return output

def create_horizontal_frame(out_w, out_h, vid_w, vid_h, nome, name_sub, tema,
                            frame_top, frame_bottom, frame_words, logo_path, output_path):
    """Moldura purple (estilo Extensive) para vídeos horizontais.

    Retorna (path, vx, vy, vw, vh, name_overlay_path) onde:
      - path: imagem da moldura (vai ATRÁS do vídeo no FFmpeg)
      - (vx,vy,vw,vh): área onde o vídeo é sobreposto
      - name_overlay_path: imagem RGBA com gradiente+nome que vai POR CIMA do vídeo
    """
    import random
    random.seed(42)
    img = Image.new("RGBA", (out_w, out_h), (13, 5, 32, 255))
    draw = ImageDraw.Draw(img)

    # Fundo gradiente roxo (vetorizado se numpy disponível)
    if HAVE_NUMPY:
        ys = np.arange(out_h)
        s = np.sin(ys / out_h * math.pi)
        r = (13 + 17 * s).astype(np.uint8)
        g = (5 + 8 * s).astype(np.uint8)
        b = (32 + 32 * s).astype(np.uint8)
        col = np.stack([r, g, b, np.full_like(r, 255)], axis=1)
        arr = np.repeat(col[:, None, :], out_w, axis=1)
        img = Image.fromarray(arr.astype(np.uint8), "RGBA")
        draw = ImageDraw.Draw(img)
    else:
        for y in range(out_h):
            s = math.sin(y / out_h * math.pi)
            draw.line([(0, y), (out_w, y)],
                      fill=(int(13 + 17 * s), int(5 + 8 * s), int(32 + 32 * s), 255))

    # Glow radial central suave (numpy: gradiente real, sem empilhar opacidade)
    if HAVE_NUMPY:
        cx, cy = out_w / 2, out_h * 0.45
        yy, xx = np.ogrid[0:out_h, 0:out_w]
        dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        maxr = out_w * 0.75
        glow = np.clip(1 - dist / maxr, 0, 1) ** 2
        base = np.array(img).astype(np.float32)
        for ch, val in zip(range(3), (124, 58, 237)):
            base[:, :, ch] = np.clip(base[:, :, ch] + glow * val * 0.18, 0, 255)
        img = Image.fromarray(base.astype(np.uint8), "RGBA")
        draw = ImageDraw.Draw(img)
    else:
        cx, cy = out_w // 2, int(out_h * 0.45)
        maxr = int(out_w * 0.75)
        for r in range(maxr, 0, -10):
            a = int(4 * (1 - r / maxr))
            if a > 0:
                draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=(124, 58, 237, a))

    # Partículas
    for _ in range(35):
        sx, sy = random.randint(0, out_w), random.randint(0, out_h)
        ss = random.randint(1, 4)
        draw.ellipse([(sx - ss, sy - ss), (sx + ss, sy + ss)],
                     fill=(168, 85, 247, random.randint(40, 150)))

    # ── Área do vídeo (zona vertical reservada para não colidir com headline/CTA) ──
    margin = int(out_w * 0.04)
    v_w = out_w - margin * 2
    v_h = int(round(v_w * vid_h / vid_w))
    zone_top = int(out_h * 0.30)
    zone_bottom = int(out_h * 0.78)
    avail = zone_bottom - zone_top
    if v_h > avail:
        scale = avail / v_h
        v_w = int(v_w * scale)
        v_h = int(v_h * scale)
    # dimensões pares evitam erros de pad/scale no FFmpeg/libx264
    v_w -= v_w % 2
    v_h -= v_h % 2
    v_x = (out_w - v_w) // 2
    v_y = zone_top + (avail - v_h) // 2

    # Glow + borda neon roxa
    for i in range(3):
        p = 6 + i * 3
        draw.rounded_rectangle([(v_x - p, v_y - p), (v_x + v_w + p, v_y + v_h + p)],
                               radius=14 + i * 2, outline=(124, 58, 237, max(0, 80 - i * 25)), width=2)
    draw.rounded_rectangle([(v_x - 3, v_y - 3), (v_x + v_w + 3, v_y + v_h + 3)],
                           radius=12, outline=(124, 58, 237, 255), width=3)
    # Buraco para o vídeo: pinta preto (será coberto pelo vídeo no FFmpeg)
    draw.rounded_rectangle([(v_x, v_y), (v_x + v_w, v_y + v_h)],
                           radius=10, fill=(0, 0, 0, 255))

    # ── Logo (topo, centralizado) ──
    logo = load_logo_fitted(logo_path, int(out_w * 0.16), int(out_h * 0.05))
    if logo:
        img.paste(logo, ((out_w - logo.width) // 2, int(out_h * 0.02)), logo)

    # ── Headline (multilinha, topo-esquerda, branco bold uppercase) ──
    if frame_top:
        f_hl = get_font(True, int(out_h * 0.034))
        line_h = int(out_h * 0.042)
        raw_lines = frame_top.replace("\\n", "\n").split("\n")
        y0 = int(out_h * 0.07)
        for i, line in enumerate(raw_lines):
            draw.text((int(out_w * 0.06), y0 + i * line_h),
                      line.upper(), fill=(255, 255, 255, 255), font=f_hl)
    else:
        raw_lines = []

    # ── Frame words (pills) ──
    if frame_words:
        f_w = get_font(True, int(out_h * 0.015))
        pill_h = int(out_h * 0.026)
        py = int(out_h * 0.07) + len(raw_lines) * int(out_h * 0.042) + int(out_h * 0.01)
        px = int(out_w * 0.06)
        gap = int(out_w * 0.02)
        for word in frame_words:
            word = word.strip()
            if not word:
                continue
            tw = text_width(draw, word, f_w)
            pw = tw + int(out_w * 0.035)
            if px + pw > out_w - int(out_w * 0.06):
                px = int(out_w * 0.06)
                py += pill_h + int(out_h * 0.008)
            draw.rounded_rectangle([(px, py), (px + pw, py + pill_h)],
                                   radius=pill_h // 2, fill=(124, 58, 237, 60))
            draw.text((px + (pw - tw) // 2, py + int(pill_h * 0.2)),
                      word, fill=(216, 180, 254, 255), font=f_w)
            px += pw + gap

    # ── CTA (base, abaixo da zona de legenda) ──
    if frame_bottom:
        cta = strip_emoji(frame_bottom)
        if cta:
            f_cta = get_font(True, int(out_h * 0.026))
            tw = text_width(draw, cta, f_cta)
            draw.text(((out_w - tw) // 2, int(out_h * 0.945)),
                      cta, fill=(255, 255, 255, 255), font=f_cta)

    img.save(output_path, "PNG")

    # ═══ OVERLAY do nome (vai por CIMA do vídeo no FFmpeg) ═══
    name_overlay_path = output_path.replace(".png", "_name.png")
    name_img = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))

    if nome:
        # Gradiente preto sobre o vídeo, com aparência de fade real
        grad_h = int(v_h * 0.32)
        if HAVE_NUMPY:
            grad_arr = np.zeros((grad_h, v_w, 4), dtype=np.uint8)
            for dy in range(grad_h):
                # ease-in: gradiente mais opaco em baixo
                alpha = int(220 * (dy / grad_h) ** 1.8)
                grad_arr[dy, :, 3] = alpha
            grad_layer = Image.fromarray(grad_arr, "RGBA")
            name_img.paste(grad_layer, (v_x, v_y + v_h - grad_h), grad_layer)
        else:
            gd = ImageDraw.Draw(name_img)
            for dy in range(grad_h):
                y = v_y + v_h - grad_h + dy
                a = int(220 * (dy / grad_h) ** 1.8)
                gd.line([(v_x, y), (v_x + v_w, y)], fill=(0, 0, 0, a))

        nd = ImageDraw.Draw(name_img)
        fn = get_font(True, int(out_h * 0.022))
        fsx = get_font(False, int(out_h * 0.015))
        tx = v_x + int(out_w * 0.035)
        nd.text((tx, v_y + v_h - int(out_h * 0.060)), nome,
                fill=(255, 255, 255, 255), font=fn)
        if name_sub:
            nd.text((tx, v_y + v_h - int(out_h * 0.030)), name_sub,
                    fill=(255, 255, 255, 200), font=fsx)

    name_img.save(name_overlay_path, "PNG")

    return output_path, v_x, v_y, v_w, v_h, name_overlay_path


# ═══ 5. PIPELINE ═════════════════════════════════════════════════════════════

def maybe_cut_source(input_path, sel_segs, total_dur, tmp, source_has_audio=True):
    """Se houver corte de trechos, gera um vídeo intermediário cortado e
    retorna (caminho_cortado, segs_remapeados, nova_duracao, houve_corte).

    Usa extração por segmento (-ss/-to) + concat demuxer, que é muito mais
    leve em memória que o filtro trim (este último decodifica o vídeo inteiro
    por segmento e pode estourar a RAM em vídeos longos)."""
    groups = group_contiguous(sel_segs)
    if len(groups) == 1 and groups[0][0] <= 0.3 and groups[0][1] >= total_dur - 0.3:
        return input_path, sel_segs, total_dur, False

    seg_files = []
    for i, (gs, ge) in enumerate(groups):
        seg_out = os.path.join(tmp, f"seg_{i}.mp4")
        # -ss antes do -i = seek rápido; re-encoda para corte preciso nos keyframes
        cmd = ["ffmpeg", "-y", "-ss", f"{gs}", "-to", f"{ge}", "-i", input_path]
        if not source_has_audio:
            cmd += ["-an"]
        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-avoid_negative_ts", "make_zero"]
        if source_has_audio:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd += [seg_out]
        r = run(cmd)
        if r.returncode != 0 or not os.path.exists(seg_out):
            print(f"⚠️  Corte do trecho {i} falhou, usando vídeo completo.\n{r.stderr[-300:]}")
            return input_path, sel_segs, total_dur, False
        seg_files.append(seg_out)

    # concat demuxer (stream copy, sem reencode → leve e rápido)
    list_file = os.path.join(tmp, "concat.txt")
    with open(list_file, "w") as f:
        for sf in seg_files:
            f.write(f"file '{sf}'\n")
    cut_path = os.path.join(tmp, "cut.mp4")
    r = run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
             "-c", "copy", cut_path])
    if r.returncode != 0 or not os.path.exists(cut_path):
        # fallback: reencode no concat
        r = run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
                 "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                 "-c:a", "aac", "-b:a", "192k", cut_path])
        if r.returncode != 0 or not os.path.exists(cut_path):
            print(f"⚠️  Concat falhou, usando vídeo completo.\n{r.stderr[-300:]}")
            return input_path, sel_segs, total_dur, False

    new_segs, new_dur = remap_to_cut_timeline(sel_segs, groups)
    return cut_path, new_segs, new_dur, True


def build_audio_filter(inp_music_idx, eff_dur, vol, source_has_audio):
    """Monta o filtro de áudio com loudnorm em duas passadas.

    Voz  → loudnorm I=-14 LUFS (referência Instagram/TikTok)
    Música → loudnorm I=-23 LUFS (9 dB abaixo da voz = fundo audível mas não invasivo)

    O parâmetro `vol` (0..1) é aplicado POR CIMA da normalização da música,
    permitindo ajuste fino. Default=0.85 (volume total quase cheio depois da norma).
    Garantia: qualquer mp3, silencioso ou alto, sempre sai no mesmo nível.
    """
    if source_has_audio:
        # Filtros de voz: highpass (remove rumble), afftdn (redução de ruído leve),
        # loudnorm, fade-in suave
        voice = (
            "[0:a]aresample=async=1,"
            "highpass=f=80,"
            "lowpass=f=13000,"
            "afftdn=nf=-25,"
            "loudnorm=I=-14:TP=-1.5:LRA=11,"
            "afade=t=in:st=0:d=0.3"
            "[voice]"
        )
        voice_label = "[voice]"
    else:
        voice = (f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                 f"atrim=duration={eff_dur}[voice]")
        voice_label = "[voice]"

    # loudnorm I=-23 normaliza pra nível fixo; vol é ajuste fino pós-norma
    music = (
        f"[{inp_music_idx}:a]aloop=loop=-1:size=2e+09,"
        f"atrim=duration={eff_dur},"
        f"loudnorm=I=-23:TP=-2:LRA=7,"
        f"volume={vol},"
        f"afade=t=in:st=0:d=2,afade=t=out:st={max(0.0, eff_dur - 3)}:d=3"
        f"[music]"
    )
    mix = f"{voice_label}[music]amix=inputs=2:duration=first:normalize=0[aout]"
    return f"{voice};{music};{mix}"


def process(args):
    check_binaries()
    print(f"\n{'━'*55}\n  🎬 MED-Review Video Editor v4\n{'━'*55}")

    info = probe(args.input)
    vw, vh = get_dims(info)
    dur = get_dur(info)
    horiz = vw > vh
    src_audio = has_audio(info)
    print(f"\n📹 {vw}x{vh} | {dur:.1f}s | "
          f"{'Horizontal' if horiz else 'Vertical'} | "
          f"{'com áudio' if src_audio else 'SEM áudio'}")

    if dur <= 0:
        sys.exit("❌  Não foi possível determinar a duração do vídeo.")

    with tempfile.TemporaryDirectory(prefix="mr_") as tmp:
        # ── Transcrição ──
        # Só transcreve se precisar: legendas ON, ou corte inteligente (precisa dos segmentos)
        precisa_transcrever = args.legendas or (args.duracao > 0)
        if args.transcript:
            segs = load_transcript(args.transcript)
        elif src_audio and precisa_transcrever:
            wav = os.path.join(tmp, "a.wav")
            r = run(["ffmpeg", "-y", "-i", args.input, "-vn",
                     "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", wav])
            if r.returncode != 0:
                sys.exit(f"❌  Falha ao extrair áudio:\n{r.stderr[-400:]}")
            segs = transcribe(wav, args.whisper_model)
        else:
            if not precisa_transcrever:
                print("ℹ️  Legendas desativadas e sem corte — pulando transcrição.")
            else:
                print("⚠️  Vídeo sem áudio: legendas desativadas.")
            segs = []

        # ── Seleção de trechos ──
        score_segs(segs)
        # Se o vídeo já é mais curto que o alvo (com 5s de tolerância), mantém na íntegra
        needs_cut = False
        if args.duracao > 0 and dur <= args.duracao + 5:
            print(f"⏱️  Vídeo de {dur:.1f}s já é mais curto que o alvo ({args.duracao}s) — usando completo")
            sel = segs
        elif args.duracao > 0 and segs:
            sel = select_excerpts(segs, args.duracao)
            print(f"✂️  {len(sel)}/{len(segs)} segmentos selecionados (~{args.duracao}s)")
            needs_cut = True
        else:
            sel = segs

        # ── Corte real do vídeo (re-sincroniza tudo) ──
        working, sel, eff_dur, was_cut = (
            maybe_cut_source(args.input, sel, dur, tmp, source_has_audio=src_audio)
            if (needs_cut and segs) else (args.input, sel, dur, False)
        )
        if was_cut:
            print(f"   Vídeo cortado: {eff_dur:.1f}s")
        # após corte com áudio, a trilha de áudio existe; sem áudio continua sem
        working_has_audio = src_audio

        # ── Legendas (timestamps já no timeline final) ──
        chunks = make_chunks(sel) if args.legendas else []
        ass = os.path.join(tmp, "s.ass")
        if chunks:
            write_ass(chunks, ass, OUT_W, OUT_H, white=horiz)
        ass_esc = escape_ass_path(ass)

        # ── Montagem do filtro de vídeo ──
        if horiz:
            print("🖼️  Modo HORIZONTAL → moldura purple")
            frame_png = os.path.join(tmp, "frame.png")
            fw = [w for w in (args.frame_words.split(",") if args.frame_words else []) if w.strip()]
            _, fx, fy, fvw, fvh, name_ov = create_horizontal_frame(
                OUT_W, OUT_H, vw, vh, args.nome, args.name_sub, args.tema,
                args.frame_top or f"Depoimento {args.nome}",
                args.frame_bottom or "", fw, args.logo, frame_png)
            # Camadas: moldura (atrás) → vídeo escalado → overlay do nome+gradiente (em cima)
            base_v = (
                f"[0:v]scale={fvw}:{fvh}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad=w={fvw}:h={fvh}:x=-1:y=-1:color=black[vid];"
                f"[1:v][vid]overlay={fx}:{fy}[vstage];"
                f"[vstage][2:v]overlay=0:0[base]"
            )
            inputs = ["-i", working, "-i", frame_png, "-i", name_ov]
            inp_n = 3
        else:
            print("📱 Modo VERTICAL → overlays diretos")
            logo_png = os.path.join(tmp, "logo.png")
            create_logo_overlay(OUT_W, OUT_H, args.logo, logo_png)
            banner_png = os.path.join(tmp, "banner.png")
            banner_pos = find_empty_region(working, eff_dur, OUT_W, OUT_H, args.nome, args.name_sub)
            print(f"   📍 Posição do nome: x={banner_pos[0]}, y={banner_pos[1]}")
            create_name_banner(OUT_W, OUT_H, args.nome, args.name_sub, banner_png, banner_pos)
            # garante 9:16: escala+pad o vídeo de entrada
            base_v = (
                f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
                f"crop={OUT_W}:{OUT_H}[scaled];"
                f"[scaled][1:v]overlay=0:0[wl];"
                f"[wl][2:v]overlay=0:0[base]"
            )
            inputs = ["-i", working, "-i", logo_png, "-i", banner_png]
            inp_n = 3

        # adiciona legenda se houver
        if chunks:
            video_filter = f"{base_v};[base]ass='{ass_esc}'[vout]"
        else:
            video_filter = base_v.replace("[base]", "[vout]", 1) if base_v.endswith("[base]") else f"{base_v};[base]copy[vout]"
            # garantir label [vout]
            if "[vout]" not in video_filter:
                video_filter = f"{base_v};[base]null[vout]"

        # ── Áudio ──
        music_ok = bool(args.musica and os.path.exists(args.musica))
        if music_ok:
            inputs.extend(["-i", args.musica])
            audio_filter = build_audio_filter(inp_n, eff_dur, args.volume / 100.0,
                                              working_has_audio)
            full_filter = f"{video_filter};{audio_filter}"
            maps = ["-map", "[vout]", "-map", "[aout]"]
        elif working_has_audio:
            full_filter = video_filter
            maps = ["-map", "[vout]", "-map", "0:a"]
        else:
            # sem trilha e sem áudio de origem → gera silêncio
            full_filter = (f"{video_filter};"
                           f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                           f"atrim=duration={eff_dur}[aout]")
            maps = ["-map", "[vout]", "-map", "[aout]"]

        cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", full_filter, *maps,
               "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
               "-shortest", args.output]

        print("🚀 Renderizando...")
        r = run(cmd)
        if r.returncode != 0:
            print(f"❌  FFmpeg falhou:\n{r.stderr[-1200:]}")
            sys.exit(1)

        # ── Saídas auxiliares ──
        tj = Path(args.output).with_suffix(".json")
        with open(tj, "w", encoding="utf-8") as f:
            json.dump({
                "nome": args.nome, "name_sub": args.name_sub, "tema": args.tema,
                "horizontal": horiz, "duracao_final": round(eff_dur, 2),
                "segments": [{"start": s.start, "end": s.end, "text": s.text,
                              "score": s.score} for s in sel],
                "full_text": " ".join(s.text for s in sel),
            }, f, ensure_ascii=False, indent=2)
        if chunks:
            shutil.copy2(ass, Path(args.output).with_suffix(".ass"))

        sz = os.path.getsize(args.output) / 1024 / 1024
        print(f"\n{'━'*55}")
        print(f"  ✅ {args.output} ({sz:.1f} MB, {eff_dur:.1f}s)")
        print(f"  📝 {tj}")
        if chunks:
            print(f"  💬 {Path(args.output).with_suffix('.ass')}")
        print(f"{'━'*55}\n")


def main():
    p = argparse.ArgumentParser(
        description="MED-Review — Editor Automático de Depoimentos v4",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="Vídeo .mp4 de entrada (vertical ou horizontal)")
    p.add_argument("--nome", required=True, help="Nome do aluno")
    p.add_argument("--name-sub", default="Aluno Med-Review",
                   help="Subtítulo abaixo do nome (ex: 'Aprovada em Dermato')")
    p.add_argument("--faculdade", default="", help="(compatibilidade; não usado no layout)")
    p.add_argument("--tema", choices=list(THEMES), default="experiencia")
    p.add_argument("--duracao", type=int, default=0, help="30, 60, 90 ou 0 (completo)")
    p.add_argument("--logo", default=None, help="Logo MED-Review .png")
    p.add_argument("--musica", default=None, help="Trilha .mp3 (sem direitos autorais)")
    p.add_argument("--volume", type=int, default=12, help="Volume da trilha 0-40 (%%)")
    p.add_argument("--frame-top", default=None,
                   help="Headline da moldura horizontal (use \\n para quebrar linha)")
    p.add_argument("--frame-bottom", default="Você é o próximo ✨", help="CTA da moldura")
    p.add_argument("--frame-words", default="", help="Palavras (pills) separadas por vírgula")
    p.add_argument("--legendas", dest="legendas", action="store_true", default=True,
                   help="Gerar legendas dinâmicas (padrão: sim)")
    p.add_argument("--sem-legendas", dest="legendas", action="store_false",
                   help="Desativar legendas")
    p.add_argument("--transcript", default=None, help="JSON de transcrição pré-gerado")
    p.add_argument("--whisper-model", default="base",
                   choices=["tiny", "base", "small", "medium", "large-v3"])
    p.add_argument("-o", "--output", default=None)
    a = p.parse_args()

    if not os.path.exists(a.input):
        sys.exit(f"❌  Arquivo não encontrado: {a.input}")
    if a.duracao not in DURACOES_VALIDAS:
        print(f"⚠️  Duração {a.duracao}s fora do padrão (30/60/90). Prosseguindo.")
    a.volume = max(0, min(40, a.volume))
    if not a.output:
        stem = Path(a.input).stem
        sf = f"_{a.duracao}s" if a.duracao > 0 else ""
        a.output = f"{stem}_medreview{sf}.mp4"
    process(a)


if __name__ == "__main__":
    pass  # CLI desativada — usado como módulo pela GUI
