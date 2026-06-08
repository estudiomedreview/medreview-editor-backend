# ============================================================
#  MED-Review Video Editor — Hugging Face Space (Gradio)
# ============================================================
#  CORREÇÃO CRÍTICA: o gradio_client tem um bug
#  ("argument of type 'bool' is not iterable") que quebra
#  o /info endpoint e causa "Error: No API found".
#  Patcheamos AS DUAS funções afetadas ANTES de importar gradio.
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
from pathlib import Path

THEMES = {"Produto": "produto", "Aprovação": "aprovacao", "Experiência": "experiencia"}
DURACOES = {"Completo": 0, "30s": 30, "60s": 60, "90s": 90}


def process_video(video_file, nome, name_sub, tema, duracao, legendas):
    # Validações
    if not video_file:
        return None, "❌ Selecione um vídeo."
    if not nome or not nome.strip():
        return None, "❌ Preencha o nome do aluno."

    # gr.File pode retornar str (path) ou objeto com .name
    video_path = video_file if isinstance(video_file, str) else getattr(video_file, "name", None)
    if not video_path or not os.path.exists(video_path):
        return None, "❌ Não consegui ler o arquivo de vídeo."

    # Importa o motor (lazy, pra capturar erro de import com traceback)
    try:
        import medreview_engine as engine
    except Exception:
        return None, f"❌ Erro ao carregar o motor:\n{traceback.format_exc()}"

    class Args:
        pass

    a = Args()
    a.input         = video_path
    a.nome          = nome.strip()
    a.name_sub      = (name_sub or "Aluno Med-Review").strip()
    a.faculdade     = ""
    a.tema          = THEMES.get(tema, "experiencia")
    a.duracao       = DURACOES.get(duracao, 0)
    a.logo          = "logo.png" if os.path.exists("logo.png") else None
    a.musica        = "music.mp3" if os.path.exists("music.mp3") else None
    a.volume        = 12
    a.frame_top     = None
    a.frame_bottom  = "Você é o próximo"
    a.frame_words   = ""
    a.transcript    = None
    a.whisper_model = "base"
    a.legendas      = (legendas == "Sim")

    try:
        stem   = Path(video_path).stem
        suffix = f"_{a.duracao}s" if a.duracao > 0 else ""
        # Diretório de saída persistente (Gradio precisa servir o arquivo)
        out_dir = "/tmp/medreview_out"
        os.makedirs(out_dir, exist_ok=True)
        a.output = os.path.join(out_dir, f"{stem}_medreview{suffix}.mp4")

        engine.process(a)

        if not os.path.exists(a.output):
            return None, "❌ O processamento terminou mas não gerou arquivo."

        sz = os.path.getsize(a.output) / 1024 / 1024
        return a.output, f"✅ Pronto! ({sz:.1f} MB)"
    except Exception:
        return None, f"❌ Erro no processamento:\n{traceback.format_exc()}"


demo = gr.Interface(
    fn=process_video,
    inputs=[
        gr.File(label="📹 Vídeo do depoimento", file_types=[".mp4", ".mov", ".avi", ".mkv"]),
        gr.Textbox(label="Nome do aluno", placeholder="Ex: Igor Pires"),
        gr.Textbox(label="Subtítulo", value="Aluno Med-Review", placeholder="Ex: Aprovada em Dermato"),
        gr.Dropdown(["Produto", "Aprovação", "Experiência"], value="Experiência", label="Tema"),
        gr.Dropdown(["Completo", "30s", "60s", "90s"], value="Completo", label="Duração"),
        gr.Radio(["Sim", "Não"], value="Sim", label="Gerar legendas automáticas"),
    ],
    outputs=[
        gr.File(label="⬇️ Vídeo editado (.mp4)"),
        gr.Textbox(label="Status", lines=3),
    ],
    title="🎬 MED-Review Video Editor",
    description="Transcreve, legenda e edita depoimentos automaticamente. O processamento leva 1-3 min.",
    flagging_mode="never",
)

if __name__ == "__main__":
    demo.launch()
