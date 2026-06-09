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

    video_path = video_file if isinstance(video_file, str) else getattr(video_file, "name", None)
    if not video_path or not os.path.exists(video_path):
        return "", "❌ Arquivo de vídeo inválido."

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
            {"word": w.word, "start": w.start, "end": w.end}
            for w in (s.words or [])
        ]} for s in segs]
        segs_json = json.dumps(segs_data, ensure_ascii=False)

        return transcript_text, f"✅ Transcrição concluída — revise e corrija o texto abaixo, depois clique em Renderizar.\n\nSegmentos JSON (não edite): {segs_json}"

    except Exception:
        return "", f"❌ Erro na transcrição:\n{traceback.format_exc()}"


# ── ETAPA 2: Renderização ───────────────────────────────────
def step2_renderizar(video_file, nome, name_sub, tema, duracao, legendas, transcript_text):
    """Renderiza o vídeo com o transcript editado pelo usuário."""
    if not video_file:
        return None, "❌ Selecione um vídeo."
    if not nome or not nome.strip():
        return None, "❌ Preencha o nome do aluno."

    video_path = video_file if isinstance(video_file, str) else getattr(video_file, "name", None)
    if not video_path or not os.path.exists(video_path):
        return None, "❌ Arquivo de vídeo inválido."

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
    a.duracao       = DURACOES.get(duracao, 0)
    a.logo          = _get_path("logo.png")
    a.musica        = _get_path("music.mp3")
    a.volume        = 85
    a.frame_top     = None
    a.frame_bottom  = "Você é o próximo"
    a.frame_words   = ""
    a.whisper_model = "small"
    a.legendas      = (legendas == "Sim")

    try:
        # Se há transcrição editada, salva como arquivo JSON temporário
        transcript_file = None
        if a.legendas and transcript_text and transcript_text.strip():
            # Reconstrói segmentos a partir do texto editado (sem timestamps → aprox.)
            lines = [l.strip() for l in transcript_text.strip().split("\n") if l.strip()]
            # Tenta extrair duração total pro cálculo de timestamps aproximados
            info = engine.probe(video_path)
            total_dur = engine.get_dur(info)
            seg_dur = total_dur / max(len(lines), 1)
            segs_data = [
                {
                    "text": line,
                    "start": round(i * seg_dur, 2),
                    "end": round((i + 1) * seg_dur, 2),
                    "words": []
                }
                for i, line in enumerate(lines)
            ]
            tmp_json = tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                                   delete=False, encoding="utf-8")
            json.dump({"segments": segs_data}, tmp_json, ensure_ascii=False)
            tmp_json.close()
            transcript_file = tmp_json.name

        a.transcript = transcript_file

        stem   = Path(video_path).stem
        suffix = f"_{a.duracao}s" if a.duracao > 0 else ""
        out_dir = "/tmp/medreview_out"
        os.makedirs(out_dir, exist_ok=True)
        a.output = os.path.join(out_dir, f"{stem}_medreview{suffix}.mp4")

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
            tema_input   = gr.Dropdown(["Produto","Aprovação","Experiência"], value="Experiência", label="Tema")
            dur_input    = gr.Dropdown(["Completo","30s","60s","90s"], value="Completo", label="Duração")
            leg_input    = gr.Radio(["Sim","Não"], value="Sim", label="Gerar legendas")

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
    )

    btn_render.click(
        fn=step2_renderizar,
        inputs=[video_input, nome_input, sub_input, tema_input, dur_input, leg_input, transcript_box],
        outputs=[video_out, status_box],
    )

demo.launch()
