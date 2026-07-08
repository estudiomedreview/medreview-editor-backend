# ============================================================
#  Monkey-patch: bug "argument of type 'bool' is not iterable"
#  no gradio_client (afeta versões 1.3.x e 1.5.x)
# ============================================================
import gradio_client.utils as _gcu

_orig_j2p = _gcu._json_schema_to_python_type
def _safe_j2p(schema, defs=None):
    if not isinstance(schema, dict):
        return "Any"
    try:
        return _orig_j2p(schema, defs)
    except (TypeError, KeyError):
        return "Any"
_gcu._json_schema_to_python_type = _safe_j2p

_orig_get_type = _gcu.get_type
def _safe_get_type(schema):
    if not isinstance(schema, dict):
        return "Any"
    return _orig_get_type(schema)
_gcu.get_type = _safe_get_type
# ============================================================

import gradio as gr
import os
import re
import subprocess
import tempfile
import shutil
import traceback

# Garante que o diretório de cache do Gradio existe após restart do Space
os.makedirs("/tmp/gradio", exist_ok=True)
import json
from pathlib import Path

THEMES  = {"Produto": "produto", "Aprovação": "aprovacao", "Experiência": "experiencia"}
DURACOES = {"Completo": 0, "30s": 30, "60s": 60, "90s": 90}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _get_path(filename):
    """Retorna path absoluto de arquivo no mesmo dir do app.py."""
    p = os.path.join(BASE_DIR, filename)
    return p if os.path.exists(p) else None


def _resolve_video_path(video_file):
    """Normaliza o objeto de vídeo do Gradio para um path de string."""
    if isinstance(video_file, str):
        return video_file
    elif hasattr(video_file, "path"):
        return video_file.path
    elif hasattr(video_file, "name"):
        return video_file.name
    elif isinstance(video_file, dict):
        return video_file.get("path") or video_file.get("name")
    return str(video_file) if video_file else None


# ── ETAPA 0: Detecção de silêncio ───────────────────────────
def step0_detect_silence(video_file):
    """Detecta silêncio no início/fim do vídeo e sugere pontos de trim.
    Retorna JSON com: duration, trim_start, trim_end, waveform, silence_starts, silence_ends."""
    video_path = _resolve_video_path(video_file)
    if not video_path or not os.path.exists(str(video_path)):
        return json.dumps({"error": f"Arquivo inválido: {repr(video_file)}"})

    try:
        import medreview_engine as engine
    except Exception:
        return json.dumps({"error": f"Erro ao carregar engine:\n{traceback.format_exc()}"})

    try:
        info = engine.probe(video_path)
        dur  = engine.get_dur(info)
        if dur <= 0:
            return json.dumps({"error": "Não foi possível ler a duração do vídeo."})

        # Roda silencedetect via FFmpeg
        # noise=-35dB: silêncio abaixo de -35dBFS; duration=0.3: mínimo de 0.3s contínuo
        r = subprocess.run([
            "ffmpeg", "-y", "-i", video_path,
            "-af", "silencedetect=noise=-35dB:duration=0.3",
            "-f", "null", "-"
        ], capture_output=True, text=True)

        stderr = r.stderr
        starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", stderr)]
        ends   = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", stderr)]

        # Sugere trim_start: fim do primeiro silêncio se começa logo no início (< 1s)
        trim_start = 0.0
        if starts and ends and starts[0] < 1.0:
            trim_start = round(min(ends[0], dur), 2)

        # Sugere trim_end: início do último silêncio se começa a < 5s do final
        trim_end = round(dur, 2)
        if starts:
            last_start = starts[-1]
            if last_start > dur - 5.0:
                trim_end = round(last_start, 2)

        # Gera waveform simplificado (RMS por janela) para visualização no frontend
        waveform = []
        try:
            r2 = subprocess.run([
                "ffmpeg", "-y", "-i", video_path,
                "-af", "aresample=8000,astats=metadata=1:reset=1,"
                       "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
                "-f", "null", "-"
            ], capture_output=True, text=True, timeout=30)
            rms_vals = re.findall(r"lavfi\.astats\.Overall\.RMS_level=([-\d.]+)", r2.stdout + r2.stderr)
            for v in rms_vals[:200]:
                try:
                    db = float(v)
                    norm = max(0.0, min(1.0, (db + 60) / 60))
                    waveform.append(round(norm, 3))
                except ValueError:
                    pass
        except Exception:
            pass  # waveform é opcional; não bloqueia o fluxo

        return json.dumps({
            "duration":       round(dur, 2),
            "trim_start":     trim_start,
            "trim_end":       trim_end,
            "waveform":       waveform,
            "silence_starts": [round(s, 2) for s in starts],
            "silence_ends":   [round(s, 2) for s in ends],
        }, ensure_ascii=False)

    except Exception:
        return json.dumps({"error": traceback.format_exc()})


# ── ETAPA 1: Transcrição ────────────────────────────────────
def step1_transcrever(video_file, nome, name_sub, tema, duracao, legendas, trim_start, trim_end):
    """Transcreve o vídeo (já trimado) e retorna o texto editável."""
    if not video_file:
        return "", "❌ Selecione um vídeo."
    if not nome or not nome.strip():
        return "", "❌ Preencha o nome do aluno."

    video_path = _resolve_video_path(video_file)
    if not video_path or not os.path.exists(str(video_path)):
        return "", f"❌ Arquivo de vídeo inválido: {repr(video_file)}"

    if legendas != "Sim":
        return "", "ℹ️ Legendas desativadas — clique em Renderizar diretamente."

    try:
        import medreview_engine as engine
    except Exception:
        return "", f"❌ Erro ao carregar engine:\n{traceback.format_exc()}"

    try:
        ts = float(trim_start or 0)
        te = float(trim_end   or 0)
        dur = engine.get_dur(engine.probe(video_path))

        # Extrai WAV do trecho trimado para o Whisper (não cria arquivo intermediário)
        with tempfile.TemporaryDirectory() as tmp:
            wav = os.path.join(tmp, "a.wav")

            # Monta o comando respeitando trim
            cmd = ["ffmpeg", "-y"]
            if ts > 0.05:
                cmd += ["-ss", f"{ts:.3f}"]
            if te > 0.05 and te < dur - 0.05:
                cmd += ["-to", f"{te:.3f}"]
            cmd += ["-i", video_path, "-vn",
                    "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", wav]

            r = engine.run(cmd)
            if r.returncode != 0:
                return "", f"❌ Erro ao extrair áudio:\n{r.stderr[-300:]}"

            segs = engine.transcribe(wav, "small")

        # Formata como texto editável, um parágrafo por segmento
        lines = [s.text.strip() for s in segs]
        transcript_text = "\n".join(lines)

        # Salva JSON de segmentos pra usar na renderização
        segs_data = [{"text": s.text, "start": s.start, "end": s.end, "words": [
            {"word": w.text, "start": w.start, "end": w.end}
            for w in (s.words or [])
        ]} for s in segs]
        segs_json = json.dumps(segs_data, ensure_ascii=False)

        import hashlib
        vid_key = hashlib.md5(video_path.encode()).hexdigest()[:12]
        segs_cache = f"/tmp/segs_{vid_key}.json"
        with open(segs_cache, "w", encoding="utf-8") as sf:
            sf.write(segs_json)

        return transcript_text, f"✅ Transcrição concluída! Revise o texto e clique em Renderizar.|||{segs_json}"

    except Exception:
        return "", f"❌ Erro na transcrição:\n{traceback.format_exc()}"


# ── ETAPA 2: Renderização ───────────────────────────────────
def step2_renderizar(video_file, nome, name_sub, tema, duracao, legendas, musica, volume,
                     vertical, estilo_legenda, mostrar_titulo, posicao_titulo,
                     transcript_text, trim_start, trim_end):
    """Renderiza o vídeo com o transcript editado pelo usuário."""
    if not video_file:
        return None, "❌ Selecione um vídeo."
    if not nome or not nome.strip():
        return None, "❌ Preencha o nome do aluno."

    video_path = _resolve_video_path(video_file)
    if not video_path or not os.path.exists(str(video_path)):
        return None, f"❌ Arquivo de vídeo inválido: {repr(video_file)}"

    try:
        import medreview_engine as engine
    except Exception:
        return None, f"❌ Erro ao carregar engine:\n{traceback.format_exc()}"

    class Args: pass
    a = Args()
    a.input         = video_path
    a.nome          = nome.strip()
    a.name_sub      = (name_sub or "Aluno Med-Review").strip()
    a.faculdade     = ""
    a.tema          = THEMES.get(tema, "experiencia")
    VERTICAL_MAP = {"MED-Review": "medreview", "OFT-Review": "oft",
                    "ANEST-Review": "anest", "ORTOP-Review": "ortop"}
    a.vertical      = VERTICAL_MAP.get(vertical, "medreview")
    a.legenda_estilo = "popin" if estilo_legenda == "Pop-in" else "dinamica"
    a.mostrar_titulo = (mostrar_titulo != "Não")
    a.titulo_posicao = "bottom" if posicao_titulo == "Base" else "top"
    a.duracao       = DURACOES.get(duracao, 0)
    a.logo          = _get_path("logo.png")
    a.trim_start    = float(trim_start or 0)
    a.trim_end      = float(trim_end   or 0)

    # Trilha: OFF → sem música; ON → trilha aleatória da pasta music/
    if musica == "Sim":
        import glob, random
        music_dir = os.path.join(BASE_DIR, "music")
        tracks = glob.glob(os.path.join(music_dir, "*.mp3")) + glob.glob(os.path.join(music_dir, "*.wav"))
        if not tracks:
            tracks = [p for p in [_get_path("music.mp3")] if p]
        a.musica = random.choice(tracks) if tracks else None
    else:
        a.musica = None
    try:
        a.volume = max(0, min(40, int(float(volume or 12))))
    except (ValueError, TypeError):
        a.volume = 12
    a.frame_top     = None
    a.frame_bottom  = "Você é o próximo"
    a.frame_words   = ""
    a.whisper_model = "small"
    a.legendas      = (legendas == "Sim")

    try:
        import tempfile  # necessário para NamedTemporaryFile (escopo local)
        transcript_file = None
        if a.legendas and transcript_text and transcript_text.strip():
            import hashlib
            vid_key = hashlib.md5(video_path.encode()).hexdigest()[:12]
            segs_cache = f"/tmp/segs_{vid_key}.json"

            cached_segs = []
            if os.path.exists(segs_cache):
                try:
                    with open(segs_cache) as sf:
                        cached_segs = json.load(sf)
                except Exception:
                    cached_segs = []

            def remap_words(orig_words, new_text):
                edited = new_text.split()
                if not edited or not orig_words:
                    return []
                n_orig, n_edit = len(orig_words), len(edited)
                if n_edit >= n_orig:
                    return [{"word": edited[i], "start": orig_words[i]["start"],
                             "end": orig_words[i]["end"]} for i in range(min(n_orig, n_edit))]
                else:
                    result = []
                    slots_per = n_orig / n_edit
                    for i, word in enumerate(edited):
                        s = int(i * slots_per)
                        e = min(int((i + 1) * slots_per), n_orig) - 1
                        result.append({"word": word,
                                       "start": orig_words[s]["start"],
                                       "end": orig_words[e]["end"]})
                    return result

            edited_lines = [l.strip() for l in transcript_text.strip().split("\n") if l.strip()]
            if cached_segs and len(cached_segs) == len(edited_lines):
                for seg, new_text in zip(cached_segs, edited_lines):
                    seg["text"] = new_text
                    seg["words"] = remap_words(seg.get("words", []), new_text)
                segs_data = cached_segs
            else:
                info = engine.probe(video_path)
                total_dur = engine.get_dur(info)
                seg_dur = total_dur / max(len(edited_lines), 1)
                segs_data = [
                    {"text": line, "start": round(i * seg_dur, 2),
                     "end": round((i + 1) * seg_dur, 2), "words": []}
                    for i, line in enumerate(edited_lines)
                ]

            tmp_json = tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                                    delete=False, encoding="utf-8")
            json.dump({"segments": segs_data}, tmp_json, ensure_ascii=False)
            tmp_json.close()
            transcript_file = tmp_json.name

        a.transcript = transcript_file

        nome_slug = re.sub(r'[^\w\s-]', '', a.nome).strip().replace(' ', '_')
        suffix = f"_{a.duracao}s" if a.duracao > 0 else ""

        out_tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=".mp4",
            dir="/tmp",
            prefix=f"depoimento_{nome_slug}{suffix}_"
        )
        out_tmp.close()
        a.output = out_tmp.name

        engine.process(a)

        if transcript_file and os.path.exists(transcript_file):
            os.unlink(transcript_file)

        if not os.path.exists(a.output):
            return None, "❌ Processamento terminou mas não gerou arquivo."

        sz = os.path.getsize(a.output) / 1024 / 1024
        return a.output, f"✅ Pronto! ({sz:.1f} MB)"

    except Exception:
        return None, f"❌ Erro:\n{traceback.format_exc()}"


# ── Interface ────────────────────────────────────────────────
with gr.Blocks(title="MED-Review Video Editor") as demo:
    gr.Markdown("# 🎬 MED-Review Video Editor")
    gr.Markdown("**Etapa 1:** Preencha os campos e clique em *Transcrever* · **Etapa 2:** Revise o texto e clique em *Renderizar*")

    with gr.Row():
        with gr.Column(scale=1):
            video_input  = gr.File(label="📹 Vídeo do depoimento", file_types=[".mp4", ".mov", ".avi"])
            nome_input   = gr.Textbox(label="Nome do aluno", placeholder="Ex: Igor Pires")
            sub_input    = gr.Textbox(label="Subtítulo", value="Aluno Med-Review")
            vert_input   = gr.Dropdown(["MED-Review","OFT-Review","ANEST-Review","ORTOP-Review"],
                                       value="MED-Review", label="Vertical")
            tema_input   = gr.Dropdown(["Produto","Aprovação","Experiência"], value="Experiência", label="Tema")
            dur_input    = gr.Dropdown(["Completo","30s","60s","90s"], value="Completo", label="Duração")
            leg_input    = gr.Radio(["Sim","Não"], value="Sim", label="Gerar legendas")
            estilo_input = gr.Radio(["Dinâmica","Pop-in"], value="Dinâmica", label="Estilo de legenda")
            mus_input    = gr.Radio(["Sim","Não"], value="Sim", label="Trilha musical")
            vol_input    = gr.Slider(0, 40, value=12, step=1, label="Volume da trilha (%)")
            titulo_input = gr.Radio(["Sim","Não"], value="Sim", label="Mostrar título (nome do aluno)")
            pos_input    = gr.Radio(["Topo","Base"], value="Topo", label="Posição do título")

            # Campos hidden para trim (gerenciados pelo frontend Next.js via API)
            trim_start_input = gr.Number(value=0, label="Trim início (s)", visible=False)
            trim_end_input   = gr.Number(value=0, label="Trim fim (s)",    visible=False)

            btn_detect     = gr.Button("🔍 Detectar silêncio", variant="secondary")
            btn_transcribe = gr.Button("🎙️ Etapa 1 — Transcrever", variant="secondary")
            btn_render     = gr.Button("🚀 Etapa 2 — Renderizar", variant="primary")

        with gr.Column(scale=1):
            detect_out     = gr.Textbox(label="Resultado da detecção (JSON)", lines=4, interactive=False)
            transcript_box = gr.Textbox(
                label="📝 Transcrição (edite antes de renderizar)",
                lines=12,
                placeholder="A transcrição aparece aqui após a Etapa 1.\nVocê pode corrigir nomes, termos médicos, etc.",
            )
            status_box  = gr.Textbox(label="Status", lines=3, interactive=False)
            video_out   = gr.File(label="⬇️ Vídeo editado (.mp4)")

    btn_detect.click(
        fn=step0_detect_silence,
        inputs=[video_input],
        outputs=[detect_out],
        api_name="detectar_silencio",
    )

    btn_transcribe.click(
        fn=step1_transcrever,
        inputs=[video_input, nome_input, sub_input, tema_input, dur_input, leg_input,
                trim_start_input, trim_end_input],
        outputs=[transcript_box, status_box],
        api_name="transcrever",
    )

    btn_render.click(
        fn=step2_renderizar,
        inputs=[video_input, nome_input, sub_input, tema_input, dur_input, leg_input,
                mus_input, vol_input, vert_input, estilo_input, titulo_input, pos_input,
                transcript_box, trim_start_input, trim_end_input],
        outputs=[video_out, status_box],
        api_name="renderizar",
    )

demo.queue(max_size=10)
demo.launch(show_api=True, allowed_paths=["/tmp"])
