import gradio as gr
import os, tempfile, shutil, traceback
from pathlib import Path

def process_video(video, nome, name_sub, tema, duracao, legendas):
    try:
        import medreview_engine as engine
    except Exception as e:
        return None, f"Erro ao carregar engine:\n{traceback.format_exc()}"

    if not video:
        return None, "Selecione um vídeo"
    if not nome or not nome.strip():
        return None, "Preencha o nome do aluno"

    # Gradio 5: video pode ser dict ou string
    video_path = video if isinstance(video, str) else video.get("path", video.get("url", ""))

    THEMES = {"Produto": "produto", "Aprovação": "aprovacao", "Experiência": "experiencia"}
    DURACOES = {"Vídeo completo": 0, "30s": 30, "60s": 60, "90s": 90}

    class Args: pass
    args = Args()
    args.input       = video_path
    args.nome        = nome.strip()
    args.name_sub    = (name_sub or "Aluno Med-Review").strip()
    args.faculdade   = ""
    args.tema        = THEMES.get(tema, "experiencia")
    args.duracao     = DURACOES.get(duracao, 0)
    args.logo        = "logo.png" if os.path.exists("logo.png") else None
    args.musica      = "music.mp3" if os.path.exists("music.mp3") else None
    args.volume      = 12
    args.frame_top   = None
    args.frame_bottom = "Você é o próximo"
    args.frame_words  = ""
    args.transcript  = None
    args.whisper_model = "base"
    args.legendas    = bool(legendas)

    try:
        stem   = Path(video_path).stem
        suffix = f"_{args.duracao}s" if args.duracao > 0 else ""
        with tempfile.TemporaryDirectory() as tmp:
            args.output = os.path.join(tmp, f"{stem}_medreview{suffix}.mp4")
            engine.process(args)
            out = f"/tmp/out_{stem}{suffix}.mp4"
            shutil.copy2(args.output, out)
        return out, "✅ Pronto!"
    except Exception as e:
        return None, f"Erro:\n{traceback.format_exc()}"

demo = gr.Interface(
    fn=process_video,
    inputs=[
        gr.Video(label="📹 Vídeo (.mp4)"),
        gr.Textbox(label="Nome do aluno", placeholder="Ex: Igor Pires"),
        gr.Textbox(label="Subtítulo", value="Aluno Med-Review"),
        gr.Dropdown(["Produto","Aprovação","Experiência"], value="Experiência", label="Tema"),
        gr.Dropdown(["Vídeo completo","30s","60s","90s"], value="Vídeo completo", label="Duração"),
        gr.Radio(["Sim","Não"], value="Sim", label="Gerar legendas"),
    ],
    outputs=[
        gr.Video(label="✅ Vídeo editado"),
        gr.Textbox(label="Status"),
    ],
    title="🎬 MED-Review Video Editor",
    description="Editor automático de depoimentos — transcreve, legenda e edita.",
    flagging_mode="never",
)

demo.launch()
