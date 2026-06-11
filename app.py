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
import tempfile
import shutil
import traceback
import json
from pathlib import Path

THEMES  = {"Produto": "produto", "Aprovação": "aprovacao", "Experiência": "experiencia"}
DURACOES = {"Completo": 0, "30s": 30, "60s": 60, "90s": 90}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _get_path(filename):
    """Retorna path absoluto de arquivo no mesmo dir do app.py."""
    p = os.path.join(BASE_DIR, filename)
    return p if os.path.exists(p) else None


# ── ETAPA 1: Transcrição ────────────────────────────────────
def step1_transcrever(video_file, nome, name_sub, tema, duracao, legendas):
    """Transcreve o vídeo e retorna o texto editável."""
    if not video_file:
        return "", "❌ Selecione um vídeo."
    if not nome or not nome.strip():
        return "", "❌ Preencha o nome do aluno."

    # Gradio 5 passes FileData object OR string path — handle both
    if isinstance(video_file, str):
        video_path = video_file
    elif hasattr(video_file, "path"):
        video_path = video_file.path       # Gradio 5 FileData object
    elif hasattr(video_file, "name"):
        video_path = video_file.name       # Gradio 4 style
    elif isinstance(video_file, dict):
        video_path = video_file.get("path") or video_file.get("name")
    else:
        video_path = str(video_file) if video_file else None
    if not video_path or not os.path.exists(str(video_path)):
        return "", f"❌ Arquivo de vídeo inválido: {repr(video_file)}"

    if legendas != "Sim":
        return "", "ℹ️ Legendas desativadas — clique em Renderizar diretamente."

    try:
        import medreview_engine as engine
    except Exception:
        return "", f"❌ Erro ao carregar engine:\n{traceback.format_exc()}"

    try:
        # Extrai WAV e roda Whisper
        with tempfile.TemporaryDirectory() as tmp:
            wav = os.path.join(tmp, "a.wav")
            r = engine.run(["ffmpeg", "-y", "-i", video_path, "-vn",
                            "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", wav])
            if r.returncode != 0:
                return "", f"❌ Erro ao extrair áudio:\n{r.stderr[-300:]}"

            segs = engine.transcribe(wav, "small")

        # Formata como texto editável, um parágrafo por segmento
        lines = []
        for s in segs:
            lines.append(s.text.strip())
        transcript_text = "\n".join(lines)

        # Salva JSON de segmentos pra usar na renderização
        segs_data = [{"text": s.text, "start": s.start, "end": s.end, "words": [
            {"word": w.text, "start": w.start, "end": w.end}
            for w in (s.words or [])
        ]} for s in segs]
        segs_json = json.dumps(segs_data, ensure_ascii=False)

        # Save segments JSON to temp file keyed by a hash of the video path
        import hashlib
        vid_key = hashlib.md5(video_path.encode()).hexdigest()[:12]
        segs_cache = f"/tmp/segs_{vid_key}.json"
        with open(segs_cache, "w", encoding="utf-8") as sf:
            sf.write(segs_json)

        return transcript_text, f"✅ Transcrição concluída! Revise o texto e clique em Renderizar.|||{segs_json}"

    except Exception:
        return "", f"❌ Erro na transcrição:\n{traceback.format_exc()}"


# ── ETAPA 2: Renderização ───────────────────────────────────
def step2_renderizar(video_file, nome, name_sub, tema, duracao, legendas, musica, vertical, transcript_text):
    """Renderiza o vídeo com o transcript editado pelo usuário."""
    if not video_file:
        return None, "❌ Selecione um vídeo."
    if not nome or not nome.strip():
        return None, "❌ Preencha o nome do aluno."

    # Gradio 5 passes FileData object OR string path — handle both
    if isinstance(video_file, str):
        video_path = video_file
    elif hasattr(video_file, "path"):
        video_path = video_file.path
    elif hasattr(video_file, "name"):
        video_path = video_file.name
    elif isinstance(video_file, dict):
        video_path = video_file.get("path") or video_file.get("name")
    else:
        video_path = str(video_file) if video_file else None
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
    a.duracao       = DURACOES.get(duracao, 0)
    a.logo          = _get_path("logo.png")
    # Trilha: OFF → sem música; ON → trilha aleatória da pasta music/
    if musica == "Sim":
        import glob, random
        music_dir = os.path.join(BASE_DIR, "music")
        tracks = glob.glob(os.path.join(music_dir, "*.mp3")) + glob.glob(os.path.join(music_dir, "*.wav"))
        if not tracks:
            # fallback: music.mp3 na raiz (compatibilidade)
            tracks = [p for p in [_get_path("music.mp3")] if p]
        a.musica = random.choice(tracks) if tracks else None
    else:
        a.musica = None
    a.volume        = 85
    a.frame_top     = None
    a.frame_bottom  = "Você é o próximo"
    a.frame_words   = ""
    a.whisper_model = "small"
    a.legendas      = (legendas == "Sim")

    try:
        import tempfile  # necessário para NamedTemporaryFile (escopo local)
        # Usa a transcrição editada pelo usuário (com timestamps aproximados)
        transcript_file = None
        if a.legendas and transcript_text and transcript_text.strip():
            import hashlib
            vid_key = hashlib.md5(video_path.encode()).hexdigest()[:12]
            segs_cache = f"/tmp/segs_{vid_key}.json"

            # Tenta usar os segmentos cacheados do step1 pra preservar timestamps
            cached_segs = []
            if os.path.exists(segs_cache):
                try:
                    with open(segs_cache) as sf:
                        cached_segs = json.load(sf)
                except Exception:
                    cached_segs = []

            # Mapeia o texto editado de volta pros segmentos (mantém timestamps)
            edited_lines = [l.strip() for l in transcript_text.strip().split("\n") if l.strip()]
            if cached_segs and len(cached_segs) == len(edited_lines):
                # Mesmo número de linhas: preserva timestamps originais
                for seg, new_text in zip(cached_segs, edited_lines):
                    seg["text"] = new_text
                segs_data = cached_segs
            else:
                # Linha count mudou: distribui uniformemente
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

        # Nome do arquivo baseado no nome do aluno
        nome_slug = re.sub(r'[^\w\s-]', '', a.nome).strip().replace(' ', '_')
        suffix = f"_{a.duracao}s" if a.duracao > 0 else ""

        # Usa arquivo temporário gerenciado pelo sistema (Gradio serve /tmp)
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
            mus_input    = gr.Radio(["Sim","Não"], value="Sim", label="Trilha musical")

            btn_transcribe = gr.Button("🎙️ Etapa 1 — Transcrever", variant="secondary")
            btn_render     = gr.Button("🚀 Etapa 2 — Renderizar", variant="primary")

        with gr.Column(scale=1):
            transcript_box = gr.Textbox(
                label="📝 Transcrição (edite antes de renderizar)",
                lines=12,
                placeholder="A transcrição aparece aqui após a Etapa 1.\nVocê pode corrigir nomes, termos médicos, etc.",
            )
            status_box  = gr.Textbox(label="Status", lines=3, interactive=False)
            video_out   = gr.File(label="⬇️ Vídeo editado (.mp4)")

    btn_transcribe.click(
        fn=step1_transcrever,
        inputs=[video_input, nome_input, sub_input, tema_input, dur_input, leg_input],
        outputs=[transcript_box, status_box],
        api_name="transcrever",
    )

    btn_render.click(
        fn=step2_renderizar,
        inputs=[video_input, nome_input, sub_input, tema_input, dur_input, leg_input, mus_input, vert_input, transcript_box],
        outputs=[video_out, status_box],
        api_name="renderizar",
    )

demo.queue(max_size=10)
demo.launch(show_api=True, allowed_paths=["/tmp"])
