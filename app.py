"""
MED-Review Video Editor — Hugging Face Spaces (Gradio 5)
"""
import gradio as gr
import os
import tempfile
import shutil
import traceback
from pathlib import Path

def process_video(video, nome, name_sub, tema, duracao, legendas):
    try:
        import medreview_engine as engine
    except Exception as e:
        raise gr.Error(f"Erro ao carregar engine: {e}\n{traceback.format_exc()}")

    if not video:
        raise gr.Error("Selecione um vídeo")
    if not nome or not nome.strip():
        raise gr.Error("Preencha o nome do aluno")

    THEMES = {"Produto": "produto", "Aprovação": "aprovacao", "Experiência": "experiencia"}
    DURACOES = {"Vídeo completo": 0, "30 segundos": 30, "60 segundos": 60, "90 segundos": 90}

    class Args:
        pass

    args = Args()
    args.input = video
    args.nome = nome.strip()
    args.name_sub = (name_sub or "Aluno Med-Review").strip()
    args.faculdade = ""
    args.tema = THEMES.get(tema, "experiencia")
    args.duracao = DURACOES.get(duracao, 0)
    args.logo = "logo.png" if os.path.exists("logo.png") else None
    args.musica = "music.mp3" if os.path.exists("music.mp3") else None
    args.volume = 12
    args.frame_top = None
    args.frame_bottom = "Você é o próximo"
    args.frame_words = ""
    args.transcript = None
    args.whisper_model = "base"
    args.legendas = legendas

    try:
        stem = Path(video).stem
        suffix = f"_{args.duracao}s" if args.duracao > 0 else ""
        with tempfile.TemporaryDirectory() as tmp:
            args.output = os.path.join(tmp, f"{stem}_medreview{suffix}.mp4")
            engine.process(args)
            out = f"/tmp/out_{stem}{suffix}.mp4"
            shutil.copy2(args.output, out)
        return out
    except Exception as e:
        raise gr.Error(f"Erro no processamento: {e}\n{traceback.format_exc()}")

with gr.Blocks(title="MED-Review Video Editor") as app:
    gr.Markdown("# 🎬 MED-Review Video Editor\nEditor automático de depoimentos.")
    with gr.Row():
        with gr.Column():
            video_input = gr.Video(label="📹 Vídeo (.mp4)")
            nome_input = gr.Textbox(label="Nome do aluno", placeholder="Igor Pires")
            namesub_input = gr.Textbox(label="Subtítulo", value="Aluno Med-Review")
            tema_input = gr.Dropdown(
                choices=["Produto", "Aprovação", "Experiência"],
                value="Experiência", label="Tema")
            duracao_input = gr.Dropdown(
                choices=["Vídeo completo", "30 segundos", "60 segundos", "90 segundos"],
                value="Vídeo completo", label="Duração")
            legendas_check = gr.Checkbox(value=True, label="Gerar legendas")
            btn = gr.Button("🚀 Processar", variant="primary")
        with gr.Column():
            video_output = gr.Video(label="✅ Resultado")
            gr.Markdown("*1-3 min por vídeo*")

    btn.click(process_video,
              [video_input, nome_input, namesub_input, tema_input, duracao_input, legendas_check],
              video_output)

app.launch()
