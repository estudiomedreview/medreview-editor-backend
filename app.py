"""
MED-Review Video Editor — Hugging Face Spaces (Gradio 5)
"""
import gradio as gr
import os
import tempfile
import shutil
from pathlib import Path

import medreview_engine as engine

THEMES = {"Produto": "produto", "Aprovação": "aprovacao", "Experiência": "experiencia"}
DURACOES = {"Vídeo completo": 0, "30 segundos": 30, "60 segundos": 60, "90 segundos": 90}

def process_video(video, nome, name_sub, tema, duracao, legendas):
    if not video:
        raise gr.Error("Selecione um vídeo")
    if not nome or not nome.strip():
        raise gr.Error("Preencha o nome do aluno")

    class Args:
        pass

    args = Args()
    args.input = video
    args.nome = nome.strip()
    args.name_sub = name_sub.strip() if name_sub else "Aluno Med-Review"
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

    stem = Path(video).stem
    suffix = f"_{args.duracao}s" if args.duracao > 0 else ""

    with tempfile.TemporaryDirectory() as tmp:
        args.output = os.path.join(tmp, f"{stem}_medreview{suffix}.mp4")
        engine.process(args)
        out = f"/tmp/output_{stem}{suffix}.mp4"
        shutil.copy2(args.output, out)

    return out

with gr.Blocks(title="MED-Review Video Editor") as app:
    gr.Markdown("# 🎬 MED-Review Video Editor\nEditor automático de depoimentos.")

    with gr.Row():
        with gr.Column():
            video_input = gr.Video(label="📹 Vídeo do depoimento (.mp4)")
            nome_input = gr.Textbox(label="Nome do aluno", placeholder="Ex: Igor Pires")
            namesub_input = gr.Textbox(label="Subtítulo", value="Aluno Med-Review")
            tema_input = gr.Dropdown(choices=list(THEMES.keys()), value="Experiência", label="Tema")
            duracao_input = gr.Dropdown(choices=list(DURACOES.keys()), value="Vídeo completo", label="Duração")
            legendas_check = gr.Checkbox(value=True, label="Gerar legendas automáticas")
            process_btn = gr.Button("🚀 Processar", variant="primary")

        with gr.Column():
            video_output = gr.Video(label="✅ Vídeo editado")
            gr.Markdown("*Processamento leva 1-3 min*")

    process_btn.click(
        fn=process_video,
        inputs=[video_input, nome_input, namesub_input, tema_input, duracao_input, legendas_check],
        outputs=video_output,
    )

app.launch()
