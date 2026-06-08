import gradio as gr
import os, tempfile, shutil, traceback
from pathlib import Path

def process_video(video_file, nome, name_sub, tema, duracao, legendas):
    try:
        import medreview_engine as engine
    except Exception as e:
        return None, f"Erro engine:\n{traceback.format_exc()}"

    if not video_file:
        return None, "Selecione um vídeo"
    if not nome or not nome.strip():
        return None, "Preencha o nome"

    # gr.File retorna string (path) em v4 e v5
    video_path = video_file if isinstance(video_file, str) else video_file.name

    THEMES = {"Produto": "produto", "Aprovação": "aprovacao", "Experiência": "experiencia"}
    DURACOES = {"Completo": 0, "30s": 30, "60s": 60, "90s": 90}

    class Args: pass
    a = Args()
    a.input        = video_path
    a.nome         = nome.strip()
    a.name_sub     = (name_sub or "Aluno Med-Review").strip()
    a.faculdade    = ""
    a.tema         = THEMES.get(tema, "experiencia")
    a.duracao      = DURACOES.get(duracao, 0)
    a.logo         = "logo.png" if os.path.exists("logo.png") else None
    a.musica       = "music.mp3" if os.path.exists("music.mp3") else None
    a.volume       = 12
    a.frame_top    = None
    a.frame_bottom = "Você é o próximo"
    a.frame_words  = ""
    a.transcript   = None
    a.whisper_model = "base"
    a.legendas     = (legendas == "Sim")

    try:
        stem   = Path(video_path).stem
        suffix = f"_{a.duracao}s" if a.duracao > 0 else ""
        with tempfile.TemporaryDirectory() as tmp:
            a.output = os.path.join(tmp, f"{stem}_medreview{suffix}.mp4")
            engine.process(a)
            out = f"/tmp/out_{stem}{suffix}.mp4"
            shutil.copy2(a.output, out)
        return out, "✅ Pronto!"
    except Exception as e:
        return None, f"Erro:\n{traceback.format_exc()}"

demo = gr.Interface(
    fn=process_video,
    inputs=[
        gr.File(label="📹 Vídeo (.mp4)", file_types=[".mp4", ".mov", ".avi"]),
        gr.Textbox(label="Nome do aluno", placeholder="Ex: Igor Pires"),
        gr.Textbox(label="Subtítulo", value="Aluno Med-Review"),
        gr.Dropdown(["Produto","Aprovação","Experiência"], value="Experiência", label="Tema"),
        gr.Dropdown(["Completo","30s","60s","90s"], value="Completo", label="Duração"),
        gr.Radio(["Sim","Não"], value="Sim", label="Legendas automáticas"),
    ],
    outputs=[
        gr.File(label="⬇️ Vídeo editado (.mp4)"),
        gr.Textbox(label="Status"),
    ],
    title="🎬 MED-Review Video Editor",
    description="Transcreve, legenda e edita depoimentos automaticamente.",
    allow_flagging="never",
)

demo.launch()
